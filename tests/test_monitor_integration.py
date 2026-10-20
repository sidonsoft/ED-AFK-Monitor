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

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = '''
import os, runpy, sys, time, types, urllib.request
os.get_terminal_size = lambda *args: os.terminal_size((120, 40))
from pathlib import Path
marker = Path('network-attempted')
real_sleep = time.sleep
def interruptible_sleep(seconds):
    if Path('stop-requested').exists():
        raise KeyboardInterrupt()
    real_sleep(seconds)
time.sleep = interruptible_sleep
def forbidden(*args, **kwargs):
    marker.write_text('unexpected network call')
    raise AssertionError('network disabled in integration test')
urllib.request.urlopen = forbidden
class FakeWebhook:
    def __init__(self, **kwargs):
        self.thread_name = None
        self.thread_id = None
    execute = forbidden
    edit = forbidden
sys.modules['discord_webhook'] = types.SimpleNamespace(DiscordWebhook=FakeWebhook)
sys.argv = ['afk_monitor.py', '--test', '--debug', '-j', 'journals'] + sys.argv[1:]
runpy.run_path('afk_monitor.py', run_name='__main__')
'''


class MonitorIntegrationTests(unittest.TestCase):
    def test_restart_rotation_preview_and_secret_redaction(self):
        self.run_scenario()

    def test_commander_change_stops_before_using_wrong_profile(self):
        self.run_scenario(change_commander=True)

    def run_scenario(self, change_commander=False):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('afk_monitor.py', 'notifications.py', 'journal_reader.py'):
                shutil.copyfile(ROOT / name, root / name)
            (root / 'bootstrap.py').write_text(BOOTSTRAP)
            config = (ROOT / 'afk_monitor.example.toml').read_text().replace("BotToken = ''", "BotToken = 'TEST_SECRET_TOKEN'")
            config = config.replace('ChatID = 0', 'ChatID = -123')
            config = config.replace('LiveStatus = true', 'LiveStatus = false').replace('DynamicTitle = true', 'DynamicTitle = false')
            (root / 'afk_monitor.toml').write_text(config)
            journals = root / 'journals'
            journals.mkdir()
            first = journals / 'Journal.2026-10-20T120000.01.log'
            def write(path, events, mode='w'):
                with path.open(mode, encoding='utf-8') as file:
                    for event in events:
                        file.write(json.dumps({'timestamp': '2026-10-20T12:00:00Z', **event}) + '\n')
            write(first, [{'event': 'Commander', 'Name': 'Test Commander'}])
            env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
            env.pop('ED_AFK_TELEGRAM_TOKEN', None)
            process = subprocess.Popen([sys.executable, 'bootstrap.py'], cwd=root, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       stdin=subprocess.DEVNULL)
            lines = queue.Queue()
            output = []
            def collect():
                for line in process.stdout:
                    lines.put(line)
            thread = Thread(target=collect, daemon=True)
            thread.start()
            def wait_for(text):
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
                wait_for('TELEGRAM PREVIEW: Test Commander: 📖 Monitor started')
                # The update check is unrelated to notification test mode; clear its marker.
                (root / 'network-attempted').unlink(missing_ok=True)
                write(first, [{'event': 'Shutdown'}], mode='a')
                wait_for('Quit to desktop')
                self.assertIsNone(process.poll())
                second = journals / 'Journal.2026-10-20T130000.01.log'
                write(second, [{'event': 'Commander', 'Name': 'Test Commander'},
                               {'event': 'LoadGame', 'Ship': 'Anaconda', 'GameMode': 'Solo'},
                               {'event': 'HullDamage', 'PlayerPilot': True, 'Fighter': False, 'Health': 0.6}])
                wait_for('TELEGRAM PREVIEW: Test Commander: 🛠️ Ship hull damaged!')
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
                self.assertIn('DISCORD PREVIEW:', combined)
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
