"""Session history store: recording, pruning, totals and report output."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from session_store import SessionStore


def make(folder):
    return SessionStore(Path(folder) / "session_history" / "default.db")


class SessionStoreTests(unittest.TestCase):
    def test_record_and_sessions_round_trip_newest_first(self):
        with TemporaryDirectory() as folder:
            store = make(folder)
            now = time.time()
            store.record(now - 7200, now - 3600, {"kills": 30, "bounties": 500000},
                         {"done": 1, "total": 3}, journal="Journal.log", gamemode="Open")
            store.record(now - 3600, now, {"kills": 60, "bounties": 900000, "scans": 4},
                         {"done": 0, "total": 0})
            sessions = store.sessions()
            store.close()
            self.assertEqual(len(sessions), 2)
            self.assertGreaterEqual(sessions[0]["started"], sessions[1]["started"])
            self.assertEqual(sessions[0]["kills"], 60)
            self.assertEqual(sessions[0]["scans"], 4)
            self.assertEqual(sessions[1]["missions_total"], 3)
            self.assertEqual(sessions[1]["gamemode"], "Open")

    def test_totals_aggregate_rates_from_total_duration(self):
        with TemporaryDirectory() as folder:
            store = make(folder)
            now = time.time()
            store.record(now - 7200, now - 3600, {"kills": 60, "bounties": 7_200_000})
            store.record(now - 3600, now, {"kills": 60, "bounties": 7_200_000})
            totals = store.totals()
            store.close()
            self.assertEqual(totals["runs"], 2)
            self.assertEqual(totals["kills"], 120)
            self.assertAlmostEqual(totals["kills_hour"], 60, places=0)
            self.assertAlmostEqual(totals["bounties_hour"], 7_200_000, delta=1000)

    def test_prune_removes_only_old_runs(self):
        with TemporaryDirectory() as folder:
            store = make(folder)
            now = time.time()
            store.record(now - 100 * 86400, now - 100 * 86400 + 600, {"kills": 1})
            store.record(now - 86400, now - 86400 + 600, {"kills": 2})
            self.assertEqual(store.prune(90), 1)
            sessions = store.sessions()
            store.close()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["kills"], 2)

    def test_reopening_existing_database_keeps_rows(self):
        with TemporaryDirectory() as folder:
            first = make(folder)
            now = time.time()
            first.record(now - 600, now, {"kills": 5})
            first.close()
            second = make(folder)
            self.assertEqual(len(second.sessions()), 1)
            second.close()

    def test_report_text_with_and_without_rows(self):
        with TemporaryDirectory() as folder:
            store = make(folder)
            self.assertIn("No saved sessions", store.report_text())
            now = time.time()
            store.record(now - 3600, now, {"kills": 42, "bounties": 1000000},
                         {"done": 2, "total": 5})
            report = store.report_text(title="Test profile")
            store.close()
            self.assertIn("Test profile", report)
            self.assertIn("42", report)
            self.assertIn("2/5", report)
            self.assertIn("TOTAL", report)

    def test_broken_store_records_nothing_without_raising(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "blocked"
            path.write_text("not a database", encoding="utf-8")
            store = SessionStore(path / "sessions.db")
            self.assertFalse(store.record(1, 2, {"kills": 1}))
            self.assertEqual(store.sessions(), [])
            self.assertIsNotNone(store.error)
            store.close()
