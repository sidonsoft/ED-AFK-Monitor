import json
import threading
import unittest
import urllib.error
from time import time
from urllib.request import Request, urlopen
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from missions import FILE_VERSION
from wing_relay import (_age_words, _listing_entries, as_url_list, fetch_spread, fetch_spreads, push_ledger, redact, relay_meta, serve_advice, served_count, stale_notes, stale_spread, start_wing_relay, url_shape_warnings, wing_test_lines)

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
        self.assertIsNone(stale_spread({"updated": self.NOW - 10 * 3600},
                                       self.NOW, older_than_hours="not-a-number"))
        # junk threshold behaves like the 24h default, not like 'never stale'
        self.assertIsNotNone(stale_spread({"updated": self.NOW - 30 * 3600},
                                          self.NOW, older_than_hours="not-a-number"))
        self.assertIsNotNone(stale_spread({"updated": self.NOW - 10 * 3600},
                                          self.NOW, older_than_hours="6"))
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

    def test_wing_test_describes_roots_and_failures(self):
        (Path(self.folder.name) / "mate.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        lines, ok = wing_test_lines([f"{self.base}/"], timeout=3)
        self.assertTrue(ok)
        self.assertEqual([kind for kind, _ in lines], ["ok", "ok"])  # meta + ledger
        self.assertIn("mate.json: 0/1 complete · 2 left",
                      next(text for _, text in lines if text.startswith("mate.json")))
        lines, ok = wing_test_lines([f"{self.base}/ghost.json"], timeout=3)
        self.assertFalse(ok)
        self.assertEqual([kind for kind, _ in lines], ["warn"])
        lines, ok = wing_test_lines([], timeout=3)
        self.assertFalse(ok)
        self.assertIn("yielded nothing", lines[0][1])

    def test_header_token_works_without_query_secret(self):
        # URLs end up in shell history, proxies and browser bars; the
        # header form keeps the secret out of the recorded line
        (Path(self.folder.name) / "mate.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        server, error = start_wing_relay(0, self.folder.name, token="secret")
        self.assertEqual(error, "")
        self.base = f"http://127.0.0.1:{server.server_address[1]}/wing"
        req = Request(f"{self.base}/mate.json", method="GET", headers={"X-Wing-Token": "secret"})
        with urlopen(req, timeout=3) as response:
            self.assertEqual(json.loads(response.read())["commander"], LEDGER["commander"])
        body = json.dumps(dict(LEDGER, commander="Header Pusher")).encode()
        req = Request(f"{self.base}/hdr.json", method="PUT", data=body,
                      headers={"X-Wing-Token": "secret"})
        with urlopen(req, timeout=3) as response:
            self.assertEqual(response.status, 204)
        self.assertTrue((Path(self.folder.name) / "hdr.json").exists())
        req = Request(f"{self.base}/mate.json", method="GET", headers={"X-Wing-Token": "wrong"})
        try:
            urlopen(req, timeout=3)
            self.fail("wrong header token should be denied")
        except urllib.error.HTTPError as failure:
            self.assertEqual(failure.code, 401)
        server.shutdown()

    def test_env_token_authorizes_without_touching_urls(self):
        import os
        from unittest import mock
        (Path(self.folder.name) / "mate.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        server, error = start_wing_relay(0, self.folder.name, token="sekret")
        self.assertEqual(error, "")
        base = f"http://127.0.0.1:{server.server_address[1]}/wing"
        with TemporaryDirectory() as folder:
            path = Path(folder) / "envmate.json"
            path.write_text(json.dumps(LEDGER), encoding="utf-8")
            with mock.patch.dict(os.environ, {"ED_AFK_WING_TOKEN": "sekret"}):
                spread, _, err = fetch_spread(f"{base}/mate.json", timeout=3)
                self.assertEqual(err, "")
                self.assertEqual(spread["total"], 1)  # the fixture mission
                self.assertEqual(push_ledger(path, base, timeout=3), "")
            # no env: same URLs are denied again
            self.assertIn("401 -", fetch_spread(f"{base}/mate.json", timeout=3)[2])
        self.assertTrue((Path(self.folder.name) / "envmate.json").exists())
        server.shutdown()

    def test_listing_marks_silent_wingmates(self):
        quiet = dict(LEDGER, updated=time() - 100 * 3600)
        (Path(self.folder.name) / "quiet.json").write_text(json.dumps(quiet), encoding="utf-8")
        fresh = dict(LEDGER, updated=time() - 60)
        (Path(self.folder.name) / "fresh.json").write_text(json.dumps(fresh), encoding="utf-8")
        with urlopen(f"{self.base}/", timeout=3) as response:
            entries = {e["name"]: e for e in json.loads(response.read())["ledgers"]}
        self.assertTrue(entries["quiet.json"]["stale"])
        self.assertFalse(entries["fresh.json"]["stale"])
        page = urlopen(Request(f"{self.base}/", headers={"Accept": "text/html"}),
                       timeout=3).read().decode("utf-8")
        self.assertIn("silent wingmate", page)

    def test_format_mismatch_put_is_refused_with_an_explanation(self):
        for body, label in [({"version": FILE_VERSION - 1, "missions": []}, "old format"),
                            ({"missions": []}, "no version"),
                            ([1, 2, 3], "not an object")]:
            req = Request(f"{self.base}/junk.json", method="PUT",
                          data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
            try:
                urlopen(req, timeout=3)
                self.fail(f"{label} should 409")
            except urllib.error.HTTPError as caught:
                self.assertEqual(caught.code, 409)
                self.assertIn("not a v", json.loads(caught.read())["error"])
        # and the good one still lands
        req = Request(f"{self.base}/good.json", method="PUT",
                      data=json.dumps(LEDGER).encode(),
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=3) as response:
            self.assertEqual(response.status, 204)
        self.assertFalse((Path(self.folder.name) / "junk.json").exists())

    def test_rejected_pushes_show_up_as_trouble_on_the_page(self):
        bad = {"version": FILE_VERSION + 3, "missions": []}
        req = Request(f"{self.base}/badmate.json", method="PUT",
                      data=json.dumps(bad).encode(), headers={"Content-Type": "application/json"})
        try:
            urlopen(req, timeout=3)
        except urllib.error.HTTPError:
            pass
        page = urlopen(Request(f"{self.base}/", headers={"Accept": "text/html"}),
                       timeout=3).read().decode("utf-8")
        self.assertIn("recent trouble", page)
        self.assertIn("badmate.json: ledger format", page)

    def test_page_lists_recent_pushes_newest_first(self):
        req = Request(f"{self.base}/one.json", method="PUT",
                      data=json.dumps(LEDGER).encode(), headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=3) as response:
            self.assertEqual(response.status, 204)
        req = Request(f"{self.base}/two.json", method="PUT",
                      data=json.dumps(LEDGER).encode(), headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=3) as response:
            self.assertEqual(response.status, 204)
        page = urlopen(Request(f"{self.base}/", headers={"Accept": "text/html"}),
                       timeout=3).read().decode("utf-8")
        self.assertIn("recent pushes", page)
        self.assertLess(page.index("two.json at"), page.index("one.json at"))

    def test_junk_stale_hours_falls_back_to_the_default_window(self):
        for junk in (-5, "nonsense", True):
            server, error = start_wing_relay(0, self.folder.name, stale_hours=junk)
            self.assertEqual(error, "")
            try:
                fresh = dict(LEDGER, updated=time() - 60)
                (Path(self.folder.name) / "fresh.json").write_text(json.dumps(fresh), encoding="utf-8")
                with urlopen(f"http://127.0.0.1:{server.server_address[1]}/wing/", timeout=3) as response:
                    entries = {e["name"]: e for e in json.loads(response.read())["ledgers"]}
                self.assertFalse(entries["fresh.json"]["stale"], f"junk {junk!r} flagged fresh")
                (Path(self.folder.name) / "fresh.json").unlink()
            finally:
                server.shutdown()
        numeric_string = start_wing_relay(0, self.folder.name, stale_hours="48")
        self.assertEqual(numeric_string[1], "")
        try:
            old = dict(LEDGER, updated=time() - 30 * 3600)
            (Path(self.folder.name) / "old30h.json").write_text(json.dumps(old), encoding="utf-8")
            with urlopen(f"http://127.0.0.1:{numeric_string[0].server_address[1]}/wing/", timeout=3) as response:
                entries = {e["name"]: e for e in json.loads(response.read())["ledgers"]}
            self.assertFalse(entries["old30h.json"]["stale"])  # 48h window honored
        finally:
            numeric_string[0].shutdown()

    def test_wing_test_learns_the_remote_relay_build(self):
        server, error = start_wing_relay(0, self.folder.name, build="4242")
        self.assertEqual(error, "")
        try:
            lines, ok = wing_test_lines(
                [f"http://127.0.0.1:{server.server_address[1]}/wing/"], timeout=3)
        finally:
            server.shutdown()
        self.assertTrue(ok)
        self.assertTrue(any("build v4242" in text for kind, text in lines))
        self.assertTrue(any("ledger format v" in text for kind, text in lines))
        build, fmt, err = relay_meta(f"{self.base}/", timeout=3)
        self.assertEqual(err, "")  # setUp server: format known, build blank

    def test_page_advertises_hosting_build_and_ledger_format(self):
        server, error = start_wing_relay(0, self.folder.name, build="999999")
        self.assertEqual(error, "")
        try:
            page = urlopen(Request(f"http://127.0.0.1:{server.server_address[1]}/wing/",
                                   headers={"Accept": "text/html"}),
                           timeout=3).read().decode("utf-8")
        finally:
            server.shutdown()
        self.assertIn("ED AFK Monitor build v999999", page)
        self.assertIn(f"ledger format v{FILE_VERSION}", page)

    def test_open_relay_advertises_its_own_exposure_on_the_page(self):
        server, error = start_wing_relay(0, self.folder.name, host="0.0.0.0",
                                         banner=serve_advice("0.0.0.0", ""))
        self.assertEqual(error, "")
        try:
            page = urlopen(Request(f"http://127.0.0.1:{server.server_address[1]}/wing/",
                                   headers={"Accept": "text/html"}), timeout=3).read().decode("utf-8")
        finally:
            server.shutdown()
        self.assertIn("no token", page)
        self.assertIn("ServeToken", page)

    def test_token_deny_pages_self_document_for_browsers(self):
        server, error = start_wing_relay(0, self.folder.name, token="sekret")
        self.assertEqual(error, "")
        base = f"http://127.0.0.1:{server.server_address[1]}/wing"
        try:
            req = Request(f"{base}/", headers={"Accept": "text/html"})
            try:
                urlopen(req, timeout=3)
                self.fail("tokenless browser GET should 401")
            except urllib.error.HTTPError as caught:
                page = caught.read().decode("utf-8")
                self.assertIn("text/html", caught.headers.get("Content-Type"))
                self.assertIn("needs ?token=", page)
                # machines (no explicit html Accept) keep getting JSON
                try:
                    urlopen(f"{base}/", timeout=3)
                except urllib.error.HTTPError as machine:
                    self.assertIn("needs ?token=", json.loads(machine.read())["error"])
        finally:
            server.shutdown()

    def test_browser_gets_a_landing_page_json_gets_the_list(self):
        (Path(self.folder.name) / "mate.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        html_req = Request(f"{self.base}/", headers={"Accept": "text/html"})
        with urlopen(html_req, timeout=3) as response:
            page = response.read().decode("utf-8")
            self.assertIn("text/html", response.headers.get("Content-Type"))
        self.assertIn("<a href=\"mate.json\">mate.json</a>", page)
        self.assertIn("ledger(s) served here", page)
        with urlopen(f"{self.base}/", timeout=3) as response:
            self.assertEqual(response.headers.get("Content-Type"), "application/json")

    def test_same_ledger_via_two_urls_counts_once(self):
        (Path(self.folder.name) / "mate.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        spreads, errors = fetch_spreads([f"{self.base}/", f"{self.base}/mate.json"], timeout=3)
        self.assertEqual(errors, [])
        self.assertEqual([name for name, _ in spreads], ["mate.json"])
        # two different ledgers that share a basename both survive
        (Path(self.folder.name) / "second.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        spreads, errors = fetch_spreads([f"{self.base}/second.json",
                                         f"{self.base}/mate.json?x=1",
                                         f"{self.base}/mate.json?x=1"], timeout=3)
        self.assertEqual(sorted(name for name, _ in spreads), ["mate.json", "second.json"])

    def test_relay_root_fetchurl_expands_the_inventory(self):
        (Path(self.folder.name) / "mate.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        with TemporaryDirectory() as folder:
            path = Path(folder) / "second.json"
            path.write_text(json.dumps(dict(LEDGER, commander="Second Wingmate")), encoding="utf-8")
            self.assertEqual(push_ledger(path, self.base, timeout=3), "")
        spreads, errors = fetch_spreads([f"{self.base}/"], timeout=3)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(name for name, _ in spreads), ["mate.json", "second.json"])

    def test_error_verdicts_are_anti_cache_too(self):
        # browsers cache 404s: a ghost fetch must not stick
        import urllib.error
        from urllib.request import urlopen
        for url, want in ((f"{self.base}/ghost.json", 404),
                          (f"{self.base}/badpath.txt", 404)):
            try:
                urlopen(url, timeout=3)
                self.fail(f"{url} should have failed")
            except urllib.error.HTTPError as failure:
                self.assertEqual(failure.code, want)
                self.assertEqual(failure.headers.get("Cache-Control"), "no-store")

    def test_ledger_and_listing_responses_are_anti_cache(self):
        # wingmates poll these URLs expecting fresh data; a browser or proxy
        # replaying a 404 listing would hide a push that already landed
        (Path(self.folder.name) / "mate.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        from urllib.request import urlopen
        for url in (f"{self.base}/mate.json", f"{self.base}/"):
            with urlopen(url, timeout=3) as response:
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")

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
        self.assertIn("401 -", error)
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
        self.assertIn("401 -", error)
        # a wrong-token PUSH must also surface as a clean 401, not a wedged
        # connection: the body has to be drained before the verdict
        with TemporaryDirectory() as folder:
            path = Path(folder) / "mate3.json"
            path.write_text(json.dumps(LEDGER), encoding="utf-8")
            failure = push_ledger(path, f"{base}?token=wrong", timeout=3)
        self.assertIn("401 -", failure)
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


class ServeAdviceTests(unittest.TestCase):
    def test_open_bind_without_token_warns_once_named(self):
        advice = serve_advice("0.0.0.0", "")
        self.assertIn("0.0.0.0", advice)
        self.assertIn("ServeToken", advice)
        self.assertEqual(serve_advice("0.0.0.0", "sekret"), "")

    def test_loopback_stays_quiet(self):
        for host in ("127.0.0.1", "localhost", "::1", ""):
            self.assertEqual(serve_advice(host, ""), "")


class ServedCountTests(unittest.TestCase):
    def test_names_dedupe_across_tiers(self):
        with TemporaryDirectory() as folder:
            wing = Path(folder) / "wing"
            wing.mkdir()
            (wing / "mate.json").write_text("{}", encoding="utf-8")
            (Path(folder) / "own.json").write_text("{}", encoding="utf-8")
            self.assertEqual(served_count([wing, Path(folder)]), 2)
            (wing / "own.json").write_text("{}", encoding="utf-8")
            self.assertEqual(served_count([wing, Path(folder)]), 2)
            self.assertEqual(served_count([Path(folder) / "missing"]), 0)


class UrlShapeWarningTests(unittest.TestCase):
    def test_bare_hostnames_and_schemes_flag_once_each(self):
        warns = url_shape_warnings(["host:8099/wing", "http://ok/x", "ftp://x/y",
                                    "https://ok/", ""])
        self.assertEqual(len(warns), 2)  # empty entries are dropped upstream
        self.assertTrue(any("host:8099/wing" in w for w in warns))
        self.assertTrue(any("ftp://x/y" in w for w in warns))


class RelayExplainsItselfTests(unittest.TestCase):
    def test_token_401_surfaces_the_relay_error_text(self):
        with TemporaryDirectory() as served:
            server, error = start_wing_relay(0, served, token="sekret")
            self.assertEqual(error, "")
            base = f"http://127.0.0.1:{server.server_address[1]}/wing"
            try:
                spread, name, failure = fetch_spread(f"{base}/mate.json", timeout=3)
                self.assertIn("needs ?token=", failure)
                self.assertTrue(failure.startswith("Could not fetch"))
                listing, reason, _ = _listing_entries(base + "/", timeout=3)
                self.assertIsNone(listing)
                self.assertIn("needs ?token=", reason)
            finally:
                server.shutdown()


class AgeWordsTests(unittest.TestCase):
    def test_fresh_to_ancient_ladder(self):
        now = 1_000_000.0
        self.assertEqual(_age_words(now - 30, now), "just now")
        self.assertEqual(_age_words(now - 1800, now), "under 1 h ago")
        self.assertEqual(_age_words(now - 7200, now), "2 h ago")
        self.assertEqual(_age_words(now - 200 * 3600, now), "8 d ago")
        self.assertEqual(_age_words(None, now), "unknown when")
        self.assertEqual(_age_words("junk", now), "unknown when")


class MixedFormatWingTests(unittest.TestCase):
    def setUp(self):
        RelayHandler.received = {}
        RelayHandler.served = {"/mate.json": LEDGER,
                               "/": {"ledgers": [{"name": "mate.json", "updated": time()}],
                                     "format": 999, "host_build": "7"}}
        self.server = HTTPServer(("127.0.0.1", 0), RelayHandler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_format_drift_warns_once_per_summary_not_silently(self):
        spreads, errors = fetch_spreads(f"{self.base}/", timeout=3)
        self.assertEqual([name for name, _ in spreads], ["mate.json"])
        self.assertTrue(any("speaks ledger format v999" in e for e in errors))
        # same-format relays (the real handler) stay warning-free
        spreads, errors = fetch_spreads(self.base + "/mate.json", timeout=3)
        self.assertEqual(errors, [])


class NotARelayTests(unittest.TestCase):
    def setUp(self):
        RelayHandler.received = {}
        RelayHandler.served = {"/": "Hello from some web server"}
        self.server = HTTPServer(("127.0.0.1", 0), RelayHandler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_html_answer_explains_this_is_not_a_relay(self):
        spreads, errors = fetch_spreads(f"{self.base}/", timeout=3)
        self.assertEqual(spreads, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("not with a relay listing", errors[0])
        self.assertIn("Hello from some web server", errors[0])


class RelayMetaNotARelayTests(unittest.TestCase):
    def test_meta_probe_shares_the_plain_language_diagnosis(self):
        RelayHandler.received = {}
        RelayHandler.served = {"/": "<html>a blog, not a relay</html>"}
        server = HTTPServer(("127.0.0.1", 0), RelayHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            build, fmt, err = relay_meta(f"http://127.0.0.1:{server.server_address[1]}/", timeout=3)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual((build, fmt), ("", ""))
        self.assertIn("not with a relay listing", err)
        self.assertIn("a blog, not a relay", err)
