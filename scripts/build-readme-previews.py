"""Render deterministic documentation previews using the production mission reports."""
import argparse
import html
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from missions import MissionLedger


def markdown(text):
    text = html.escape(text)
    return re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as temp:
        ledger = MissionLedger(Path(temp) / 'demo.json', commander='Demo Commander')
        fixtures = [
            (1, 'Liberals of Ahayan', 40, 20000000),
            (2, 'Liberals of Ahayan', 30, 14000000),
            (3, 'United Tagii League', 50, 24000000),
            (4, 'United Tagii League', 20, 10000000),
            (5, 'Workers of Dimocorna Union', 60, 26000000),
        ]
        for mission_id, issuer, quota, reward in fixtures:
            ledger.accept({'Name': 'Mission_MassacreWing_name', 'MissionID': mission_id,
                           'Faction': issuer, 'TargetFaction': 'Anana Brotherhood',
                           'KillCount': quota, 'Wing': True, 'Reward': reward,
                           'OriginSystem': 'Ahayan' if mission_id < 3 else 'Puneith',
                           'OriginStation': 'Pascal Platform' if mission_id < 3 else 'Wheelock Port',
                           'DestinationSystem': 'Anana',
                           'timestamp': f'2026-10-26T08:00:0{mission_id}Z'})
        for _ in range(25):
            ledger.kill('Anana Brotherhood')
        ledger.redirected(1)
        terminal = ledger.report_text()
        live = ledger.report_live()
        # Save exact production text alongside the previews for verification.
        (output / 'terminal.txt').write_text(terminal, encoding='utf-8')
        (output / 'discord.txt').write_text(live, encoding='utf-8')

    common = '''*{box-sizing:border-box}body{margin:0;background:#12151b;color:#e8edf3;
font-family:Arial,sans-serif} .frame{border:1px solid #3b4350;border-radius:12px;
overflow:hidden} .bar{background:#252b35;padding:17px 22px;font-size:14px;color:#bdc8d5;
display:flex;justify-content:space-between}.badge{color:#9edac3}.footer{padding:14px 22px;
background:#202631;color:#b6c0ce;font-size:13px}'''
    terminal_html = f'''<!doctype html><meta charset="utf-8"><style>{common}
body{{padding:24px;width:1480px}}pre{{margin:0;padding:24px;background:#0e1117;
font:16px/1.55 Menlo,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}}
.command{{color:#8ed5b0}}.status{{font:14px Menlo,Consolas,monospace;padding:15px 24px;
background:#d6e1ee;color:#172033}}</style><div class="frame"><div class="bar">
<span>ED AFK Monitor · Terminal report</span><span class="badge">SIMULATED DATA</span></div>
<pre><span class="command">Session summary · mission section</span>\n\n{html.escape(terminal)}</pre>
<div class="status">Demo Commander · Anana · 1/5 objectives complete · 45 estimated kills left</div>
<div class="footer">Production report output · Local kill estimates; completion confirmed by journal events</div></div>'''
    discord_html = f'''<!doctype html><meta charset="utf-8"><style>{common}
body{{padding:24px;width:820px;background:#1e1f22}}.bar{{background:#2b2d31}}
.channel{{padding:19px 24px;background:#313338;border-bottom:1px solid #42444a;
font-size:18px;font-weight:bold}}.message{{display:flex;gap:16px;background:#313338;padding:24px}}
.avatar{{width:44px;height:44px;flex-shrink:0;border-radius:50%;background:#dc7827;
display:grid;place-items:center;color:#10141a;font-size:15px;font-weight:bold}}
.author{{font-size:16px;color:#eee;margin-bottom:12px}}.time{{color:#a4a8b1;font-size:12px;
margin-left:9px}}.bot{{background:#5865f2;border-radius:3px;font-size:10px;padding:3px 5px;
margin-left:5px}}.content{{font-size:16px;line-height:1.6;white-space:pre-wrap;color:#dbdee1}}
strong{{font-weight:700;color:#f3f4f5}}.footer{{background:#2b2d31;line-height:1.5}}</style>
<div class="frame"><div class="bar"><span>Discord message preview</span>
<span class="badge">SIMULATED DATA</span></div><div class="channel"># afk-monitor</div>
<div class="message"><div class="avatar">ED</div><div><div class="author">AFK Monitor
<span class="bot">APP</span><span class="time">Today at 1:30 PM</span></div>
<div class="content"><strong>Session Stats</strong> (30m) [1/5]\n▪ <strong>Kills:</strong> 25 (50/h)\n\n{markdown(live)}</div></div></div>
<div class="footer">Message styling is illustrative. Mission content comes directly from the production Discord report; no notification was sent.</div></div>'''
    (output / 'terminal.html').write_text(terminal_html, encoding='utf-8')
    (output / 'discord.html').write_text(discord_html, encoding='utf-8')
    print(f'Built report previews in {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'work/readme-previews')
    build(parser.parse_args().output_dir)
