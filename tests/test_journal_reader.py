"""Exercise production functions without executing CLI/network startup code."""
import ast
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "afk_monitor.py"


def load_function(name, namespace=None):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    scope = {} if namespace is None else namespace
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope[name]


read_complete_line = load_function("read_complete_line")


class JournalReaderTests(unittest.TestCase):
    def test_events_appended_between_preload_and_live_open_are_preserved(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.log"
            history = '{"event":"Bounty","pilot":"André"}\n'
            live = '{"event":"Bounty","pilot":"Zoë"}\n'
            path.write_text(history, encoding="utf-8")
            with path.open(encoding="utf-8") as reader:
                self.assertEqual(read_complete_line(reader), history)
                self.assertEqual(read_complete_line(reader), "")
                position = reader.tell()
            with path.open("a", encoding="utf-8") as writer:
                writer.write(live)
            with path.open(encoding="utf-8") as reader:
                reader.seek(position)
                self.assertEqual(read_complete_line(reader), live)
                self.assertEqual(read_complete_line(reader), "")

    def test_partial_event_survives_preload_and_multiple_live_polls(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.log"
            path.write_text('{"event":"Bounty"}\n{"event":', encoding="utf-8")
            with path.open(encoding="utf-8") as reader:
                self.assertEqual(read_complete_line(reader), '{"event":"Bounty"}\n')
                position = reader.tell()
                self.assertEqual(read_complete_line(reader), "")
                self.assertEqual(reader.tell(), position)
            with path.open(encoding="utf-8") as reader:
                reader.seek(position)
                for _ in range(3):
                    self.assertEqual(read_complete_line(reader), "")
                    self.assertEqual(reader.tell(), position)
                with path.open("a", encoding="utf-8") as writer:
                    writer.write('"Bounty"}\n{"event":"MissionRedirected"}\n')
                self.assertEqual(read_complete_line(reader), '{"event":"Bounty"}\n')
                self.assertEqual(read_complete_line(reader), '{"event":"MissionRedirected"}\n')
                self.assertEqual(read_complete_line(reader), "")

    def test_rejected_discord_response_is_reported_even_if_falsey(self):
        class RejectedResponse:
            status_code = 403

            def __bool__(self):
                return False

        messages = []
        webhook = SimpleNamespace(execute=lambda: RejectedResponse())
        send = load_function("discordsend", {
            "webhook_log": webhook,
            "discord_test": False,
            "conf_discord": {"ForumChannel": False},
            "msg": SimpleNamespace(log=messages.append),
        })
        send("test event")
        self.assertEqual(webhook.content, "test event")
        self.assertEqual(len(messages), 1)
        self.assertIn("HTTP 403", messages[0])

    def test_successful_discord_response_does_not_log_error(self):
        messages = []
        send = load_function("discordsend", {
            "webhook_log": SimpleNamespace(execute=lambda: SimpleNamespace(status_code=200)),
            "discord_test": False,
            "conf_discord": {"ForumChannel": False},
            "msg": SimpleNamespace(log=messages.append),
        })
        send("test event")
        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
