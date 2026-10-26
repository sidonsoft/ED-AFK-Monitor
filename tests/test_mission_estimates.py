"""Target-faction forecasts and uncertainty at session boundaries."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from missions import MissionLedger


class MissionEstimateTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = MissionLedger(Path(self.temp.name) / 'missions.json')
        self.start = datetime(2026, 10, 26, 0, tzinfo=timezone.utc)
        self.ledger.reset_rates(self.start)

    def accept(self, mid=1, target='Pirates', quota=10, issuer='Issuer'):
        self.ledger.accept({'Name': 'Mission_MassacreWing', 'MissionID': mid,
                            'Faction': issuer, 'TargetFaction': target,
                            'KillCount': quota, 'Wing': True})

    def kill(self, seconds, target='Pirates', alternate=None):
        self.ledger.kill(target, alternate, timestamp=self.start + timedelta(seconds=seconds))

    def estimate(self, seconds):
        return self.ledger.estimate_text(self.start + timedelta(seconds=seconds))

    def test_uses_observation_time_and_remaining_quota(self):
        self.accept()
        for second in (120, 240, 360):
            self.kill(second)
        # Three kills over 600 seconds; seven remaining need 1400 seconds.
        self.assertIn('next ~24m | full stack ~24m', self.estimate(600))

    def test_next_mission_is_serial_but_other_issuers_are_parallel(self):
        self.accept(quota=10)
        self.accept(2, quota=2)
        self.accept(3, quota=5, issuer='Another issuer')
        for second in (120, 240, 360):
            self.kill(second)
        self.assertIn('next ~7m | full stack ~30m', self.estimate(600))

    def test_unrelated_kills_do_not_inflate_rate(self):
        self.accept()
        for second in (120, 240, 360):
            self.kill(second)
        for second in range(400, 550):
            self.kill(second, 'Unrelated Faction')
        self.assertIn('full stack ~24m', self.estimate(600))
        self.assertNotIn('unrelated faction', self.ledger.recent_target_kills)

    def test_localised_and_raw_alias_only_record_one_matching_kill(self):
        self.accept()
        for second in (120, 240, 360):
            self.kill(second, 'Pirates', 'Pirates')
        self.assertEqual(len(self.ledger.recent_target_kills['pirates']), 3)
        self.assertIn('full stack ~24m', self.estimate(600))

    def test_multitarget_completion_uses_overlapping_wall_clock_rates(self):
        self.accept(quota=6)
        self.accept(2, target='Other Pirates', quota=9)
        for second in (120, 240, 360):
            self.kill(second)
            self.kill(second, 'Other Pirates')
        self.assertIn('next ~10m | full stack ~20m', self.estimate(600))

    def test_missing_rate_for_one_target_does_not_claim_full_forecast(self):
        self.accept()
        self.accept(2, target='Other Pirates')
        for second in (120, 240, 360):
            self.kill(second)
        self.assertIn('insufficient data', self.estimate(600))

    def test_warmup_and_small_samples(self):
        self.accept()
        for second in (10, 20, 30):
            self.kill(second)
        self.assertIn('insufficient data', self.estimate(60))
        self.ledger.reset_rates(self.start)
        self.kill(120)
        self.kill(240)
        self.assertIn('insufficient data', self.estimate(600))

    def test_stale_rate_is_not_presented_as_an_eta(self):
        self.accept()
        for second in (120, 240, 360):
            self.kill(second)
        self.assertIn('stalled', self.estimate(960))
        self.assertNotIn('~', self.estimate(960))
        self.kill(970)
        self.assertIn('full stack ~', self.estimate(980))

    def test_window_excludes_old_and_future_events(self):
        self.accept()
        for second in (10, 20, 30, 2400):
            self.kill(second)
        self.assertIn('insufficient data', self.estimate(2100))
        self.assertEqual(len(self.ledger.recent_target_kills['pirates']), 1)

    def test_session_reset_and_restart_do_not_reuse_rates(self):
        self.accept()
        for second in (120, 240, 360):
            self.kill(second)
        self.ledger.save()
        restored = MissionLedger(self.ledger.path)
        self.assertIn('insufficient data', restored.estimate_text(self.start + timedelta(seconds=600)))
        self.ledger.reset_rates(self.start + timedelta(seconds=600))
        self.assertIn('insufficient data', self.estimate(900))
        self.assertEqual(self.ledger.missions[1].kills, 3)

    def test_zero_estimated_remaining_waits_for_confirmation(self):
        self.accept(quota=3)
        for second in (120, 240, 360):
            self.kill(second)
        self.assertIn('awaiting game confirmation', self.estimate(600))
        self.ledger.redirected(1)
        self.assertEqual(self.estimate(600), 'Mission ETA: objectives complete')

    def test_unknown_details_and_no_missions(self):
        self.assertEqual(self.estimate(600), '')
        self.ledger.reconcile([{'MissionID': 1, 'Name': 'Mission_MassacreWing'}])
        self.assertIn('unknown mission details', self.estimate(600))
