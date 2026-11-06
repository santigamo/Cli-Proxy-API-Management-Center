import csv
import datetime as dt
import http.server
import io
import json
import pathlib
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
        Handler.clients = [
            {"name": "oasis", "label": "Oasis"},
            {"name": "stradem", "label": "Stradem"},
        ]
        pathlib.Path(Handler.ui_path).write_text("<h1>UI</h1>", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
