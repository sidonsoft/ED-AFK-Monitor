"""Storage-level checks for the durable notification outbox."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from time import time

from outbox import (Outbox, profile_directory_name, summarize_pending,
                           view_outbox)


class OutboxTests(unittest.TestCase):
    def test_added_entries_are_written_completed_and_loaded_in_order(self):
        with TemporaryDirectory() as folder:
            outbox = Outbox(Path(folder), ["telegram"])
            outbox.add("telegram", "one", "first message")
            outbox.add("telegram", "two", "second message")
            outbox.complete("telegram", "one")

            pending, warnings = Outbox(Path(folder), ["telegram"]).load()
            self.assertEqual(warnings, [])
            self.assertEqual([entry["text"] for entry in pending["telegram"]], ["second message"])
            data = json.loads((Path(folder) / "telegram.json").read_text(encoding="utf-8"))
            self.assertEqual([entry["id"] for entry in data["entries"]], ["two"])

    def test_only_configured_channels_are_stored(self):
        with TemporaryDirectory() as folder:
            outbox = Outbox(Path(folder), ["discord"])
            with self.assertRaises(KeyError):
                outbox.add("telegram", "one", "not saved")
            self.assertFalse((Path(folder) / "telegram.json").exists())

    def test_expired_malformed_and_duplicate_entries_are_skipped(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "telegram.json"
            now = time()
            path.write_text(json.dumps({"version": 1, "entries": [
                {"id": "old", "text": "too old", "created": now - 7 * 24 * 3600},
                {"id": "bad", "text": "", "created": now},
                {"id": "ok", "text": "keep", "created": now},
                {"id": "ok", "text": "duplicate", "created": now},
                "not-an-entry",
            ]}), encoding="utf-8")

            pending, warnings = Outbox(Path(folder), ["telegram"]).load()
            self.assertEqual([entry["text"] for entry in pending["telegram"]], ["keep"])
            self.assertTrue(any("expired" in warning for warning in warnings))
            self.assertTrue(any("unreadable" in warning for warning in warnings))
            self.assertTrue(any("duplicate" in warning for warning in warnings))

    def test_unreadable_file_is_ignored_without_raising(self):
        with TemporaryDirectory() as folder:
            (Path(folder) / "telegram.json").write_text("not json", encoding="utf-8")
            pending, warnings = Outbox(Path(folder), ["telegram"]).load()
            self.assertEqual(pending["telegram"], [])
            self.assertTrue(warnings)

    def test_entry_limit_keeps_the_newest_messages(self):
        with TemporaryDirectory() as folder:
            outbox = Outbox(Path(folder), ["telegram"], max_entries=2)
            for index in range(4):
                outbox.add("telegram", f"id{index}", f"message {index}")
            pending, _ = outbox.load()
            self.assertEqual([entry["text"] for entry in pending["telegram"]],
                             ["message 2", "message 3"])

    def test_profile_directory_names_are_safe_and_distinct(self):
        self.assertEqual(profile_directory_name(None), "default")
        self.assertEqual(profile_directory_name("Jameson"), "Jameson")
        self.assertNotEqual(profile_directory_name("Cmdr One"), profile_directory_name("Cmdr/One"))
        self.assertNotIn("/", profile_directory_name("Cmdr/One"))


if __name__ == "__main__":
    unittest.main()


class SummarizePendingTests(unittest.TestCase):
    def test_peek_reports_counts_ages_and_next_text(self):
        with TemporaryDirectory() as folder:
            store = Outbox(Path(folder), ["telegram", "discord"], max_age=3600)
            store.load(now=1000)
            store.add("telegram", "m1", "Session summary line\nsecond line", created=800)
            store.add("telegram", "m2", "Later message", created=900)
            store.add("discord", "d1", "Webhook retry", created=400)
            lines, warnings = summarize_pending(Path(folder), ["telegram", "discord"],
                                                max_age=3600, now=1500)
        joined = "\n".join(lines)
        self.assertIn("telegram: 2 waiting, oldest 11 min ago", joined)
        self.assertIn("next up: Session summary line", joined)
        self.assertIn("discord: 1 waiting, oldest 18 min ago", joined)
        self.assertEqual(warnings, [])


class ClearTests(unittest.TestCase):
    def test_clear_drops_and_reports_but_keeps_the_channel_usable(self):
        with TemporaryDirectory() as folder:
            store = Outbox(Path(folder), ["telegram"], max_age=3600)
            store.load(now=1000)
            store.add("telegram", "m1", "gone soon", created=999)
            self.assertEqual(store.clear("telegram"), 1)
            again = Outbox(Path(folder), ["telegram"], max_age=3600)
            pending, warnings = again.load(now=1000)
            self.assertEqual(pending["telegram"], [])
            self.assertEqual(warnings, [])
            self.assertEqual(store.clear("telegram"), 0)  # idempotent


class ViewOutboxTests(unittest.TestCase):
    def test_peek_then_clear_in_one_call_returns_both_views(self):
        with TemporaryDirectory() as folder:
            store = Outbox(Path(folder), ["telegram"], max_age=3600)
            store.load(now=1000)
            store.add("telegram", "m1", "Queue me", created=950)
            lines, warnings, cleared = view_outbox(Path(folder), ["telegram"],
                                                   max_age=3600, now=1500)
            self.assertIsNone(cleared)  # peeking must not mutate
            self.assertIn("telegram: 1 waiting", "\n".join(lines))
            second = Outbox(Path(folder), ["telegram"], max_age=3600)
            self.assertEqual(len(second.load(now=1500)[0]["telegram"]), 1)
            lines, warnings, cleared = view_outbox(Path(folder), ["telegram"],
                                                   clear=True, max_age=3600, now=1500)
            self.assertEqual(cleared, 1)
            third = Outbox(Path(folder), ["telegram"], max_age=3600)
            self.assertEqual(third.load(now=1500)[0]["telegram"], [])
