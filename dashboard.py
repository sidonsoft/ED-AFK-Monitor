"""Small read-only localhost dashboard for live stats and mission spread.

Served with the standard library only. Keep handlers tiny: every payload
comes from a provider callable supplied by the monitor, so this module has
no state of its own and cannot block the journal loop.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<title>ED AFK Monitor</title>
<style>
 body { background:#0d1117; color:#e6e8eb; font-family:system-ui,monospace; margin:2rem; }
 h1 { color:#f0a500; font-size:1.4rem; }
 pre { background:#161b22; padding:1rem; border-radius:8px; overflow-x:auto; }
 a { color:#78c8ff; }
</style></head><body>
<h1>ED AFK Monitor</h1>
<pre id="live">loading…</pre>
<h1>Mission spread</h1>
<pre id="missions">loading…</pre>
<p>Auto-refreshes every 15s · raw data: <a href="/live.json">/live.json</a>
 · <a href="/missions.json">/missions.json</a></p>
<script>
const query = location.search;
fetch('live.json' + query).then(r => r.json()).then(d =>
  document.getElementById('live').textContent = JSON.stringify(d, null, 1));
fetch('missions.json' + query).then(r => r.json()).then(d =>
  document.getElementById('missions').textContent = JSON.stringify(d, null, 1));
</script></body></html>
"""


def make_handler(live_provider, missions_provider, token=""):
    """Build a request handler bound to the monitor's data providers."""

    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the monitor's terminal clean
            pass

        def _send(self, status, body, content_type="application/json"):
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            supplied = self.headers.get("X-Dashboard-Token", "")
            for part in query.split("&"):
                if part.startswith("token="):
                    supplied = part[len("token="):]
            if token and supplied != token:
                return self._send(401, json.dumps({"error": "use ?token=... or X-Dashboard-Token"}))
            if path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/live.json":
                return self._send(200, json.dumps(live_provider(), ensure_ascii=False))
            if path == "/missions.json":
                return self._send(200, json.dumps(missions_provider(), ensure_ascii=False))
            return self._send(404, json.dumps({"error": "not found"}))

    return DashboardHandler


def start_dashboard(port, live_provider, missions_provider, token="", host="127.0.0.1"):
    """Start the daemon-threaded server; returns (server, error_message)."""
    try:
        server = ThreadingHTTPServer((host, port),
                                     make_handler(live_provider, missions_provider, token))
    except OSError as failure:
        return None, f"Could not start dashboard on {host}:{port}: {failure}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, ""
