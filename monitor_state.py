"""Typed session-state core: stats counters, session tracking, rate/time helpers.

Importable on its own (no terminal, journal or network side effects), which
lets these classes be unit-tested without booting the full monitor script.
"""
import time
from datetime import datetime, timedelta, timezone

__all__ = ["Stats", "Tracking", "per_hour", "time_format", "num_format"]


class Stats:
    """Mutable per-session/per-run counters; reset() returns a clean slate."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.scansinrecents: list = []
        self.scansoutrecents: list = []
        self.lastkillutc: float = 0
        self.lastkillmono: float = 0
        self.killstime: float = 0
        self.kill_times: list = []
        self.scansin: int = 0
        self.scanstime: float = 0
        self.scan_times: list = []
        self.lastscanutc: float = 0
        self.lastscanmono: float = 0
        self.kills: int = 0
        self.bounties: int = 0
        self.factions: dict = {}
        self.merits: float = 0
        self.lastsecurity: str = ""
        self.baitfails: int = 0
        self.fuellasttime: float = 0
        self.fuellastremain: float = 0
        self.meritstoreport: float = 0


class Tracking:
    """Journal-loop bookkeeping: session windows, CMDR profile, duplicate guard.

    The monitor wires itself in with attach(); without attachments the debug
    and status hooks are no-ops, so the core can run in isolation for tests.
    """

    def __init__(self) -> None:
        self.deploytime: datetime | None = None
        self.totaltime: float = 0
        self.warnednokills: datetime | None = None
        self.warnedkillrate: datetime | None = None
        self.fuelcapacity: int = 64
        self.killtype: str = "bounties"
        self.fighterhull: int = 0
        self.logged: int = 0
        self.lines: int = 0
        self.missions: bool = False
        self.lasteventname: str | None = None
        self.thiseventtime: datetime | None = None
        self.dupeevent: str = ""
        self.duperepeats: int = 1
        self.dupewarn: bool = False
        self.warned_missions: set = set()
        self.runstart: float | None = None
        self.seen_events: dict = {}
        self.preloading: bool = True
        self.cmdrname: str | None = None
        self.cmdrship: str | None = None
        self.cmdrcombatrank: str | None = None
        self.cmdrcombatprogress: str | None = None
        self.cmdrgamemode: str | None = None
        self.cmdrlocation: str | None = None
        self.lastcheck: float | None = None
        self.live_update = None
        self.live_reset: bool = True
        self.live_generation: int = 0
        self.live_delivery_generation: int | None = None
        self.journalfile: str = ""
        self.relay_warned: bool = False
        self.last_relay_push: float = 0.0
        self.muted_until: float | None = None
        self.laststatusline: str = ""
        self._session: Stats | None = None
        self._debug = lambda *args: None
        self._status = lambda reset=False: None

    def attach(self, session: Stats | None = None, on_debug=None, on_status=None) -> None:
        """Bind the live session stats and the monitor's debug/status hooks."""
        if session is not None:
            self._session = session
        if on_debug is not None:
            self._debug = on_debug
        if on_status is not None:
            self._status = on_status

    def sessionstart(self, reset: bool = False, delta: int = 0) -> None:
        if not self.deploytime or reset:
            # --resetsession runs at startup, before any journal event set
            # thiseventtime; fall back to "now" so the time-based watchdog
            # (warnings, relay pushes) stays armed.
            base = self.thiseventtime or datetime.now(timezone.utc)
            self.deploytime = base if not delta else base - timedelta(seconds=delta)
            if self._session is not None:
                self._session.reset()
                self._session.kill_times.append(self.deploytime)
                self._session.scan_times.append(self.deploytime)
            self._debug(f"Session started: {self.deploytime}")
            self.warnednokills = None
            self.warnedkillrate = None
            self.lastcheck = time.monotonic()
            self.live_update = None
            self._status()

    def sessionend(self) -> None:
        if self.deploytime and self.thiseventtime:
            self.totaltime += (self.thiseventtime - self.deploytime).total_seconds()
            self._debug(f"Session ended: {self.thiseventtime}"
                        f" ({time_format((self.thiseventtime - self.deploytime).total_seconds())})"
                        f" [Total: {time_format(self.totaltime)}]")
            self.deploytime = None
            self.live_reset = True
            self._status(True)


def per_hour(seconds=0, precision=None):
    if seconds > 0:
        return round(3600 / seconds, precision)
    else:
        return 0


def time_format(seconds: int) -> str:
    if seconds is not None and seconds >= 0:
        seconds = int(seconds)
        h = seconds // 3600
        m = seconds % 3600 // 60
        s = seconds % 3600 % 60
        if h > 0:
            return '{:d}h{:d}m'.format(h, m)
        elif m > 0:
            return '{:d}m{:d}s'.format(m, s)
        else:
            return '{:d}s'.format(s)
    else:
        return "-"


def num_format(number: int) -> str:
    if number is not None:
        number = int(number)
        if number >= 999_500:
            return f"{round(number / 1_000_000, 1):g}m"
        elif number >= 1_000:
            return f"{round(number / 1_000):g}k"
        else:
            return number
