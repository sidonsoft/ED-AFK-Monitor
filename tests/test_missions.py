"""Mission ledger, faction spread and persistence."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from missions import MissionLedger, merge_spreads, wing_spreads, parse_mission_list


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

    def test_kills_credit_one_mission_per_issuer_for_the_target_faction(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.accept(accepted(1, target="Pirates A", kills=3))
            ledger.accept(accepted(2, target="Pirates A", kills=5))
            ledger.accept(accepted(3, target="Pirates B", kills=2))
            for _ in range(4):
                ledger.kill("pirates a")
            self.assertEqual(ledger.missions[1].kills, 3)
            self.assertEqual(ledger.missions[2].kills, 1)
            self.assertEqual(ledger.missions[3].kills, 0)
            ledger.kill("Pirates B")
            self.assertEqual(ledger.missions[3].kills, 1)

    def test_spread_uses_maximum_requirement_per_faction(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.accept(accepted(1, target="Pirates A", kills=40))
            ledger.accept(accepted(2, target="Pirates A", kills=30, faction="Other issuer"))
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
    def test_exact_mission_id_credits_that_mission_directly(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            ledger.accept(accepted(1, target="Pirate Faction", kills=5))
            ledger.accept(accepted(2, target="Pirate Faction", kills=5))
            credited = ledger.kill("", None, mission_id=2)
            self.assertEqual([m.mission_id for m in credited], [2])
            self.assertEqual(ledger.missions[1].kills, 0)
            self.assertEqual(ledger.missions[2].kills, 1)

    def test_unknown_mission_id_falls_back_to_faction_credit(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            ledger.accept(accepted(1, target="Pirate Faction", kills=5))
            credited = ledger.kill("Pirate Faction", None, mission_id=999)
            self.assertEqual([m.mission_id for m in credited], [1])

    def test_kill_gated_when_mission_destination_system_known_elsewhere(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            event = accepted(1, target="Pirate Faction", kills=5)
            event["DestinationSystem"] = "LTT 9750"
            ledger.accept(event)
            ledger.observe_location({"event": "FSDJump", "StarSystem": "Shinrarta Dezhra"})
            self.assertEqual(ledger.kill("Pirate Faction"), [])
            ledger.observe_location({"event": "FSDJump", "StarSystem": "LTT 9750"})
            self.assertEqual(len(ledger.kill("Pirate Faction")), 1)

    def test_kill_matches_raw_or_localised_faction_once(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            ledger.accept(accepted(1, target="Lords of Ngaliba", kills=5))
            ledger.kill("Señor Ngaliba", "Lords of Ngaliba")
            self.assertEqual(ledger.missions[1].kills, 1)
            ledger.kill("Lords of Ngaliba", "Lords of Ngaliba")
            self.assertEqual(ledger.missions[1].kills, 2)


class MissionListImportTests(unittest.TestCase):
    def make(self, folder):
        return MissionLedger(Path(folder) / "missions.json", commander="Test", profile="Test")

    def test_parse_positional_and_header_lists(self):
        rows, errors = parse_mission_list("""
            # target faction, kills, wing
            Lords of Ngaliba, 40, wing
            Ngaliba Blue Boys, 30
            1001, Crimson State Ltd, 20, solo
        """)
        self.assertEqual(errors, [])
        self.assertEqual(
            [(row["target"], row["kills"], row["wing"], row["id"]) for row in rows],
            [("Lords of Ngaliba", 40, True, None),
             ("Ngaliba Blue Boys", 30, False, None),
             ("Crimson State Ltd", 20, False, 1001)])

        rows, errors = parse_mission_list("""
            Target Faction,Kills Required,Wing,Mission ID
            "Lords of Ngaliba",40,yes,2001
            Ngaliba Blue Boys,30,no,
        """)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["id"], 2001)
        self.assertEqual(rows[0]["target"], "Lords of Ngaliba")
        self.assertTrue(rows[0]["wing"])
        self.assertEqual(rows[1]["target"], "Ngaliba Blue Boys")
        self.assertFalse(rows[1]["wing"])

    def test_import_replaces_manual_missions_and_keeps_progress(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.import_text("Lords of Ngaliba, 40, wing\nNgaliba Blue Boys, 30\n")
            self.assertEqual(sum(1 for mission in ledger.missions.values() if mission.manual), 2)
            for _ in range(3):
                ledger.kill("Lords of Ngaliba")
            ledger.import_text("Lords of Ngaliba, 40, wing\nNgaliba Blue Boys, 30\n")
            manual = [mission for mission in ledger.missions.values() if mission.manual]
            self.assertEqual(ledger.count(), 2)
            lord = next(mission for mission in manual if mission.target == "Lords of Ngaliba")
            self.assertEqual(lord.kills, 3)

    def test_import_with_id_updates_known_mission_without_resetting_progress(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.accept(accepted(1001, target="Pirates", kills=40))
            ledger.kill("Pirates")
            ledger.import_text("1001, Pirates, 50, solo\n")
            mission = ledger.missions[1001]
            self.assertFalse(mission.manual)
            self.assertEqual(mission.kill_count, 50)
            self.assertEqual(mission.kills, 1)

    def test_import_errors_leave_the_ledger_unchanged(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.import_text("Lords of Ngaliba, 40\n")
            before = ledger.count()
            added, replaced, errors = ledger.import_text("Lords of Ngaliba, not-a-number\n")
            self.assertEqual(added, 0)
            self.assertTrue(errors)
            self.assertEqual(ledger.count(), before)

    def test_manual_mission_completes_when_requirement_is_reached(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.import_text("Lords of Ngaliba, 2\n")
            for _ in range(2):
                ledger.kill("Lords of Ngaliba")
            mission = next(iter(ledger.missions.values()))
            self.assertTrue(mission.done)
            self.assertEqual(ledger.progress(), (1, 1))
            self.assertEqual(ledger.spread()["left"], 0)

    def test_reconcile_keeps_imported_missions(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.import_text("Lords of Ngaliba, 40\n")
            ledger.accept(accepted(1001, target="Pirates", kills=10))
            ledger.reconcile([{"MissionID": 2002, "Name": "Mission_Massacre", "Expires": 100}])
            self.assertEqual(sum(1 for mission in ledger.missions.values() if mission.manual), 1)
            self.assertNotIn(1001, ledger.missions)
            self.assertIn(2002, ledger.missions)

    def test_export_contains_spread_and_missions(self):
        with TemporaryDirectory() as folder:
            ledger = self.make(folder)
            ledger.import_text("3001, Lords of Ngaliba, 40, wing\n")
            data = ledger.export()
            self.assertEqual(data["commander"], "Test")
            self.assertEqual(data["missions"][0]["id"], 3001)
            self.assertEqual(data["spread"]["total"], 1)

    def test_backfill_fills_details_and_credits_replayed_kills(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(
                Path(folder) / "missions.json",
                details_lookup=lambda ids: {1001: accepted(1001, target="Pirates", kills=40)})
            ledger.preloading = True
            ledger.reconcile([{"MissionID": 1001, "Name": "Mission_Massacre", "Expires": 3600}])
            self.assertEqual(ledger.backfilled, 1)
            mission = ledger.missions[1001]
            self.assertTrue(mission.known)
            self.assertEqual(mission.target, "Pirates")
            ledger.kill("Pirates")
            self.assertEqual(mission.kills, 1)


class LiveMissionReportTests(unittest.TestCase):
    def test_live_report_is_compact_and_truncated(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            for index in range(12):
                ledger.accept(accepted(index + 1, target=f"Pirates {index + 1:02d}", kills=10))
            ledger.kill("Pirates 01")
            text = ledger.report_live(max_factions=12)
            self.assertIn("**Mission spread** 0/12 complete", text)
            self.assertIn("Pirates 01", text)
            self.assertIn("1/10", text)
            truncated = ledger.report_live(max_factions=10)
            self.assertIn("and 2 more faction(s)", truncated)
            self.assertNotIn("Pirates 01", truncated)

    def test_live_report_is_empty_without_missions(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            self.assertEqual(ledger.report_live(), "")


class IssuerStackTests(unittest.TestCase):
    def test_parallel_issuers_and_serial_missions(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            ledger.accept(accepted(1, kills=3, faction="A"))
            ledger.accept(accepted(2, kills=2, faction="a"))
            ledger.accept(accepted(3, kills=4, faction="B"))
            for _ in range(4):
                ledger.kill("Pirate Faction")
            self.assertEqual([ledger.missions[i].kills for i in (1, 2, 3)], [3, 1, 4])
            data = ledger.spread()
            self.assertEqual(data['need'], 5)
            self.assertEqual(data['left'], 1)
            self.assertEqual(data['next_completion'], 1)
            self.assertEqual([r['difference'] for r in data['stacks']], [0, 1])
            self.assertEqual(data['shareable_reward'], 3000000)
            self.assertEqual(data['turn_in'], 0)
            ledger.redirected(1)
            self.assertEqual(ledger.spread()['shareable_turn_in'], 1000000)
            self.assertIn('Issuer stacks', ledger.report_live())
            self.assertIn('Diff 1', ledger.report_text())

    def test_next_completion_respects_acceptance_queue_and_timestamps(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            newer = accepted(1, kills=1)
            newer['timestamp'] = '2026-10-26T10:00:00Z'
            older = accepted(2, kills=10)
            older['timestamp'] = '2026-10-26T09:00:00Z'
            ledger.accept(newer)
            ledger.accept(older)
            self.assertEqual(ledger.spread()['next_completion'], 10)
            ledger.kill('Pirate Faction')
            self.assertEqual(ledger.missions[2].kills, 1)
            self.assertEqual(ledger.missions[1].kills, 0)

    def test_origin_and_destination_persist_and_travel_clears_station(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'missions.json'
            ledger = MissionLedger(path)
            ledger.observe_location({'event': 'Docked', 'StarSystem': 'Puneith', 'StationName': 'Wheelock Port'})
            event = accepted(1)
            event['DestinationSystem'] = 'Anana'
            ledger.accept(event)
            restored = MissionLedger(path)
            mission = restored.missions[1]
            self.assertEqual((mission.origin_system, mission.origin_station, mission.target_system),
                             ('Puneith', 'Wheelock Port', 'Anana'))
            self.assertIn('Wheelock Port', restored.report_text(include_missions=True))
            ledger.observe_location({'event': 'FSDJump', 'StarSystem': 'Anana'})
            ledger.accept(accepted(2))
            self.assertEqual(ledger.missions[2].origin_station, '')

    def test_different_progress_does_not_hide_later_mission_remaining(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / 'missions.json')
            ledger.accept(accepted(1, kills=40, faction='A'))
            ledger.accept(accepted(2, kills=30, faction='B'))
            ledger.missions[1].kills = 39
            self.assertEqual(ledger.spread()['left'], 30)

    def test_live_report_fits_notification_budget(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / 'missions.json')
            for i in range(20):
                ledger.accept(accepted(i + 1, target='Anana Brotherhood', faction=f'Long issuing faction name number {i}' + ' x' * 70))
            self.assertLessEqual(len(ledger.report_live()), 1400)
            self.assertIn('More details', ledger.report_live())


class MissionExpiryTests(unittest.TestCase):
    NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)

    def accept(self, ledger, mission_id, expiry):
        return ledger.accept({"event": "MissionAccepted", "Name": "Mission_Massacre",
                              "MissionID": mission_id, "Faction": "Issuer",
                              "TargetFaction": "Pirate Faction", "KillCount": 10,
                              "Expiry": expiry})

    def test_expiring_returns_window_matches_soonest_first(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            self.accept(ledger, 1, (self.NOW + timedelta(minutes=5)).isoformat())
            self.accept(ledger, 2, (self.NOW + timedelta(minutes=1)).isoformat())
            self.accept(ledger, 3, (self.NOW + timedelta(minutes=120)).isoformat())
            found = ledger.expiring(30, now=self.NOW)
            self.assertEqual([mission.mission_id for mission, _ in found], [2, 1])
            self.assertTrue(all(minutes <= 30 for _, minutes in found))

    def test_past_expiry_included_with_negative_minutes(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            self.accept(ledger, 1, (self.NOW - timedelta(minutes=2)).isoformat())
            found = ledger.expiring(15, now=self.NOW)
            self.assertEqual([(m.mission_id, minutes) for m, minutes in found], [(1, -2)])

    def test_done_missing_or_invalid_expiry_are_skipped(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            done = self.accept(ledger, 1, (self.NOW + timedelta(minutes=5)).isoformat())
            done.done = True
            self.accept(ledger, 2, "")
            self.accept(ledger, 3, "not-a-date")
            self.assertEqual(ledger.expiring(60, now=self.NOW), [])

    def test_naive_expiry_treated_as_utc(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            self.accept(ledger, 1, (self.NOW + timedelta(minutes=10)).replace(tzinfo=None).isoformat())
            found = ledger.expiring(30, now=self.NOW)
            self.assertEqual([mission.mission_id for mission, _ in found], [1])


class MissionProjectionTests(unittest.TestCase):
    def ledger(self, folder):
        ledger = MissionLedger(Path(folder) / "missions.json")
        ledger.accept({"event": "MissionAccepted", "Name": "Mission_Massacre", "MissionID": 1,
                       "Faction": "Issuer", "TargetFaction": "Pirates", "KillCount": 50,
                       "Reward": 1_000_000})
        ledger.accept({"event": "MissionAccepted", "Name": "Mission_Massacre", "MissionID": 2,
                       "Faction": "Issuer", "TargetFaction": "Pirates", "KillCount": 50,
                       "Reward": 1_000_000})
        return ledger

    def test_projection_scales_average_reward_by_live_kill_rate(self):
        with TemporaryDirectory() as folder:
            ledger = self.ledger(folder)
            proj = ledger.projections(100)
            self.assertEqual(proj["cr_per_hour"], 2_000_000)  # 2cr avg per unique kill
            self.assertEqual(proj["finish_secs"], 100 * 36)    # 100 unique kills at 100/h

    def test_projection_empty_without_rate_or_missions(self):
        with TemporaryDirectory() as folder:
            ledger = self.ledger(folder)
            self.assertEqual(ledger.projections(0), {})
            self.assertEqual(ledger.projections(None), {})
            empty = MissionLedger(Path(folder) / "other.json")
            self.assertEqual(empty.projections(100), {})

    def test_finished_stack_projection_has_no_time(self):
        with TemporaryDirectory() as folder:
            ledger = self.ledger(folder)
            for mission in ledger.missions.values():
                mission.done = True
                mission.kills = mission.kill_count
            proj = ledger.projections(100)
            self.assertEqual(proj["finish_secs"], 0)
            self.assertEqual(proj["turn_in"], 2_000_000)


class MissionEmbedTests(unittest.TestCase):
    def ledger(self, folder):
        ledger = MissionLedger(Path(folder) / "missions.json")
        for mission_id, faction in ((1, "Pirates A"), (2, "Pirates B")):
            ledger.accept({"event": "MissionAccepted", "Name": "Mission_Massacre",
                           "MissionID": mission_id, "Faction": "Issuer",
                           "TargetFaction": faction, "KillCount": 10, "Reward": 500000})
        return ledger

    def test_embed_structure_with_projection_field(self):
        with TemporaryDirectory() as folder:
            embed = self.ledger(folder).report_embed(
                "Projection at current rate: 1,000 cr/h",
                {"done": 1, "total": 3, "wing": 1, "left": 7, "reward": 0, "shareable_reward": 0,
                 "turn_in": 0, "shareable_turn_in": 0, "unknown": 0, "ledgers": 2, "rows": []}, 1)
            self.assertIn("Mission spread", embed["title"])
            self.assertIn("Ready to turn in", embed["description"])
            names = [field["name"] for field in embed["fields"]]
            self.assertIn("Pirates A", names)
            self.assertIn("Wing view", names)
            self.assertIn("Projection", names)
            wing_field = next(f for f in embed["fields"] if f["name"] == "Wing view")
            self.assertIn("local + 1 shared ledger(s)", wing_field["value"])
            self.assertIn("7 kills left across the wing", wing_field["value"])
            self.assertIn("footer", embed)
            self.assertLessEqual(len(embed["fields"]), 25)

    def test_embed_none_without_missions(self):
        with TemporaryDirectory() as folder:
            self.assertIsNone(MissionLedger(Path(folder) / "m.json").report_embed())

    def test_many_rows_are_capped_with_more_note(self):
        with TemporaryDirectory() as folder:
            ledger = MissionLedger(Path(folder) / "missions.json")
            for mission_id in range(1, 19):
                ledger.accept({"event": "MissionAccepted", "Name": "Mission_Massacre",
                               "MissionID": mission_id, "Faction": f"Issuer {mission_id}",
                               "TargetFaction": f"Faction {mission_id}", "KillCount": 10})
            embed = ledger.report_embed()
            names = [field["name"] for field in embed["fields"]]
            self.assertEqual(len(embed["fields"]), 16)
            self.assertIn("…and more", names)


class WingMergeTests(unittest.TestCase):
    def test_merge_spreads_combines_rows_and_totals(self):
        first = {"done": 1, "total": 2, "wing": 1, "left": 3, "reward": 2000,
                 "shareable_reward": 1000, "turn_in": 500, "shareable_turn_in": 0,
                 "unknown": 0,
                 "rows": [{"faction": "Pirates", "missions": 2, "done": 4, "need": 7, "left": 3}]}
        second = {"done": 0, "total": 1, "wing": 0, "left": 2, "reward": 900,
                  "shareable_reward": 0, "turn_in": 0, "shareable_turn_in": 0,
                  "unknown": 1,
                  "rows": [{"faction": "Pirates", "missions": 1, "done": 0, "need": 2, "left": 2},
                           {"faction": "SLF", "missions": 1, "done": 0, "need": 5, "left": 5}]}
        merged = merge_spreads([first, second])
        self.assertEqual((merged["ledgers"], merged["done"], merged["total"],
                          merged["left"], merged["unknown"]), (2, 1, 3, 5, 1))
        rows = {row["faction"]: row for row in merged["rows"]}
        self.assertEqual(rows["Pirates"], {"faction": "Pirates", "missions": 3,
                                           "done": 4, "need": 9, "left": 5})
        self.assertEqual(merged["rows"][0]["faction"], "Pirates")

    def test_merge_spreads_tolerates_nothing(self):
        merged = merge_spreads([{}, None])
        self.assertEqual(merged["ledgers"], 1)
        self.assertEqual(merged["left"], 0)

    def test_wing_spreads_reads_saved_ledger_files_and_skips_broken(self):
        with TemporaryDirectory() as folder:
            wing = Path(folder) / "wing"
            wing.mkdir()
            ledger = MissionLedger(wing / "mate.json", commander="Mate")
            ledger.accept(accepted(7, target="Pirate Faction", kills=3))
            ledger.save()
            (wing / "broken.json").write_text("{not json", encoding="utf-8")
            entries = wing_spreads(wing)
            self.assertEqual([name for name, _ in entries], ["mate.json"])
            self.assertEqual(entries[0][1]["total"], 1)

    def test_wing_spreads_missing_folder_is_empty(self):
        with TemporaryDirectory() as folder:
            self.assertEqual(wing_spreads(Path(folder) / "nope"), [])
