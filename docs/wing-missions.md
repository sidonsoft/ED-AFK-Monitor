# Wing mission stacks

AFK Monitor reads Elite Dangerous journals directly. Elite Tracker does not need to
be running: its mission stack view is the reference for the information shown here.
The monitor reports missions accepted by the tracked commander; it does not combine
ledgers from other wing members.

## Show the report

Run these commands from the source folder, using the same Python environment as the
monitor. On Windows, `py` below can be replaced by `.venv\Scripts\python.exe` if that
is where your environment lives.

```powershell
py afk_monitor.py --missions
py afk_monitor.py --missions --json
```

Both commands read saved ledgers and exit without starting monitoring, contacting
Discord/Telegram, or scanning journals. They list every saved profile. To refresh the
saved details, start the monitor normally with the relevant commander/profile and
let it load the current journal before requesting another report.

The normal terminal/Telegram session summary includes faction stack details. Discord
uses a shorter block with stack value, shareable value, ready-to-turn-in value and
issuer progress. Long Discord reports omit rows to fit the message budget; use
`--missions` or JSON for the full list.

To show the block, edit the existing `[LogLevels]` section in `afk_monitor.toml`:

```toml
[LogLevels]
SummaryMissions = 2
```

`0` hides it, `1` shows it in the terminal, and `2` also includes it in configured
Discord and Telegram summaries. Discord's edited live session message additionally
requires `WebhookLiveURL` in `[Discord]`. That message refreshes during monitoring;
`SummaryMissions` does not create a separate webhook or change the summary schedule.

## How a stack is counted

Missions from one issuing faction run in acceptance order. Matching local bounty
kills advance one unfinished mission for that issuer at a time. Different issuers
progress in parallel against the same target faction. Issuer names are matched
without regard to letter case.

For example, issuer A offers missions for 40 and 30 kills, and issuer B offers a
50-kill mission, all against the same target faction:

| Issuer | Missions | Required | Estimated credited kills after 10 matching kills | Left | Diff |
| --- | ---: | ---: | ---: | ---: | ---: |
| A | 2 | 70 | 10 | 60 | 0 |
| B | 1 | 50 | 10 | 40 | 20 |

The stack requires 70 unique kills initially, with 60 estimated kills remaining in
this example. `Diff` is the gap between an issuer's remaining requirement and the
largest remaining requirement for that target faction. A zero identifies an issuer
with the largest remaining stack. Requirements for different target factions are
added together in the overall kills-left figure.

Unknown issuers are treated as independent estimates rather than assuming they are
the same faction. Supply the issuer when importing missions for useful stack totals.

## Reading the fields

| Field | Meaning |
| --- | --- |
| Complete in the heading | Missions whose kill objectives are confirmed complete by `MissionRedirected`; manually imported missions without IDs complete when their estimated quota is reached. This is not the number of rewards claimed. |
| Need / Done / Left in the target row | Largest total issuer requirement / that requirement minus the largest remaining issuer requirement / largest remaining issuer requirement. `Done` is aggregate progress, not the sum of kills on every mission. |
| Issuer progress | Credited kills across that issuer's missions versus their total requirements. Confirmed missions contribute their full quotas. |
| Stack value | Sum of recorded rewards for missions still in the ledger, including objectives completed but not yet turned in. |
| Shareable value | Recorded reward on wing missions. It is not multiplied by wing size and does not confirm that a mission is currently shared. |
| Ready to turn in | Recorded reward on missions marked objective-complete. Shareable turn-in value counts wing missions only. |
| Avg/kill | Stack value divided by the unique initial requirement for the missions currently in the ledger. It is not a measured earnings rate. |
| Avg/mission | Stack value divided by the number of ledger missions. |
| Kill ratio | Sum of known mission quotas divided by the unique initial requirement. |
| Next completion | Smallest estimated quota remaining on the next unfinished mission for any issuer. `~0` can mean no further estimated kills are needed while journal confirmation is still pending. |
| Origin / Target | Acceptance system/station and destination system, recovered from journal events when available. Missing values show as `?`. |

`--missions` adds mission IDs, per-mission progress, wing flag, expiry, origin,
issuer, target system and payout. JSON includes raw mission fields and the summary's
`spread.rows` and `spread.stacks`, plus reward and efficiency totals.

When a mission is turned in, abandoned or failed, it leaves the ledger. Totals and
averages therefore describe the current stack, not lifetime earnings. Missions with
unknown targets or quotas are reported separately; known-stack totals are incomplete
until their details are recovered or imported.

## Accuracy and recovery

`Bounty` events identify a victim faction but do not identify every mission target.
Wing members' kills and missing bounty events can also make local estimates differ
from the game. Reaching an estimated quota does not confirm completion for a
journal-linked mission: `MissionRedirected` confirms the kill objective and fills its
quota. Use the in-game Transactions panel to check a discrepancy.

Acceptance details and progress are saved in `mission_ledger/<profile>.json` beside
the source files. On a `Missions` event, the monitor can search up to 20 recent
journals for missing acceptance details and origins. This lookup does not reconstruct
a complete historical kill count. Saved progress is preserved across restarts and
upgrades, including estimates recorded before the issuer attribution fix.

For a mission missing its acceptance event, import a CSV file:

```csv
Mission ID,Target Faction,Kills Required,Wing,Issuer,Reward
123456789,Example Pirates,40,yes,Example Issuing Faction,20000000
```

```powershell
py afk_monitor.py --import-missions missions.csv
py afk_monitor.py --missions
```

Stop the monitor before importing so its in-memory ledger cannot overwrite the edit.
An ID links the imported details to journal lifecycle events. Without IDs, rows are
manual estimates and complete at their estimated quotas. Plain lists such as
`Example Pirates,40,wing` are also supported, but omit issuer information. Include
IDs when available and verify them against your own journal; the example ID above is
illustrative. Re-importing replaces manual rows and preserves their progress when
target, quota and wing flag match. With multiple saved profiles, use
`--profile <profile>` to select the import destination.

The monitor reports completion; it does not automatically exit Elite Dangerous or
turn in missions.

## Sharing without a relay host

One wingmate can host for everyone: run their monitor with `--wing-serve 8099`
(or set `ServePort = 8099` under `[Wing]`, handy in Docker; `ServeHost` and
`ServeToken` cover the flag equivalents)
(it serves and accepts `.json` ledgers on that port). Everyone else sets
`PushURL`/`FetchURL` to `http://<that-machine>:8099/wing` - the monitor also
accepts PUTs, so pushed ledgers land in its `mission_ledger/wing` folder and
are immediately fetchable. `--wing-serve` alone binds to 127.0.0.1, so add
`--wing-serve-host 0.0.0.0` (or a specific interface address) for wingmates on
other machines; the host must be reachable (LAN, Tailscale, or a port
forward); if the port is busy at startup the monitor logs a warning and runs on.

On a shared network anyone can drop by, add `--wing-serve-token <value>` and
wingmates append `?token=<value>` to their `PushURL`/`FetchURL` (the filename
splices in before the query, so a base ending in `?token=…` works). Requests
without the right token are refused with 401; comparison is timing-safe.

Wingmate fetches time out after 5 seconds each; mesh and VPN links can be
slower, so `[Wing] FetchTimeout = 10` buys patience (per URL, both live and
`--missions --wing`).

`GET http://<host>:8099/wing/` lists every ledger the relay carries with its
last-save time - the fastest way to prove a URL (or a token) works.

A token-gated relay can also take the secret as a header, keeping it out of
shell history and proxy logs: `curl -H 'X-Wing-Token: sekret'
http://<host>:8099/wing/`. The 401 body names both forms; the monitor's own
`FetchURL`/`PushURL` configs keep using `?token=` in the URL.

When a wingmate's config looks right but the wing view stays empty, run the
diagnostic: `--wing-test http://<host>:8099/wing/` (or a single ledger URL)
prints what that address actually carries and exits nonzero if nothing
usable came back - tokens, typos and firewalls tell on themselves.

That listing doubles as config: point `FetchURL` at the relay address ending
in `/` (e.g. `http://<host>:8099/wing/`) and the monitor pulls every ledger
the host carries - no filenames, and a `?token=` on the base applies to them
all.

The relay address works for pushing too: `PushURL` or
`--wing-push http://<host>:8099/wing/` uploads every local ledger in one
line - trailing slash optional.

Set `WingTurnInAlert = 1000000` under `[Settings]` and the monitor nudges once
per session when the wing's completed-but-unbanked reward crosses that line -
recruitment offices pay wings more, and someone has to remember.

## Sharing ledgers with wingmates

Every commander keeps their own ledger; the monitor can fold copies together
into one wing view wherever a `SummaryMissions` summary, `--missions --wing`,
the `[w]` hotkey or the Discord mission embed renders missions.

Two ways to get wingmate ledgers in reach:

- **Shared folder** — wingmates copy their `mission_ledger/<profile>.json` into
  your `mission_ledger/wing/` folder (any network share that syncs files).
- **HTTP relay** — any host that accepts `PUT` works:

```powershell
py afk_monitor.py --wing-push https://relay.example/wing   # upload local ledgers, then exit
py afk_monitor.py --missions --wing --wing-url https://relay.example/wing/default.json
```

For hands-free operation, configure it once in `afk_monitor.toml`:

```toml
[Wing]
PushURL = 'https://relay.example/wing'                     # pushed on every monitor stop
PushEvery = 900                                            # ...and mid-session, every 15 minutes
FetchURL = ['https://relay.example/wing/ana.json',
            'https://relay.example/wing/bruno.json']       # folded into summaries (SummaryMissions = 3)
```

Unreachable fetch URLs warn once per outage, not per summary, and whatever
answered still joins the wing line. A ledger nobody updated for over 24 hours
(died, or forgot to run the monitor) is flagged in the same warnings as
'mate: no updates for Nh - counts may be stale' while its numbers still count. Tune the quiet period with `StaleHours` under `[Wing]` (default 24).
