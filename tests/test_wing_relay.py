import json
import threading
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from missions import FILE_VERSION
from wing_relay import fetch_spread, push_ledger

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
