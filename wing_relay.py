"""Share wing ledgers over HTTP: push local ledgers and fetch wingmate copies.

The relay is deliberately dumb - any host that accepts PUT (static bucket,
nginx dav, a tiny relay) works: each ledger lands at {base}/{filename}.
"""
from hmac import compare_digest
from datetime import datetime
from time import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from collections import deque
from html import escape as html_escape
import re
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.error import HTTPError
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


def _age_words(updated, now=None):
    try:
        seconds = (time() if now is None else now) - float(updated)
    except (TypeError, ValueError):
        return "unknown when"
    if seconds < 120:
        return "just now"
    hours = int(seconds // 3600)
    if hours < 1:
        return "under 1 h ago"
    if hours < 48:
        return f"{hours} h ago"
    return f"{hours // 24} d ago"


def _as_hours(value):
    """Numbers and numeric strings pass; junk and non-positive values come
    back as 0.0 so the caller's default applies - the same rule the fetch
    side's staleness notes use, never 'everything is stale' or 'nothing is'."""
    try:
        hours = float(value if value not in ("", None) else 0)
    except (TypeError, ValueError):
        return 0.0
    return hours if hours > 0 else 0.0


def relay_meta(url, timeout=5):
    """Ask a relay root what it is: ('build', 'format', '') or ('', '', error)."""
    try:
        with urlopen(Request(url, method="GET",
                             headers=wing_auth_headers(url)), timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except Exception as failure:
        return "", "", _describe_failure(failure, url)
    try:
        listing = json.loads(raw)
    except ValueError:
        return "", "", (f"that URL answered, but not with a relay listing; "
                        f"it sent {redact(raw[:60])!r}")
    if not isinstance(listing, dict):
        return "", "", "not a relay listing response"
    return str(listing.get("host_build", "")), str(listing.get("format", "")), ""


def wing_test_lines(urls, timeout=5):
    """Diagnostic lines for --wing-test: ('ok', text) per usable ledger,
    ('warn', text) per failure; plus whether anything usable was found."""
    lines, ok = [], False
    for url in as_url_list(urls):
        if not urlsplit(url).path.rsplit("/", 1)[-1].endswith(".json"):
            build, fmt, meta_error = relay_meta(url, timeout=timeout)
            if not meta_error:
                ok = True  # a talking relay is news even with zero ledgers
                lines.append(("ok", "relay answers"
                                    + (f" (build v{build})" if build else "")
                                    + (f", ledger format v{fmt}" if fmt else "")))
        spreads, errors = fetch_spreads(url, timeout=timeout)
        for name, spread in spreads:
            ok = True
            lines.append(("ok", f"{name}: {spread.get('done', 0)}/{spread.get('total', 0)} "
                                f"complete · {spread.get('left', 0)} left · "
                                f"{spread.get('turn_in', 0):,} Cr ready · "
                                f"updated {_age_words(spread.get('updated'))}"))
        for error in errors:
            lines.append(("warn", error))
    if not lines:
        lines.append(("warn", "that URL yielded nothing: is it a relay root "
                              "ending in '/' or a ledger .json?"))
    return lines, ok


def _describe_failure(failure, url):
    """HTTP errors from our own relay carry a JSON 'error' explaining the
    fix (a 401 says 'needs ?token=...'); surface it instead of the bare
    'Unauthorized' the status line alone gives."""
    if isinstance(failure, HTTPError):
        try:
            payload = json.loads(failure.read(400).decode("utf-8"))
            detail = payload.get("error") if isinstance(payload, dict) else None
        except Exception:
            detail = None
        if detail:
            return f"{failure.code} - {detail}"
        return f"HTTP {failure.code} ({failure.reason})"
    return f"{getattr(failure, 'reason', None) or failure}"


def _listing_entries(url, timeout=5):
    """A FetchURL ending in '/' is a relay address, not a ledger: pull the
    inventory and expand it into one URL per ledger. Tokens in the base
    query are carried onto every entry."""
    try:
        with urlopen(Request(url, method="GET",
                             headers=wing_auth_headers(url)), timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except Exception as failure:
        return None, f"could not read relay listing: {_describe_failure(failure, url)}", ""
    try:
        listing = json.loads(raw)
    except ValueError:
        return None, ("that URL answered, but not with a relay listing - "
                      "an ED AFK relay root answers JSON like "
                      '{"ledgers": [...]}; this one sent '
                      f"{redact(raw[:60])!r}"), ""
    entries = listing.get("ledgers") if isinstance(listing, dict) else None
    if not isinstance(entries, list):
        return None, "relay listing response has no 'ledgers' list", ""
    from missions import FILE_VERSION
    warning = ""
    remote_format = str(listing.get("format", "") if isinstance(listing, dict) else "")
    if remote_format and remote_format != str(FILE_VERSION):
        warning = (f"relay {redact(url)} speaks ledger format v{remote_format}, "
                   f"this monitor merges v{FILE_VERSION}: its ledgers may be "
                   "skipped - whoever hosts it should update their monitor")
    base = urlsplit(url)
    root = base.path if base.path.endswith("/") else base.path + "/"
    return [urlunsplit((base.scheme, base.netloc, root + str(entry.get("name", "")),
                        base.query, ""))
            for entry in entries if isinstance(entry, dict) and entry.get("name")], "", warning


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
            targets, error, warning = _listing_entries(url, timeout=timeout)
            if targets is None:
                errors.append(f"{redact(url)}: {error}")
                continue
            if warning:
                errors.append(warning)
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
        headers = {"Content-Type": "application/json", **wing_auth_headers(url)}
        with urlopen(Request(url, data=body, method="PUT",
                             headers=headers), timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return f"Relay rejected {redact(url)}: HTTP {response.status}"
    except Exception as failure:
        return f"Could not push {redact(url)}: {_describe_failure(failure, url)}"
    return ""


def url_shape_warnings(urls):
    """Config-time smell test: entries that can never be fetched. Cheaper
    than watching the same fetch error repeat in every summary."""
    warnings = []
    for url in as_url_list(urls):
        if not str(url).startswith(("http://", "https://")):
            warnings.append(f"'{redact(url)}' is not an http(s) URL - "
                            "wingmates will use it verbatim, so a bare "
                            "hostname or missing scheme fails every fetch")
    return warnings


def wing_auth_headers(url):
    """ED_AFK_WING_TOKEN (like ED_AFK_TELEGRAM_TOKEN before it) carries the
    relay secret in the environment; URLs keep working with ?token=, and an
    in-URL token always wins over the env fallback."""
    token = os.environ.get("ED_AFK_WING_TOKEN", "")
    if token and "token=" not in urlsplit(url).query:
        return {"X-Wing-Token": token}
    return {}


def fetch_spread(url, timeout=5):
    """GET one ledger JSON and return (spread, name, error) for wing merging."""
    try:
        with urlopen(Request(url, method="GET",
                             headers=wing_auth_headers(url)), timeout=timeout) as response:
            if response.status != 200:
                return None, "", f"HTTP {response.status} from {url}"
            data = json.loads(response.read().decode("utf-8"))
    except Exception as failure:
        return None, "", f"Could not fetch {redact(url)}: {_describe_failure(failure, url)}"
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

    DENY_MESSAGE = ("this relay needs ?token=<value> in the URL "
                    "or an X-Wing-Token header")

    def _deny(self):
        # a bare 401 makes people rebuild the URL from scratch; spell out
        # the query form so a browser visit self-documents the token rule
        self._status(401)
        if "text/html" in self.headers.get("Accept", ""):
            # browsers get a page a human can act on; urllib, curl and the
            # monitor's own fetch path keep the JSON error contract
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<!doctype html><meta charset=utf-8>"
                              "<title>ED AFK Monitor wing relay</title>"
                              "<h2>401 - wing relay needs a token</h2><p>"
                              + html_escape(self.DENY_MESSAGE) + ".</p>"
                              "<p>For example: <code>?token=yourtoken</code> "
                              "at the end of the URL.</p>").encode())
            return
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": self.DENY_MESSAGE}).encode())

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
            names = sorted(ledgers)
            now = time()

            def silent_for(updated):
                try:
                    return (now - float(updated)) / 3600
                except (TypeError, ValueError):
                    return None

            silent = {name: (silent_for(ledgers[name]) or 0) > self.stale_hours
                      for name in names}
            if "text/html" in self.headers.get("Accept", ""):
                # a human pasting the relay URL into a browser deserves a
                # page, not raw JSON; machines never send Accept: text/html
                rows = []
                for name in names:
                    try:
                        when = datetime.fromtimestamp(float(ledgers[name])).strftime("%Y-%m-%d %H:%M")
                    except (TypeError, ValueError, OSError, OverflowError):
                        when = "unknown"
                    rows.append(f'<tr><td><a href="{html_escape(name)}">'
                                f"{html_escape(name)}</a></td>"
                                f"<td>{when}{' - silent wingmate' if silent[name] else ''}</td></tr>")
                body = ("<!doctype html><meta charset=utf-8><title>ED AFK Monitor "
                        "wing relay</title><h2>ED AFK Monitor wing relay</h2>"
                        + (f"<p><b>{html_escape(self.banner)}</b></p>" if self.banner else "")
                        + f"<p>{len(names)} ledger(s) served here. "
                        "The JSON view is the same URL without a browser Accept "
                        "header (or via any monitor's --wing-test).</p>"
                        "<table border=1 cellpadding=4><tr><th>ledger</th>"
                        f"<th>last update</th></tr>{''.join(rows)}</table>"
                        + ("<p><small>served by ED AFK Monitor"
                           + (f" build v{html_escape(self.build)}" if self.build else "")
                           + f", ledger format v{__import__('missions').FILE_VERSION}</small></p>")
                        + (("<h3>recent pushes</h3><ul>"
                            + "".join(f"<li>{html_escape(n)} at "
                                      f"{datetime.fromtimestamp(t).strftime('%H:%M:%S')}</li>"
                                      for n, t in list(self.recent)) + "</ul>")
                           if self.recent else "")
                        + (("<h3>recent trouble</h3><ul>"
                            + "".join(f"<li>{html_escape(n)}: {html_escape(why)} at "
                                      f"{datetime.fromtimestamp(t).strftime('%H:%M:%S')}</li>"
                                      for n, why, t in list(self.trouble)) + "</ul>")
                           if self.trouble else "")).encode()
                self._status(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            from missions import FILE_VERSION as host_format
            body = json.dumps({"ledgers": [{"name": k, "updated": ledgers[k],
                                           "stale": silent[k]} for k in names],
                               "host_build": self.build, "format": host_format}).encode()
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

    def _trouble(self, name, why):
        self.trouble.appendleft((name, why, int(time())))
        if self.on_reject:
            try:
                self.on_reject(name, why)
            except Exception:
                pass  # host visibility must never break the answer

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
            self._trouble(name or "?", "not a .json path")
            self._status(400)
            self.end_headers()
            return
        try:
            if len(body) > MAX_LEDGER_BYTES:
                self._trouble(name, "too big for a ledger")
                self._status(413)
                self.end_headers()
                return
            parsed = json.loads(body.decode("utf-8"))  # only real ledgers on disk
        except (ValueError, UnicodeDecodeError):
            self._trouble(name, "body was not JSON")
            self._status(400)
            self.end_headers()
            return
        from missions import FILE_VERSION
        got = parsed.get("version") if isinstance(parsed, dict) else None
        if got != FILE_VERSION:
            # say so now instead of storing junk the merge will silently
            # skip forever: the pusher's own run fails with this sentence
            self._trouble(name, f"ledger format {got!r}")
            self._status(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": (
                f"that is not a v{FILE_VERSION} ledger (its version field is "
                f"{got!r}); update the sending monitor - this relay only keeps "
                "ledgers its own wing view can actually merge")}).encode())
            return
        (self.directories[0] / name).write_bytes(body)
        # newest-first ring, shared by every thread of this bound handler
        self.recent.appendleft((name, int(time())))
        if self.on_put:
            try:
                self.on_put(name, len(body))
            except Exception:
                pass  # visibility must never cost a wingmate their push
        self._status(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_wing_relay(port, directory, host="127.0.0.1", token="", on_put=None,
                   stale_hours=None, banner="", build="", on_reject=None):
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
                    "on_put": staticmethod(on_put) if callable(on_put) else None,
                    "stale_hours": (lambda sh: sh if sh > 0 else STALE_AFTER_SECS / 3600)(
                        _as_hours(stale_hours)),
                    "recent": deque(maxlen=8),
                    "trouble": deque(maxlen=5),
                    "on_reject": staticmethod(on_reject) if callable(on_reject) else None,
                    "banner": str(banner or ""),
                    "build": str(build or "")})
    try:
        server = ThreadingHTTPServer((host, int(port)), handler)
    except OSError as failure:
        return None, f"wing relay could not bind {host}:{port}: {failure}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, ""
