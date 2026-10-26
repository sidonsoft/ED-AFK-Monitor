import unittest

from hotkeys import COMMAND_NAMES, format_keymap, parse_keymap, run_keypress


class ParseKeymapTests(unittest.TestCase):
    def test_preset_shortcuts(self):
        self.assertEqual(parse_keymap("status,missions,mute"),
                         {"s": "status", "m": "missions", "u": "mute"})
        self.assertEqual(parse_keymap("1,2,3"), {"1": "status", "2": "missions", "3": "mute"})

    def test_custom_mapping_parses_and_rejects_with_warnings(self):
        warnings = []
        keymap = parse_keymap("status=S,missions=M,junk=X,nope=Z,stat=T,mute=TOO,LONG", warnings.append)
        self.assertEqual(keymap, {"S": "status", "M": "missions"})
        self.assertEqual(len(warnings), 5)  # junk, nope, stat, mute=TOO, LONG

    def test_all_commands_are_documented(self):
        self.assertEqual(sorted(COMMAND_NAMES), ["missions", "mute", "status"])


class RunKeypressTests(unittest.TestCase):
    def callbacks(self):
        calls = []
        return {
            "__keymap__": parse_keymap("status,missions,mute"),
            "status": lambda: (calls.append("status"), "line one\nline two")[1],
            "missions": lambda: (calls.append("missions"), "")[1],
            "mute": lambda: (calls.append("mute"), "")[1],
        }, calls

    def test_known_key_runs_callback_once_and_logs_lines(self):
        callbacks, calls = self.callbacks()
        logged = []
        run_keypress("s", callbacks, logged.append)
        self.assertEqual(calls, ["status"])
        self.assertEqual(logged, ["\u2328\ufe0f line one", "\u2328\ufe0f line two"])

    def test_unknown_key_is_silent(self):
        callbacks, calls = self.callbacks()
        run_keypress("x", callbacks, lambda line: self.fail("should not log"))
        self.assertEqual(calls, [])

    def test_empty_reports(self):
        callbacks, calls = self.callbacks()
        logged = []
        run_keypress("m", callbacks, logged.append)  # empty missions -> notice
        run_keypress("u", callbacks, logged.append)  # empty mute -> silent
        self.assertEqual(logged, ["\u2328\ufe0f missions: nothing to report"])

    def test_format_keymap_lists_all(self):
        text = format_keymap(parse_keymap("status,missions,mute"))
        for command in COMMAND_NAMES:
            self.assertIn(command, text)
