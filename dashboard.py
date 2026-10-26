"""Small read-only localhost dashboard: rendered stats, mission table and wing view.

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
 h2 { color:#78c8ff; font-size:1.05rem; margin-bottom:.4rem; }
 pre { background:#161b22; padding:1rem; border-radius:8px; overflow-x:auto; }
 table { border-collapse:collapse; margin:.4rem 0; }
 td, th { padding:.15rem .6rem; text-align:right; }
 th { color:#9aa4af; font-weight:normal; border-bottom:1px solid #30363d; }
 .panel { background:#161b22; padding:1rem; border-radius:8px; margin-bottom:1rem; }
 .warn { color:#ffb86c; }
 .cols { display:flex; gap:1rem; flex-wrap:wrap; }
 a { color:#78c8ff; }
</style></head><body>
<h1 id="title">ED AFK Monitor</h1>
<div class="cols">
 <div class="panel"><h2>This session</h2><div id="session">loading…</div></div>
 <div class="panel"><h2>Run total</h2><div id="total">loading…</div></div>
</div>
<div class="panel"><h2>Mission spread</h2><div id="missions">loading…</div></div>
<div class="panel" id="wingpanel" hidden><h2>Wing view</h2><div id="wing"></div></div>
<div class="panel" id="sessionspanel" hidden><h2>Recent sessions</h2><div id="sessions"></div></div>
<p>Auto-refreshes every 15s · raw data: <a href="/live.json">/live.json</a>
 · <a href="/missions.json">/missions.json</a></p>
<script>
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const fmt = n => typeof n === 'number' ? n.toLocaleString() : esc(n ?? '');
const query = location.search;
fetch('live.json' + query).then(r => r.json()).then(d => {
  if (d.cmdrname) document.getElementById('title').textContent = 'ED AFK Monitor · ' + d.cmdrname;
  for (const key of ['session', 'total']) {
    const s = d[key];
    if (!s) continue;
    let html = '<table><tr><th>kills</th><th>bounties</th><th>scans</th><th>merits</th></tr>'
      + '<tr><td>' + fmt(s.kills) + '</td><td>' + fmt(s.bounties) + '</td><td>'
      + fmt(s.scans_in) + '</td><td>' + fmt(s.merits) + '</td></tr></table>';
    const tops = Object.entries(s.top_factions || {});
    if (tops.length) html += '<table><tr><th>top factions</th><th>kills</th></tr>'
      + tops.map(([f, k]) => '<tr><td>' + esc(f) + '</td><td>' + fmt(k) + '</td></tr>').join('')
      + '</table>';
    document.getElementById(key).innerHTML = html;
  }
});
fetch('missions.json' + query).then(r => r.json()).then(d => {
  if (!d.rows) { document.getElementById('missions').textContent = JSON.stringify(d, null, 1); return; }
  let html = (d.total ? (d.done + '/' + d.total + ' complete · ' + fmt(d.left) + ' kills left · '
             + fmt(d.reward) + ' cr stack') : 'no open massacre missions');
  if (d.rows.length) html += '<table><tr><th>target faction</th><th>open</th><th>wing</th>'
    + '<th>need</th><th>done</th><th>left</th></tr>' + d.rows.map(row =>
      '<tr><td>' + esc(row.faction) + '</td><td>' + fmt(row.missions) + '</td><td>' + fmt(row.wing)
      + '</td><td>' + fmt(row.need) + '</td><td>' + fmt(row.done) + '</td><td>' + fmt(row.left)
      + '</td></tr>').join('') + '</table>';
  document.getElementById('missions').innerHTML = html;
  const wing = d.wing_view;
  if (wing) {
    document.getElementById('wingpanel').hidden = false;
    const c = wing.combined;
    let w = 'local + ' + wing.sources.length + ' shared ledger(s) · ' + c.done + '/' + c.total
      + ' complete · ' + fmt(c.left) + ' kills left across the wing<br>sources: '
      + esc(wing.sources.join(', ') || 'none');
    if (wing.errors && wing.errors.length)
      w += '<div class="warn">' + wing.errors.map(esc).join('<br>') + '</div>';
    document.getElementById('wing').innerHTML = w;
  }
});
fetch('sessions.json' + query).then(r => r.ok ? r.json() : null).then(rows => {
  if (!rows || !rows.length) return;
  document.getElementById('sessionspanel').hidden = false;
  const dur = s => s >= 3600 ? (s / 3600).toFixed(1) + 'h' : Math.round(s / 60) + 'm';
  document.getElementById('sessions').innerHTML =
    '<table><tr><th>started</th><th>time</th><th>kills</th><th>bounties</th>'
    + '<th>missions</th></tr>' + rows.map(row =>
      '<tr><td>' + esc(new Date(row.started * 1000).toLocaleString()) + '</td><td>'
      + dur(row.duration) + '</td><td>' + fmt(row.kills) + '</td><td>' + fmt(row.bounties)
      + '</td><td>' + (row.missions_total ? row.missions_done + '/' + row.missions_total : '-')
      + '</td></tr>').join('') + '</table>';
});
</script></body></html>
"""

def make_handler(live_provider, missions_provider, token="", sessions_provider=None):
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
            if path == "/sessions.json" and sessions_provider is not None:
                return self._send(200, json.dumps(sessions_provider(), ensure_ascii=False))
            return self._send(404, json.dumps({"error": "not found"}))

    return DashboardHandler


class DashboardServer(ThreadingHTTPServer):
    """Daemon threads so monitor shutdown is never blocked by the dashboard."""
    daemon_threads = True


def start_dashboard(port, live_provider, missions_provider, token="", host="127.0.0.1",
                    sessions_provider=None):
    """Start the daemon-threaded server; returns (server, error_message)."""
    try:
        server = DashboardServer((host, port), make_handler(
            live_provider, missions_provider, token, sessions_provider))
    except OSError as failure:
        return None, f"Could not start dashboard on {host}:{port}: {failure}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, ""
