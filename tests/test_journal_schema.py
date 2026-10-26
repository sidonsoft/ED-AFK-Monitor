import unittest

from journal_schema import HANDLED_EVENTS, IGNORED_EVENTS, unhandled_types

class JournalSchemaDriftTests(unittest.TestCase):
    def test_handled_events_carry_no_handled_prefix_conflicts(self):
        self.assertFalse(HANDLED_EVENTS & IGNORED_EVENTS)

    def test_unknown_event_types_surface_busiest_first(self):
        seen = {"Status": 500, "TotallyNew": 3, "Bounty": 9, "OlderNew": 7,
                "ShieldState": 40, "Docked": 12}
        self.assertEqual(unhandled_types(seen), [(7, "OlderNew"), (3, "TotallyNew")])

    def test_fully_handled_run_reports_nothing(self):
        seen = {"Commander": 1, "Bounty": 4, "Status": 30, "Docked": 2, "Fileheader": 1}
        self.assertEqual(unhandled_types(seen), [])


class DriftIgnoreTests(unittest.TestCase):
    def test_user_ignores_silence_named_events(self):
        seen = {"BrandNew": 5, "StillNew": 2, "LoadGame": 9}
        self.assertEqual(unhandled_types(seen, [" stillnew "]), [(5, "BrandNew")])

    def test_default_behaviour_unchanged(self):
        seen = {"BrandNew": 5}
        self.assertEqual(unhandled_types(seen, []), [(5, "BrandNew")])
