import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ship_pools import load_ship_pools

EASY = ["viper"]
HARD = ["anaconda"]


class ShipPoolTests(unittest.TestCase):
    def test_missing_file_uses_defaults_without_warning(self):
        with TemporaryDirectory() as directory:
            easy, hard, warnings = load_ship_pools(Path(directory) / "ships.json", EASY, HARD)
            self.assertEqual((easy, hard), (EASY, HARD))
            self.assertEqual(warnings, [])

    def test_valid_file_replaces_and_normalises_pools(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ships.json"
            path.write_text(json.dumps({"easy": [" Adder ", "KRAIT_LIGHT"], "hard": ["Vulture"]}),
                            encoding="utf-8")
            easy, hard, warnings = load_ship_pools(path, EASY, HARD)
            self.assertEqual(easy, ["adder", "krait_light"])
            self.assertEqual(hard, ["vulture"])
            self.assertEqual(warnings, [])

    def test_invalid_pool_falls_back_per_pool_with_warning(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ships.json"
            path.write_text(json.dumps({"easy": "not-a-list", "hard": ["Vulture"]}), encoding="utf-8")
            easy, hard, warnings = load_ship_pools(path, EASY, HARD)
            self.assertEqual(easy, EASY)
            self.assertEqual(hard, ["vulture"])
            self.assertEqual(len(warnings), 1)

    def test_malformed_file_falls_back_with_warning(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ships.json"
            path.write_text("{ not json", encoding="utf-8")
            easy, hard, warnings = load_ship_pools(path, EASY, HARD)
            self.assertEqual((easy, hard), (EASY, HARD))
            self.assertTrue(warnings)

    def test_ships_file_shipped_with_repo_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "ships.json"
        easy, hard, warnings = load_ship_pools(path, [], [])
        self.assertEqual(warnings, [])
        self.assertTrue(easy and hard)
        self.assertTrue(all(item == item.lower() and " " not in item for item in easy + hard))
        self.assertFalse(set(easy) & set(hard))
