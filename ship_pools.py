"""Load easy/hard ship pools from an optional JSON data file.

Game updates can add or rename ships; keeping the pools in a data file means
those updates do not require code changes. Any problem with the file falls
back to the built-in defaults with a warning, so monitoring never stops.
"""
import json
from pathlib import Path


def load_ship_pools(path, fallback_easy, fallback_hard):
    """Return (easy, hard) lists of ship ids and a list of warnings."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return list(fallback_easy), list(fallback_hard), []
    except (OSError, ValueError):
        return (list(fallback_easy), list(fallback_hard),
                [f"Ship pools: ignoring unreadable {path.name} (using built-in defaults)"])
    if not isinstance(data, dict):
        return (list(fallback_easy), list(fallback_hard),
                [f"Ship pools: ignoring malformed {path.name} (using built-in defaults)"])
    pools = {}
    warnings = []
    for key, fallback in (("easy", fallback_easy), ("hard", fallback_hard)):
        value = data.get(key)
        if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
            pools[key] = [item.strip().lower() for item in value]
        else:
            pools[key] = list(fallback)
            warnings.append(f"Ship pools: '{key}' missing or invalid in {path.name} (using built-in defaults)")
    return pools["easy"], pools["hard"], warnings
