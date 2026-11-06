import csv
import datetime as dt
import http.server
import io
import json
import pathlib
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from server import hub


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(pathlib.Path(self.temp.name) / "usage.sqlite3")
        hub.init_db(self.db)
        self.now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.fixture = {
            "timestamp": self.now,
            "provider": "anthropic",
            "model": "claude-test",
            "alias": "safe alias",
            "latency_ms": 9,
            "tokens": {
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_read_tokens": 7,
                "cache_creation_tokens": 5,
                "reasoning_tokens": 2,
                "total_tokens": 25,
            },
            "api_key": "SECRET_SENTINEL",
            "prompt": "PRIVATE_PROMPT_SENTINEL",
            "response": "PRIVATE_RESPONSE_SENTINEL",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_allowlist_persistence_dedup_and_per_organization_summary(self):
        distinct_event = {**self.fixture, "prompt": "another private prompt"}
        hub.store(self.db, "oasis", [self.fixture, self.fixture, distinct_event])
        hub.store(self.db, "stradem", [self.fixture])
        hub.init_db(self.db)
        all_data = hub.summary(self.db, 30, "all", {"oasis", "stradem"})
        oasis = hub.summary(self.db, 30, "oasis", {"oasis", "stradem"})
        self.assertEqual({row["client"] for row in all_data["totals"]}, {"oasis", "stradem"})
        self.assertEqual(len(oasis["totals"]), 1)
        self.assertEqual(oasis["totals"][0]["requests"], 2)
        self.assertEqual(oasis["totals"][0]["total_tokens"], 50)
        self.assertEqual(oasis["totals"][0]["cache_read_tokens"], 14)
        self.assertEqual({row["client"] for row in oasis["models"]}, {"oasis"})
        with hub.connect(self.db) as conn:
            dump = "\n".join(conn.iterdump())
        for secret in ("SECRET_SENTINEL", "PRIVATE_PROMPT_SENTINEL", "PRIVATE_RESPONSE_SENTINEL"):
            self.assertNotIn(secret, dump)

    def test_unknown_org_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown organization"):
            hub.summary(self.db, 30, "../../etc/passwd", {"oasis", "stradem"})

    def test_public_origin_config_is_explicit_https_only(self):
        config_path = pathlib.Path(self.temp.name) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "public_origin": "https://panel.example.ts.net",
                    "clients": [{"name": "oasis", "port": 8317, "key": "secret"}],
                }
            ),
            encoding="utf-8",
        )
        clients, public_origin = hub.load_service_config(str(config_path))
        self.assertEqual([client["name"] for client in clients], ["oasis"])
        self.assertEqual(public_origin, "https://panel.example.ts.net")
        with self.assertRaisesRegex(ValueError, "HTTPS origin"):
            hub.normalize_public_origin("http://panel.example.ts.net")

    def test_all_excludes_removed_clients_and_reserved_all_name_is_invalid(self):
        hub.store(self.db, "oasis", [self.fixture])
        hub.store(self.db, "stradem", [self.fixture])
        hub.store(self.db, "removed-client", [self.fixture])
        with hub.connect(self.db) as conn:
            for client in ("oasis", "stradem", "removed-client"):
                conn.execute("INSERT INTO health(client,last_success,error) VALUES (?,?,?)", (client, self.now, None))
        all_data = hub.summary(self.db, 30, "all", {"oasis", "stradem"})
        self.assertEqual({row["client"] for row in all_data["totals"]}, {"oasis", "stradem"})
        self.assertEqual({row["client"] for row in all_data["models"]}, {"oasis", "stradem"})
        self.assertEqual({row["client"] for row in all_data["daily"]}, {"oasis", "stradem"})
        self.assertEqual({row["client"] for row in all_data["health"]}, {"oasis", "stradem"})

        config_path = pathlib.Path(self.temp.name) / "config.json"
        config_path.write_text(
            json.dumps({"clients": [{"name": "all", "port": 8317, "key": "test"}]}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid client name"):
            hub.load_clients(str(config_path))

    def _start_server(self):
        class Handler(hub.HubHandler):
            pass

        Handler.db_path = self.db
        Handler.ui_path = str(pathlib.Path(self.temp.name) / "index.html")
        Handler.public_origin = ""
        Handler.clients = [
            {"name": "oasis", "label": "Oasis"},
            {"name": "stradem", "label": "Stradem"},
        ]
        pathlib.Path(Handler.ui_path).write_text(
            '<img src="data:image/jpeg;base64,AA=="><h1>UI</h1>', encoding="utf-8"
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_port}"

    def test_http_summary_orgs_validation_csv_filter_and_read_only_methods(self):
        hub.store(self.db, "oasis", [self.fixture])
        hub.store(self.db, "stradem", [self.fixture])
        base = self._start_server()
        with urllib.request.urlopen(base + "/") as response:
            csp = response.headers["Content-Security-Policy"]
            html = response.read().decode()
        self.assertIn("data:image/jpeg;base64,AA==", html)
        self.assertIn("img-src 'self' data:", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("https:", csp)
        rebinding_request = urllib.request.Request(
            base + "/api/summary?days=30", headers={"Host": "attacker.invalid"}
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            with urllib.request.urlopen(rebinding_request):
                pass
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with urllib.request.urlopen(base + "/api/organizations") as response:
            orgs = json.load(response)
        self.assertEqual(orgs, [{"id": "oasis", "name": "Oasis"}, {"id": "stradem", "name": "Stradem"}])
        with urllib.request.urlopen(base + "/api/summary?days=30&organization=oasis") as response:
            summary = json.load(response)
        self.assertEqual([row["client"] for row in summary["totals"]], ["oasis"])
        for query in ("days=0", "days=366", "organization=other"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                with urllib.request.urlopen(base + "/api/summary?" + query):
                    pass
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        with urllib.request.urlopen(base + "/export.csv?days=30&organization=stradem") as response:
            rows = list(csv.DictReader(io.StringIO(response.read().decode())))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["client"], "stradem")
        with self.assertRaises(urllib.error.HTTPError) as error:
            with urllib.request.urlopen(
                urllib.request.Request(base + "/api/summary", method="POST")
            ):
                pass
        self.assertEqual(error.exception.code, 405)
        error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            with urllib.request.urlopen(
                urllib.request.Request(
                    base + "/api/summary?organization=unknown",
                    method="HEAD",
                )
            ):
                pass
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        with urllib.request.urlopen(base + "/api/summary?days=30") as response:
            body = response.read().decode()
        self.assertNotIn("SECRET_SENTINEL", body)

    def test_scoped_gateway_injects_org_keys_and_preserves_admin_api_contract(self):
        captures = []

        class FakeUpstream(http.server.BaseHTTPRequestHandler):
            secret = ""
            support_plugins = "0"
            resource_path = "/v0/resource/plugins/test/summary"

            def _respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/v8/management/config/access/api-keys":
                    response_body = json.dumps([self.secret + "_CLIENT_KEY"]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                    return
                if self.path == "/v8/management/plugins":
                    response_body = json.dumps(
                        {"plugins": [{"menus": [{"path": self.resource_path}]}]}
                    ).encode()
                    self.send_response(200)
                    self.send_header("X-CPA-Support-Plugin", self.support_plugins)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                    return
                if self.path.startswith("/v1/models"):
                    captures.append(
                        {
                            "secret": self.secret,
                            "method": self.command,
                            "path": self.path,
                            "body": body,
                            "headers": {key.lower(): value for key, value in self.headers.items()},
                        }
                    )
                    response_body = b'{"data":[{"id":"example-model"}]}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                    return
                if self.path == "/v8/management/redirect-external":
                    self.send_response(302)
                    self.send_header("Location", "https://example.invalid/steal")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                captures.append(
                    {
                        "secret": self.secret,
                        "method": self.command,
                        "path": self.path,
                        "body": body,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                    }
                )
                response_body = b"\x00\xffbinary-export"
                self.send_response(207)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename=export.bin")
                self.send_header("ETag", '"rev-7"')
                self.send_header("X-CPA-Version", "8.0.58")
                self.send_header("X-CPA-Build-Date", "2026-11-06")
                self.send_header("X-CPA-Support-Plugin", "true")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

            do_GET = _respond
            do_POST = _respond
            do_PUT = _respond
            do_PATCH = _respond
            do_DELETE = _respond

            def log_message(self, _format, *_args):
                return

        upstreams = []
        clients = []
        for name, secret in (("oasis", "OASIS_MANAGEMENT_SECRET"), ("stradem", "STRADEM_MANAGEMENT_SECRET")):
            handler = type(
                name.title() + "Upstream",
                (FakeUpstream,),
                {
                    "secret": secret,
                    "support_plugins": "1" if name == "stradem" else "0",
                },
            )
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            upstreams.append(server)
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            clients.append({"name": name, "label": name.title(), "port": server.server_port, "key": secret})

        class GatewayHandler(hub.HubHandler):
            pass

        GatewayHandler.clients = clients
        GatewayHandler.public_origin = "https://panel.example.ts.net"
        gateway = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        self.addCleanup(gateway.shutdown)
        self.addCleanup(gateway.server_close)
        base = f"http://127.0.0.1:{gateway.server_port}"
        origin = "https://panel.example.ts.net"
        route = "/organizations/oasis/v8/management/auth-files/export"

        with self.assertRaises(urllib.error.HTTPError) as error:
            with urllib.request.urlopen(base + route):
                pass
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        self.assertEqual(captures, [])

        cross_site = urllib.request.Request(
            base + route,
            headers={
                "X-Hub-Request": "1",
                "Origin": "http://attacker.invalid",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            with urllib.request.urlopen(cross_site):
                pass
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

        request = urllib.request.Request(
            base + route + "?format=bin",
            data=b'{"operation":"export"}',
            method="POST",
            headers={
                "X-Hub-Request": "1",
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "Host": "panel.example.ts.net",
                "Authorization": "Bearer browser-placeholder",
                "Cookie": "session=browser-cookie",
                "X-Forwarded-For": "203.0.113.99",
                "Content-Type": "application/json",
                "If-Match": '"revision-6"',
            },
        )
        with urllib.request.urlopen(request) as response:
            payload = response.read()
            self.assertEqual(response.status, 207)
            self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
            self.assertEqual(response.headers["Content-Disposition"], "attachment; filename=export.bin")
            self.assertEqual(response.headers["ETag"], '"rev-7"')
            self.assertEqual(response.headers["X-CPA-Version"], "8.0.58")
            self.assertEqual(response.headers["X-CPA-Build-Date"], "2026-11-06")
            self.assertEqual(response.headers["X-CPA-Support-Plugin"], "true")
        self.assertEqual(payload, b"\x00\xffbinary-export")
        oasis_request = captures[-1]
        self.assertEqual(oasis_request["secret"], "OASIS_MANAGEMENT_SECRET")
        self.assertEqual(oasis_request["method"], "POST")
        self.assertEqual(oasis_request["path"], "/v8/management/auth-files/export?format=bin")
        self.assertEqual(oasis_request["body"], b'{"operation":"export"}')
        self.assertEqual(oasis_request["headers"]["authorization"], "Bearer OASIS_MANAGEMENT_SECRET")
        self.assertEqual(oasis_request["headers"]["if-match"], '"revision-6"')
        self.assertEqual(oasis_request["headers"]["content-type"], "application/json")
        self.assertNotIn("cookie", oasis_request["headers"])
        self.assertNotIn("x-forwarded-for", oasis_request["headers"])
        self.assertNotIn("x-hub-request", oasis_request["headers"])

        stradem_request = urllib.request.Request(
            base + "/organizations/stradem/v8/management/config",
            headers={
                "X-Hub-Request": "1",
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "Host": "127.0.0.1",
            },
        )
        with urllib.request.urlopen(stradem_request) as response:
            self.assertEqual(response.read(), b"\x00\xffbinary-export")
        self.assertEqual(captures[-1]["secret"], "STRADEM_MANAGEMENT_SECRET")
        self.assertEqual(captures[-1]["headers"]["authorization"], "Bearer STRADEM_MANAGEMENT_SECRET")

        models_request = urllib.request.Request(
            base + "/organizations/oasis/v1/models?client_version=test",
            headers={
                "X-Hub-Request": "1",
                "Origin": origin,
                "Host": "panel.example.ts.net",
            },
        )
        with urllib.request.urlopen(models_request) as response:
            models_body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(models_body, b'{"data":[{"id":"example-model"}]}')
        self.assertNotIn(b"OASIS_MANAGEMENT_SECRET", models_body)
        self.assertEqual(captures[-1]["path"], "/v1/models?client_version=test")
        self.assertEqual(captures[-1]["headers"]["authorization"], "Bearer OASIS_MANAGEMENT_SECRET_CLIENT_KEY")

        capture_count = len(captures)
        for path in (
            "/organizations/oasis/v1/models",
            "/organizations/stradem/v0/resource/plugins/test/summary",
        ):
            raw_request = (
                f"GET {path} HTTP/1.1\r\n"
                "Host: panel.example.ts.net\r\n"
                "Origin: https://panel.example.ts.net\r\n"
                "X-Hub-Request: 1\r\n"
                "Content-Length: not-an-integer\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            with socket.create_connection(("127.0.0.1", gateway.server_port)) as client_socket:
                client_socket.sendall(raw_request)
                response = client_socket.recv(4096)
            self.assertEqual(response.split(b"\r\n", 1)[0].split()[1], b"400")
        self.assertEqual(len(captures), capture_count)

        unsupported_resource = urllib.request.Request(
            base + "/organizations/oasis/v0/resource/plugins/test/summary",
            headers={
                "X-Hub-Request": "1",
                "Origin": origin,
                "Host": "panel.example.ts.net",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            with urllib.request.urlopen(unsupported_resource):
                pass
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        supported_resource = urllib.request.Request(
            base + "/organizations/stradem/v0/resource/plugins/test/summary",
            headers={
                "X-Hub-Request": "1",
                "Origin": origin,
                "Host": "panel.example.ts.net",
            },
        )
        with urllib.request.urlopen(supported_resource) as response:
            self.assertEqual(response.read(), b"\x00\xffbinary-export")
        self.assertEqual(captures[-1]["path"], "/v0/resource/plugins/test/summary")

        arbitrary_resource = urllib.request.Request(
            base + "/organizations/stradem/v0/resource/plugins/other/path",
            headers={
                "X-Hub-Request": "1",
                "Origin": origin,
                "Host": "panel.example.ts.net",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            with urllib.request.urlopen(arbitrary_resource):
                pass
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

        for invalid_path, status in (
            ("/organizations/unknown/v8/management/config", 404),
            ("/organizations/oasis/v8/management/%252e%252e/config", 400),
        ):
            invalid = urllib.request.Request(
                base + invalid_path,
                headers={"X-Hub-Request": "1", "Origin": origin},
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                with urllib.request.urlopen(invalid):
                    pass
            self.assertEqual(error.exception.code, status)
            error.exception.close()

        external_redirect = urllib.request.Request(
            base + "/organizations/oasis/v8/management/redirect-external",
            headers={"X-Hub-Request": "1", "Origin": origin, "Host": "panel.example.ts.net"},
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            with urllib.request.urlopen(external_redirect):
                pass
        self.assertEqual(error.exception.code, 502)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
