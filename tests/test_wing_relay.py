import json
import threading
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from missions import FILE_VERSION
from wing_relay import as_url_list, fetch_spread, fetch_spreads, push_ledger

LEDGER = {"version": FILE_VERSION, "commander": "Wing Mate", "profile": "mate",
          "missions": [{"id": 4242, "name": "Clean the skies", "faction": "Security",
                       "target": "Pirates", "kill_count": 5, "wing": True, "expiry": "",
                       "reward": 100000, "accepted": 1761000000.0,
                       "kills": 3, "done": False, "known": True}]}


class RelayHandler(BaseHTTPRequestHandler):
    def do_PUT(self):
        size = int(self.headers.get("Content-Length", 0))
        RelayHandler.received[self.path] = self.rfile.read(size)
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        body = json.dumps(RelayHandler.served.get(self.path, {})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class WingRelayTests(unittest.TestCase):
    def setUp(self):
        RelayHandler.received = {}
        RelayHandler.served = {"/mate.json": LEDGER}
        self.server = HTTPServer(("127.0.0.1", 0), RelayHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_push_ledger_uploads_file_under_base(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "default.json"
            path.write_text(json.dumps(LEDGER), encoding="utf-8")
            failure = push_ledger(path, f"http://127.0.0.1:{self.port}/wing/")
            self.assertEqual(failure, "")
            self.assertEqual(json.loads(RelayHandler.received["/wing/default.json"]), LEDGER)

    def test_push_missing_file_reports_error(self):
        failure = push_ledger("/nope/default.json", f"http://127.0.0.1:{self.port}")
        self.assertIn("Could not read", failure)

    def test_fetch_spread_merges_remote_ledger(self):
        spread, name, error = fetch_spread(f"http://127.0.0.1:{self.port}/mate.json")
        self.assertEqual(error, "")
        self.assertEqual(name, "mate.json")
        self.assertEqual((spread["total"], spread["done"], spread["left"]), (1, 0, 2))
        self.assertTrue(spread["wing"])

    def test_fetch_empty_or_broken_ledger_reports(self):
        RelayHandler.served["/empty.json"] = {"version": FILE_VERSION, "missions": []}
        spread, name, error = fetch_spread(f"http://127.0.0.1:{self.port}/empty.json")
        self.assertIsNone(spread)
        self.assertIn("no missions", error)

    def test_fetch_connection_failure_is_clean(self):
        spread, name, error = fetch_spread("http://127.0.0.1:1/none.json", timeout=1)
        self.assertIsNone(spread)
        self.assertIn("Could not fetch", error)


class UrlListTests(unittest.TestCase):
    def test_as_url_list_accepts_str_list_and_blanks(self):
        self.assertEqual(as_url_list("http://a/x.json"), ["http://a/x.json"])
        self.assertEqual(as_url_list(["http://a", " ", "http://b"]), ["http://a", "http://b"])

    def test_fetch_spreads_mixes_success_and_failure(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        from missions import FILE_VERSION

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/good.json":
                    body = json.dumps({"version": FILE_VERSION, "missions": [
                        {"id": 99, "target": "Pirates", "kill_count": 2, "kills": 0, "known": True}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            spreads, errors = fetch_spreads([f"http://127.0.0.1:{port}/good.json",
                                             f"http://127.0.0.1:{port}/missing.json"])
            self.assertEqual([name for name, _ in spreads], ["good.json"])
            self.assertEqual(len(errors), 1)
            self.assertIn("missing.json", errors[0])
        finally:
            server.shutdown()
            server.server_close()
