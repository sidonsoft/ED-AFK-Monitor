"""Persist undelivered notifications so the next start can deliver them."""
import hashlib
import json
import os
import re
from pathlib import Path
from threading import Lock
from time import time

FILE_VERSION = 1
MAX_AGE = 24 * 60 * 60
MAX_ENTRIES = 200


def profile_directory_name(profile):
    """A filesystem-safe name that cannot collide between configuration profiles."""
    name = (profile or "").strip() or "default"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:40].strip("._") or "default"
    if safe != name:
        safe = f"{safe}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]}"
    return safe


def summarize_pending(directory, channels, max_age=MAX_AGE, now=None):
    """Read-only peek for --outbox: per-channel counts, oldest age and the
    next queued text. Never rewrites or deletes anything."""
    now = time() if now is None else now
    store = Outbox(directory, channels, max_age=max_age)
    pending, warnings = store.load(now=now)
    lines = []
    for channel in store.channels:
        entries = pending[channel]
        if not entries:
            continue
        oldest = min(entry["created"] for entry in entries)
        minutes = max(0, int((now - oldest) / 60))
        preview = entries[0]["text"].splitlines()[0][:80]
        lines.append(f"{channel}: {len(entries)} waiting, oldest {minutes} min ago")
        lines.append(f"   next up: {preview}")
    return lines, warnings


class Outbox:
    """One bounded JSON file per channel, replaced atomically after each change.

    Only plain-text notifications are stored; live status updates are deliberately
    left out because each new update replaces the previous one. The directory is
    created on the first saved message, so an unused outbox leaves no trace.
    """

    def __init__(self, directory, channels, max_entries=MAX_ENTRIES, max_age=MAX_AGE):
        self.directory = Path(directory)
        self.channels = tuple(dict.fromkeys(channels))
        self.max_entries = max_entries
        self.max_age = max_age
        self._entries = {}
        self._lock = Lock()
        self._loaded = False

    def path(self, channel):
        return self.directory / f"{channel}.json"

    def load(self, limit=None, now=None):
        """Return saved entries and warnings about anything that was discarded."""
        now = time() if now is None else now
        limit = self.max_entries if limit is None else max(0, limit)
        warnings = []
        pending = {}
        with self._lock:
            for channel in self.channels:
                entries, channel_warnings = self._read(channel, now, limit)
                pending[channel] = entries
                warnings.extend(channel_warnings)
            self._entries = {channel: list(entries) for channel, entries in pending.items()}
            self._loaded = True
        return pending, warnings

    def _read(self, channel, now, limit):
        path = self.path(channel)
        if not path.exists():
            return [], []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return [], [f"Notifications: ignoring unreadable saved-message file {path.name}"]
        if (not isinstance(data, dict) or data.get("version") != FILE_VERSION
                or not isinstance(data.get("entries"), list)):
            return [], [f"Notifications: ignoring unrecognised saved-message file {path.name}"]

        entries = []
        warnings = []
        seen = set()
        expired = skipped = duplicates = 0
        for raw in data["entries"]:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            entry_id = raw.get("id")
            text = raw.get("text")
            created = raw.get("created")
            if not isinstance(entry_id, str) or not entry_id or not isinstance(text, str) or not text:
                skipped += 1
                continue
            if isinstance(created, bool) or not isinstance(created, (int, float)) or created <= 0:
                skipped += 1
                continue
            if now - created > self.max_age:
                expired += 1
                continue
            if entry_id in seen:
                duplicates += 1
                continue
            seen.add(entry_id)
            entries.append({"id": entry_id, "text": text, "created": created})

        dropped = max(0, len(entries) - limit)
        if dropped:
            entries = entries[-limit:] if limit else []
        if expired:
            warnings.append(f"Notifications: discarded {expired} expired saved message(s)")
        if skipped:
            warnings.append(f"Notifications: ignored {skipped} unreadable saved message(s)")
        if duplicates:
            warnings.append(f"Notifications: ignored {duplicates} duplicate saved message(s)")
        if dropped:
            warnings.append(f"Notifications: discarded {dropped} older saved message(s)")
        return entries, warnings

    def add(self, channel, entry_id, text, created=None):
        if channel not in self.channels:
            raise KeyError(channel)
        self._ensure_loaded()
        with self._lock:
            entries = self._entries.setdefault(channel, [])
            entries.append({"id": entry_id, "text": text,
                            "created": time() if created is None else created})
            if len(entries) > self.max_entries:
                del entries[:len(entries) - self.max_entries]
            self._write(channel)

    def complete(self, channel, entry_id):
        if channel not in self.channels:
            return
        with self._lock:
            entries = self._entries.get(channel)
            if not entries:
                return
            remaining = [entry for entry in entries if entry["id"] != entry_id]
            if len(remaining) != len(entries):
                self._entries[channel] = remaining
                self._write(channel)

    def clear(self, channel):
        """Drop every queued entry on one channel (deliberate --outbox-clear,
        e.g. when a stale burst should never reach the channel)."""
        with self._lock:
            dropped = len(self._entries.get(channel, []))
            self._entries[channel] = []
            self._write(channel)
        return dropped

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def _write(self, channel):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path(channel)
        temp = path.with_name(f"{path.name}.tmp")
        payload = {"version": FILE_VERSION, "entries": self._entries.get(channel, [])}
        try:
            with open(temp, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, path)
        except OSError:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
