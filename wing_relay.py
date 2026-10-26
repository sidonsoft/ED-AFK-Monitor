"""Share wing ledgers over HTTP: push local ledgers and fetch wingmate copies.

The relay is deliberately dumb - any host that accepts PUT (static bucket,
nginx dav, a tiny relay) works: each ledger lands at {base}/{filename}.
"""
from time import time
import json
from pathlib import Path
from urllib.request import Request, urlopen


STALE_AFTER_SECS = 24 * 3600  # a ledger nobody updated in a day is wingmate-lying


def stale_spread(spread, now=None):
    """Hours since the ledger last saved, when that exceeds STALE_AFTER_SECS."""
    try:
        updated = float(spread.get("updated") or 0)
    except (TypeError, ValueError):
        return None
    if updated <= 0:
        return None
    hours = ((now if now is not None else time()) - updated) / 3600
    return hours if hours > STALE_AFTER_SECS / 3600 else None


def stale_notes(wings):
    """Warnings for shared spreads nobody kept fresh; wings is [(name, spread)]."""
    notes = []
    for name, spread in wings:
        hours = stale_spread(spread)
        if hours is not None:
            notes.append(f"{name[:-5]}: no updates for {hours:.0f}h - counts may be stale")
    return notes


def as_url_list(value):
    """Accept one URL string or a list of them; ignore blanks."""
    if isinstance(value, str):
        value = [value]
    return [str(url).strip() for url in value if str(url).strip()]


def fetch_spreads(urls, timeout=5):
    """Fetch every URL; returns ([(name, spread)], [error strings])."""
    spreads, errors = [], []
    for url in as_url_list(urls):
        spread, name, error = fetch_spread(url, timeout=timeout)
        if error:
            errors.append(f"{url}: {error}")
        else:
            spreads.append((name, spread))
    return spreads, errors


def push_ledger(path, base_url, timeout=5):
    """PUT one ledger file under base_url; returns '' or an error string."""
    try:
        body = Path(path).read_bytes()
    except OSError as failure:
        return f"Could not read {path}: {failure}"
    url = base_url.rstrip("/") + "/" + Path(path).name
    try:
        with urlopen(Request(url, data=body, method="PUT",
                             headers={"Content-Type": "application/json"}), timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return f"Relay rejected {url}: HTTP {response.status}"
    except Exception as failure:
        return f"Could not push {url}: {getattr(failure, 'reason', None) or failure}"
    return ""


def fetch_spread(url, timeout=5):
    """GET one ledger JSON and return (spread, name, error) for wing merging."""
    try:
        with urlopen(Request(url, method="GET"), timeout=timeout) as response:
            if response.status != 200:
                return None, "", f"HTTP {response.status} from {url}"
            data = json.loads(response.read().decode("utf-8"))
    except Exception as failure:
        return None, "", f"Could not fetch {url}: {getattr(failure, 'reason', None) or failure}"
    from missions import MissionLedger
    from urllib.parse import urlsplit, unquote
    name = unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1] or "remote.json"
    if not name.endswith(".json"):
        name += ".json"
    ledger = MissionLedger(Path(name))
    ledger.load_data(data)
    spread = ledger.spread()
    if ledger.warnings or not spread.get("total"):
        return None, name, "ledger carries no missions"
    return spread, name, ""
