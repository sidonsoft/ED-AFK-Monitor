from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from unittest.mock import patch
import unittest
from journal_reader import JournalReader


class JournalFollowingTests(unittest.TestCase):
    def test_drains_old_file_then_each_successor_from_start(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / 'Journal.2026-10-20T120000.01.log'
            second = root / 'Journal.2026-10-20T130000.01.log'
            third = root / 'Journal.2026-10-20T140000.01.log'
            with first.open('w', encoding='utf-8', newline='\r\n') as writer:
                writer.write('hístory\nlive\n')
            with first.open(encoding='utf-8') as reader:
                self.assertEqual(reader.readline(), 'hístory\n')
                position = reader.tell()
            rotations = []
            with JournalReader(first, position=position, on_rotate=rotations.append, poll_interval=0) as reader:
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

    def test_abandoned_unterminated_write_does_not_block_rotation(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / 'Journal.2026-10-20T120000.01.log'
            second = root / 'Journal.2026-10-20T130000.01.log'
            # A crashed game can leave a complete or partial final line without a newline.
            first.write_text('{"event":"Bounty"}\n{"event":"LoadGame"}', encoding='utf-8')
            with JournalReader(first, poll_interval=0, rotation_grace=0.05) as reader:
                self.assertEqual(reader.readline(), '{"event":"Bounty"}\n')
                second.write_text('next\n', encoding='utf-8')
                self.assertEqual(reader.readline(), '')  # still allowing the writer to finish
                sleep(0.08)
                self.assertEqual(reader.readline(), 'next\n')
                self.assertEqual(reader.path, second)

    def test_unavailable_journal_folder_is_retried_instead_of_raising(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'Journal.2026-10-20T120000.01.log'
            path.write_text('one\n', encoding='utf-8')
            with JournalReader(path, poll_interval=0) as reader:
                self.assertEqual(reader.readline(), 'one\n')
                with patch.object(Path, 'iterdir', side_effect=OSError('unavailable')):
                    self.assertEqual(reader.readline(), '')
                path.with_name('Journal.2026-10-20T130000.01.log').write_text('two\n', encoding='utf-8')
                self.assertEqual(reader.readline(), 'two\n')

    def test_invalid_utf8_byte_does_not_stall_later_events(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'Journal.2026-10-20T120000.01.log'
            path.write_bytes(b'{"event":"Commander","Name":"Andr\xe9"}\n'
                             b'not-json\xffbytes\n'
                             b'{"event":"LoadGame"}\n')
            with JournalReader(path, follow=False) as reader:
                self.assertIn('"event":"Commander"', reader.readline())
                self.assertIn('not-json', reader.readline())
                self.assertEqual(reader.readline(), '{"event":"LoadGame"}\n')
                self.assertEqual(reader.readline(), '')
