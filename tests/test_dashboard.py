import json
import os
import unittest
import urllib.error
import urllib.request

from dashboard import start_dashboard

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
            except (urllib.error.HTTPError, ConnectionResetError, urllib.error.URLError):
                pass  # handler exception closes the socket; acceptable for a local panel
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
        server, error = start_dashboard(0, *PROVIDERS, sessions_provider=lambda: rows)
        self.assertEqual(error, "")
        port = server.server_address[1]
        try:
            self.assertEqual(json.loads(get(port, "/sessions.json")[1]), rows)
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
