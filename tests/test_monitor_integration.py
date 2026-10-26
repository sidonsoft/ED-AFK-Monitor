"""Run the real CLI against temporary journals; every network entry point is blocked."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
import unicodedata

from session_store import SessionStore

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = '''
import os, runpy, sys, time, types, urllib.request
os.get_terminal_size = lambda *args: os.terminal_size((120, 40))
from pathlib import Path
import terminal_output
terminal_output.supports_ansi = lambda stream: True
marker = Path('network-attempted')
real_sleep = time.sleep
def interruptible_sleep(seconds):
    if Path('stop-requested').exists():
        raise KeyboardInterrupt()
    real_sleep(seconds)
time.sleep = interruptible_sleep
real_urlopen = urllib.request.urlopen
def forbidden(*args, **kwargs):
    target = args[0] if args else kwargs.get('url', '')
    target = getattr(target, 'full_url', str(target))
    if '127.0.0.1' in target:  # local relay servers are part of the tests
        return real_urlopen(*args, **kwargs)
    marker.write_text('unexpected network call')
    raise AssertionError('network disabled in integration test')
urllib.request.urlopen = forbidden
class FakeWebhook:
    def __init__(self, **kwargs):
        self.thread_name = None
        self.thread_id = None
        self.embeds = []
    def add_embed(self, embed):
        self.embeds.append(embed)
    execute = forbidden
    edit = forbidden
class FakeEmbed:
    @staticmethod
    def from_dict(data):
        return data
sys.modules['discord_webhook'] = types.SimpleNamespace(DiscordWebhook=FakeWebhook, DiscordEmbed=FakeEmbed)
sys.argv = ['afk_monitor.py', '--test', '--debug', '-j', 'journals'] + sys.argv[1:]
runpy.run_path('afk_monitor.py', run_name='__main__')
'''


class MonitorIntegrationTests(unittest.TestCase):
    def test_restart_rotation_preview_and_secret_redaction(self):
        self.run_scenario()

    def test_commander_change_stops_before_using_wrong_profile(self):
        self.run_scenario(change_commander=True)

    def test_plain_cli_has_no_control_codes_or_repeating_status(self):
        self.run_scenario(plain=True)

    def test_startup_wait_follows_journal_before_commander_is_written(self):
        self.run_scenario(late_commander=True)

    def run_scenario(self, change_commander=False, plain=False, late_commander=False):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('afk_monitor.py', 'notifications.py', 'journal_reader.py', 'terminal_output.py', 'outbox.py',
                          'missions.py', 'ship_pools.py', 'ships.json', 'session_store.py',
                            'journal_schema.py', 'config_overrides.py',
                            'session_card.py', 'dashboard.py',
                            'monitor_state.py', 'hotkeys.py',
                            'instance_runner.py', 'config_edit.py',
                            'wing_relay.py'):
                shutil.copyfile(ROOT / name, root / name)
            (root / 'bootstrap.py').write_text(BOOTSTRAP, encoding='utf-8')
            config = (ROOT / 'afk_monitor.example.toml').read_text(encoding='utf-8').replace("BotToken = ''", "BotToken = 'TEST_SECRET_TOKEN'")
            config = config.replace('ChatID = 0', 'ChatID = -123')
            config = config.replace('LiveStatus = true', 'LiveStatus = false').replace('DynamicTitle = true', 'DynamicTitle = false')
            (root / 'afk_monitor.toml').write_text(config, encoding='utf-8')
            journals = root / 'journals'
            journals.mkdir()
            first = journals / 'Journal.2026-10-20T120000.01.log'
            def write(path, events, mode='w'):
                with path.open(mode, encoding='utf-8') as file:
                    for event in events:
                        file.write(json.dumps({'timestamp': '2026-10-20T12:00:00Z', **event}) + '\n')
            write(first, [] if late_commander else [{'event': 'Commander', 'Name': 'Test Commander'}])
            env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
            env.pop('ED_AFK_TELEGRAM_TOKEN', None)
            process = subprocess.Popen([sys.executable, 'bootstrap.py'] + (['--plain'] if plain else []), cwd=root, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       stdin=subprocess.DEVNULL, encoding='utf-8')
            lines = queue.Queue()
            output = []
            def collect():
                for line in process.stdout:
                    lines.put(line)
            thread = Thread(target=collect, daemon=True)
            thread.start()
            def wait_for(text):
                if plain:
                    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        line = lines.get(timeout=0.1)
                    except queue.Empty:
                        if process.poll() is not None:
                            break
                        continue
                    output.append(line)
                    if text in line:
                        return
                self.fail(f'Missing {text!r}: {"".join(output)}')
            try:
                if late_commander:
                    wait_for('Waiting for game load')
                    first = journals / 'Journal.2026-10-20T123000.01.log'
                    # A partially written Commander event must not skip profile discovery.
                    first.write_text('{"event":"Commander","Name":"Test Com', encoding='utf-8')
                    with first.open('a', encoding='utf-8') as file:
                        file.write('mander"}\n')
                wait_for('TELEGRAM PREVIEW: Test Commander: 📖 Monitor started')
                # The update check is unrelated to notification test mode; clear its marker.
                (root / 'network-attempted').unlink(missing_ok=True)
                write(first, [{'event': 'HullDamage', 'PlayerPilot': False, 'Fighter': True, 'Health': 0.8},
                              {'event': 'Shutdown'}], mode='a')
                wait_for('Quit to desktop')
                self.assertIsNone(process.poll())
                second = journals / 'Journal.2026-10-20T130000.01.log'
                write(second, [{'event': 'Commander', 'Name': 'Test Commander'},
                               {'event': 'LoadGame', 'Ship': 'Anaconda', 'GameMode': 'Solo'},
                               {'event': 'Bounty', 'Target': 'viper', 'Rewards': [{'Reward': 1234}],
                                'VictimFaction': 'Test Pirates'},
                               {'event': 'HullDamage', 'PlayerPilot': True, 'Fighter': False, 'Health': 0.6},
                               {'event': 'HullDamage', 'PlayerPilot': False, 'Fighter': True, 'Health': 0.8},
                               {'event': 'EjectCargo', 'Type': 'gold', 'Abandoned': False, 'Count': 1},
                               {'event': 'EjectCargo', 'Type': 'gold', 'Abandoned': True, 'Count': 1},
                               {'event': 'EjectCargo', 'Type': 'gold', 'Abandoned': False, 'Count': 2},
                                {'event': 'TotallyNewEvent', 'Detail': 'future schema'}])
                wait_for('TELEGRAM PREVIEW: Test Commander: 🛠️ Ship hull damaged!')
                wait_for('TELEGRAM PREVIEW: Test Commander: 🪓 Cargo lost or jettisoned (possible theft)')
                if change_commander:
                    write(second, [{'event': 'Commander', 'Name': 'Different Commander'}], mode='a')
                    wait_for('Commander changed; restart the monitor')
                else:
                    (root / 'stop-requested').touch()
                wait_for('Monitor stopped')
                process.wait(timeout=8)
                thread.join(timeout=1)
                while not lines.empty():
                    output.append(lines.get_nowait())
                combined = ''.join(output)
                self.assertIn('Following journal', combined)
                self.assertIn('Journal drift check: 1 event type(s)', combined)
                self.assertIn('TotallyNewEvent (x1)', combined)
                databases = list((root / 'session_history').glob('*.db'))
                self.assertEqual(len(databases), 1, combined)
                store = SessionStore(databases[0])
                sessions = store.sessions()
                store.close()
                self.assertEqual(len(sessions), 1, sessions)
                self.assertGreaterEqual(sessions[0]['kills'], 1)
                self.assertGreaterEqual(sessions[0]['bounties'], 1234)
                if plain:
                    self.assertNotIn('\x1b', combined)
                    self.assertNotIn('\r', combined)
                self.assertIn('DISCORD PREVIEW:', combined)
                self.assertEqual(combined.count('possible theft'), 2)
                self.assertEqual(combined.count('Fighter hull damaged!'), 4)
                self.assertNotIn('Cargo stolen!', combined)
                self.assertNotIn('TEST_SECRET_TOKEN', combined)
                self.assertNotIn('Process event error', combined)
                self.assertNotIn('Something went wrong', combined)
                self.assertFalse((root / 'network-attempted').exists())
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()


class SetupIntegrationTests(unittest.TestCase):
    def prepare(self, root):
        for name in ('afk_monitor.py', 'notifications.py', 'journal_reader.py', 'terminal_output.py', 'outbox.py',
                          'missions.py', 'ship_pools.py', 'ships.json', 'session_store.py',
                            'journal_schema.py', 'config_overrides.py',
                            'session_card.py', 'dashboard.py',
                            'monitor_state.py', 'hotkeys.py',
                            'instance_runner.py', 'config_edit.py',
                            'wing_relay.py'):
            shutil.copyfile(ROOT / name, root / name)
        (root / 'bootstrap.py').write_text(BOOTSTRAP, encoding='utf-8')

    def test_help_needs_neither_configuration_nor_network(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            result = subprocess.run([sys.executable, 'bootstrap.py', '--help'], cwd=root,
                                    capture_output=True, text=True, timeout=8, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--plain', result.stdout)
            self.assertFalse((root / 'network-attempted').exists())
            self.assertFalse((root / 'afk_monitor.toml').exists())

    def test_selector_rejects_unlisted_number_and_tolerates_partial_json(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            shutil.copyfile(ROOT / 'afk_monitor.example.toml', root / 'afk_monitor.toml')
            (root / 'journals').mkdir()
            journal = root / 'journals' / 'Journal.2026-10-20T120000.01.log'
            journal.write_text('broken JSON\n{"event":"Commander","Name":"Test Commander"}\n{"event":', encoding='utf-8')
            result = subprocess.run([sys.executable, 'bootstrap.py', '--fileselect'], cwd=root,
                                    input='2\n', capture_output=True, text=True, timeout=8, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('CMDR Test Commander', result.stdout)
            self.assertIn('Invalid number', result.stdout)
            self.assertNotIn('Traceback', result.stdout + result.stderr)

    def test_absolute_journal_path_overrides_missing_default_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            shutil.copyfile(ROOT / 'afk_monitor.example.toml', root / 'afk_monitor.toml')
            journal = root / 'Journal.2026-10-20T120000.01.log'
            journal.write_text('{"event":"Commander","Name":"Test Commander"}\n', encoding='utf-8')
            (root / 'stop-requested').touch()
            result = subprocess.run([sys.executable, 'bootstrap.py', '-j', 'missing-folder', '--setfile', str(journal)],
                                    cwd=root, capture_output=True, text=True, timeout=8, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Monitor started', result.stdout)
            self.assertIn('Commander name:', result.stdout)
            self.assertNotIn('Traceback', result.stdout + result.stderr)


class MissionLedgerIntegrationTests(unittest.TestCase):
    def test_massacre_missions_are_recorded_and_reported(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('afk_monitor.py', 'notifications.py', 'journal_reader.py',
                         'terminal_output.py', 'outbox.py', 'missions.py',
                         'ship_pools.py', 'ships.json', 'session_store.py',
                            'journal_schema.py', 'config_overrides.py',
                            'session_card.py', 'dashboard.py',
                            'monitor_state.py', 'hotkeys.py',
                            'instance_runner.py', 'config_edit.py',
                            'wing_relay.py'):
                shutil.copyfile(ROOT / name, root / name)
            (root / 'bootstrap.py').write_text(BOOTSTRAP, encoding='utf-8')
            config = (ROOT / 'afk_monitor.example.toml').read_text(encoding='utf-8')
            config = config.replace('SummaryMissions = 1', 'SummaryMissions = 2')
            config = config.replace('LiveStatus = true', 'LiveStatus = false')
            (root / 'afk_monitor.toml').write_text(config, encoding='utf-8')
            journals = root / 'journals'
            journals.mkdir()
            journal = journals / 'Journal.2026-10-20T120000.01.log'
            events = [
                {'event': 'Commander', 'Name': 'Test Commander'},
                {'event': 'LoadGame', 'Ship': 'Anaconda', 'GameMode': 'Solo'},
                {'event': 'Missions', 'Active': []},
                {'event': 'MissionAccepted', 'Name': 'Mission_MassacreWing', 'MissionID': 1001,
                 'Faction': 'Issuing Faction', 'TargetFaction': 'Pirate Faction',
                 'KillCount': 3, 'Wing': True, 'Reward': 1000000,
                 'Expiry': '2026-10-21T12:00:00Z'},
            ]
            for _ in range(2):
                events.append({'event': 'Bounty', 'Target': 'sidewinder',
                               'VictimFaction': 'Pirate Faction',
                               'Rewards': [{'Faction': 'Pirate Faction', 'Reward': 10000}],
                               'TotalReward': 10000})
            with journal.open('w', encoding='utf-8') as file:
                for event in events:
                    file.write(json.dumps({'timestamp': '2026-10-20T12:00:00Z', **event}) + '\n')

            env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
            env.pop('ED_AFK_TELEGRAM_TOKEN', None)
            process = subprocess.Popen([sys.executable, 'bootstrap.py'], cwd=root, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       stdin=subprocess.DEVNULL, encoding='utf-8')
            try:
                time.sleep(3)
                (root / 'stop-requested').touch()
                output = process.communicate(timeout=15)[0] or ''
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            self.assertEqual(process.returncode, 0, output)
            self.assertIn('Mission spread', output)
            self.assertIn('Pirate Faction', output)
            ledgers = list((root / 'mission_ledger').glob('*.json'))
            self.assertEqual(len(ledgers), 1, output)
            data = json.loads(ledgers[0].read_text(encoding='utf-8'))
            self.assertEqual(data['commander'], 'Test Commander')
            self.assertEqual(len(data['missions']), 1)
            mission = data['missions'][0]
            self.assertEqual(mission['id'], 1001)
            self.assertEqual(mission['target'], 'Pirate Faction')
            self.assertEqual(mission['kills'], 2)
            self.assertTrue(mission['wing'])

            wing = root / 'mission_ledger' / 'wing'
            wing.mkdir()
            shutil.copyfile(ledgers[0], wing / 'wingmate.json')
            listing = subprocess.run([sys.executable, 'bootstrap.py', '--missions', '--wing'],
                                     cwd=root, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, timeout=15)
            report = listing.stdout or ''
            self.assertIn('Wing view: local + 1 shared ledger(s)', report)
            self.assertIn('wingmate:', report)
            self.assertIn('2 kills left across the wing', report)


class MissionBackfillIntegrationTests(unittest.TestCase):
    def test_missions_from_an_older_journal_are_recovered(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('afk_monitor.py', 'notifications.py', 'journal_reader.py',
                         'terminal_output.py', 'outbox.py', 'missions.py',
                         'ship_pools.py', 'ships.json', 'session_store.py',
                            'journal_schema.py', 'config_overrides.py',
                            'session_card.py', 'dashboard.py',
                            'monitor_state.py', 'hotkeys.py',
                            'instance_runner.py', 'config_edit.py',
                            'wing_relay.py'):
                shutil.copyfile(ROOT / name, root / name)
            (root / 'bootstrap.py').write_text(BOOTSTRAP, encoding='utf-8')
            config = (ROOT / 'afk_monitor.example.toml').read_text(encoding='utf-8')
            config = config.replace('SummaryMissions = 1', 'SummaryMissions = 2')
            config = config.replace('LiveStatus = true', 'LiveStatus = false')
            (root / 'afk_monitor.toml').write_text(config, encoding='utf-8')
            journals = root / 'journals'
            journals.mkdir()
            older = journals / 'Journal.2026-10-20T120000.01.log'
            older.write_text(json.dumps({
                'timestamp': '2026-10-20T12:00:00Z', 'event': 'MissionAccepted',
                'Name': 'Mission_MassacreWing', 'MissionID': 3001, 'Faction': 'Issuer',
                'TargetFaction': 'Pirate Faction', 'KillCount': 3, 'Wing': True,
                'Reward': 1000000, 'Expiry': '2026-10-21T12:00:00Z'}) + '\n', encoding='utf-8')
            current = journals / 'Journal.2026-10-20T130000.01.log'
            events = [
                {'event': 'Commander', 'Name': 'Test Commander'},
                {'event': 'LoadGame', 'Ship': 'Anaconda', 'GameMode': 'Solo'},
                {'event': 'Missions', 'Active': [
                    {'MissionID': 3001, 'Name': 'Mission_MassacreWing', 'Expires': 3600}]},
            ]
            for _ in range(2):
                events.append({'event': 'Bounty', 'Target': 'sidewinder',
                               'VictimFaction': 'Pirate Faction',
                               'Rewards': [{'Faction': 'Pirate Faction', 'Reward': 10000}],
                               'TotalReward': 10000})
            with current.open('w', encoding='utf-8') as file:
                for event in events:
                    file.write(json.dumps({'timestamp': '2026-10-20T13:00:00Z', **event}) + '\n')

            env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
            env.pop('ED_AFK_TELEGRAM_TOKEN', None)
            process = subprocess.Popen([sys.executable, 'bootstrap.py'], cwd=root, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       stdin=subprocess.DEVNULL, encoding='utf-8')
            try:
                time.sleep(3)
                (root / 'stop-requested').touch()
                output = process.communicate(timeout=15)[0] or ''
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            self.assertEqual(process.returncode, 0, output)
            self.assertIn('recovered details for 1 mission', output)
            ledgers = list((root / 'mission_ledger').glob('*.json'))
            self.assertEqual(len(ledgers), 1, output)
            data = json.loads(ledgers[0].read_text(encoding='utf-8'))
            mission = data['missions'][0]
            self.assertEqual(mission['id'], 3001)
            self.assertEqual(mission['target'], 'Pirate Faction')
            self.assertEqual(mission['kills'], 2)
            self.assertIn('Mission spread', output)


class MissionImportIntegrationTests(unittest.TestCase):
    def test_import_list_and_json_export(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('afk_monitor.py', 'notifications.py', 'journal_reader.py',
                         'terminal_output.py', 'outbox.py', 'missions.py',
                         'ship_pools.py', 'ships.json', 'session_store.py',
                            'journal_schema.py', 'config_overrides.py',
                            'session_card.py', 'dashboard.py',
                            'monitor_state.py', 'hotkeys.py',
                            'instance_runner.py', 'config_edit.py',
                            'wing_relay.py'):
                shutil.copyfile(ROOT / name, root / name)
            (root / 'missions.csv').write_text(
                'Target Faction,Kills,Wing\nLords of Ngaliba,40,yes\nNgaliba Blue Boys,30,no\n',
                encoding='utf-8')
            result = subprocess.run(
                [sys.executable, 'afk_monitor.py', '--import-missions', 'missions.csv', '-p', 'Test'],
                cwd=root, capture_output=True, text=True, timeout=10, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('Imported 2 mission(s)', result.stdout)
            files = list((root / 'mission_ledger').glob('*.json'))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text(encoding='utf-8'))
            self.assertEqual(len(data['missions']), 2)
            self.assertEqual(data['profile'], 'Test')

            result = subprocess.run(
                [sys.executable, 'afk_monitor.py', '--missions', '--json'],
                cwd=root, capture_output=True, text=True, timeout=10, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload[0]['profile'], 'Test')
            self.assertEqual(len(payload[0]['missions']), 2)
            self.assertEqual(payload[0]['spread']['manual'], 2)


class MultiInstanceIntegrationTests(unittest.TestCase):
    def test_supervisor_runs_one_monitor_per_journal_folder(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('afk_monitor.py', 'notifications.py', 'journal_reader.py',
                         'terminal_output.py', 'outbox.py', 'missions.py',
                         'ship_pools.py', 'ships.json', 'session_store.py',
                         'journal_schema.py', 'config_overrides.py',
                         'session_card.py', 'dashboard.py', 'monitor_state.py',
                         'hotkeys.py', 'instance_runner.py', 'config_edit.py',
                            'wing_relay.py'):
                shutil.copyfile(ROOT / name, root / name)
            (root / 'bootstrap.py').write_text(BOOTSTRAP, encoding='utf-8')
            config = (ROOT / 'afk_monitor.example.toml').read_text(encoding='utf-8')
            config = config.replace("BotToken = ''", "BotToken = 'TEST_SECRET_TOKEN'")
            config = config.replace('ChatID = 0', 'ChatID = -123')
            config = config.replace('LiveStatus = true', 'LiveStatus = false')
            (root / 'afk_monitor.toml').write_text(config, encoding='utf-8')
            for folder, cmdr in (('journalsA', 'Alpha Oneone'), ('journalsB', 'Bravo Twotwo')):
                journals = root / folder
                journals.mkdir()
                events = [{'event': 'Commander', 'Name': cmdr},
                          {'event': 'LoadGame', 'Ship': 'Cobra Mk IV', 'GameMode': 'Open'},
                          {'event': 'HullDamage', 'PlayerPilot': False, 'Fighter': True, 'Health': 0.8},
                          {'event': 'Shutdown'}]
                with (journals / 'Journal.2026-10-20T120000.01.log').open('w', encoding='utf-8') as file:
                    for event in events:
                        file.write(json.dumps({'timestamp': '2026-10-20T12:00:00Z', **event}) + '\n')

            env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
            env.pop('ED_AFK_TELEGRAM_TOKEN', None)
            process = subprocess.Popen(
                [sys.executable, 'bootstrap.py', '--instance', 'one=journalsA',
                 '--instance', 'two=journalsB'],
                cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, stdin=subprocess.DEVNULL, encoding='utf-8')
            lines = queue.Queue()
            output = []
            def collect():
                for line in process.stdout:
                    lines.put(line)
            thread = Thread(target=collect, daemon=True)
            thread.start()
            try:
                def wait_for(text, timeout=12):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if text in ''.join(output):
                            return
                        try:
                            output.append(lines.get(timeout=0.2))
                        except queue.Empty:
                            if process.poll() is not None:
                                break
                    self.fail(f'Missing {text!r}: {"".join(output)}')
                wait_for('Instance one: watching')
                wait_for('Instance two: watching')
                for cmdr in ('Alpha Oneone', 'Bravo Twotwo'):
                    wait_for(f'TELEGRAM PREVIEW: {cmdr}:')
                def wait_count(text, count, timeout=12):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        joined = ''.join(output)
                        if joined.count(text) >= count:
                            return
                        try:
                            output.append(lines.get(timeout=0.2))
                        except queue.Empty:
                            if process.poll() is not None:
                                break
                    self.fail(f'Expected {count}x {text!r} in {"".join(output)}')
                wait_count('Monitor started', 2)
                (root / 'stop-requested').touch()
                process.wait(timeout=15)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            while not lines.empty():
                output.append(lines.get_nowait())
            thread.join(timeout=1)
            combined = ''.join(output)
            self.assertEqual(process.returncode, 0, combined)
            # 2 channels (discord + telegram preview) x 2 instances
            self.assertEqual(combined.count('Monitor started'), 4)
            self.assertIn('Stopping all instances', combined)


class WingRelayIntegrationTests(unittest.TestCase):
    """--wing-push uploads ledgers, --wing-url fetches them back through a real HTTP relay."""

    def test_push_and_fetch_round_trip(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from missions import FILE_VERSION
        store = {}

        class RelayHandler(BaseHTTPRequestHandler):
            def do_PUT(self):
                size = int(self.headers.get("Content-Length", 0))
                store[self.path] = self.rfile.read(size)
                self.send_response(204)
                self.end_headers()

            def do_GET(self):
                body = store.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('afk_monitor.py', 'notifications.py', 'journal_reader.py',
                         'terminal_output.py', 'outbox.py', 'missions.py',
                         'ship_pools.py', 'ships.json', 'session_store.py',
                         'journal_schema.py', 'config_overrides.py', 'config_edit.py',
                         'session_card.py', 'dashboard.py', 'monitor_state.py',
                         'hotkeys.py', 'instance_runner.py', 'config_edit.py',
                         'wing_relay.py'):
                shutil.copyfile(ROOT / name, root / name)
            (root / 'bootstrap.py').write_text(BOOTSTRAP, encoding='utf-8')
            config = (ROOT / 'afk_monitor.example.toml').read_text(encoding='utf-8')
            config = config.replace('LiveStatus = true', 'LiveStatus = false')
            (root / 'afk_monitor.toml').write_text(config, encoding='utf-8')
            ledgers = root / 'mission_ledger'
            ledgers.mkdir()
            ledger = {"version": FILE_VERSION, "commander": "Relay One", "profile": "",
                      "updated": 0,
                      "missions": [{"id": 4242, "name": "Clean", "faction": "Security",
                                    "target": "Pirates", "kill_count": 5, "wing": True,
                                    "expiry": "", "reward": 100000, "accepted": 1761000000.0,
                                    "kills": 3, "done": False, "known": True}]}
            (ledgers / "default.json").write_text(json.dumps(ledger), encoding="utf-8")
            relay = HTTPServer(("127.0.0.1", 0), RelayHandler)
            port = relay.server_address[1]
            Thread(target=relay.serve_forever, daemon=True).start()
            env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
            try:
                push = subprocess.run([sys.executable, 'afk_monitor.py', '--wing-push',
                                       f'http://127.0.0.1:{port}/wing', '--plain'],
                                      cwd=root, env=env, capture_output=True, text=True,
                                      timeout=30, stdin=subprocess.DEVNULL)
                self.assertIn('Pushed default.json', push.stdout + push.stderr)
                self.assertIn('/wing/default.json', store)

                pushed = json.loads(store['/wing/default.json'])
                pushed['updated'] = time.time() - 3 * 86400  # mate has been quiet 3 days
                store['/wing/default.json'] = json.dumps(pushed).encode()

                view = subprocess.run([sys.executable, 'afk_monitor.py', '--plain', '--missions', '--wing',
                                       '--wing-url', f'http://127.0.0.1:{port}/wing/default.json'],
                                      cwd=root, env=env, capture_output=True, text=True,
                                      timeout=30, stdin=subprocess.DEVNULL)
                combined = view.stdout + view.stderr
                self.assertIn('Wing view: local + 1 shared ledger(s)', combined)
                self.assertIn('4 kills left across the wing', combined)
                self.assertIn('no updates for', combined)  # offline view flags stale copies
            finally:
                relay.shutdown()
                relay.server_close()


class LiveWingRelayIntegrationTests(unittest.TestCase):
    """With [Wing] URLs configured, the monitor itself pushes and fetches."""

    def test_monitor_pushes_and_fetches_configured_relay(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from missions import FILE_VERSION
        store = {}

        class RelayHandler(BaseHTTPRequestHandler):
            def do_PUT(self):
                size = int(self.headers.get("Content-Length", 0))
                store[self.path] = self.rfile.read(size)
                self.send_response(204)
                self.end_headers()

            def do_GET(self):
                body = store.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        relay = HTTPServer(("127.0.0.1", 0), RelayHandler)
        port = relay.server_address[1]
        Thread(target=relay.serve_forever, daemon=True).start()
        mate = {"version": FILE_VERSION, "commander": "Wingy Mate", "profile": "mate",
                "updated": time.time() - 2 * 86400,  # two days quiet: must flag as stale
                "missions": [{"id": 7001, "name": "Sweep", "faction": "Security",
                              "target": "Pirates", "kill_count": 4, "wing": True,
                              "expiry": "", "reward": 50000, "accepted": 1761000000.0,
                              "kills": 1, "done": False, "known": True}]}
        store["/wing/mate.json"] = json.dumps(mate).encode()
        try:
            with TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ('afk_monitor.py', 'notifications.py', 'journal_reader.py',
                             'terminal_output.py', 'outbox.py', 'missions.py',
                             'ship_pools.py', 'ships.json', 'session_store.py',
                             'journal_schema.py', 'config_overrides.py', 'config_edit.py',
                             'session_card.py', 'dashboard.py', 'monitor_state.py',
                             'hotkeys.py', 'instance_runner.py', 'config_edit.py',
                             'wing_relay.py'):
                    shutil.copyfile(ROOT / name, root / name)
                (root / 'bootstrap.py').write_text(BOOTSTRAP, encoding='utf-8')
                import socket
                probe = socket.socket()  # OS-allocated free port; avoids CI clashes
                probe.bind(('127.0.0.1', 0))
                dash_port = probe.getsockname()[1]  # injected via config, not a flag
                probe.close()
                probe = socket.socket()
                probe.bind(('127.0.0.1', 0))
                serve_port = probe.getsockname()[1]  # config-only --wing-serve
                probe.close()
                config = (ROOT / 'afk_monitor.example.toml').read_text(encoding='utf-8')
                config = config.replace("BotToken = ''", "BotToken = 'TEST_SECRET_TOKEN'")
                config = config.replace('ChatID = 0', 'ChatID = -123')
                config = config.replace('LiveStatus = true', 'LiveStatus = false')
                config = config.replace('SummaryMissions = 2', 'SummaryMissions = 3')
                config = config.replace("PushURL = ''", f"PushURL = 'http://127.0.0.1:{port}/wing'")
                config = config.replace("PushEvery = 0", "PushEvery = 1")
                config = config.replace("ServePort = 0", f"ServePort = {serve_port}")
                config = config.replace("WarnNoKills = 20",
                                   f"WarnNoKills = 20\nWatchdogSeconds = 5"
                                   f"\nDashboard = {dash_port}", 1)  # config-file dashboard, no flag
                config = config.replace("FetchURL = []", f"FetchURL = ['http://127.0.0.1:{port}/wing/mate.json', 'http://127.0.0.1:{port}/wing/nobody.json']")
                (root / 'afk_monitor.toml').write_text(config, encoding='utf-8')
                ledgers = root / 'mission_ledger'
                ledgers.mkdir()
                local = {"version": FILE_VERSION, "commander": "Test Commander", "profile": "",
                         "updated": 0,
                         "missions": [{"id": 4242, "name": "Clean", "faction": "Security",
                                       "target": "Pirates", "kill_count": 5, "wing": True,
                                       "expiry": "", "reward": 100000, "accepted": 1761000000.0,
                                       "kills": 3, "done": False, "known": True}]}
                (ledgers / "default.json").write_text(json.dumps(local), encoding="utf-8")
                journals = root / 'journals'
                journals.mkdir()
                events = [{'event': 'Commander', 'Name': 'Test Commander'},
                          {'event': 'LoadGame', 'Ship': 'Cobra Mk IV', 'GameMode': 'Open'},
                          {'event': 'Bounty', 'Target': 'sidewinder', 'VictimFaction': 'Pirates',
                           'Rewards': [{'Faction': 'Pirates', 'Reward': 10000}]},
                          {'event': 'Bounty', 'Target': 'viper', 'VictimFaction': 'Pirates',
                           'Rewards': [{'Faction': 'Pirates', 'Reward': 12000}]},
                          {'event': 'HullDamage', 'PlayerPilot': False, 'Fighter': True, 'Health': 0.8},
                          {'event': 'Shutdown'}]
                with (journals / 'Journal.2026-10-20T120000.01.log').open('w', encoding='utf-8') as file:
                    for event in events:
                        file.write(json.dumps({'timestamp': '2026-10-20T12:00:00Z', **event}) + '\n')

                env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
                process = subprocess.Popen([sys.executable, 'bootstrap.py', '--resetsession'],
                                           cwd=root, env=env,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, stdin=subprocess.DEVNULL, encoding='utf-8')
                lines = []
                output = []
                def collect():
                    for line in process.stdout:
                        output.append(line)
                thread = Thread(target=collect, daemon=True)
                thread.start()
                try:
                    # shared CI runners (esp. congested macOS ones) can be slow
                    # to start python; the loop exits as soon as it's up
                    deadline = time.monotonic() + 60
                    while 'Monitor started' not in ''.join(output) and time.monotonic() < deadline:
                        time.sleep(0.2)
                    if 'Monitor started' not in ''.join(output):
                        # a startup crash would otherwise surface only as
                        # timeouts in every later step
                        self.fail('monitor never started:\n' + ''.join(output)[-2000:])
                    deadline = time.monotonic() + 15
                    while 'Dashboard:' not in ''.join(output) and time.monotonic() < deadline:
                        time.sleep(0.2)
                    from urllib.request import urlopen
                    dash = None
                    deadline = time.monotonic() + 30
                    while dash is None and time.monotonic() < deadline:
                        try:
                            with urlopen(f'http://127.0.0.1:{dash_port}/missions.json', timeout=5) as response:
                                dash = json.loads(response.read())
                        except OSError:
                            time.sleep(0.5)
                    self.assertIsNotNone(dash, 'dashboard never answered')
                    with urlopen(f'http://127.0.0.1:{dash_port}/live.json', timeout=5) as response:
                        live = json.loads(response.read())
                    self.assertIn('updated', live)
                    self.assertIn('total_time', live)
                    with urlopen(f'http://127.0.0.1:{dash_port}/sessions.json', timeout=5) as response:
                        hist = json.loads(response.read())
                    self.assertIsInstance(hist['sessions'], list)
                    self.assertIn('kills_hour', hist['totals'])  # lifetime aggregates ride along
                    with urlopen(f'http://127.0.0.1:{dash_port}/', timeout=5) as response:
                        page = response.read().decode('utf-8')
                    self.assertIn('id="wingpanel"', page)  # the rendered page ships too
                    # config-only relay: ServePort turns the monitor itself into
                    # a fetchable host for its pushed copies
                    deadline = time.monotonic() + 30
                    served = None
                    while served is None and time.monotonic() < deadline:
                        try:
                            with urlopen(f'http://127.0.0.1:{serve_port}/wing/default.json',
                                         timeout=5) as response:
                                served = json.loads(response.read())
                        except OSError:
                            time.sleep(0.5)
                    self.assertIsNotNone(served, 'embedded relay never served the pushed ledger')
                    self.assertEqual(served['missions'][0]['id'], 4242)
                    import urllib.error as _ue
                    try:
                        urlopen(f'http://127.0.0.1:{serve_port}/wing/ghost.json', timeout=5)
                        self.fail('ghost.json should 404')
                    except _ue.HTTPError as caught:
                        self.assertEqual(caught.code, 404)
                    # Periodic pushes ride the idle watchdog; the scenario
                    # shortens its cadence via WatchdogSeconds = 5.
                    deadline = time.monotonic() + 60
                    while '/wing/default.json' not in store and time.monotonic() < deadline:
                        time.sleep(0.5)
                    pushed_midrun = dict(store)  # before the exit push can land anything
                    (root / 'stop-requested').touch()
                    process.wait(timeout=20)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                thread.join(timeout=2)
                combined = ''.join(output)
                self.assertIn('Wing view: local + 1 shared ledger(s)', combined)
                self.assertIn('Wing relay fetch', combined)  # the missing URL warns once
                self.assertIn('mate: no updates for', combined)  # stale wingmate flagged
                self.assertIn("'name': 'Wing view'", combined)  # the Discord embed carries it too
                self.assertIn('mate.json', dash['wing_view']['sources'])
                self.assertEqual(dash['wing_view']['combined'], {'done': 0, 'total': 2, 'left': 5, 'turn_in': 0})
                self.assertTrue(any('nobody.json' in error for error in dash['wing_view']['errors']))
                self.assertTrue(any('no updates for' in error for error in dash['wing_view']['errors']))
                self.assertIn('Wing relay: serving', combined)  # config-only ServePort came up
                self.assertIn('/wing/default.json', store)
                self.assertIn('/wing/default.json', pushed_midrun)  # PushEvery, not just on stop
                pushed = json.loads(store['/wing/default.json'])
                self.assertEqual(pushed['missions'][0]['id'], 4242)
        finally:
            relay.shutdown()
            relay.server_close()
