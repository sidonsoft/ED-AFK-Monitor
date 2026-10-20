"""Track massacre missions, their target factions and local kill progress."""
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import time

FILE_VERSION = 1
SAVE_INTERVAL = 5


def faction_key(name):
    return str(name or "").strip().casefold()


def _int(value, default=0):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _short(value, width=24):
    text = str(value or "")
    return text if len(text) <= width else text[:width - 3] + "..."


def _expiry_text(expiry, now=None):
    if not expiry:
        return ""
    try:
        when = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc) if now is None else now
    seconds = int((when - now).total_seconds())
    if seconds <= 0:
        return "expired"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}"


@dataclass
class Mission:
    """One massacre mission, with the details the journal only gives on accept."""
    mission_id: int
    name: str = ""
    faction: str = ""
    target: str = ""
    kill_count: int = 0
    wing: bool = False
    expiry: str = ""
    reward: int = 0
    accepted: float = 0.0
    kills: int = 0
    done: bool = False
    known: bool = False
    fresh: bool = False

    def as_dict(self):
        return {
            "id": self.mission_id,
            "name": self.name,
            "faction": self.faction,
            "target": self.target,
            "kill_count": self.kill_count,
            "wing": self.wing,
            "expiry": self.expiry,
            "reward": self.reward,
            "accepted": self.accepted,
            "kills": self.kills,
            "done": self.done,
            "known": self.known,
        }

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            return None
        mission_id = _int(raw.get("id"))
        if not mission_id:
            return None
        return cls(
            mission_id=mission_id,
            name=str(raw.get("name") or ""),
            faction=str(raw.get("faction") or ""),
            target=str(raw.get("target") or ""),
            kill_count=max(0, _int(raw.get("kill_count"))),
            wing=bool(raw.get("wing")),
            expiry=str(raw.get("expiry") or ""),
            reward=max(0, _int(raw.get("reward"))),
            accepted=max(0.0, _float(raw.get("accepted"))),
            kills=max(0, _int(raw.get("kills"))),
            done=bool(raw.get("done")),
            known=bool(raw.get("known")),
        )


class MissionLedger:
    """Mission state kept across journal restarts and reported as a spread.

    The journal only gives faction and kill count in MissionAccepted, so those details
    are persisted. Kills are credited by target faction, because one kill advances every
    mission for that faction; targets that are not mission targets cannot be told apart.
    """

    def __init__(self, path, commander="", profile="", save_interval=SAVE_INTERVAL):
        self.path = Path(path)
        self.commander = commander or ""
        self.profile = profile or ""
        self.save_interval = save_interval
        self.missions = {}
        self.warnings = []
        self.preloading = False
        self._dirty = False
        self._broken = False
        self._last_save = time()
        self.load()

    # ---- journal events -------------------------------------------------

    def accept(self, event):
        name = str(event.get("Name") or "")
        if "Mission_Massacre" not in name:
            return None
        mission_id = _int(event.get("MissionID"))
        if not mission_id:
            return None
        mission = self.missions.get(mission_id)
        if mission is None:
            mission = Mission(mission_id=mission_id, accepted=time())
            self.missions[mission_id] = mission
        was_known = mission.known
        mission.name = name or mission.name
        if event.get("Faction"):
            mission.faction = str(event["Faction"])
        if event.get("TargetFaction"):
            mission.target = str(event["TargetFaction"])
        if _int(event.get("KillCount")) > 0:
            mission.kill_count = _int(event.get("KillCount"))
        mission.wing = bool(event.get("Wing", mission.wing))
        if event.get("Expiry"):
            mission.expiry = str(event["Expiry"])
        if _int(event.get("Reward")) > 0:
            mission.reward = _int(event.get("Reward"))
        mission.known = bool(mission.target and mission.kill_count > 0)
        if mission.known and not was_known and self.preloading:
            # Kills for an already persisted mission must not be counted twice when the
            # journal is replayed; a mission first seen in this replay has no stored kills.
            mission.fresh = True
        self._clamp(mission)
        self._dirty = True
        self.save()
        return mission

    def reconcile(self, active, timestamp=None):
        """Match the game's active list, keeping details for missions already known."""
        active_ids = set()
        for entry in active or ():
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("Name") or "")
            if "Mission_Massacre" not in name:
                continue
            mission_id = _int(entry.get("MissionID"))
            if not mission_id:
                continue
            active_ids.add(mission_id)
            mission = self.missions.get(mission_id)
            if mission is None:
                mission = Mission(mission_id=mission_id, name=name, accepted=time())
                self.missions[mission_id] = mission
                self._dirty = True
            if not mission.expiry:
                expires = _int(entry.get("Expires"))
                if expires > 0 and isinstance(timestamp, datetime):
                    mission.expiry = (timestamp + timedelta(seconds=expires)).isoformat()
        removed = [mission for mission_id, mission in self.missions.items()
                   if mission_id not in active_ids]
        for mission in removed:
            del self.missions[mission.mission_id]
        if removed:
            self._dirty = True
        self.save()
        return removed

    def kill(self, victim_faction, alternate=None):
        keys = {faction_key(victim_faction), faction_key(alternate)}
        keys.discard("")
        if not keys:
            return []
        credited = []
        for mission in self.missions.values():
            if mission.done or not mission.known:
                continue
            if faction_key(mission.target) not in keys:
                continue
            if self.preloading and not mission.fresh:
                continue
            if mission.kills >= mission.kill_count:
                continue
            mission.kills += 1
            credited.append(mission)
        if credited:
            self._dirty = True
        return credited

    def redirected(self, mission_id):
        mission = self.missions.get(_int(mission_id))
        if mission is None:
            return None
        mission.done = True
        mission.fresh = False
        if mission.kill_count:
            mission.kills = max(mission.kills, mission.kill_count)
        self._dirty = True
        self.save()
        return mission

    def close(self, mission_id):
        mission = self.missions.pop(_int(mission_id), None)
        if mission is not None:
            self._dirty = True
            self.save()
        return mission

    def end_preload(self):
        self.preloading = False
        for mission in self.missions.values():
            mission.fresh = False
        if self._dirty:
            self.save()

    # ---- reporting ------------------------------------------------------

    def progress(self):
        done = sum(1 for mission in self.missions.values() if mission.done)
        return done, len(self.missions)

    def count(self):
        return len(self.missions)

    def has_missions(self):
        return bool(self.missions)

    def status_line(self):
        done, total = self.progress()
        return f"{done}/{total}"

    def spread(self):
        groups = {}
        unknown = 0
        open_missions = [mission for mission in self.missions.values() if not mission.done]
        for mission in open_missions:
            if not mission.known:
                unknown += 1
                continue
            groups.setdefault(mission.target, []).append(mission)
        rows = []
        for target, group in groups.items():
            need = max(mission.kill_count for mission in group)
            delivered = max(min(mission.kills, mission.kill_count) for mission in group)
            rows.append({
                "faction": target,
                "missions": len(group),
                "wing": sum(1 for mission in group if mission.wing),
                "need": need,
                "done": min(delivered, need),
                "left": max(0, need - delivered),
            })
        rows.sort(key=lambda row: (-row["left"], row["faction"].casefold()))
        done_total, total = self.progress()
        return {
            "rows": rows,
            "unknown": unknown,
            "done": done_total,
            "total": total,
            "wing": sum(1 for mission in self.missions.values() if mission.wing),
            "left": sum(row["left"] for row in rows),
            "sum_kills": sum(mission.kill_count for mission in open_missions if mission.known),
            "reward": sum(mission.reward for mission in self.missions.values()),
        }

    def report_text(self, include_missions=False, title=False):
        data = self.spread()
        lines = []
        if title:
            lines.append(f"{self.commander or 'Unknown commander'} (profile: {self.profile or 'default'})")
        if not data["total"]:
            lines.append("No active massacre missions.")
            return "\n".join(lines)
        lines.append(f"Mission spread: {data['done']}/{data['total']} complete"
                     f" | {data['wing']} wing | {data['left']} kills left")
        lines.append(f"  {'Faction':<24} {'Open':>4} {'Wing':>4} {'Need':>5} {'Done':>5} {'Left':>5}")
        for row in data["rows"]:
            lines.append(f"  {_short(row['faction']):<24} {row['missions']:>4} {row['wing']:>4}"
                         f" {row['need']:>5} {row['done']:>5} {row['left']:>5}")
        if data["unknown"]:
            lines.append(f"  {data['unknown']} mission(s) with unknown target/faction")
        lines.append(f"  Unique kills left: {data['left']:,}"
                     f" (sum of mission kills: {data['sum_kills']:,})")
        if data["reward"]:
            lines.append(f"  Mission rewards: {data['reward']:,} cr")
        if include_missions:
            lines.append("")
            lines.append(f"  {'Mission ID':>10}  {'Target faction':<24} {'Kills':>9} {'Wing':>4} {'Expires':>7}")
            ordered = sorted(self.missions.values(),
                             key=lambda mission: (mission.done, mission.target.casefold(), mission.mission_id))
            for mission in ordered:
                target = mission.target if mission.known else "unknown"
                kills = f"{mission.kills}/{mission.kill_count}" if mission.known else "-"
                if mission.done:
                    kills = "done"
                lines.append(f"  {mission.mission_id:>10}  {_short(target):<24} {kills:>9}"
                             f" {'yes' if mission.wing else '-':>4} {_expiry_text(mission.expiry):>7}")
        return "\n".join(lines)

    def report_discord(self):
        data = self.spread()
        if not data["total"]:
            return "No active massacre missions."
        lines = [f"**Mission spread** · {data['done']}/{data['total']} complete"
                 f" · {data['wing']} wing · **{data['left']}** kills left"]
        for row in data["rows"]:
            lines.append(f"• **{row['faction']}** — {row['missions']} open ({row['wing']} wing)"
                         f" · {row['done']}/{row['need']} · {row['left']} left")
        if data["unknown"]:
            lines.append(f"• {data['unknown']} mission(s) with unknown target/faction")
        if data["reward"]:
            lines.append(f"Rewards: {data['reward']:,} cr")
        return "\n".join(lines)

    # ---- persistence ----------------------------------------------------

    def load(self):
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.warnings.append(f"Missions: ignoring unreadable ledger {self.path.name}")
            return
        if not isinstance(data, dict) or data.get("version") != FILE_VERSION:
            self.warnings.append(f"Missions: ignoring unrecognised ledger {self.path.name}")
            return
        if not self.commander:
            self.commander = str(data.get("commander") or "")
        if not self.profile:
            self.profile = str(data.get("profile") or "")
        for raw in data.get("missions") or ():
            mission = Mission.from_dict(raw)
            if mission is not None:
                self._clamp(mission)
                self.missions[mission.mission_id] = mission

    def save(self):
        if self._broken:
            return False
        if not self.missions and not self.path.exists():
            return True
        payload = {
            "version": FILE_VERSION,
            "commander": self.commander,
            "profile": self.profile,
            "updated": time(),
            "missions": [mission.as_dict() for mission in self.missions.values()],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            with open(temp, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.path)
        except OSError:
            self._broken = True
            self.warnings.append("Missions: could not save the ledger; progress is kept in memory")
            return False
        self._dirty = False
        self._last_save = time()
        return True

    def maybe_save(self):
        if self._dirty and not self._broken and time() - self._last_save >= self.save_interval:
            self.save()

    @staticmethod
    def _clamp(mission):
        if mission.kill_count:
            mission.kills = min(mission.kills, mission.kill_count)
