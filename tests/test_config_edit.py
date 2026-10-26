import unittest

from config_edit import save_overrides, set_config_text, toml_value

BASE = """# top comment
[Settings]
LiveStatus = true  # keep me
WarnNoKills = 20

[Discord]
WebhookURL = ''
# trailing note
"""


class SetConfigTextTests(unittest.TestCase):
    def test_replaces_value_and_keeps_other_lines(self):
        out = set_config_text(BASE, "Settings", "WarnNoKills", 30)
        self.assertIn("WarnNoKills = 30", out)
        self.assertIn("# keep me", out)
        self.assertIn("# top comment", out)
        self.assertIn("# trailing note", out)
        self.assertEqual(out.count("WarnNoKills"), 1)

    def test_typed_values_render_as_toml(self):
        self.assertEqual(toml_value(True), "true")
        self.assertEqual(toml_value(False), "false")
        self.assertEqual(toml_value([15, 30]), "[15, 30]")
        self.assertEqual(toml_value('say "hi"'), '"say \\"hi\\""')

    def test_changes_types_in_place(self):
        out = set_config_text(BASE, "Settings", "LiveStatus", False)
        self.assertIn("LiveStatus = false  # keep me", out)

    def test_missing_key_inserted_into_section(self):
        out = set_config_text(BASE, "Discord", "Timestamp", True)
        self.assertIn("Timestamp = true", out)
        self.assertLess(out.index("Timestamp = true"), out.index("# trailing note"))

    def test_missing_section_appended(self):
        out = set_config_text(BASE, "Ntfy", "URL", "https://ntfy.sh/x")
        self.assertIn("[Ntfy]", out)
        self.assertIn('URL = "https://ntfy.sh/x"', out)
        self.assertIn("[Discord]", out)


class SaveOverridesTests(unittest.TestCase):
    def test_atomic_round_trip(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as folder:
            path = Path(folder) / "afk_monitor.toml"
            path.write_text(BASE, encoding="utf-8")
            failure = save_overrides(path, [("Settings", "WarnNoKills", 99), ("Telegram", "ChatID", -5)])
            self.assertEqual(failure, "")
            import tomllib
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["Settings"]["WarnNoKills"], 99)
            self.assertEqual(data["Telegram"]["ChatID"], -5)
            self.assertIn("# keep me", path.read_text(encoding="utf-8"))
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_missing_file_reports_error(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            failure = save_overrides(Path(folder) / "gone.toml", [("Settings", "x", 1)])
            self.assertIn("Could not read", failure)
