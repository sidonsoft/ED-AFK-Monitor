"""Share wing ledgers over HTTP: push local ledgers and fetch wingmate copies.

The relay is deliberately dumb - any host that accepts PUT (static bucket,
nginx dav, a tiny relay) works: each ledger lands at {base}/{filename}.
"""
import json
from pathlib import Path
from urllib.request import Request, urlopen


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
