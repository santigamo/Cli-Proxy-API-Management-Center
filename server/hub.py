#!/usr/bin/env python3
"""Read-only local usage dashboard and single-consumer CLIProxyAPI collector."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import http.server
import io
import json
import os
import pathlib
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_DB = "/var/lib/pi-hub-metrics/usage.sqlite3"
DEFAULT_CONFIG = "/etc/pi-hub-metrics/config.json"
DEFAULT_UI = "/opt/pi-hub-metrics/index.html"
ALLOWED_ORGANIZATIONS = {"all"}
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


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
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
        if not isinstance(port, int) or not 1 <= port <= 65535 or not isinstance(key, str) or not key:
            raise ValueError("invalid client connection settings")
        found.add(name)
        label = client.get("label", name)
        if not isinstance(label, str):
            label = name
        normalized.append({"name": name, "label": label[:60], "port": port, "key": key})
    return normalized


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

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _query_value(query: dict[str, list[str]], key: str, default: str) -> str:
        values = query.get(key, [default])
        if len(values) != 1:
            raise ValueError("invalid query")
        return values[0]

    def do_GET(self) -> None:
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
        self._method_not_allowed()

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST

    def do_HEAD(self) -> None:
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
    init_db(args.db)
    clients = load_clients(args.config)
    lock_path = os.path.join(os.path.dirname(args.db), "collector.lock")
    lock_file = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another usage collector is already running") from None
    HubHandler.db_path = args.db
    HubHandler.ui_path = args.ui
    HubHandler.clients = clients
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
