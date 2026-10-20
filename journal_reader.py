"""Read complete journal events and follow chronological journal rotation."""
import re
import json
from pathlib import Path
from time import monotonic

JOURNAL_NAME = re.compile(r"Journal\.\d{4}-\d{2}-\d{2}T\d{6}\.\d{2}\.log")
JOURNAL_ERRORS = "replace"  # A corrupt byte must not stall the reader forever.


def read_complete_line(file):
    """Leave unfinished text/UTF-8 writes available for the next poll."""
    position = file.tell()
    try:
        line = file.readline()
    except UnicodeDecodeError:
        file.seek(position)
        return ""
    if line and not line.endswith("\n"):
        file.seek(position)
        return ""
    return line


def commander_from_line(line):
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if isinstance(event, dict) and event.get("event") == "Commander":
        name = event.get("Name")
        return name if isinstance(name, str) and name else None
    return None


class JournalReader:
    def __init__(self, path, position=0, follow=True, on_rotate=None, poll_interval=1,
                 rotation_grace=5):
        self.path = Path(path)
        self.file = self.path.open(encoding="utf-8", errors=JOURNAL_ERRORS)
        self.file.seek(position)
        self.follow = follow
        self.on_rotate = on_rotate
        self.poll_interval = poll_interval
        self.rotation_grace = rotation_grace
        self.last_poll = float("-inf")
        self._tail_since = None

    def _has_partial(self):
        position = self.file.tell()
        # TextIO tell cookies must be restored through the same text stream.
        self.file.seek(0, 2)
        has_partial = self.file.tell() != position
        self.file.seek(position)
        return has_partial

    def readline(self):
        line = read_complete_line(self.file)
        if line:
            self._tail_since = None
            return line
        if not self.follow:
            return line
        now = monotonic()
        if now - self.last_poll < self.poll_interval:
            return ""
        self.last_poll = now
        try:
            candidates = sorted(p for p in self.path.parent.iterdir()
                                if p.name > self.path.name and JOURNAL_NAME.fullmatch(p.name) and p.is_file())
        except OSError:
            return ""  # The folder may be unavailable; keep polling rather than failing.
        if not candidates:
            self._tail_since = None
            return ""
        if self._has_partial():
            # A newer journal exists but this line has no newline yet. Allow the writer a
            # brief moment to finish it, then switch: an abandoned write must not block
            # following the next journal.
            if self._tail_since is None:
                self._tail_since = now
            if now - self._tail_since < self.rotation_grace:
                return ""
        # Oldest unseen file first: never skip an intermediate journal.
        candidate = candidates[0]
        try:
            new_file = candidate.open(encoding="utf-8", errors=JOURNAL_ERRORS)
        except OSError:
            return ""  # A writer may still be creating/moving it; retry next poll.
        self.file.close()
        self.file = new_file
        self.path = candidate
        self._tail_since = None
        if self.on_rotate:
            self.on_rotate(candidate)
        return self.readline()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.file.close()


def find_mission_accepts(folder, mission_ids, max_files=20):
    """Find MissionAccepted events for the given ids in recent journals, newest first."""
    wanted = set()
    for value in mission_ids:
        try:
            wanted.add(int(value))
        except (TypeError, ValueError):
            continue
    found = {}
    if not wanted:
        return found
    try:
        paths = sorted((path for path in Path(folder).iterdir()
                        if JOURNAL_NAME.fullmatch(path.name) and path.is_file()),
                       reverse=True)
    except OSError:
        return found
    for path in paths[:max_files]:
        if wanted <= found.keys():
            break
        try:
            with path.open(encoding="utf-8", errors="replace") as file:
                for line in file:
                    if "MissionAccepted" not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict) or event.get("event") != "MissionAccepted":
                        continue
                    try:
                        mission_id = int(event.get("MissionID"))
                    except (TypeError, ValueError):
                        continue
                    if mission_id in wanted and mission_id not in found:
                        found[mission_id] = event
        except OSError:
            continue
    return found
