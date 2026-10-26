"""Share wing ledgers over HTTP: push local ledgers and fetch wingmate copies.

The relay is deliberately dumb - any host that accepts PUT (static bucket,
nginx dav, a tiny relay) works: each ledger lands at {base}/{filename}.
"""
from hmac import compare_digest
from time import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
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


def redact(url):
    """Hide relay ?token= values: wing summaries log failing URLs verbatim,
    and every wingmate reads those summaries."""
    return re.sub(r"([?&]token=)[^&]*", lambda m: m.group(1) + "\u2022", str(url))


def fetch_spreads(urls, timeout=5):
    """Fetch every URL; returns ([(name, spread)], [error strings])."""
    spreads, errors = [], []
    for url in as_url_list(urls):
        spread, name, error = fetch_spread(url, timeout=timeout)
        if error:
            errors.append(f"{redact(url)}: {error}")
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
                return f"Relay rejected {redact(url)}: HTTP {response.status}"
    except Exception as failure:
        return (f"Could not push {redact(url)}: "
                f"{getattr(failure, 'reason', None) or failure}")
    return ""


def fetch_spread(url, timeout=5):
    """GET one ledger JSON and return (spread, name, error) for wing merging."""
    try:
        with urlopen(Request(url, method="GET"), timeout=timeout) as response:
            if response.status != 200:
                return None, "", f"HTTP {response.status} from {url}"
            data = json.loads(response.read().decode("utf-8"))
    except Exception as failure:
        return None, "", (f"Could not fetch {redact(url)}: "
                          f"{getattr(failure, 'reason', None) or failure}")
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


MAX_LEDGER_BYTES = 2 * 1024 * 1024  # a fat wingmate PUT must not fill the disk


class _WingRelayHandler(BaseHTTPRequestHandler):
    directories = []  # GET searches each in order; PUT lands in the first
    token = ""  # empty disables auth; mates carry it in the URL query
    on_put = None  # optional host(name, bytes) callback: accepted pushes appear in the monitor log

    def _authorized(self):
        if not self.token:
            return True
        from urllib.parse import parse_qs
        supplied = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
        return compare_digest(supplied.encode("utf-8"), self.token.encode("utf-8"))

    def _deny(self):
        # a bare 401 makes people rebuild the URL from scratch; spell out
        # the query form so a browser visit self-documents the token rule
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(
            {"error": "this relay needs ?token=<value> in the URL"}).encode())

    def do_GET(self):
        if not self._authorized():
            return self._deny()
        path = unquote(urlsplit(self.path).path)
        name = path.lstrip("/").rsplit("/", 1)[-1]
        if not name:
            # 'GET /wing/' lists what this host carries: the quickest way for
            # a wingmate to confirm a URL (or a token) actually works
            ledgers = {}
            for directory in reversed(self.directories):
                for entry in sorted(directory.glob("*.json")):
                    try:
                        updated = json.loads(entry.read_bytes().decode("utf-8")).get("updated", 0)
                    except (ValueError, OSError, UnicodeDecodeError):
                        updated = entry.stat().st_mtime
                    ledgers[entry.name] = updated
            body = json.dumps({"ledgers": [{"name": k, "updated": ledgers[k]}
                                           for k in sorted(ledgers)]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        found = next((d / name for d in self.directories
                      if name.endswith(".json") and (d / name).is_file()), None)
        if found is None:
            self.send_response(404)
            self.end_headers()
            return
        body = found.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if not self._authorized():
            return self._deny()
        name = unquote(urlsplit(self.path).path).lstrip("/").rsplit("/", 1)[-1]
        if not name.endswith(".json"):
            self.send_response(400)
            self.end_headers()
            return
        try:
            declared = int(self.headers.get("Content-Length", 0))
            if declared > 16 * MAX_LEDGER_BYTES:
                # absurd: answer without ingesting the request at all
                self.send_response(413)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return
            body = self.rfile.read(declared)
            if len(body) > MAX_LEDGER_BYTES:
                self.send_response(413)
                self.end_headers()
                return
            json.loads(body.decode("utf-8"))  # only real ledgers on disk
        except (ValueError, UnicodeDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        (self.directories[0] / name).write_bytes(body)
        if self.on_put:
            try:
                self.on_put(name, len(body))
            except Exception:
                pass  # visibility must never cost a wingmate their push
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_wing_relay(port, directory, host="127.0.0.1", token="", on_put=None):
    """Tiny embedded relay: GET {base}/{name}.json reads, PUT writes.

    Returns (server, error): bind failures come back as an error string so
    the monitor can report them the same way it reports dashboard failures.
    """
    # one folder or a chain: GET searches each (mates' pushed copies first,
    # then the host's own ledgers), PUT always writes to the first
    directories = [Path(d) for d in (directory if isinstance(directory, (list, tuple))
                                     else [directory])]
    directories[0].mkdir(parents=True, exist_ok=True)
    handler = type("Bound", (_WingRelayHandler,),
                   {"directories": directories, "token": str(token or ""),
                    # staticmethod: a plain callable in the class dict would
                    # bind as a method and hand the handler in as an argument
                    "on_put": staticmethod(on_put) if callable(on_put) else None})
    try:
        server = ThreadingHTTPServer((host, int(port)), handler)
    except OSError as failure:
        return None, f"wing relay could not bind {host}:{port}: {failure}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, ""
