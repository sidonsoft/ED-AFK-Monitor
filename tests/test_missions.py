"""Mission ledger, faction spread and persistence."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from missions import MissionLedger


def accepted(mission_id, target="Pirate Faction", kills=40, wing=True,
             faction="Issuing Faction", reward=1000000):
    return {
        "event": "MissionAccepted",
        "Name": "Mission_MassacreWing" if wing else "Mission_Massacre",
        "MissionID": mission_id,
        "Faction": faction,
        "TargetFaction": target,
        "KillCount": kills,
        "Wing": wing,
        "Reward": reward,
        "Expiry": "2026-10-21T12:00:00Z",
    }


class MissionLedgerTests(unittest.TestCase):
    def make(self, folder):
        return MissionLedger(Path(folder) / "missions.json", commander="Test", profile="Test")

    def test_accept_records_faction_kill_count_and_wing(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            mission = ledger.accept(accepted(1001))
            self.assertIsNotNone(mission)
            self.assertEqual(mission.target, "Pirate Faction")
            self.assertEqual(mission.kill_count, 40)
            self.assertTrue(mission.wing)
            self.assertEqual(mission.reward, 1000000)
            self.assertTrue(mission.known)
            self.assertEqual(ledger.count(), 1)

    def test_non_massacre_and_duplicate_accepts(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            self.assertIsNone(ledger.accept({"Name": "Mission_Courier", "MissionID": 5}))
            ledger.accept(accepted(1001, kills=40))
            ledger.kill("Pirate Faction")
            ledger.accept(accepted(1001, kills=50, wing=False))
            self.assertEqual(ledger.count(), 1)
            mission = ledger.missions[1001]
            self.assertEqual(mission.kill_count, 50)
            self.assertEqual(mission.kills, 1)
            self.assertFalse(mission.wing)

    def test_kills_credit_every_mission_for_the_target_faction(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.accept(accepted(1, target="Pirates A", kills=3))
            ledger.accept(accepted(2, target="Pirates A", kills=5))
            ledger.accept(accepted(3, target="Pirates B", kills=2))
            for _ in range(4):
                ledger.kill("pirates a")
            self.assertEqual(ledger.missions[1].kills, 3)
            self.assertEqual(ledger.missions[2].kills, 4)
            self.assertEqual(ledger.missions[3].kills, 0)
            ledger.kill("Pirates B")
            self.assertEqual(ledger.missions[3].kills, 1)

    def test_spread_uses_maximum_requirement_per_faction(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.accept(accepted(1, target="Pirates A", kills=40))
            ledger.accept(accepted(2, target="Pirates A", kills=30))
            ledger.accept(accepted(3, target="Pirates B", kills=20, wing=False))
            for _ in range(35):
                ledger.kill("Pirates A")
            spread = ledger.spread()
            rows = {row["faction"]: row for row in spread["rows"]}
            self.assertEqual(rows["Pirates A"]["missions"], 2)
            self.assertEqual(rows["Pirates A"]["wing"], 2)
            self.assertEqual(rows["Pirates A"]["need"], 40)
            self.assertEqual(rows["Pirates A"]["done"], 35)
            self.assertEqual(rows["Pirates A"]["left"], 5)
            self.assertEqual(rows["Pirates B"]["left"], 20)
            self.assertEqual(spread["left"], 25)
            self.assertEqual(spread["wing"], 2)
            self.assertEqual(spread["total"], 3)
            self.assertEqual(spread["sum_kills"], 90)

    def test_redirect_marks_done_and_clamps_progress(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.accept(accepted(1, kills=40))
            ledger.kill("Pirate Faction")
            mission = ledger.redirected(1)
            self.assertTrue(mission.done)
            self.assertEqual(mission.kills, 40)
            self.assertEqual(ledger.progress(), (1, 1))
            self.assertEqual(ledger.spread()["left"], 0)
            ledger.kill("Pirate Faction")
            self.assertEqual(mission.kills, 40)

    def test_reconcile_keeps_details_adds_unknown_and_prunes_closed(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.accept(accepted(1, target="Pirates A", kills=40))
            ledger.accept(accepted(2, target="Pirates B", kills=20))
            removed = ledger.reconcile([
                {"MissionID": 1, "Name": "Mission_Massacre", "Expires": 3600},
                {"MissionID": 9, "Name": "Mission_MassacreWing", "Expires": 7200},
                {"MissionID": 12, "Name": "Mission_Courier", "Expires": 7200},
            ])
            self.assertEqual([mission.mission_id for mission in removed], [2])
            self.assertTrue(ledger.missions[1].known)
            self.assertEqual(ledger.missions[1].target, "Pirates A")
            self.assertFalse(ledger.missions[9].known)
            self.assertEqual(ledger.count(), 2)
            ledger.kill("Pirates B")
            self.assertEqual(ledger.missions[9].kills, 0)

    def test_close_removes_mission(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.accept(accepted(1))
            mission = ledger.close(1)
            self.assertEqual(mission.mission_id, 1)
            self.assertEqual(ledger.count(), 0)
            self.assertIsNone(ledger.close(1))

    def test_preload_counts_kills_only_for_new_missions(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.accept(accepted(1, target="Pirates A", kills=40))
            ledger.kill("Pirates A")
            ledger.save()
            ledger.preloading = True
            ledger.kill("Pirates A")
            self.assertEqual(ledger.missions[1].kills, 1)
            ledger.accept(accepted(2, target="Pirates B", kills=10))
            ledger.kill("Pirates B")
            self.assertEqual(ledger.missions[2].kills, 1)
            ledger.end_preload()
            ledger.kill("Pirates A")
            self.assertEqual(ledger.missions[1].kills, 2)

    def test_ledger_persists_details_and_progress(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "missions.json"
            ledger = MissionLedger(path, commander="Test", profile="Test")
            ledger.accept(accepted(1, target="Pirates A", kills=40))
            ledger.kill("Pirates A")
            ledger.save()
            restored = MissionLedger(path)
            self.assertEqual(restored.commander, "Test")
            self.assertEqual(restored.profile, "Test")
            mission = restored.missions[1]
            self.assertEqual(mission.target, "Pirates A")
            self.assertEqual(mission.kills, 1)
            self.assertEqual(mission.kill_count, 40)

    def test_corrupt_ledger_is_ignored(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "missions.json"
            path.write_text("not json", encoding="utf-8")
            ledger = MissionLedger(path)
            self.assertEqual(ledger.count(), 0)
            self.assertTrue(ledger.warnings)

    def test_report_contains_spread_and_mission_list(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.accept(accepted(1, target="Pirates A", kills=40))
            ledger.kill("Pirates A")
            text = ledger.report_text(include_missions=True, title=True)
            self.assertIn("Mission spread", text)
            self.assertIn("Pirates A", text)
            self.assertIn("Unique kills left", text)
            self.assertIn("1/40", text)
            discord = ledger.report_discord()
            self.assertIn("**Mission spread**", discord)
            self.assertIn("Pirates A", discord)


if __name__ == "__main__":
    unittest.main()


class MissionAttributionTests(unittest.TestCase):
    def test_kill_matches_raw_or_localised_faction_once(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            ledger.accept(accepted(1, target="Lords of Ngaliba", kills=5))
            ledger.kill("Señor Ngaliba", "Lords of Ngaliba")
            self.assertEqual(ledger.missions[1].kills, 1)
            ledger.kill("Lords of Ngaliba", "Lords of Ngaliba")
            self.assertEqual(ledger.missions[1].kills, 2)
