import unittest
from datetime import datetime, timedelta, timezone

from monitor_state import Stats, Tracking, num_format, per_hour, time_format


class StatsTests(unittest.TestCase):
    def test_reset_returns_clean_counters(self):
        stats = Stats()
        stats.kills = 9
        stats.factions["Pirates"] = 3
        stats.kill_times.append(1)
        stats.bounties = 500
        stats.reset()
        self.assertEqual((stats.kills, stats.bounties), (0, 0))
        self.assertEqual((stats.factions, stats.kill_times), ({}, []))


class TrackingAttachTests(unittest.TestCase):
    def make_track(self):
        session = Stats()
        events = []
        track = Tracking()
        track.attach(session=session, on_debug=events.append,
                     on_status=lambda reset=False: events.append(("status", reset)))
        return session, track, events

    def test_session_window_lifecycle(self):
        session, track, events = self.make_track()
        session.kills = 7
        track.thiseventtime = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        track.sessionstart()
        self.assertIsNotNone(track.deploytime)
        self.assertEqual(session.kills, 0)  # attached session was reset
        self.assertEqual(session.kill_times, [track.deploytime])
        self.assertIn(("status", False), events)
        track.thiseventtime = track.deploytime + timedelta(seconds=120)
        track.sessionend()
        self.assertIsNone(track.deploytime)
        self.assertEqual(track.totaltime, 120.0)
        self.assertIn(("status", True), events)

    def test_sessionstart_delta_backdates_deploy(self):
        _, track, _ = self.make_track()
        track.thiseventtime = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        track.sessionstart(delta=600)
        self.assertEqual((track.thiseventtime - track.deploytime), timedelta(seconds=600))

    def test_unattached_tracking_is_inert(self):
        track = Tracking()
        track.thiseventtime = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        track.sessionstart()
        track.sessionend()
        self.assertIsNone(track.deploytime)


class FormatHelpersTests(unittest.TestCase):
    def test_per_hour_guards_zero_duration(self):
        self.assertEqual(per_hour(0), 0)
        self.assertEqual(per_hour(3600, 1), 1.0)

    def test_time_format_branches(self):
        self.assertEqual(time_format(3700), "1h1m")
        self.assertEqual(time_format(95), "1m35s")
        self.assertEqual(time_format(9), "9s")
        self.assertEqual(time_format(-1), "-")

    def test_num_format_branches_match_monitor_output(self):
        self.assertEqual(num_format(999_500), "1m")
        self.assertEqual(num_format(1500), "2k")  # original integer-rounding quirk kept
        self.assertEqual(num_format(900), 900)
        self.assertIsNone(num_format(None))
