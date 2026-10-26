import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from session_card import render_card
from session_store import SessionStore


class SessionCardTests(unittest.TestCase):
    def make_store(self, folder):
        store = SessionStore(Path(folder) / "session_history" / "profile.db")
        store.record(1_761_000_000, 1_761_014_400, {"kills": 120, "bounties": 480000, "merits": 132})
        store.record(1_761_100_000, 1_761_103_600, {"kills": 40, "bounties": 90000})
        return store

    def test_empty_store_produces_no_card(self):
        with TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / "s.db")
            error = render_card(store, Path(folder) / "card.png")
            self.assertIn("No saved sessions", error)
            store.close()

    def test_card_renders_png_file(self):
        with TemporaryDirectory() as folder:
            store = self.make_store(folder)
            out = Path(folder) / "out" / "card.png"
            error = render_card(store, out)
            if error == "PNG cards need 'pip install pillow'":
                self.skipTest("Pillow not installed")
            self.assertEqual(error, "")
            self.assertTrue(out.is_file())
            self.assertEqual(out.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            store.close()
