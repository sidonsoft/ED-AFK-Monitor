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
    origin_system: str = ""
    origin_station: str = ""
    target_system: str = ""

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
            "origin_system": self.origin_system,
            "origin_station": self.origin_station,
            "target_system": self.target_system,
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
            origin_system=str(raw.get("origin_system") or ""),
            origin_station=str(raw.get("origin_station") or ""),
            target_system=str(raw.get("target_system") or ""),
        )


class MissionLedger:
    """Mission state kept across journal restarts and reported as a spread.

    The journal only gives faction and kill count in MissionAccepted, so those details
    are persisted. One matching kill advances the oldest unfinished mission per issuer.
    Bounty events cannot distinguish mission targets from other wanted ships; a kill is
    not credited to a mission whose destination system is known and differs from the
    system the commander is currently in, and events carrying an exact MissionID credit
    that mission directly.
    """

    def __init__(self, path, commander="", profile="", save_interval=SAVE_INTERVAL,
                 details_lookup=None):
        self.path = Path(path)
        self.commander = commander or ""
        self.profile = profile or ""
        self.save_interval = save_interval
        self.details_lookup = details_lookup
        self.origin_system = ""
        self.origin_station = ""
        self.current_system = ""
        self.missions = {}
        self.warnings = []
        self.backfilled = 0
        self.preloading = False
        self._dirty = False
        self._broken = False
        self._last_save = time()
        self.load()

    # ---- journal events -------------------------------------------------

    def observe_location(self, event):
        """Remember acceptance location, without treating travel as a mission update."""
        if event.get("event") in {"Location", "FSDJump", "CarrierJump", "Docked"}:
            self.origin_system = str(event.get("StarSystem") or "")
            self.origin_station = str(event.get("StationName") or "")
        elif event.get("event") == "Undocked":
            self.origin_station = ""
        system = str(event.get("StarSystem") or "")
        if system:
            self.current_system = system

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
        mission.origin_system = mission.origin_system or self.origin_system
        mission.origin_station = mission.origin_station or self.origin_station
        self._dirty = True
        self.save()
        return mission

    def _apply(self, mission, event):
        name = str(event.get("Name") or "")
        was_known = mission.known
        if event.get("timestamp"):
            try:
                mission.accepted = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                pass
        for field, key in (("origin_system", "OriginSystem"),
                           ("origin_station", "OriginStation"),
                           ("target_system", "DestinationSystem")):
            if event.get(key):
                setattr(mission, field, str(event[key]))
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
                   if mission_id not in self.missions or not self.missions[mission_id].known
                   or not self.missions[mission_id].origin_system]
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

    def kill(self, victim_faction, alternate=None, mission_id=None):
        """Credit a kill; an exact MissionID (e.g. FactionKillBond) is definitive."""
        if mission_id is not None:
            mission = self.missions.get(_int(mission_id))
            if (mission is not None and mission.known and not mission.done
                    and mission.kills < mission.kill_count
                    and (not self.preloading or mission.fresh)):
                mission.kills += 1
                if mission.manual and mission.kills >= mission.kill_count:
                    mission.done = True
                self._dirty = True
                return [mission]
        keys = {faction_key(victim_faction), faction_key(alternate)}
        keys.discard("")
        if not keys:
            return []
        credited = []
        issuers = set()
        for mission in sorted(self.missions.values(), key=lambda m: (m.accepted, m.mission_id)):
            if mission.done or not mission.known:
                continue
            if faction_key(mission.target) not in keys:
                continue
            # A known mission destination must match the system the kill happened in.
            if (mission.target_system and self.current_system
                    and mission.target_system.casefold() != self.current_system.casefold()):
                continue
            if self.preloading and not mission.fresh:
                continue
            if mission.kills >= mission.kill_count:
                continue
            # Unknown issuers remain independent estimates; never merge unrelated imports.
            issuer = faction_key(mission.faction) or ("unknown", mission.mission_id)
            if issuer in issuers:
                continue
            issuers.add(issuer)
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

    def expiring(self, window_minutes, now=None):
        """Unfinished missions expiring within window_minutes, soonest first.

        Returns (mission, minutes_left) pairs; already-past expiry gives a
        negative value. Missions without a stored expiry are never reported.
        """
        now = datetime.now(timezone.utc) if now is None else now
        found = []
        for mission in self.missions.values():
            if mission.done or not mission.expiry:
                continue
            try:
                when = datetime.fromisoformat(str(mission.expiry).replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            remaining = (when - now).total_seconds()
            if remaining <= window_minutes * 60:
                found.append((remaining, mission))
        found.sort(key=lambda item: item[0])
        return [(mission, int(remaining // 60)) for remaining, mission in found]

    def stacks(self):
        """Serial requirements per issuer, parallel requirements across issuers."""
        groups = {}
        for mission in self.missions.values():
            if not mission.known:
                continue
            issuer = faction_key(mission.faction) or ("unknown", mission.mission_id)
            key = (faction_key(mission.target), issuer)
            groups.setdefault(key, []).append(mission)
        rows = []
        for group in groups.values():
            first = group[0]
            need = sum(m.kill_count for m in group)
            done = sum(m.kill_count if m.done else min(m.kills, m.kill_count) for m in group)
            rows.append({"issuer": first.faction or "Unknown issuer", "faction": first.target,
                         "systems": sorted({m.origin_system for m in group if m.origin_system}),
                         "target_systems": sorted({m.target_system for m in group if m.target_system}),
                         "missions": len(group), "wing": sum(m.wing for m in group),
                         "need": need, "done": done, "left": need - done,
                         "reward": sum(m.reward for m in group),
                         "complete": sum(m.done for m in group)})
        for row in rows:
            row["difference"] = max(r["left"] for r in rows
                                    if faction_key(r["faction"]) == faction_key(row["faction"])) - row["left"]
        return sorted(rows, key=lambda r: (r["faction"].casefold(), r["issuer"].casefold()))

    def spread(self):
        stacks = self.stacks()
        groups = {}
        for stack in stacks:
            groups.setdefault(faction_key(stack["faction"]), []).append(stack)
        rows = []
        for group in groups.values():
            need = max(r["need"] for r in group)
            left = max(r["left"] for r in group)
            if not any(r["complete"] < r["missions"] for r in group):
                continue
            rows.append({"faction": group[0]["faction"],
                         "missions": sum(r["missions"] - r["complete"] for r in group),
                         "wing": sum(m.wing for m in self.missions.values()
                                     if not m.done and faction_key(m.target) == faction_key(group[0]["faction"])),
                         "need": need, "done": need - left, "left": left})
        rows.sort(key=lambda row: (-row["left"], row["faction"].casefold()))
        done, total = self.progress()
        need = sum(max(r["need"] for r in group) for group in groups.values())
        reward = sum(m.reward for m in self.missions.values())
        next_by_issuer = {}
        for m in sorted(self.missions.values(), key=lambda m: (m.accepted, m.mission_id)):
            if m.known and not m.done and m.kills < m.kill_count:
                key = (faction_key(m.target), faction_key(m.faction) or m.mission_id)
                next_by_issuer.setdefault(key, m.kill_count - m.kills)
        return {"rows": rows, "stacks": stacks,
                "unknown": sum(not m.known and not m.done for m in self.missions.values()),
                "done": done, "total": total,
                "wing": sum(m.wing for m in self.missions.values()),
                "manual": sum(m.manual for m in self.missions.values()),
                "left": sum(r["left"] for r in rows), "need": need,
                "sum_kills": sum(m.kill_count for m in self.missions.values() if m.known and not m.done),
                "reward": reward, "shareable_reward": sum(m.reward for m in self.missions.values() if m.wing),
                "turn_in": sum(m.reward for m in self.missions.values() if m.done),
                "shareable_turn_in": sum(m.reward for m in self.missions.values() if m.done and m.wing),
                "average_per_kill": reward / need if need else 0,
                "average_per_mission": reward / total if total else 0,
                "kill_ratio": sum(m.kill_count for m in self.missions.values() if m.known) / need if need else 0,
                "next_completion": min(next_by_issuer.values(), default=0)}

    def projections(self, kills_per_hour):
        """Mission income projections at the current kill rate; {} when unavailable.

        cr_per_hour uses average mission reward per unique required kill, so it
        already accounts for stacks that share kills between issuers.
        """
        data = self.spread()
        if not data["total"] or not kills_per_hour or kills_per_hour <= 0:
            return {}
        return {
            "cr_per_hour": round(data["average_per_kill"] * kills_per_hour) if data["average_per_kill"] else 0,
            "finish_secs": int(data["left"] / kills_per_hour * 3600) if data["left"] else 0,
            "turn_in": data["turn_in"],
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
        lines.append(f"  Stack value: {data['reward']:,} cr | Shareable: {data['shareable_reward']:,} cr")
        lines.append(f"  Ready to turn in: {data['turn_in']:,} cr | Shareable: {data['shareable_turn_in']:,} cr")
        lines.append(f"  Avg/kill: {data['average_per_kill']:,.0f} cr | Avg/mission: {data['average_per_mission']:,.0f} cr"
                     f" | Kill ratio: {data['kill_ratio']:.2f} | Next completion: ~{data['next_completion']} kills")
        lines.append("  Issuing faction stacks (estimated local kills):")
        for row in data['stacks']:
            lines.append(f"    {row['issuer']} -> {row['faction']}"
                         f" | {row['done']}/{row['need']} | {row['left']} left | Diff {row['difference']}"
                         f" | {row['missions']} missions ({row['wing']} wing) | {row['reward']:,} cr"
                         f" | Origin: {', '.join(row['systems']) or '?'}"
                         f" | Target: {', '.join(row['target_systems']) or '?'}")
        lines.append("  Kill counts are estimates; objective completion is confirmed by the journal.")
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
                lines.append(f"              {mission.origin_system or '?'} / {mission.origin_station or '?'}"
                             f" | {mission.faction or 'Unknown issuer'} -> {mission.target_system or '?'}"
                             f" | {mission.reward:,} cr")
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

    def report_live(self, max_factions=10, max_chars=1400):
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
        lines.append(f"Stack: {data['reward']:,} cr · Shareable: {data['shareable_reward']:,} cr"
                     f" · Ready: {data['turn_in']:,} cr")
        lines.append("Issuer stacks (estimated):")
        for row in data['stacks'][:max_factions]:
            lines.append(f"• {row['issuer']}: {row['done']}/{row['need']} · {row['left']} left"
                         f" · diff {row['difference']} · {row['reward']:,} cr")
        if len(data['stacks']) > max_factions:
            lines.append(f"• …and {len(data['stacks']) - max_factions} more issuer stack(s)")
        text = "\n".join(lines)
        if len(text) > max_chars:
            suffix = "\n• More details: --missions"
            text = text[:max(0, max_chars - len(suffix))].rsplit("\n", 1)[0] + suffix
        return text

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
