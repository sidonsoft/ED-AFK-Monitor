"""Track massacre missions, their target factions and local kill progress."""
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import time

FILE_VERSION = 1
SAVE_INTERVAL = 5
MAX_IMPORT = 100

HEADER_ALIASES = {
    "id": {"id", "missionid"},
    "target": {"target", "targetfaction", "faction", "targetfactionname"},
    "kills": {"kills", "killcount", "count", "required", "killsrequired", "killreq"},
    "wing": {"wing", "shared", "iswing"},
    "issuer": {"issuer", "missionfaction", "giver", "factionissuer"},
    "expiry": {"expiry", "expires"},
    "reward": {"reward", "credits"},
}
WING_VALUES = {"1", "true", "yes", "y", "wing", "shared"}
SOLO_VALUES = {"0", "false", "no", "n", "solo", "single"}


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


def _column_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _cell(value):
    return str(value or "").strip().strip('"').strip("'").strip()


def _wing_value(value):
    text = _column_key(value)
    if not text:
        return False
    if text in WING_VALUES:
        return True
    if text in SOLO_VALUES:
        return False
    raise ValueError(f"invalid wing value '{value}'")


def _looks_numeric(value):
    try:
        int(str(value).replace(",", "").strip())
        return True
    except ValueError:
        return False


def _header_value(values, name):
    for alias in HEADER_ALIASES[name]:
        if values.get(alias):
            return values[alias]
    return ""


def _row_from_position(parts):
    if len(parts) < 2:
        raise ValueError("expected 'TargetFaction, Kills [, Wing]' or 'MissionID, TargetFaction, Kills [, Wing]'")
    if _looks_numeric(parts[0]) and len(parts) >= 3:
        mission_id = int(parts[0].replace(",", ""))
        target, kills_text = parts[1], parts[2]
        wing_text = parts[3] if len(parts) > 3 else ""
    else:
        mission_id = None
        target, kills_text = parts[0], parts[1]
        wing_text = parts[2] if len(parts) > 2 else ""
    if mission_id is not None and mission_id <= 0:
        raise ValueError("mission id must be positive")
    target = _cell(target)
    if not target:
        raise ValueError("target faction is missing")
    try:
        kills = int(kills_text.replace(",", "").strip())
    except ValueError:
        raise ValueError(f"invalid kills value '{kills_text}'") from None
    if kills <= 0:
        raise ValueError("kills must be positive")
    return {"id": mission_id, "target": target, "kills": kills,
            "wing": _wing_value(wing_text), "issuer": "", "expiry": "", "reward": 0}


def _row_from_header(parts, header):
    values = {key: (parts[index] if index < len(parts) else "")
              for index, key in enumerate(header) if key}
    target = _cell(_header_value(values, "target"))
    if not target:
        raise ValueError("target faction is missing")
    kills_text = _header_value(values, "kills")
    try:
        kills = int(kills_text.replace(",", "").strip())
    except ValueError:
        raise ValueError(f"invalid kills value '{kills_text}'") from None
    if kills <= 0:
        raise ValueError("kills must be positive")
    id_text = _header_value(values, "id")
    mission_id = int(id_text) if id_text else None
    if mission_id is not None and mission_id <= 0:
        raise ValueError("mission id must be positive")
    expiry = _header_value(values, "expiry")
    if expiry:
        try:
            expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00")).isoformat()
        except ValueError:
            raise ValueError(f"invalid expiry '{expiry}'") from None
    reward_text = _header_value(values, "reward")
    reward = _int(reward_text.replace(",", "")) if reward_text else 0
    return {"id": mission_id, "target": target, "kills": kills,
            "wing": _wing_value(_header_value(values, "wing")),
            "issuer": _cell(_header_value(values, "issuer")),
            "expiry": expiry, "reward": max(0, reward)}


def parse_mission_list(text, limit=MAX_IMPORT):
    """Parse pasted/CSV mission lists into rows; returns (rows, errors)."""
    rows = []
    errors = []
    header = None
    for number, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [_cell(part) for part in re.split(r"[,;\t]", line)]
        while parts and not parts[-1]:
            parts.pop()
        keys = {_column_key(part) for part in parts}
        if header is None and keys & HEADER_ALIASES["target"] and keys & HEADER_ALIASES["kills"]:
            header = [_column_key(part) for part in parts]
            continue
        if len(rows) >= limit:
            errors.append(f"too many missions (limit {limit})")
            break
        try:
            row = _row_from_header(parts, header) if header else _row_from_position(parts)
        except ValueError as error:
            errors.append(f"line {number}: {error}")
            continue
        rows.append(row)
    if not rows and not errors:
        errors.append("no missions found")
    return rows, errors


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
    manual: bool = False

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
            "manual": self.manual,
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
            manual=bool(raw.get("manual")),
        )


class MissionLedger:
    """Mission state kept across journal restarts and reported as a spread.

    The journal only gives faction and kill count in MissionAccepted, so those details
    are persisted. Kills are credited by target faction, because one kill advances every
    mission for that faction; targets that are not mission targets cannot be told apart.
    """

    def __init__(self, path, commander="", profile="", save_interval=SAVE_INTERVAL,
                 details_lookup=None):
        self.path = Path(path)
        self.commander = commander or ""
        self.profile = profile or ""
        self.save_interval = save_interval
        self.details_lookup = details_lookup
        self.missions = {}
        self.warnings = []
        self.backfilled = 0
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
        self._apply(mission, event)
        self._dirty = True
        self.save()
        return mission

    def _apply(self, mission, event):
        name = str(event.get("Name") or "")
        was_known = mission.known
        if name:
            mission.name = name
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
            mission.reward = _int(event["Reward"])
        mission.known = bool(mission.target and mission.kill_count > 0)
        if mission.known and not was_known and self.preloading:
            # Kills for an already persisted mission must not be counted twice when the
            # journal is replayed; a mission first seen in this replay has no stored kills.
            mission.fresh = True
        self._clamp(mission)
        return mission

    def reconcile(self, active, timestamp=None):
        """Match the game's active list, recovering details from earlier journals."""
        self.backfilled = 0
        entries = []
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
            entries.append((mission_id, entry))
            active_ids.add(mission_id)

        missing = [mission_id for mission_id, _ in entries
                   if mission_id not in self.missions or not self.missions[mission_id].known]
        if missing and self.details_lookup is not None:
            try:
                events = self.details_lookup(missing) or {}
            except Exception:
                events = {}
            for mission_id, event in events.items():
                mission = self.missions.get(_int(mission_id))
                if mission is None:
                    mission = Mission(mission_id=_int(mission_id), accepted=time())
                    self.missions[mission.mission_id] = mission
                if isinstance(event, dict):
                    self._apply(mission, event)
                    if mission.known:
                        self.backfilled += 1

        for mission_id, entry in entries:
            mission = self.missions.get(mission_id)
            if mission is None:
                mission = Mission(mission_id=mission_id,
                                  name=str(entry.get("Name") or ""),
                                  accepted=time())
                self.missions[mission_id] = mission
                self._dirty = True
            if not mission.expiry:
                expires = _int(entry.get("Expires"))
                if expires > 0 and isinstance(timestamp, datetime):
                    mission.expiry = (timestamp + timedelta(seconds=expires)).isoformat()

        removed = [mission for mission in self.missions.values()
                   if not mission.manual and mission.mission_id not in active_ids]
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
            if mission.manual and mission.kills >= mission.kill_count:
                mission.done = True
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

    # ---- import ---------------------------------------------------------

    def import_text(self, text):
        """Replace imported (manual) missions with this list, keeping saved progress."""
        rows, errors = parse_mission_list(text)
        if errors:
            return 0, 0, errors
        existing = [mission for mission in self.missions.values() if mission.manual]
        pool = {}
        for mission in existing:
            key = (faction_key(mission.target), mission.kill_count, mission.wing)
            pool.setdefault(key, []).append(mission)
        for mission in existing:
            del self.missions[mission.mission_id]

        added = 0
        next_manual_id = -1
        for row in rows:
            if row["id"] is not None:
                mission = self.missions.get(row["id"])
                if mission is None:
                    mission = Mission(mission_id=row["id"], name="Mission_Massacre",
                                      accepted=time())
                    self.missions[row["id"]] = mission
                mission.manual = False
            else:
                while next_manual_id in self.missions:
                    next_manual_id -= 1
                mission = Mission(mission_id=next_manual_id, name="Mission_Massacre",
                                  accepted=time(), manual=True)
                key = (faction_key(row["target"]), row["kills"], row["wing"])
                kept = pool.get(key)
                if kept:
                    old = kept.pop()
                    mission.kills = old.kills
                    mission.done = old.done
                    mission.accepted = old.accepted
                self.missions[next_manual_id] = mission
                next_manual_id -= 1
            mission.name = mission.name or "Mission_Massacre"
            mission.target = row["target"]
            mission.kill_count = row["kills"]
            mission.wing = row["wing"]
            if row["issuer"]:
                mission.faction = row["issuer"]
            if row["expiry"]:
                mission.expiry = row["expiry"]
            if row["reward"]:
                mission.reward = row["reward"]
            mission.known = bool(mission.target and mission.kill_count > 0)
            self._clamp(mission)
            added += 1
        self._dirty = True
        self.save()
        return added, len(existing), []

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
            "manual": sum(1 for mission in self.missions.values() if mission.manual),
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
        imported = f" | {data['manual']} imported" if data["manual"] else ""
        lines.append(f"Mission spread: {data['done']}/{data['total']} complete"
                     f" | {data['wing']} wing | {data['left']} kills left{imported}")
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
                identifier = "imported" if mission.manual else str(mission.mission_id)
                lines.append(f"  {identifier:>10}  {_short(target):<24} {kills:>9}"
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

    def report_live(self, max_factions=10):
        """Compact mission block for the edited live session-stats message."""
        data = self.spread()
        if not data["total"]:
            return ""
        lines = [f"**Mission spread** {data['done']}/{data['total']} complete"
                 f" · **{data['left']}** kills left"]
        for row in data["rows"][:max_factions]:
            lines.append(f"• **{row['faction']}** {row['done']}/{row['need']}"
                         f" · {row['left']} left")
        hidden = len(data["rows"]) - max_factions
        if hidden > 0:
            lines.append(f"• …and {hidden} more faction(s)")
        if data["unknown"]:
            lines.append(f"• {data['unknown']} mission(s) with unknown target")
        return "\n".join(lines)

    def export(self):
        return {
            "commander": self.commander,
            "profile": self.profile,
            "updated": time(),
            "spread": self.spread(),
            "missions": [mission.as_dict() for mission in self.missions.values()],
        }

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
        if mission.manual and mission.kill_count and mission.kills >= mission.kill_count:
            mission.done = True
