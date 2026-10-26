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

    older_than_hours: config-driven threshold in hours; None falls back to
    STALE_AFTER_SECS (a day). Junk thresholds fall back too - a crash here
    would take down every summary that reads the wing."""
    try:
        updated = float(spread.get("updated") or 0)
    except (TypeError, ValueError):
        return None
    try:
        threshold = (STALE_AFTER_SECS / 3600 if older_than_hours is None
                     else max(1, int(older_than_hours)))
    except (TypeError, ValueError):
        # a config typo must not blind the wing to staleness: fall back to
        # the default day, never to 'nothing is ever stale'
        threshold = STALE_AFTER_SECS / 3600
    if updated <= 0:
        return None
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


def served_count(directories):
    """Ledger names a relay would currently serve (deduped across tiers)."""
    names = set()
    for directory in directories:
        try:
            names.update(entry.name for entry in Path(directory).glob("*.json"))
        except OSError:
            pass
    return len(names)


def serve_advice(host, token):
    """One-line safety note for a serving monitor; '' when nothing to say."""
    if token:
        return ""
    if str(host).strip() not in ("", "127.0.0.1", "localhost", "::1"):
        return (f"wing relay is open on {host} with no token: anyone who can "
                "reach this port can read and replace ledgers "
                "(set ServeToken or --wing-serve-token)")
    return ""


def redact(url):
    """Hide relay ?token= values: wing summaries log failing URLs verbatim,
    and every wingmate reads those summaries."""
    return re.sub(r"([?&]token=)[^&]*", lambda m: m.group(1) + "\u2022", str(url))


def wing_test_lines(urls, timeout=5):
    """Diagnostic lines for --wing-test: ('ok', text) per usable ledger,
    ('warn', text) per failure; plus whether anything usable was found."""
    lines, ok = [], False
    for url in as_url_list(urls):
        spreads, errors = fetch_spreads(url, timeout=timeout)
        for name, spread in spreads:
            ok = True
            lines.append(("ok", f"{name}: {spread.get('done', 0)}/{spread.get('total', 0)} "
                                f"complete · {spread.get('left', 0)} left · "
                                f"{spread.get('turn_in', 0):,} Cr ready"))
        for error in errors:
            lines.append(("warn", error))
    if not lines:
        lines.append(("warn", "that URL yielded nothing: is it a relay root "
                              "ending in '/' or a ledger .json?"))
    return lines, ok


def _listing_entries(url, timeout=5):
    """A FetchURL ending in '/' is a relay address, not a ledger: pull the
    inventory and expand it into one URL per ledger. Tokens in the base
    query are carried onto every entry."""
    try:
        with urlopen(Request(url, method="GET"), timeout=timeout) as response:
            listing = json.loads(response.read().decode("utf-8"))
    except Exception as failure:
        return None, f"could not read relay listing: {getattr(failure, 'reason', None) or failure}"
    entries = listing.get("ledgers") if isinstance(listing, dict) else None
    if not isinstance(entries, list):
        return None, "relay listing response has no 'ledgers' list"
    base = urlsplit(url)
    root = base.path if base.path.endswith("/") else base.path + "/"
    return [urlunsplit((base.scheme, base.netloc, root + str(entry.get("name", "")),
                        base.query, ""))
            for entry in entries if isinstance(entry, dict) and entry.get("name")], ""


def fetch_spreads(urls, timeout=5):
    """Fetch every URL; returns ([(name, spread)], [error strings]).

    The same *URL* fetched twice (relay root expansion plus an explicit
    entry for it) is kept once: double-counted wing totals read as a busy
    wing, not a config accident. Different relays carrying same-named
    ledgers (two wingmates both pushing 'default.json') stay distinct.
    """
    spreads, errors, seen = [], [], set()
    for url in as_url_list(urls):
        if not urlsplit(url).path.rsplit("/", 1)[-1].endswith(".json"):
            targets, error = _listing_entries(url, timeout=timeout)
            if targets is None:
                errors.append(f"{redact(url)}: {error}")
                continue
        else:
            targets = [url]
        for target in targets:
            if target in seen:
                continue
            spread, name, error = fetch_spread(target, timeout=timeout)
            if error:
                errors.append(f"{redact(target)}: {error}")
            else:
                seen.add(target)
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
        supplied = (self.headers.get("X-Wing-Token")
                    or parse_qs(urlsplit(self.path).query).get("token", [""])[0])
        return compare_digest(supplied.encode("utf-8"), self.token.encode("utf-8"))

    def _status(self, code):
        # every answer - success or verdict - is live data: browsers
        # happily cache 404s and 401s too, hiding pushes that landed
        self.send_response(code)
        self.send_header("Cache-Control", "no-store")

    def _deny(self):
        # a bare 401 makes people rebuild the URL from scratch; spell out
        # the query form so a browser visit self-documents the token rule
        self._status(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(
            {"error": "this relay needs ?token=<value> in the URL "
                          "or an X-Wing-Token header"}).encode())

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
            self._status(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        found = next((d / name for d in self.directories
                      if name.endswith(".json") and (d / name).is_file()), None)
        if found is None:
            self._status(404)
            self.end_headers()
            return
        body = found.read_bytes()
        self._status(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        name = unquote(urlsplit(self.path).path).lstrip("/").rsplit("/", 1)[-1]
        try:
            declared = int(self.headers.get("Content-Length", 0))
        except ValueError:
            declared = -1
        if declared < 0 or declared > 16 * MAX_LEDGER_BYTES:
            # absurd or unparseable: answer without ingesting the request
            self._status(413 if declared > 0 else 400)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        # read the body before any verdict: answering mid-request on a
        # keep-alive connection wedges the client before it finishes
        # sending, so the 401/400 it was promised never arrives intact
        body = self.rfile.read(declared)
        if not self._authorized():
            return self._deny()
        if not name.endswith(".json"):
            self._status(400)
            self.end_headers()
            return
        try:
            if len(body) > MAX_LEDGER_BYTES:
                self._status(413)
                self.end_headers()
                return
            json.loads(body.decode("utf-8"))  # only real ledgers on disk
        except (ValueError, UnicodeDecodeError):
            self._status(400)
            self.end_headers()
            return
        (self.directories[0] / name).write_bytes(body)
        if self.on_put:
            try:
                self.on_put(name, len(body))
            except Exception:
                pass  # visibility must never cost a wingmate their push
        self._status(204)
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
