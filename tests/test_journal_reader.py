"""Exercise production functions without executing CLI/network startup code."""
import ast
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from notifications import DeliveryError, LiveUpdate, check_response
from journal_reader import find_mission_accepts, read_complete_line


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
        webhook = SimpleNamespace(id=None)
        def execute():
            calls.append('create')
            webhook.id = 'message-1'
            return SimpleNamespace(status_code=200)
        def edit():
            calls.append('edit')
            return SimpleNamespace(status_code=200)
        webhook.execute = execute
        webhook.edit = edit
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


class DiscordForumTests(unittest.TestCase):
    def test_successful_forum_creation_keeps_original_thread_for_later_events(self):
        calls = []
        webhook = SimpleNamespace(thread_name='session', thread_id=None, id=None)
        def execute():
            calls.append((webhook.thread_name, webhook.thread_id, webhook.content))
            webhook.id = str(len(calls))
            return SimpleNamespace(status_code=200)
        webhook.execute = execute
        send = load_function('discordsend', {
            'webhook_log': webhook, 'DeliveryError': DeliveryError,
            'check_response': check_response, 'conf_discord': {'ForumChannel': True}})
        send('startup')
        send('kill')
        self.assertEqual(calls, [('session', None, 'startup'), (None, '1', 'kill')])
        self.assertEqual(webhook.thread_id, '1')

    def test_failed_forum_creation_does_not_advance_thread_state(self):
        webhook = SimpleNamespace(thread_name='session', thread_id=None, id=None,
                                  execute=lambda: SimpleNamespace(status_code=429))
        send = load_function('discordsend', {
            'webhook_log': webhook, 'DeliveryError': DeliveryError,
            'check_response': check_response, 'conf_discord': {'ForumChannel': True}})
        with self.assertRaises(DeliveryError):
            send('startup')
        self.assertEqual(webhook.thread_name, 'session')
        self.assertIsNone(webhook.thread_id)


class SummaryRoutingTests(unittest.TestCase):
    def test_telegram_summary_omits_terminal_only_fields(self):
        from datetime import datetime, timezone
        calls = []
        stats = SimpleNamespace(kills=2, factions={'Private Faction': 2}, bounties=0,
                                scansin=0, merits=0, kill_times=[])
        summarize = load_function('summary', {
            'track': SimpleNamespace(killtype='bounties', totaltime=3600),
            'mission_ledger': SimpleNamespace(has_missions=lambda: False, status_line=lambda: '0/0'),
            'conf_log_levels': {'SummaryKills': 2, 'SummaryFaction': 1,
                                'SummaryBounties': 0, 'SummaryMerits': 0, 'SummaryScans': 0},
            'conf_settings': {'ExtendedStats': False},
            'per_hour': lambda seconds, precision=None: round(3600 / seconds, precision),
            'time_format': lambda seconds: '1h',
            'logevent': lambda **kwargs: calls.append(kwargs),
        })
        summarize(stats, logtime=datetime.now(timezone.utc), session=False)
        self.assertIn('Private Faction', calls[0]['msg_term'])
        self.assertNotIn('Private Faction', calls[0]['msg_discord'])
        self.assertNotIn('Private Faction', calls[0]['msg_telegram'])
        self.assertIn('Kills:', calls[0]['msg_telegram'])

    def test_live_summary_includes_mission_progress(self):
        import time as time_module
        from datetime import datetime, timedelta, timezone
        calls = []
        now = datetime.now(timezone.utc)
        stats = SimpleNamespace(kills=2, factions={'Pirates': 2}, bounties=0,
                                scansin=0, merits=0, kill_times=[])
        summarize = load_function('summary', {
            'track': SimpleNamespace(killtype='bounties', totaltime=3600,
                                     deploytime=now - timedelta(seconds=3600)),
            'conf_settings': {'ExtendedStats': False},
            'conf_log_levels': {'SummaryKills': 2, 'SummaryFaction': 0, 'SummaryBounties': 0,
                                'SummaryMerits': 0, 'SummaryScans': 0, 'SummaryMissions': 2},
            'mission_ledger': SimpleNamespace(has_missions=lambda: True,
                                              status_line=lambda: '0/1',
                                              report_text=lambda **kwargs: '',
                                              report_discord=lambda: '',
                                              report_live=lambda: '**Mission spread** 0/1 complete',
                                              report_embed=lambda line=None: None,
                                              projections=lambda rate: {}),
            'per_hour': lambda seconds, precision=None: round(3600 / seconds, precision),
            'time_format': lambda seconds: '1h',
            'logevent': lambda **kwargs: calls.append(('log', kwargs)),
            'discord_live_update': lambda text: calls.append(('live', text)),
            'time': time_module,
        })
        summarize(stats, logtime=now, live=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'live')
        self.assertIn('**Mission spread** 0/1 complete', calls[0][1])

    def test_deleted_live_message_is_reposted(self):
        from types import SimpleNamespace
        calls = []
        def edit():
            calls.append('edit')
            return SimpleNamespace(status_code=404)
        def execute():
            calls.append('execute')
            return SimpleNamespace(status_code=200)
        webhook = SimpleNamespace(id='message-1', content=None, edit=edit, execute=execute)
        send = load_function('discord_live_send', {
            'webhook_live': webhook, 'DeliveryError': DeliveryError,
            'check_response': check_response, 'live_delivery_generation': 1})
        send(LiveUpdate('session stats', 1))
        self.assertEqual(calls, ['edit', 'execute'])
        self.assertEqual(send.__globals__['live_delivery_generation'], 1)

    def test_live_message_without_id_is_created(self):
        from types import SimpleNamespace
        calls = []
        webhook = SimpleNamespace(id=None, content=None,
                                  execute=lambda: (calls.append('execute')
                                                   or SimpleNamespace(status_code=200)))
        send = load_function('discord_live_send', {
            'webhook_live': webhook, 'DeliveryError': DeliveryError,
            'check_response': check_response, 'live_delivery_generation': 1})
        send(LiveUpdate('session stats', 1))
        self.assertEqual(calls, ['execute'])

    def test_summary_appends_mission_block_to_discord(self):
        from datetime import datetime, timezone
        calls = []
        now = datetime.now(timezone.utc)
        stats = SimpleNamespace(kills=2, factions={'Pirates': 2}, bounties=0,
                                scansin=0, merits=0, kill_times=[])
        summarize = load_function('summary', {
            'track': SimpleNamespace(killtype='bounties', totaltime=3600,
                                     deploytime=now, missionredirects=0, missionsactive=[]),
            'conf_settings': {'ExtendedStats': False},
            'conf_discord': {'DiscordEmbeds': False},
            'conf_log_levels': {'SummaryKills': 2, 'SummaryFaction': 0, 'SummaryBounties': 0,
                                'SummaryMerits': 0, 'SummaryScans': 0, 'SummaryMissions': 2},
            'mission_ledger': SimpleNamespace(has_missions=lambda: True,
                                              status_line=lambda: '0/1',
                                              report_text=lambda **kwargs: 'Mission spread text',
                                              report_discord=lambda: 'Mission spread discord',
                                              report_live=lambda: '**Mission spread** live',
                                              report_embed=lambda line=None: None,
                                              projections=lambda rate: {'cr_per_hour': 500000,
                                                                       'finish_secs': 3600,
                                                                       'turn_in': 123}),
            'per_hour': lambda seconds, precision=None: round(3600 / seconds, precision),
            'time_format': lambda seconds: '1h',
            'logevent': lambda **kwargs: calls.append(kwargs),
        })
        summarize(stats, logtime=now, session=False)
        self.assertEqual(len(calls), 1)
        self.assertIn('Mission spread text', calls[0]['msg_term'])
        self.assertIn('**Mission spread** live', calls[0]['msg_discord'])
        self.assertIn('Mission spread text', calls[0]['msg_telegram'])
        self.assertIn('Projection at current rate: 500,000 cr/h from mission rewards', calls[0]['msg_discord'])
        self.assertIn('stack completes in 1h', calls[0]['msg_discord'])
        self.assertIn('123 cr ready to turn in', calls[0]['msg_telegram'])

    def test_event_uses_explicit_telegram_text_and_respects_terminal_only_level(self):
        import re
        from datetime import datetime, timezone
        calls = []
        log = load_function('logevent', {
            'datetime': datetime, 'timezone': timezone, 're': re,
            'track': SimpleNamespace(preloading=False, logged=0, dupeevent=None),
            'discord_test': False, 'webhook_log': None, 'telegram_enabled': True,
            'conf_settings': {'UseUTC': True},
            'conf_discord': {'UserID': 0, 'Timestamp': False, 'PrependCmdrName': False},
            'DUPE_MAX': 5, 'msg': SimpleNamespace(log=lambda _: None),
            'sendmessage': lambda *args, **kwargs: calls.append(kwargs),
        })
        log.__globals__['track'].cmdrname = 'Test'
        log('private terminal details', msg_telegram='remote summary', loglevel=2)
        self.assertIn('remote summary', calls[0]['telegram_message'])
        self.assertNotIn('private terminal details', calls[0]['telegram_message'])
        log('terminal-only event', loglevel=1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()


class MissionBackfillTests(unittest.TestCase):
    def test_finds_accepts_in_older_journals(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            newer = root / "Journal.2026-10-20T130000.01.log"
            older = root / "Journal.2026-10-20T120000.01.log"
            newer.write_text('{"event":"LoadGame"}\n', encoding="utf-8")
            older.write_text(
                '{"event":"MissionAccepted","MissionID":1001,"TargetFaction":"Pirates","KillCount":40}\n'
                '{"event":"MissionAccepted","MissionID":1002,"TargetFaction":"Others","KillCount":20}\n',
                encoding="utf-8")
            (root / "not-a-journal.log").write_text(
                '{"event":"MissionAccepted","MissionID":1003}\n', encoding="utf-8")
            found = find_mission_accepts(root, [1001, 9999])
            self.assertEqual(set(found), {1001})
            self.assertEqual(found[1001]["TargetFaction"], "Pirates")

    def test_missing_folder_and_invalid_lines_are_ignored(self):
        self.assertEqual(find_mission_accepts("/does/not/exist", [1]), {})
        with TemporaryDirectory() as folder:
            path = Path(folder) / "Journal.2026-10-20T120000.01.log"
            path.write_bytes(b'{"event":"MissionAccepted","MissionID":\xff}\n'
                             b'{"event":"MissionAccepted","MissionID":5,"KillCount":10}\n')
            found = find_mission_accepts(folder, [5])
            self.assertEqual(found[5]["KillCount"], 10)

    def test_file_limit_is_respected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for index, mission_id in enumerate((7, 101, 102)):
                path = root / f"Journal.2026-10-2{index}T120000.01.log"
                path.write_text('{"event":"MissionAccepted","MissionID":%d,"KillCount":1}\n' % mission_id,
                                encoding="utf-8")
            self.assertEqual(find_mission_accepts(root, [7], max_files=2), {})
            self.assertIn(7, find_mission_accepts(root, [7], max_files=3))


class MissionOriginRecoveryTests(unittest.TestCase):
    def test_lookup_recovers_acceptance_station_and_clears_after_travel(self):
        from journal_reader import find_mission_accepts
        with TemporaryDirectory() as folder:
            events = [
                {'event': 'Docked', 'StarSystem': 'Puneith', 'StationName': 'Wheelock Port'},
                {'event': 'MissionAccepted', 'MissionID': 1},
                {'event': 'Undocked'},
                {'event': 'FSDJump', 'StarSystem': 'Anana'},
                {'event': 'MissionAccepted', 'MissionID': 2},
            ]
            path = Path(folder) / 'Journal.2026-10-26T120000.01.log'
            path.write_text(''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')
            found = find_mission_accepts(folder, [1, 2])
            self.assertEqual(found[1]['OriginStation'], 'Wheelock Port')
            self.assertEqual(found[2]['OriginStation'], '')
            self.assertEqual(found[2]['OriginSystem'], 'Anana')


class EmbedRoutingTests(unittest.TestCase):
    def test_summary_sends_missions_as_discord_embed_payload(self):
        from datetime import datetime, timezone
        calls = []
        now = datetime.now(timezone.utc)
        stats = SimpleNamespace(kills=2, factions={'Pirates': 2}, bounties=0,
                                scansin=0, merits=0, kill_times=[])
        summarize = load_function('summary', {
            'track': SimpleNamespace(killtype='bounties', totaltime=3600,
                                     deploytime=now, missionredirects=0, missionsactive=[]),
            'conf_settings': {'ExtendedStats': False},
            'conf_discord': {'DiscordEmbeds': True},
            'conf_log_levels': {'SummaryKills': 2, 'SummaryFaction': 0, 'SummaryBounties': 0,
                                'SummaryMerits': 0, 'SummaryScans': 0, 'SummaryMissions': 2},
            'mission_ledger': SimpleNamespace(has_missions=lambda: True,
                                              status_line=lambda: '0/1',
                                              report_text=lambda **kwargs: 'Mission spread text',
                                              report_discord=lambda: '',
                                              report_live=lambda: 'should not be used',
                                              report_embed=lambda line=None: {'title': 'spread', 'line': line},
                                              projections=lambda rate: {'cr_per_hour': 1000,
                                                                       'finish_secs': 0,
                                                                       'turn_in': 0}),
            'per_hour': lambda seconds, precision=None: round(3600 / seconds, precision),
            'time_format': lambda seconds: '1h',
            'logevent': lambda **kwargs: calls.append(kwargs),
        })
        summarize(stats, logtime=now, session=False)
        payload = calls[0]['msg_discord']
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload['embeds'][0]['title'], 'spread')
        self.assertIn('Projection at current rate', payload['embeds'][0]['line'])
        self.assertIn('Mission spread text', calls[0]['msg_term'])
        self.assertIn('Mission spread text', calls[0]['msg_telegram'])

    def test_discordsend_maps_dict_payload_to_webhook_content_and_embeds(self):
        webhook = SimpleNamespace(content=None, embeds=[], id=None, thread_name=None, thread_id=None,
                                  execute=lambda: SimpleNamespace(status_code=200, headers={}))
        send = load_function('discordsend', {
            'webhook_log': webhook,
            'conf_discord': {'ForumChannel': False, 'ThreadCmdrNames': False},
            'DiscordEmbed': SimpleNamespace(from_dict=staticmethod(lambda e: e)),
            'DeliveryError': DeliveryError, 'check_response': check_response,
        })
        send({"text": "stats body", "embeds": [{"title": "spread"}]})
        self.assertEqual(webhook.content, "stats body")
        self.assertEqual(webhook.embeds, [{"title": "spread"}])
        send("plain text")
        self.assertEqual(webhook.content, "plain text")
        self.assertEqual(webhook.embeds, [])
