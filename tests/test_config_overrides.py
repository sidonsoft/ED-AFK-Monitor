import unittest

from config_overrides import apply_overrides, parse_overrides

class ParseOverridesTests(unittest.TestCase):
    def test_typed_values_are_parsed(self):
        parsed = dict(((sec, key), value) for sec, key, value in parse_overrides([
            "Settings.WinTitleCmdrName=true",
            "Discord.UserID=123",
            "Settings.WarnCooldown=7.5",
            "Settings.RecentIntervals=[15,30]",
            "Ntfy.URL='https://ntfy.sh/x'",
            "Webhook.URL=https://example.com/plain string",
        ]))
        self.assertIs(parsed[("Settings", "WinTitleCmdrName")], True)
        self.assertEqual(parsed[("Discord", "UserID")], 123)
        self.assertEqual(parsed[("Settings", "WarnCooldown")], 7.5)
        self.assertEqual(parsed[("Settings", "RecentIntervals")], [15, 30])
        self.assertEqual(parsed[("Ntfy", "URL")], "https://ntfy.sh/x")
        self.assertEqual(parsed[("Webhook", "URL")], "https://example.com/plain string")

    def test_malformed_pairs_warn_and_skip(self):
        warnings = []
        self.assertEqual(parse_overrides(["Nope", "NoEquals 1", "A.B.C=1"], warnings.append), [])
        self.assertEqual(len(warnings), 3)

class ApplyOverridesTests(unittest.TestCase):
    def conf(self):
        return {"Flag": False, "Count": 10, "Ratio": [1, 2], "Name": "x"}

    def test_matching_keys_apply(self):
        conf = self.conf()
        apply_overrides(conf, "S", parse_overrides(["S.Flag=true", "S.Count=5",
                                                   "S.Name=hello", "S.Ratio=[3,4]"]))
        self.assertEqual(conf, {"Flag": True, "Count": 5, "Ratio": [3, 4], "Name": "hello"})

    def test_unknown_key_and_other_sections_skipped_with_warning(self):
        conf = self.conf()
        warnings = []
        apply_overrides(conf, "S", parse_overrides(["S.Nope=1", "Other.Flag=true"]), warnings.append)
        self.assertEqual(conf, self.conf())
        self.assertEqual(len(warnings), 1)

    def test_type_mismatches_are_rejected(self):
        conf = self.conf()
        warnings = []
        apply_overrides(conf, "S", parse_overrides(["S.Flag=yes", "S.Count=abc",
                                                   "S.Ratio=3", "S.Count=2.0"]), warnings.append)
        self.assertEqual(conf["Flag"], False)
        self.assertEqual(conf["Count"], 2)
        self.assertEqual(conf["Ratio"], [1, 2])
        self.assertEqual(len(warnings), 3)

    def test_numbers_and_flags_coerce_into_strings(self):
        conf = self.conf()
        apply_overrides(conf, "S", parse_overrides(["S.Name=42"]))
        self.assertEqual(conf["Name"], "42")
