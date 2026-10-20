"""Console output with a plain fallback for older consoles and redirected logs."""
import ctypes
import os
import re
import shutil
import sys
import unicodedata


def supports_ansi(stream):
    if not getattr(stream, "isatty", lambda: False)() or os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    try:
        import msvcrt
        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel = ctypes.windll.kernel32
        mode = ctypes.c_ulong()
        if not kernel.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
            return False
        # Enable virtual-terminal processing for this process; no registry changes.
        return bool(kernel.SetConsoleMode(ctypes.c_void_p(handle), mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


class StatusLogger:
    """Write log lines and an optional status line without requiring a modern console."""
    ANSI_CLEAR_LINE = "\x1b[2K"
    ANSI_COL_END = "\x1b[0m"
    ANSI_CURSOR_UP = "\x1b[A"
    ANSI_HIDE_CURSOR = "\x1b[?25l"
    ANSI_SHOW_CURSOR = "\x1b[?25h"
    ANSI_REGEX_STRIP = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    ANSI_TOKENS = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]|.", re.DOTALL)

    def __init__(self, clear_cols=False, pad_char="", safe_margin=2, plain=False, stream=None):
        self._stream = sys.stdout if stream is None else stream
        self.plain = plain or not supports_ansi(self._stream)
        self.endcol = self.ANSI_COL_END if clear_cols else ""
        self.pad = str(pad_char) if pad_char else ""
        self.safe = safe_margin
        self._status = ""
        self._prev = False

    def _write(self, text):
        if self.plain:
            text = self.ANSI_REGEX_STRIP.sub("", text).replace("\r", "")
            text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        # An older Windows code page may not encode emojis even when ANSI works.
        encoding = getattr(self._stream, "encoding", None)
        if encoding:
            text = text.encode(encoding, "replace").decode(encoding)
        self._stream.write(text)
        self._stream.flush()

    def get_columns(self, text):
        length = 0
        previous_width = 0
        for char in self.ANSI_REGEX_STRIP.sub("", text):
            if char == "\ufe0f" and previous_width == 1:
                # Emoji presentation widens otherwise single-column symbols.
                length += 1
                previous_width = 2
                continue
            if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
                continue
            previous_width = 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
            length += previous_width
        return length

    def _columns(self):
        return max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)

    def _clear_lines(self):
        if self.plain or not self._prev:
            return ""
        wraps = max(0, (self.get_columns(self._status) - 1) // self._columns())
        return self.ANSI_CLEAR_LINE + (self.ANSI_CURSOR_UP + self.ANSI_CLEAR_LINE) * wraps

    def set_status(self, msg):
        if self.plain:
            return  # Log lines remain useful; do not print the status every second.
        clear_lines = self._clear_lines()
        available = max(1, self._columns() - max(0, self.safe))
        if self.get_columns(msg) > available:
            parts = []
            for token in self.ANSI_TOKENS.findall(msg):
                if self.get_columns("".join(parts) + token) > available - 1:
                    break
                parts.append(token)
            output = "".join(parts) + ">"
        else:
            pad_width = self.get_columns(self.pad)
            padding = (available - self.get_columns(msg)) // pad_width if pad_width else 0
            output = msg + self.pad * padding
        self._write(f"{clear_lines}\r{output}{self.ANSI_COL_END}")
        self._status = output
        self._prev = True

    def clear_status(self):
        if self._prev:
            self._write(f"{self._clear_lines()}\r")
            self._status = ""
            self._prev = False

    def log(self, msg):
        if self._prev:
            self._write(f"{self._clear_lines()}\r{msg}\n{self._status}{self.endcol}")
        else:
            self._write(f"{msg}\n")

    def hide_cursor(self):
        if not self.plain:
            self._write(self.ANSI_HIDE_CURSOR)

    def show_cursor(self):
        if not self.plain:
            self._write(self.ANSI_SHOW_CURSOR)
