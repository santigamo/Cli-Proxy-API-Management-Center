#!/usr/bin/env python3
"""Local organization-scoped management gateway and single-consumer usage collector."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import http.client
import http.server
import ipaddress
import io
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

DEFAULT_DB = "/var/lib/pi-hub-metrics/usage.sqlite3"
DEFAULT_CONFIG = "/etc/pi-hub-metrics/config.json"
DEFAULT_UI = "/opt/pi-hub-metrics/index.html"
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MANAGEMENT_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
REQUEST_HEADER_ALLOWLIST = {
    "accept": "Accept",
    "content-type": "Content-Type",
    "if-match": "If-Match",
    "if-none-match": "If-None-Match",
    "range": "Range",
    "accept-language": "Accept-Language",
}
RESPONSE_HEADER_ALLOWLIST = {
    "content-type",
    "content-disposition",
    "accept-ranges",
    "content-range",
    "etag",
    "last-modified",
    "location",
    "x-cpa-version",
    "x-cpa-build-date",
    "x-cpa-support-plugin",
    "x-server-version",
    "x-server-build-date",
}
SUM_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "total_tokens",
    "failed",
)
EVENT_FIELDS = (
    "client",
    "ts",
    "provider",
    "model",
    "alias",
    "latency_ms",
    *SUM_FIELDS,
    "status",
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool | None:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    """Create the deployed schema if absent; existing databases remain compatible."""
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, client TEXT, ts TEXT, provider TEXT, model TEXT,
                alias TEXT, latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER,
                cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
                reasoning_tokens INTEGER, total_tokens INTEGER, failed INTEGER, status INTEGER
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS events_ts_client ON events(ts,client)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS health (
                client TEXT PRIMARY KEY, last_success TEXT, error TEXT
            )"""
        )


def number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def normalize(client: str, record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract approved usage metadata; raw request, prompt, response, and credentials drop."""
    token_data = record.get("tokens") or {}
    if not isinstance(token_data, dict):
        token_data = {}
    stamp = record.get("timestamp")
    try:
        parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        stamp = parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (ValueError, AttributeError, TypeError):
        raise ValueError("invalid usage timestamp") from None

    failure = record.get("fail") or {}
    if not isinstance(failure, dict):
        failure = {}
    row: dict[str, Any] = {
        "client": client,
        "ts": stamp,
        "provider": str(record.get("provider", "unknown"))[:100],
        "model": str(record.get("model", "unknown"))[:200],
        "alias": str(record.get("alias", ""))[:200],
        "latency_ms": number(record.get("latency_ms")),
        "failed": int(bool(record.get("failed"))),
        "status": number(failure.get("status_code")),
    }
    for field in SUM_FIELDS[:-2]:
        row[field] = number(token_data.get(field))
    if "cache_read_tokens" not in token_data:
        row["cache_read_tokens"] = number(token_data.get("cached_tokens"))
    row["total_tokens"] = (
        number(token_data.get("total_tokens"))
        if "total_tokens" in token_data
        else row["input_tokens"] + row["output_tokens"]
    )
    # The upstream record has no stable event ID. Hash its canonical bytes for deduplication,
    # matching the deployed collector; only the digest is persisted, never the source record.
    digest_input = (client + "\n" + json.dumps(record, sort_keys=True, separators=(",", ":"))).encode()
    return hashlib.sha256(digest_input).hexdigest(), row


def store(db_path: str, client: str, records: list[Any]) -> None:
    with connect(db_path) as conn:
        for record in records:
            if isinstance(record, str):
                record = json.loads(record)
            if not isinstance(record, dict):
                continue
            event_id, row = normalize(client, record)
            placeholders = ",".join("?" for _ in range(len(EVENT_FIELDS) + 1))
            conn.execute(
                f"INSERT OR IGNORE INTO events VALUES ({placeholders})",
                [event_id, *[row[field] for field in EVENT_FIELDS]],
            )


def summary(
    db_path: str,
    days: int,
    organization: str = "all",
    configured_organizations: set[str] | None = None,
) -> dict[str, Any]:
    allowed = configured_organizations or set()
    if organization != "all" and organization not in allowed:
        raise ValueError("unknown organization")
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    sums_sql = ", ".join(f"SUM({field}) AS {field}" for field in SUM_FIELDS)
    if organization == "all":
        configured = sorted(allowed)
        if configured:
            client_sql = " AND client IN (" + ",".join("?" for _ in configured) + ")"
            args: tuple[Any, ...] = (cutoff, *configured)
            health_filter = " WHERE client IN (" + ",".join("?" for _ in configured) + ")"
            health_args: tuple[Any, ...] = tuple(configured)
        else:
            client_sql = " AND 0"
            args = (cutoff,)
            health_filter = " WHERE 0"
            health_args = ()
    else:
        client_sql = " AND client = ?"
        args = (cutoff, organization)
        health_filter = " WHERE client = ?"
        health_args = (organization,)
    with connect(db_path) as conn:
        def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(row) for row in conn.execute(sql, args)]

        return {
            "days": days,
            "totals": rows(
                "SELECT client, COUNT(*) AS requests, " + sums_sql +
                " FROM events WHERE ts >= ?" + client_sql + " GROUP BY client"
            ),
            "models": rows(
                "SELECT client, provider, model, COUNT(*) AS requests, " + sums_sql +
                " FROM events WHERE ts >= ?" + client_sql +
                " GROUP BY client,provider,model ORDER BY SUM(total_tokens) DESC"
            ),
            "daily": rows(
                "SELECT substr(ts,1,10) AS day,client,COUNT(*) AS requests," +
                "SUM(total_tokens) AS total_tokens FROM events WHERE ts >= ?" + client_sql +
                " GROUP BY day,client ORDER BY day DESC"
            ),
            "health": [
                dict(row) for row in conn.execute("SELECT * FROM health" + health_filter, health_args)
            ],
            "updated": dt.datetime.now(dt.timezone.utc).isoformat(),
        }


def load_clients(config_path: str) -> list[dict[str, Any]]:
    with open(config_path, encoding="utf-8") as config_file:
        raw = json.load(config_file)
    return clients_from_config(raw)


def clients_from_config(raw: Any) -> list[dict[str, Any]]:
    clients = raw.get("clients") if isinstance(raw, dict) else None
    if not isinstance(clients, list):
        raise ValueError("config must contain a clients array")
    found: set[str] = set()
    normalized = []
    for client in clients:
        if not isinstance(client, dict):
            raise ValueError("invalid client configuration")
        name, port, key = client.get("name"), client.get("port"), client.get("key")
        if not isinstance(name, str) or not name.isascii() or not name.replace("-", "").isalnum():
            raise ValueError("invalid client name")
        if name == "all" or not 1 <= len(name) <= 40 or name.lower() != name or name in found:
            raise ValueError("invalid client name")
        if (
            type(port) is not int
            or not 1 <= port <= 65535
            or not isinstance(key, str)
            or not key.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in key)
        ):
            raise ValueError("invalid client connection settings")
        found.add(name)
        label = client.get("label", name)
        if not isinstance(label, str):
            label = name
        normalized.append({"name": name, "label": label[:60], "port": port, "key": key})
    return normalized


def normalize_public_origin(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError("public_origin must be an HTTPS origin")
    try:
        origin = urlsplit(value)
        port = origin.port
    except ValueError:
        raise ValueError("public_origin must be an HTTPS origin") from None
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
        or origin.username is not None
        or origin.password is not None
        or any(char.isspace() for char in value)
    ):
        raise ValueError("public_origin must be an HTTPS origin")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("public_origin has invalid port")
    return value.rstrip("/")


def load_service_config(config_path: str) -> tuple[list[dict[str, Any]], str]:
    with open(config_path, encoding="utf-8") as config_file:
        raw = json.load(config_file)
    return clients_from_config(raw), normalize_public_origin(
        raw.get("public_origin") if isinstance(raw, dict) else None
    )


def poll(db_path: str, client: dict[str, Any]) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{client['port']}/v8/management/observability/usage/queue?count=500",
        headers={"Authorization": "Bearer " + client["key"]},
    )
    for _ in range(20):
        with urllib.request.urlopen(request, timeout=8) as response:
            records = json.load(response)
        if not isinstance(records, list):
            raise ValueError("unexpected queue format")
        store(db_path, client["name"], records)
        if len(records) < 500:
            break
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO health VALUES (?,?,?)",
            (client["name"], dt.datetime.now(dt.timezone.utc).isoformat(), None),
        )


def collect_forever(db_path: str, clients: list[dict[str, Any]], interval: int = 5) -> None:
    while True:
        for client in clients:
            try:
                poll(db_path, client)
            except Exception as error:  # Do not log upstream payloads or credentials.
                with connect(db_path) as conn:
                    conn.execute(
                        "INSERT INTO health(client,error) VALUES (?,?) "
                        "ON CONFLICT(client) DO UPDATE SET error=excluded.error",
                        (client["name"], type(error).__name__),
                    )
        time.sleep(interval)


class HubHandler(http.server.BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    ui_path = DEFAULT_UI
    clients: list[dict[str, Any]] = []
    public_origin = ""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _query_value(query: dict[str, list[str]], key: str, default: str) -> str:
        values = query.get(key, [default])
        if len(values) != 1:
            raise ValueError("invalid query")
        return values[0]

    def _gateway_error(self, status: int, message: str, headers: tuple[tuple[str, str], ...] = ()) -> None:
        body = (message + "\n").encode()
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _allowed_host(self) -> bool:
        host_header = self.headers.get("Host", "")
        try:
            host = urlsplit("//" + host_header)
            if host.path or host.username is not None or host.password is not None:
                return False
            if host.hostname in {"localhost", "127.0.0.1", "::1"}:
                return host.port in {None, self.server.server_port}
            if not self.public_origin:
                return False
            public = urlsplit(self.public_origin)
            expected_port = public.port or 443
            actual_port = host.port or 443
            return host.hostname == public.hostname and actual_port == expected_port
        except ValueError:
            return False

    def _same_origin_or_cli(self) -> bool:
        if not self._allowed_host():
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed_origin = urlsplit(origin)
                parsed_host = urlsplit("//" + self.headers.get("Host", ""))
                is_public_origin = False
                if self.public_origin:
                    public_origin = urlsplit(self.public_origin)
                    origin_port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)
                    public_port = public_origin.port or 443
                    is_public_origin = (
                        parsed_origin.scheme == "https"
                        and parsed_origin.hostname == public_origin.hostname
                        and origin_port == public_port
                    )
                is_loopback_origin = (
                    parsed_origin.scheme == "http"
                    and parsed_origin.hostname == parsed_host.hostname
                    and parsed_origin.port == self.server.server_port
                    and parsed_host.port in {None, self.server.server_port}
                    and parsed_host.hostname in {"localhost", "127.0.0.1", "::1"}
                )
                if (
                    parsed_origin.path != ""
                    or parsed_origin.query
                    or parsed_origin.fragment
                    or not (is_public_origin or is_loopback_origin)
                ):
                    return False
            except ValueError:
                return False
        return self.headers.get("Sec-Fetch-Site", "").lower() != "cross-site"

    def _scoped_path(self) -> tuple[dict[str, Any], str, str, str] | None:
        """Parse an organization-prefixed route without allowing path normalization tricks."""
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            raise ValueError("invalid request target") from None
        raw_path = parsed.path
        if parsed.scheme or parsed.netloc or not raw_path.startswith("/") or self.path.startswith("//"):
            raise ValueError("absolute request targets are not allowed")
        if not raw_path.startswith("/organizations/"):
            return None
        if (
            not raw_path.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in raw_path + parsed.query)
            or re.search(r"%(?![0-9a-fA-F]{2})", raw_path)
            or re.search(r"%(?:2f|5c|25)", raw_path, flags=re.IGNORECASE)
            or "\\" in raw_path
            or "//" in raw_path
        ):
            raise ValueError("invalid management path encoding")
        try:
            decoded_path = unquote(raw_path, errors="strict")
        except UnicodeDecodeError:
            raise ValueError("invalid management path encoding") from None
        segments = decoded_path.split("/")
        if any(segment in {".", ".."} for segment in segments) or "%" in decoded_path:
            raise ValueError("path traversal is not allowed")
        match = re.fullmatch(
            r"/organizations/([a-z0-9][a-z0-9-]{0,39})(/.*)?",
            decoded_path,
        )
        if not match:
            raise ValueError("invalid management path")
        organization = match.group(1)
        client = next((item for item in self.clients if item["name"] == organization), None)
        if client is None:
            raise LookupError("unknown organization")
        raw_prefix = f"/organizations/{organization}"
        if raw_path != raw_prefix and not raw_path.startswith(raw_prefix + "/"):
            raise ValueError("invalid organization path")
        return client, match.group(2) or "", raw_path[len(raw_prefix):], parsed.query

    def _management_route(self) -> tuple[dict[str, Any], str] | None:
        """Validate a scoped management URL and return its configured client + upstream target."""
        scoped = self._scoped_path()
        if scoped is None:
            return None
        client, decoded_suffix, raw_suffix, query = scoped
        if not (decoded_suffix == "/v8/management" or decoded_suffix.startswith("/v8/management/")):
            return None
        target = raw_suffix + ("?" + query if query else "")
        return client, target

    def _proxy_upstream(
        self,
        client: dict[str, Any],
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> bool:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", client["port"], timeout=60)
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                connection.close()
                self._gateway_error(502, "Upstream response too large")
                return True
            response_headers = response.getheaders()
            connection.close()
        except (OSError, http.client.HTTPException):
            self._gateway_error(502, "Management API unavailable")
            return True
        location = next((value for name, value in response_headers if name.lower() == "location"), None)
        if location:
            try:
                parsed_location = urlsplit(location)
                location_port = parsed_location.port
            except ValueError:
                self._gateway_error(502, "Invalid upstream redirect")
                return True
            if parsed_location.scheme or parsed_location.netloc:
                if (
                    parsed_location.scheme != "http"
                    or parsed_location.hostname not in {"127.0.0.1", "localhost"}
                    or location_port != client["port"]
                    or parsed_location.username is not None
                    or parsed_location.password is not None
                ):
                    self._gateway_error(502, "External upstream redirects are blocked")
                    return True
                location = f"/organizations/{client['name']}{parsed_location.path}"
                if parsed_location.query:
                    location += "?" + parsed_location.query
            elif parsed_location.path.startswith("/v8/management"):
                location = f"/organizations/{client['name']}{parsed_location.path}"
                if parsed_location.query:
                    location += "?" + parsed_location.query
        self.send_response(response.status)
        sent_headers: set[str] = set()
        for name, value in response_headers:
            lower_name = name.lower()
            if lower_name not in RESPONSE_HEADER_ALLOWLIST or lower_name in sent_headers:
                continue
            if lower_name == "location" and location is not None:
                value = location
            self.send_header(name, value)
            sent_headers.add(lower_name)
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(response_body)
        return True

    def _resolve_client_api_key(self, client: dict[str, Any]) -> str | None:
        headers = {
            "Authorization": "Bearer " + client["key"],
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        try:
            connection = http.client.HTTPConnection("127.0.0.1", client["port"], timeout=15)
            connection.request("GET", "/v8/management/config/access/api-keys", headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                response.read(1024)
                connection.close()
                return None
            data = json.loads(response.read(1024 * 1024))
            connection.close()
        except (OSError, http.client.HTTPException, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, list):
            return None
        return next((key for key in data if isinstance(key, str) and key.strip()), None)

    def _forward_models(self, scoped: tuple[dict[str, Any], str, str, str]) -> bool:
        client, decoded_suffix, _raw_suffix, query = scoped
        if decoded_suffix != "/v1/models":
            return False
        if self.command != "GET":
            self._gateway_error(405, "Only GET is allowed for model discovery")
            return True
        if self.headers.get("X-Hub-Request") != "1" or not self._same_origin_or_cli():
            self._gateway_error(403, "Hub request origin rejected")
            return True
        if not self._body_is_empty():
            self._gateway_error(400, "Model discovery does not accept a request body")
            return True
        api_key = self._resolve_client_api_key(client)
        if not api_key:
            self._gateway_error(503, "Backend client API key unavailable")
            return True
        target = "/v1/models" + ("?" + query if query else "")
        headers = {"Authorization": "Bearer " + api_key, "Accept-Encoding": "identity"}
        accept = self.headers.get("Accept")
        if accept:
            headers["Accept"] = accept
        return self._proxy_upstream(client, "GET", target, headers)

    def _body_is_empty(self) -> bool:
        if self.headers.get("Transfer-Encoding"):
            return False
        length = self.headers.get("Content-Length", "0") or "0"
        if not length.isascii() or not length.isdigit() or len(length) > 20:
            return False
        try:
            return int(length) == 0
        except ValueError:
            return False

    def _plugin_resource_paths(self, client: dict[str, Any]) -> set[str] | None:
        headers = {
            "Authorization": "Bearer " + client["key"],
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        try:
            connection = http.client.HTTPConnection("127.0.0.1", client["port"], timeout=15)
            connection.request("GET", "/v8/management/plugins", headers=headers)
            response = connection.getresponse()
            support = response.getheader("X-CPA-SUPPORT-PLUGIN", "0")
            if response.status != 200 or support != "1":
                response.read(1024)
                connection.close()
                return None
            data = json.loads(response.read(4 * 1024 * 1024))
            connection.close()
        except (OSError, http.client.HTTPException, ValueError, json.JSONDecodeError):
            return None
        plugins = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(plugins, list):
            return None
        paths: set[str] = set()
        for plugin in plugins:
            menus = plugin.get("menus") if isinstance(plugin, dict) else None
            if not isinstance(menus, list):
                continue
            for menu in menus:
                path = menu.get("path") if isinstance(menu, dict) else None
                if isinstance(path, str) and path.startswith("/v0/resource/plugins/"):
                    paths.add(path)
        return paths

    def _forward_plugin_resource(self, scoped: tuple[dict[str, Any], str, str, str]) -> bool:
        client, decoded_suffix, raw_suffix, query = scoped
        if not decoded_suffix.startswith("/v0/resource/plugins/"):
            return False
        if self.command != "GET":
            self._gateway_error(405, "Only GET is allowed for plugin resources")
            return True
        if self.headers.get("X-Hub-Request") != "1" or not self._same_origin_or_cli():
            self._gateway_error(403, "Hub request origin rejected")
            return True
        if not self._body_is_empty():
            self._gateway_error(400, "Plugin resource requests cannot include a body")
            return True
        safe_paths = self._plugin_resource_paths(client)
        if safe_paths is None:
            self._gateway_error(404, "Plugin resources are not supported by this backend")
            return True
        if decoded_suffix not in safe_paths:
            self._gateway_error(404, "Plugin resource path is not backend-declared")
            return True
        headers = {"Accept-Encoding": "identity"}
        for name in ("Accept", "Accept-Language", "If-None-Match", "Range"):
            value = self.headers.get(name)
            if value is not None:
                headers[name] = value
        target = raw_suffix + ("?" + query if query else "")
        return self._proxy_upstream(client, "GET", target, headers)

    def _forward_management(self) -> bool:
        if self.command not in MANAGEMENT_METHODS:
            self._gateway_error(
                405,
                "Method not allowed",
                (("Allow", ", ".join(sorted(MANAGEMENT_METHODS))),),
            )
            return True
        try:
            route = self._management_route()
        except LookupError:
            self._gateway_error(404, "Unknown organization")
            return True
        except ValueError as error:
            self._gateway_error(400, str(error))
            return True
        if route is None:
            try:
                scoped = self._scoped_path()
            except LookupError:
                self._gateway_error(404, "Unknown organization")
                return True
            except ValueError as error:
                self._gateway_error(400, str(error))
                return True
            if scoped is None:
                return False
            if self._forward_models(scoped) or self._forward_plugin_resource(scoped):
                return True
            self._gateway_error(404, "Scoped API path not found")
            return True
        client, target = route
        if self.headers.get("X-Hub-Request") != "1":
            self._gateway_error(403, "Hub request marker required")
            return True
        if not self._same_origin_or_cli():
            self._gateway_error(403, "Request origin rejected")
            return True
        fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
        if fetch_site == "cross-site":
            self._gateway_error(403, "Cross-site request rejected")
            return True
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding:
            self._gateway_error(400, "Transfer-Encoding is not supported")
            return True
        length_text = self.headers.get("Content-Length", "0")
        if not length_text.isascii() or not length_text.isdigit():
            self._gateway_error(400, "Invalid Content-Length")
            return True
        if len(length_text) > 20:
            self.close_connection = True
            self._gateway_error(413, "Request body too large")
            return True
        try:
            body_size = int(length_text)
        except ValueError:
            self._gateway_error(400, "Invalid Content-Length")
            return True
        if body_size > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._gateway_error(413, "Request body too large")
            return True
        if self.command in {"GET", "HEAD"} and body_size:
            self._gateway_error(400, "GET and HEAD requests cannot include a body")
            return True
        body = self.rfile.read(body_size) if body_size else None
        if body_size and body is None:
            self._gateway_error(400, "Request body is incomplete")
            return True
        if body_size:
            media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            allowed_media_types = {
                "application/json",
                "application/yaml",
                "text/yaml",
                "text/plain",
                "application/x-www-form-urlencoded",
                "application/octet-stream",
                "multipart/form-data",
            }
            if media_type not in allowed_media_types:
                self._gateway_error(415, "Unsupported request Content-Type")
                return True
        headers = {"Authorization": "Bearer " + client["key"], "Accept-Encoding": "identity"}
        for header, forwarded_name in REQUEST_HEADER_ALLOWLIST.items():
            value = self.headers.get(header)
            if value is not None:
                headers[forwarded_name] = value
        try:
            connection = http.client.HTTPConnection("127.0.0.1", client["port"], timeout=60)
            connection.request(self.command, target, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                connection.close()
                self._gateway_error(502, "Upstream response too large")
                return True
            response_headers = response.getheaders()
            connection.close()
        except (OSError, http.client.HTTPException):
            self._gateway_error(502, "Management API unavailable")
            return True
        location = next((value for name, value in response_headers if name.lower() == "location"), None)
        if location:
            parsed_location = urlsplit(location)
            if parsed_location.scheme or parsed_location.netloc:
                if (
                    parsed_location.scheme != "http"
                    or parsed_location.hostname not in {"127.0.0.1", "localhost"}
                    or parsed_location.port != client["port"]
                    or parsed_location.username is not None
                    or parsed_location.password is not None
                ):
                    self._gateway_error(502, "External upstream redirects are blocked")
                    return True
                location = f"/organizations/{client['name']}{parsed_location.path}"
                if parsed_location.query:
                    location += "?" + parsed_location.query
            elif parsed_location.path.startswith("/v8/management"):
                location = f"/organizations/{client['name']}{parsed_location.path}"
                if parsed_location.query:
                    location += "?" + parsed_location.query
        self.send_response(response.status)
        sent_headers: set[str] = set()
        for name, value in response_headers:
            lower_name = name.lower()
            if lower_name not in RESPONSE_HEADER_ALLOWLIST or lower_name in sent_headers:
                continue
            if lower_name == "location" and location is not None:
                value = location
            self.send_header(name, value)
            sent_headers.add(lower_name)
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response_body)
        return True

    def do_GET(self) -> None:
        if not self._allowed_host():
            self._gateway_error(403, "Host not allowed")
            return
        if self._forward_management():
            return
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if parsed.path == "/":
                body = pathlib.Path(self.ui_path).read_bytes()
                self._send(200, body, "text/html; charset=utf-8")
                return
            days_text = self._query_value(query, "days", "30")
            days = int(days_text)
            if not 1 <= days <= 365:
                raise ValueError("days must be between 1 and 365")
            organization = self._query_value(query, "organization", "all")
            if organization != "all" and organization not in {c["name"] for c in self.clients}:
                raise ValueError("unknown organization")
            if parsed.path == "/api/organizations":
                organizations = [{"id": c["name"], "name": c["label"]} for c in self.clients]
                self._send(200, json.dumps(organizations).encode(), "application/json; charset=utf-8")
                return
            data = summary(self.db_path, days, organization, {c["name"] for c in self.clients})
            if parsed.path == "/api/summary":
                self._send(200, json.dumps(data).encode(), "application/json; charset=utf-8")
            elif parsed.path == "/export.csv":
                output = io.StringIO(newline="")
                writer = csv.DictWriter(
                    output, fieldnames=["client", "provider", "model", "requests", *SUM_FIELDS]
                )
                writer.writeheader()
                writer.writerows(data["models"])
                body = output.getvalue().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Disposition", "attachment; filename=usage.csv")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(404, b"Not found\n", "text/plain; charset=utf-8")
        except FileNotFoundError:
            self._send(503, b"Dashboard UI is not installed\n", "text/plain; charset=utf-8")
        except (ValueError, TypeError):
            self._send(400, b"Invalid query parameters\n", "text/plain; charset=utf-8")
        except sqlite3.Error:
            self._send(503, b"Usage data is unavailable\n", "text/plain; charset=utf-8")

    def _method_not_allowed(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_POST(self) -> None:
        if not self._forward_management():
            self._method_not_allowed()

    def do_PUT(self) -> None:
        if not self._forward_management():
            self._method_not_allowed()

    def do_PATCH(self) -> None:
        if not self._forward_management():
            self._method_not_allowed()

    def do_DELETE(self) -> None:
        if not self._forward_management():
            self._method_not_allowed()

    do_OPTIONS = do_POST

    def do_HEAD(self) -> None:
        if not self._allowed_host():
            self._gateway_error(403, "Host not allowed")
            return
        if self._forward_management():
            return
        parsed = urlsplit(self.path)
        if parsed.path not in {"/", "/api/summary", "/api/organizations", "/export.csv"}:
            self._send(404, b"", "text/plain; charset=utf-8")
            return
        if parsed.path != "/":
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                days = int(self._query_value(query, "days", "30"))
                organization = self._query_value(query, "organization", "all")
                if not 1 <= days <= 365 or (
                    organization != "all"
                    and organization not in {client["name"] for client in self.clients}
                ):
                    raise ValueError("invalid query")
            except (ValueError, TypeError):
                self._send(400, b"", "text/plain; charset=utf-8")
                return
        self._send(200, b"", "text/plain; charset=utf-8")

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def run(args: argparse.Namespace) -> None:
    try:
        is_loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        is_loopback = args.host.lower() == "localhost"
    if not is_loopback:
        raise SystemExit("hub must bind to a loopback address")
    init_db(args.db)
    clients, public_origin = load_service_config(args.config)
    lock_path = os.path.join(os.path.dirname(args.db), "collector.lock")
    lock_file = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another usage collector is already running") from None
    HubHandler.db_path = args.db
    HubHandler.ui_path = args.ui
    HubHandler.clients = clients
    HubHandler.public_origin = public_origin
    threading.Thread(target=collect_forever, args=(args.db, clients), daemon=True).start()
    http.server.ThreadingHTTPServer((args.host, args.port), HubHandler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("HUB_METRICS_DB", DEFAULT_DB))
    parser.add_argument("--config", default=os.environ.get("HUB_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--ui", default=os.environ.get("HUB_UI_PATH", DEFAULT_UI))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8320)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
