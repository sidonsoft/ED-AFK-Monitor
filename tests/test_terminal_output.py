import io
import os
from unittest.mock import Mock, patch
from types import SimpleNamespace
import unittest

from terminal_output import StatusLogger, supports_ansi


class TerminalOutputTests(unittest.TestCase):
    def make_ansi_logger(self, **kwargs):
        stream = io.StringIO()
        with patch('terminal_output.supports_ansi', return_value=True):
            logger = StatusLogger(stream=stream, **kwargs)
        return logger, stream

    def test_redirected_output_is_plain_and_does_not_query_terminal_size(self):
        stream = io.StringIO()
        logger = StatusLogger(stream=stream)
        with patch('terminal_output.shutil.get_terminal_size', side_effect=AssertionError('no terminal')):
            logger.hide_cursor()
            logger.log('\x1b[31mHull warning 🛠️ for André\x1b[0m')
            for _ in range(3):
                logger.set_status('live stats')
            logger.show_cursor()
        self.assertEqual(stream.getvalue(), 'Hull warning  for Andre\n')

    def test_explicit_plain_mode_overrides_modern_terminal(self):
        with patch('terminal_output.supports_ansi', return_value=True):
            stream = io.StringIO()
            logger = StatusLogger(plain=True, stream=stream)
        logger.set_status('ignored')
        logger.log('one line')
        self.assertEqual(stream.getvalue(), 'one line\n')

    def test_dumb_terminal_uses_plain_output(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        with patch.dict(os.environ, {'TERM': 'dumb'}):
            self.assertFalse(supports_ansi(stream))

    def test_no_terminal_size_falls_back_instead_of_crashing(self):
        logger, stream = self.make_ansi_logger()
        with patch('os.get_terminal_size', side_effect=OSError()):
            logger.set_status('session stats')
            logger.log('event')
        self.assertIn('event', stream.getvalue())

    def test_one_column_terminal_cannot_loop_forever_or_split_ansi(self):
        logger, stream = self.make_ansi_logger()
        with patch('terminal_output.shutil.get_terminal_size', return_value=os.terminal_size((1, 24))):
            logger.set_status('\x1b[31mA long status\x1b[0m')
        self.assertEqual(logger.get_columns(logger._status), 1)
        stripped = logger.ANSI_REGEX_STRIP.sub('', stream.getvalue())
        self.assertNotIn('\x1b', stripped)
        self.assertIn('>', stripped)

    def test_resize_clears_only_lines_occupied_by_old_status(self):
        logger, stream = self.make_ansi_logger(safe_margin=0)
        with patch('terminal_output.shutil.get_terminal_size', return_value=os.terminal_size((80, 24))):
            logger.set_status('x' * 40)
        with patch('terminal_output.shutil.get_terminal_size', return_value=os.terminal_size((10, 24))):
            self.assertEqual(logger._clear_lines().count(logger.ANSI_CURSOR_UP), 3)

    def test_combining_marks_do_not_consume_extra_columns(self):
        logger, _ = self.make_ansi_logger()
        self.assertEqual(logger.get_columns('Andre\u0301'), 5)
        self.assertEqual(logger.get_columns('⚠️'), 2)

    def test_legacy_encoding_does_not_raise_on_emoji(self):
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding='cp1252')
        with patch('terminal_output.supports_ansi', return_value=True):
            logger = StatusLogger(stream=stream)
        logger.log('Hull warning 🛠️')
        self.assertIn(b'Hull warning', output.getvalue())

    def test_truncation_counts_emoji_presentation_across_tokens(self):
        logger, stream = self.make_ansi_logger(safe_margin=0)
        with patch('terminal_output.shutil.get_terminal_size', return_value=os.terminal_size((3, 24))):
            logger.set_status('⚠️xx')
        self.assertLessEqual(logger.get_columns(logger._status), 3)

    def test_windows_console_enables_vt_or_falls_back_without_registry_changes(self):
        stream = SimpleNamespace(isatty=lambda: True, fileno=lambda: 7)
        kernel = SimpleNamespace(GetConsoleMode=Mock(return_value=1), SetConsoleMode=Mock(return_value=1))
        with patch.dict(os.environ, {'TERM': ''}), \
             patch('terminal_output.os.name', 'nt'), \
             patch.dict('sys.modules', {'msvcrt': SimpleNamespace(get_osfhandle=lambda fd: 123)}), \
             patch('terminal_output.ctypes.windll', SimpleNamespace(kernel32=kernel), create=True):
            self.assertTrue(supports_ansi(stream))
            self.assertEqual(kernel.SetConsoleMode.call_args.args[1], 4)
            kernel.SetConsoleMode.return_value = 0
            self.assertFalse(supports_ansi(stream))


class AlertSoundTests(unittest.TestCase):
    def test_bell_writes_control_g(self):
        stream = io.StringIO()
        logger = StatusLogger(stream=stream)
        self.assertTrue(logger.beep("bell"))
        self.assertEqual(stream.getvalue(), "\a")

    def test_none_and_empty_never_play(self):
        stream = io.StringIO()
        logger = StatusLogger(stream=stream)
        self.assertFalse(logger.beep("none"))
        self.assertFalse(logger.beep(""))
        self.assertEqual(stream.getvalue(), "")

    def test_non_bell_without_winsound_is_inert(self):
        stream = io.StringIO()
        logger = StatusLogger(stream=stream)
        import builtins
        real_import = builtins.__import__
        def fake_import(name, *args, **kwargs):
            if name == "winsound":
                raise ImportError
            return real_import(name, *args, **kwargs)
        builtins.__import__ = fake_import
        try:
            self.assertFalse(logger.beep("alarm.wav"))
        finally:
            builtins.__import__ = real_import
        self.assertEqual(stream.getvalue(), "")
