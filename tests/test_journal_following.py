from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from journal_reader import JournalReader


class JournalFollowingTests(unittest.TestCase):
    def test_drains_old_file_then_each_successor_from_start(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / 'Journal.2026-10-20T120000.01.log'
            second = root / 'Journal.2026-10-20T130000.01.log'
            third = root / 'Journal.2026-10-20T140000.01.log'
            first.write_text('history\nlive\n', encoding='utf-8')
            rotations = []
            with JournalReader(first, position=len('history\n'), on_rotate=rotations.append, poll_interval=0) as reader:
                second.write_text('second\n', encoding='utf-8')
                third.write_text('third\n', encoding='utf-8')
                (root / 'not-a-journal.log').write_text('ignored\n')
                self.assertEqual(reader.readline(), 'live\n')
                self.assertEqual(reader.readline(), 'second\n')
                self.assertEqual(reader.readline(), 'third\n')
                self.assertEqual(reader.readline(), '')
            self.assertEqual(rotations, [second, third])

    def test_partial_event_is_preserved_across_polls_and_rotation(self):
        with TemporaryDirectory() as folder:
            first = Path(folder) / 'Journal.2026-10-20T120000.01.log'
            second = Path(folder) / 'Journal.2026-10-20T130000.01.log'
            first.write_text('{"event":', encoding='utf-8')
            with JournalReader(first, poll_interval=0) as reader:
                second.write_text('next\n', encoding='utf-8')
                self.assertEqual(reader.readline(), '')
                self.assertEqual(reader.readline(), '')
                with first.open('a', encoding='utf-8') as writer:
                    writer.write('"Bounty"}\n')
                self.assertEqual(reader.readline(), '{"event":"Bounty"}\n')
                self.assertEqual(reader.readline(), 'next\n')

    def test_fixed_selection_does_not_follow_new_file(self):
        with TemporaryDirectory() as folder:
            first = Path(folder) / 'Journal.2026-10-20T120000.01.log'
            first.write_text('one\n')
            with JournalReader(first, follow=False) as reader:
                second = Path(folder) / 'Journal.2026-10-20T130000.01.log'
                second.write_text('two\n')
                self.assertEqual(reader.readline(), 'one\n')
                self.assertEqual(reader.readline(), '')

    def test_split_utf8_character_is_retried_after_remaining_bytes_arrive(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'Journal.2026-10-20T120000.01.log'
            event = '{"pilot":"André"}\n'.encode('utf-8')
            split = event.index('é'.encode('utf-8')) + 1
            path.write_bytes(event[:split])
            with JournalReader(path, poll_interval=0) as reader:
                self.assertEqual(reader.readline(), '')
                with path.open('ab') as writer:
                    writer.write(event[split:])
                self.assertEqual(reader.readline(), event.decode('utf-8'))
