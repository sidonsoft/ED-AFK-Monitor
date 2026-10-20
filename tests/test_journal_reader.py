"""Exercise production functions without executing CLI/network startup code."""
import ast
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from notifications import DeliveryError, check_response
from journal_reader import read_complete_line


SOURCE = Path(__file__).resolve().parents[1] / "afk_monitor.py"


def load_function(name, namespace=None):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    scope = {} if namespace is None else namespace
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope[name]



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
            "DeliveryError": DeliveryError, "check_response": check_response,
            "webhook_log": webhook,
            "discord_test": False,
            "conf_discord": {"ForumChannel": False},
            "msg": SimpleNamespace(log=messages.append),
        })
        with self.assertRaises(DeliveryError) as error:
            send("test event")
        self.assertIn("HTTP 403", str(error.exception))
        self.assertEqual(webhook.content, "test event")
        self.assertEqual(messages, [])

    def test_successful_discord_response_does_not_log_error(self):
        messages = []
        send = load_function("discordsend", {
            "DeliveryError": DeliveryError, "check_response": check_response,
            "webhook_log": SimpleNamespace(execute=lambda: SimpleNamespace(status_code=200)),
            "discord_test": False,
            "conf_discord": {"ForumChannel": False},
            "msg": SimpleNamespace(log=messages.append),
        })
        send("test event")
        self.assertEqual(messages, [])


class LiveDeliveryTests(unittest.TestCase):
    def test_new_session_creates_new_message_and_updates_edit_it(self):
        from notifications import LiveUpdate
        calls = []
        webhook = SimpleNamespace(
            execute=lambda: (calls.append('create') or SimpleNamespace(status_code=200)),
            edit=lambda: (calls.append('edit') or SimpleNamespace(status_code=200)))
        send = load_function('discord_live_send', {
            'webhook_live': webhook, 'DeliveryError': DeliveryError,
            'check_response': check_response, 'live_delivery_generation': None})
        send(LiveUpdate('first session', 1))
        send(LiveUpdate('first update', 1))
        send(LiveUpdate('second session', 2))
        send(LiveUpdate('second update', 2))
        self.assertEqual(calls, ['create', 'edit', 'create', 'edit'])

    def test_failed_creation_is_retried_as_creation(self):
        from notifications import LiveUpdate
        calls = []
        def execute():
            calls.append('create')
            return SimpleNamespace(status_code=500 if len(calls) == 1 else 200)
        send = load_function('discord_live_send', {
            'webhook_live': SimpleNamespace(execute=execute), 'DeliveryError': DeliveryError,
            'check_response': check_response, 'live_delivery_generation': None})
        with self.assertRaises(DeliveryError):
            send(LiveUpdate('session', 1))
        send(LiveUpdate('session', 1))
        self.assertEqual(calls, ['create', 'create'])


if __name__ == "__main__":
    unittest.main()
