"""Read complete journal events and follow chronological journal rotation."""
import re
from pathlib import Path
from time import monotonic

JOURNAL_NAME = re.compile(r"Journal\.\d{4}-\d{2}-\d{2}T\d{6}\.\d{2}\.log")


class JournalReader:
    def __init__(self, path, position=0, follow=True, on_rotate=None, poll_interval=1):
        self.path = Path(path)
        self.file = self.path.open(encoding="utf-8")
        self.file.seek(position)
        self.follow = follow
        self.on_rotate = on_rotate
        self.poll_interval = poll_interval
        self.last_poll = float("-inf")

    def readline(self):
        position = self.file.tell()
        try:
            line = self.file.readline()
        except UnicodeDecodeError:
            # A write can stop in the middle of a multi-byte UTF-8 character.
            self.file.seek(position)
            return ""
        if line and not line.endswith("\n"):
            self.file.seek(position)
            return ""
        if line or not self.follow:
            return line
        now = monotonic()
        if now - self.last_poll < self.poll_interval:
            return ""
        self.last_poll = now
        candidates = sorted(p for p in self.path.parent.iterdir()
                            if p.name > self.path.name and JOURNAL_NAME.fullmatch(p.name) and p.is_file())
        if candidates:
            # Oldest unseen file first: never skip an intermediate journal.
            candidate = candidates[0]
            try:
                new_file = candidate.open(encoding="utf-8")
            except OSError:
                return ""  # A writer may still be creating/moving it; retry next poll.
            self.file.close()
            self.file = new_file
            self.path = candidate
            if self.on_rotate:
                self.on_rotate(candidate)
            return self.readline()
        return ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.file.close()
