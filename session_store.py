"""Per-profile history of monitored sessions in a small SQLite database."""
import csv
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    started REAL NOT NULL,
    ended REAL NOT NULL,
    duration REAL NOT NULL,
    kills INTEGER NOT NULL,
    scans INTEGER NOT NULL,
    bounties INTEGER NOT NULL,
    merits INTEGER NOT NULL,
    missions_done INTEGER NOT NULL,
    missions_total INTEGER NOT NULL,
    journal TEXT NOT NULL,
    gamemode TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_started ON sessions (started);"""


class SessionStore:
    """Append-only session log; one database per profile, next to the mission ledger."""

    def __init__(self, path):
        self.path = Path(path)
        self.error = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path)
            self.connection.executescript(SCHEMA)
            self.connection.commit()
        except (sqlite3.Error, OSError) as failure:
            self.connection = None
            self.error = str(failure)

    def record(self, started, ended, stats, missions=None, journal="", gamemode=""):
        """Store one completed monitoring run; returns True when saved."""
        if self.connection is None:
            return False
        missions = missions or {}
        duration = max(0.0, float(ended) - float(started))
        try:
            self.connection.execute(
                "INSERT INTO sessions (started, ended, duration, kills, scans, bounties, merits,"
                " missions_done, missions_total, journal, gamemode) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (float(started), float(ended), duration,
                 int(stats.get("kills", 0)), int(stats.get("scans", 0)),
                 int(stats.get("bounties", 0)), int(stats.get("merits", 0)),
                 int(missions.get("done", 0)), int(missions.get("total", 0)),
                 str(journal), str(gamemode)))
            self.connection.commit()
            return True
        except sqlite3.Error as failure:
            self.error = str(failure)
            return False

    def prune(self, days):
        """Delete runs started more than `days` ago; returns rows removed."""
        if self.connection is None or days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        try:
            cursor = self.connection.execute("DELETE FROM sessions WHERE started < ?", (cutoff,))
            self.connection.commit()
            return cursor.rowcount
        except sqlite3.Error:
            return 0

    def sessions(self, limit=100):
        """Most recent runs first."""
        if self.connection is None:
            return []
        try:
            cursor = self.connection.execute(
                "SELECT started, ended, duration, kills, scans, bounties, merits,"
                " missions_done, missions_total, journal, gamemode"
                " FROM sessions ORDER BY started DESC LIMIT ?", (int(limit),))
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def totals(self):
        rows = self.sessions(limit=10_000)
        out = {"runs": len(rows), "duration": 0.0, "kills": 0, "scans": 0,
               "bounties": 0, "merits": 0}
        for row in rows:
            for key in ("duration", "kills", "scans", "bounties", "merits"):
                out[key] += row[key]
        out["kills_hour"] = round(out["kills"] / out["duration"] * 3600, 1) if out["duration"] > 0 else 0
        out["bounties_hour"] = round(out["bounties"] / out["duration"] * 3600) if out["duration"] > 0 else 0
        return out

    @staticmethod
    def _duration(seconds):
        seconds = int(seconds)
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"

    def report_text(self, title="", limit=50):
        rows = self.sessions(limit)
        if not rows:
            return "No saved sessions yet; they are recorded automatically when the monitor stops."
        lines = []
        if title:
            lines.append(title)
        lines.append(f"  {'Started (UTC)':<17} {'Duration':>8} {'Kills':>6} {'K/h':>6}"
                     f" {'Bounty cr':>11} {'cr/h':>9} {'Merits':>7} {'Missions':>9}")
        for row in rows:
            started = datetime.fromtimestamp(row["started"], timezone.utc).strftime("%Y-%m-%d %H:%M")
            hours = row["duration"] / 3600
            kills_hour = round(row["kills"] / hours, 1) if hours > 0 else 0
            bounties_hour = round(row["bounties"] / hours) if hours > 0 else 0
            lines.append(f"  {started:<17} {self._duration(row['duration']):>8} {row['kills']:>6}"
                         f" {kills_hour:>6} {row['bounties']:>11} {bounties_hour:>9}"
                         f" {row['merits']:>7} {row['missions_done']}/{row['missions_total']:<8}"
                         f"{'  ' + row['gamemode'] if row['gamemode'] else ''}")
        totals = self.totals()
        lines.append(f"  {'TOTAL':<17} {self._duration(totals['duration']):>8} {totals['kills']:>6}"
                     f" {totals['kills_hour']:>6} {totals['bounties']:>11} {totals['bounties_hour']:>9}"
                     f" {totals['merits']:>7}   ({totals['runs']} runs)")
        return "\n".join(lines)

    def export(self, path, limit=10_000):
        """Write saved sessions to a .json or .csv file; returns (rows, error)."""
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix not in (".json", ".csv"):
            return 0, f"Unsupported export format '{suffix}'; use .json or .csv"
        rows = self.sessions(limit)
        for row in rows:
            row["started_iso"] = datetime.fromtimestamp(row["started"], timezone.utc).isoformat(timespec="seconds")
            row["ended_iso"] = datetime.fromtimestamp(row["ended"], timezone.utc).isoformat(timespec="seconds")
            row["duration_hours"] = round(row["duration"] / 3600, 2)
        fields = ["started_iso", "ended_iso", "duration_hours", "kills", "scans", "bounties",
                  "merits", "missions_done", "missions_total", "gamemode", "journal"]
        if suffix == ".json":
            payload = json.dumps(rows, indent=1, ensure_ascii=False)
        else:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            payload = buffer.getvalue()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # bytes avoids Windows newline translation doubling the CSV \r\n
            path.write_bytes(payload.encode("utf-8"))
        except OSError as failure:
            return 0, f"Could not write {path}: {failure}"
        return len(rows), ""

    def share_text(self):
        """Compact copy-paste recap: totals plus the single best run."""
        totals = self.totals()
        if not totals["runs"]:
            return "No saved sessions yet; they are recorded automatically when the monitor stops."
        seconds = int(totals["duration"])
        lines = [f"ED AFK Monitor · {totals['runs']} sessions · {seconds // 3600}h{(seconds % 3600) // 60:02d}m"
                 f" · {totals['kills']:,} kills ({totals['kills_hour']}/h)"
                 f" · {totals['bounties']:,} cr ({totals['bounties_hour']:,} cr/h)"
                 f" · {totals['merits']:,} merits"]
        best = max((row for row in self.sessions(limit=10_000) if row["duration"] > 0),
                   key=lambda row: row["bounties"], default=None)
        if best is not None:
            started = datetime.fromtimestamp(best["started"], timezone.utc).strftime("%Y-%m-%d")
            lines.append(f"Best run: {started} · {self._duration(best['duration'])}"
                         f" · {best['kills']:,} kills · {best['bounties']:,} cr")
        return "\n".join(lines)

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except sqlite3.Error:
                pass
