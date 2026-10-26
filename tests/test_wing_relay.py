import json
import threading
import unittest
import urllib.error
from urllib.request import Request, urlopen
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from missions import FILE_VERSION
from wing_relay import (as_url_list, fetch_spread, fetch_spreads, push_ledger,
                        redact,
                        stale_notes, stale_spread, start_wing_relay)

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
        value = RelayHandler.served.get(self.path, {})
        body = (value if isinstance(value, str) else json.dumps(value)).encode()
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

    def test_fetch_non_object_json_says_so(self):
        RelayHandler.served["/list.json"] = "[1, 2, 3]"
        spread, name, error = fetch_spread(f"http://127.0.0.1:{self.port}/list.json")
        self.assertIsNone(spread)
        self.assertIn("not an object", error)

    def test_fetch_old_format_ledger_names_the_version(self):
        stale = dict(LEDGER, version=990101)
        RelayHandler.served["/old.json"] = stale
        spread, name, error = fetch_spread(f"http://127.0.0.1:{self.port}/old.json")
        self.assertIsNone(spread)
        self.assertIn("v990101", error)
        self.assertIn("updated monitor", error)

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


class StaleLedgerTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_fresh_and_missing_never_stale(self):
        self.assertIsNone(stale_spread({"updated": self.NOW - 60}, self.NOW))
        self.assertIsNone(stale_spread({"updated": 0}, self.NOW))
        self.assertIsNone(stale_spread({}, self.NOW))
        self.assertIsNone(stale_spread({"updated": "nonsense"}, self.NOW))

    def test_day_old_flags_hours(self):
        self.assertIsNone(stale_spread({"updated": self.NOW - 10 * 3600},
                                       self.NOW, older_than_hours=48))
        self.assertIsNotNone(stale_spread({"updated": self.NOW - 10 * 3600},
                                          self.NOW, older_than_hours=6))
        hours = stale_spread({"updated": self.NOW - 30 * 3600}, self.NOW)
        self.assertIsNotNone(hours)
        self.assertAlmostEqual(hours, 30, delta=0.01)


class StaleNotesTests(unittest.TestCase):
    def test_notes_for_each_stale_named_ledger(self):
        now = 1_800_000_000.0
        wings = [("ana.json", {"updated": now - 30 * 3600}),
                 ("fresh.json", {"updated": now - 60})]
        import wing_relay
        original = wing_relay.time
        try:
            wing_relay.time = lambda: now
            notes = stale_notes(wings)
        finally:
            wing_relay.time = original
        self.assertEqual(notes, ["ana: no updates for 30h - counts may be stale"])


class WingServeTests(unittest.TestCase):
    def test_oversized_put_is_rejected_before_hitting_disk(self):
        big = json.dumps({"version": FILE_VERSION, "missions": [],
                          "note": "x" * (3 * 1024 * 1024)}).encode()
        request = Request(f"{self.base}/fat.json", method="PUT", data=big)
        try:
            urlopen(request, timeout=5)
            self.fail("3 MB ledger should 413")
        except urllib.error.HTTPError as caught:
            self.assertEqual(caught.code, 413)
        self.assertFalse((Path(self.folder.name) / "fat.json").exists())

    def test_accepted_pushes_report_to_the_host(self):
        seen = []
        self.server, error = start_wing_relay(0, self.folder.name,
                                              on_put=lambda name, size: seen.append((name, size)))
        self.assertEqual(error, "")
        base = f"http://127.0.0.1:{self.server.server_address[1]}/wing"
        with TemporaryDirectory() as folder:
            path = Path(folder) / "buddy.json"
            path.write_text(json.dumps(LEDGER), encoding="utf-8")
            self.assertEqual(push_ledger(path, base, timeout=3), "")
        self.assertEqual([name for name, _ in seen], ["buddy.json"])
        self.assertGreater(seen[0][1], 0)

    def test_relay_root_fetchurl_expands_the_inventory(self):
        (Path(self.folder.name) / "mate.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        with TemporaryDirectory() as folder:
            path = Path(folder) / "second.json"
            path.write_text(json.dumps(dict(LEDGER, commander="Second Wingmate")), encoding="utf-8")
            self.assertEqual(push_ledger(path, self.base, timeout=3), "")
        spreads, errors = fetch_spreads([f"{self.base}/"], timeout=3)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(name for name, _ in spreads), ["mate.json", "second.json"])

    def test_directory_listing_covers_both_tiers(self):
        (Path(self.folder.name) / "own.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        from urllib.request import urlopen
        with urlopen(f"{self.base}/", timeout=3) as response:
            listing = json.loads(response.read())
        self.assertEqual([entry["name"] for entry in listing["ledgers"]], ["own.json"])
        self.assertIn("updated", listing["ledgers"][0])

    def test_token_gates_get_and_put(self):
        self.server, error = start_wing_relay(0, self.folder.name, token="secret")
        self.assertEqual(error, "")
        base = f"http://127.0.0.1:{self.server.server_address[1]}/wing"
        (Path(self.folder.name) / "mate.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        _, _, error = fetch_spread(f"{base}/mate.json")
        self.assertIn("Unauthorized", error)
        from urllib.request import urlopen as _open
        try:
            _open(f"{base}/mate.json", timeout=3)
            self.fail("no-token GET should 401")
        except urllib.error.HTTPError as caught:
            self.assertIn("needs ?token=", json.loads(caught.read())["error"])
        spread, name, error = fetch_spread(f"{base}/mate.json?token=secret")
        self.assertEqual(error, "")
        self.assertEqual(spread["total"], 1)
        # PushURL with a query token must splice the filename before the query
        with TemporaryDirectory() as folder:
            path = Path(folder) / "mate2.json"
            path.write_text(json.dumps(LEDGER), encoding="utf-8")
            self.assertEqual(push_ledger(path, f"{base}?token=secret", timeout=3), "")
            self.assertTrue((Path(self.folder.name) / "mate2.json").is_file())
        _, _, error = fetch_spread(f"{base}/mate2.json?token=wrong")
        self.assertIn("Unauthorized", error)
        # a wrong-token PUSH must also surface as a clean 401, not a wedged
        # connection: the body has to be drained before the verdict
        with TemporaryDirectory() as folder:
            path = Path(folder) / "mate3.json"
            path.write_text(json.dumps(LEDGER), encoding="utf-8")
            failure = push_ledger(path, f"{base}?token=wrong", timeout=3)
        self.assertIn("Unauthorized", failure)
        self.assertFalse((Path(self.folder.name) / "mate3.json").exists())

    """The monitor-embedded relay (start_wing_relay) round-trips ledgers."""

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.server, error = start_wing_relay(0, self.folder.name)
        self.assertEqual(error, "")
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/wing"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def test_put_then_get_round_trip(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "mate.json"
            path.write_text(json.dumps(LEDGER), encoding="utf-8")
            self.assertEqual(push_ledger(path, self.base, timeout=3), "")
        spread, name, error = fetch_spread(f"{self.base}/mate.json")
        self.assertEqual(error, "")
        self.assertEqual((spread["total"], spread["done"], spread["left"]), (1, 0, 2))

    def test_serves_existing_json_and_404s_otherwise(self):
        (Path(self.folder.name) / "mate.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        spread, name, error = fetch_spread(f"{self.base}/mate.json")
        self.assertEqual(error, "")
        self.assertEqual(name, "mate.json")
        spread, name, error = fetch_spread(f"{self.base}/ghost.json")
        self.assertIsNone(spread)
        self.assertIn("Not Found", error)

    def test_rejects_non_json_paths_and_bodies(self):
        for bad in ("notjson.txt", "x.json"):
            request = Request(f"{self.base}/{bad}", method="PUT", data=b"hello")
            try:
                urlopen(request, timeout=3)
                self.fail(f"{bad or 'x.json'}: expected 400")
            except urllib.error.HTTPError as caught:
                self.assertEqual(caught.code, 400)

    def test_unresolvable_bind_host_reports_error_not_crash(self):
        server, error = start_wing_relay(0, self.folder.name, host="no.such.host.invalid")
        self.assertIsNone(server)
        self.assertIn("could not bind", error)


class RedactTests(unittest.TestCase):
    def test_tokens_never_appear_in_reported_urls(self):
        hidden = redact("http://h:1/wing?token=hunter2&x=1")
        self.assertNotIn("hunter2", hidden)
        self.assertIn("token=\u2022&x=1", hidden)
        self.assertEqual(redact("http://h/wing/x.json"), "http://h/wing/x.json")

    def test_fetch_failure_with_token_query_is_redacted(self):
        _, _, error = fetch_spread("http://127.0.0.1:1/nobody.json?token=s3cret", timeout=1)
        self.assertNotIn("s3cret", error)
        self.assertIn("nobody.json", error)


class SlowRelayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        import time as _time
        _time.sleep(1.5)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class FetchTimeoutTests(unittest.TestCase):
    def test_fetch_timeout_is_honoured(self):
        server = HTTPServer(("127.0.0.1", 0), SlowRelayHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            spreads, errors = fetch_spreads([f"http://127.0.0.1:{port}/m.json"], timeout=0.4)
            self.assertEqual(spreads, [])
            self.assertIn("timed out", errors[0])
        finally:
            server.shutdown()
            server.server_close()
