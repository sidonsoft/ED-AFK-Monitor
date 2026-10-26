import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import urllib.error
import urllib.request

from dashboard import share_warning, start_dashboard

PROVIDERS = (lambda: {"kills": 3}, lambda: {"total": 1})


def get(port, path, headers=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status, response.read()


class DashboardTests(unittest.TestCase):
    def test_serves_page_live_and_mission_json(self):
        server, error = start_dashboard(0, *PROVIDERS)
        self.assertEqual(error, "")
        port = server.server_address[1]
        try:
            status, body = get(port, "/")
            self.assertEqual(status, 200)
            self.assertIn(b"ED AFK Monitor", body)
            self.assertEqual(json.loads(get(port, "/live.json")[1]), {"kills": 3})
            status, body = get(port, "/")
            self.assertEqual(status, 200)
            page = body.decode("utf-8")
            self.assertIn('id="wingpanel"', page)
            self.assertIn('id="sessionspanel"', page)
            self.assertIn("Mission spread", page)
            self.assertEqual(json.loads(get(port, "/missions.json")[1]), {"total": 1})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                get(port, "/nope")
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()

    def test_token_required_when_configured(self):
        server, error = start_dashboard(0, *PROVIDERS, token="s3cret")
        self.assertEqual(error, "")
        port = server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                get(port, "/live.json")
            self.assertEqual(caught.exception.code, 401)
            self.assertEqual(get(port, "/live.json?token=s3cret")[0], 200)
            self.assertEqual(get(port, "/live.json", {"X-Dashboard-Token": "s3cret"})[0], 200)
        finally:
            server.shutdown()
            server.server_close()

    def test_provider_failures_do_not_kill_the_server(self):
        def broken():
            raise RuntimeError("state gone")
        server, error = start_dashboard(0, broken, PROVIDERS[1])
        port = server.server_address[1]
        try:
            try:
                get(port, "/live.json")
                self.fail("broken provider should answer 500")
            except urllib.error.HTTPError as caught:
                self.assertEqual(caught.code, 500)
                self.assertIn("state gone", json.loads(caught.read())["error"])
            self.assertEqual(json.loads(get(port, "/missions.json")[1]), {"total": 1})
        finally:
            server.shutdown()
            server.server_close()

    # Windows (without SO_EXCLUSIVEADDRUSE, which broke binds on CI) happily
    # rebinds an in-use port, so conflict detection is POSIX-side behaviour.
    @unittest.skipIf(os.name == "nt", "Windows permits rebinding an in-use port")
    def test_port_conflict_reports_error_without_raising(self):
        first, _ = start_dashboard(0, *PROVIDERS)
        port = first.server_address[1]
        server, error = start_dashboard(port, *PROVIDERS)
        self.assertIsNone(server)
        self.assertIn("Could not start", error)
        first.shutdown()
        first.server_close()


class SessionPanelTests(unittest.TestCase):
    def test_sessions_endpoint_when_provider_given(self):
        rows = [{"started": 1761000000.0, "ended": 1761003600.0, "duration": 3600.0,
                 "kills": 12, "scans": 4, "bounties": 250000, "merits": 1,
                 "missions_done": 1, "missions_total": 3, "journal": "J", "gamemode": "Open"}]
        payload = {"sessions": rows, "totals": {"runs": 1, "duration": 3600.0, "kills": 12,
                                                "scans": 4, "bounties": 250000, "merits": 1,
                                                "kills_hour": 12.0, "bounties_hour": 250000}}
        server, error = start_dashboard(0, *PROVIDERS, sessions_provider=lambda: payload)
        self.assertEqual(error, "")
        port = server.server_address[1]
        try:
            self.assertEqual(json.loads(get(port, "/sessions.json")[1]), payload)
        finally:
            server.shutdown()
            server.server_close()

    def test_sessions_404_without_provider(self):
        server, error = start_dashboard(0, *PROVIDERS)
        self.assertEqual(error, "")
        port = server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                get(port, "/sessions.json")
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


class ShareWarningTests(unittest.TestCase):
    def test_loopback_or_token_quiet(self):
        for host in ("", "127.0.0.1", "localhost", "::1"):
            self.assertEqual(share_warning(host, ""), "")
        self.assertEqual(share_warning("0.0.0.0", "s3cret"), "")

    def test_open_lan_binding_warns(self):
        warning = share_warning("0.0.0.0", "")
        self.assertIn("token", warning)
        self.assertIn("0.0.0.0", warning)


class PageScriptTests(unittest.TestCase):
    """Keep the dashboard's inline JS syntactically valid on every OS."""

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_inline_script_parses(self):
        import re
        from dashboard import PAGE
        script = re.search(r"<script>(.*)</script>", PAGE, re.S)
        self.assertIsNotNone(script, "PAGE lost its <script> block")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script.group(1))
            path = handle.name
        try:
            result = subprocess.run(["node", "--check", path],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr[:500])
        finally:
            os.unlink(path)


class FreshHostAdvertiseTests(unittest.TestCase):
    """dashboard_missions shows the wing panel once a relay exists, even
    before anyone has missions - a new wing must be wireable."""

    def load(self, serve_label):
        import ast
        from missions import merge_spreads
        source = (Path(__file__).resolve().parents[1] / "afk_monitor.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        def find(node):
            for child in getattr(node, "body", []):
                if isinstance(child, ast.FunctionDef) and child.name == "dashboard_missions":
                    return child
                found = find(child)
                if found:
                    return found
        func = find(tree)
        lines = source.splitlines()
        import textwrap
        body = textwrap.dedent("\n".join(lines[func.lineno - 1:func.end_lineno]))
        namespace = {
            "mission_ledger": type("L", (), {"spread": lambda self: {"done": 0, "total": 0}})(),
            "conf_log_levels": {"SummaryMissions": 3},
            "wing_view": lambda: ([], []),
            "merge_spreads": merge_spreads,
            "wing_serve_label": serve_label,
            "wing_serve_dirs": [],
            "served_count": lambda dirs: 0,
        }
        exec(body, namespace)
        return namespace["dashboard_missions"]()

    def test_relay_alone_produces_a_wing_view(self):
        data = self.load("0.0.0.0:8099")
        self.assertEqual(data["wing_view"]["sources"], [])
        self.assertEqual(data["wing_view"]["serve"], "0.0.0.0:8099")
        self.assertEqual(data["wing_view"]["combined"]["total"], 0)

    def test_no_relay_no_missions_stays_quiet(self):
        self.assertNotIn("wing_view", self.load(""))
