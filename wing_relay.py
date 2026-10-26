"""Share wing ledgers over HTTP: push local ledgers and fetch wingmate copies.

The relay is deliberately dumb - any host that accepts PUT (static bucket,
nginx dav, a tiny relay) works: each ledger lands at {base}/{filename}.
"""
from hmac import compare_digest
from time import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen


STALE_AFTER_SECS = 24 * 3600  # a ledger nobody updated in a day is wingmate-lying


def stale_spread(spread, now=None, older_than_hours=None):
    """Hours since the ledger last saved, when that exceeds the threshold.

    older_than_hours defaults to STALE_AFTER_SECS (a day); None keeps the
    config-driven value from _default_stale_hours."""
    try:
        updated = float(spread.get("updated") or 0)
    except (TypeError, ValueError):
        return None
    if updated <= 0:
        return None
    threshold = (STALE_AFTER_SECS / 3600 if older_than_hours is None
                 else max(1, int(older_than_hours)))
    hours = ((now if now is not None else time()) - updated) / 3600
    return hours if hours > threshold else None


def stale_notes(wings, older_than_hours=None):
    """Warnings for shared spreads nobody kept fresh; wings is [(name, spread)]."""
    notes = []
    for name, spread in wings:
        hours = stale_spread(spread, older_than_hours=older_than_hours)
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
    # splice before any query string: PushURL may carry ?token= for a relay
    base = urlsplit(base_url.rstrip("/"))
    url = urlunsplit((base.scheme, base.netloc,
                      base.path + "/" + Path(path).name, base.query, ""))
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
    from missions import FILE_VERSION, MissionLedger
    from urllib.parse import urlsplit, unquote
    name = unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1] or "remote.json"
    if not name.endswith(".json"):
        name += ".json"
    if not isinstance(data, dict):
        return None, name, f"{name}: not a ledger file (JSON top level is not an object)"
    if data.get("version") != FILE_VERSION:
        return None, name, (f"{name}: ledger format v{data.get('version')}"
                            f" needs an updated monitor (this is v{FILE_VERSION})")
    ledger = MissionLedger(Path(name))
    ledger.load_data(data)
    spread = ledger.spread()
    if ledger.warnings or not spread.get("total"):
        return None, name, "ledger carries no missions"
    return spread, name, ""


class _WingRelayHandler(BaseHTTPRequestHandler):
    directory = Path(".")
    token = ""  # empty disables auth; mates carry it in the URL query

    def _authorized(self):
        if not self.token:
            return True
        from urllib.parse import parse_qs
        supplied = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
        return compare_digest(supplied.encode("utf-8"), self.token.encode("utf-8"))

    def do_GET(self):
        if not self._authorized():
            self.send_response(401)
            self.end_headers()
            return
        name = unquote(urlsplit(self.path).path).lstrip("/").rsplit("/", 1)[-1]
        if not name.endswith(".json") or not (self.directory / name).is_file():
            self.send_response(404)
            self.end_headers()
            return
        body = (self.directory / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if not self._authorized():
            self.send_response(401)
            self.end_headers()
            return
        name = unquote(urlsplit(self.path).path).lstrip("/").rsplit("/", 1)[-1]
        if not name.endswith(".json"):
            self.send_response(400)
            self.end_headers()
            return
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            json.loads(body.decode("utf-8"))  # only real ledgers on disk
        except (ValueError, UnicodeDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        (self.directory / name).write_bytes(body)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_wing_relay(port, directory, host="127.0.0.1", token=""):
    """Tiny embedded relay: GET {base}/{name}.json reads, PUT writes.

    Returns (server, error): bind failures come back as an error string so
    the monitor can report them the same way it reports dashboard failures.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handler = type("Bound", (_WingRelayHandler,),
                   {"directory": directory, "token": str(token or "")})
    try:
        server = ThreadingHTTPServer((host, int(port)), handler)
    except OSError as failure:
        return None, f"wing relay could not bind {host}:{port}: {failure}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, ""
