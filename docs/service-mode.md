# Running as a service (headless rigs)

For a box that only watches journals (server, NAS, second PC), run the monitor
with `--plain` so it writes clean lines instead of a live status bar, and let the OS supervisor keep it alive and capture logs.

No shell flags in your supervisor? Set `Dashboard = <port>` under `[Settings]`
in `afk_monitor.toml` (plus `DashboardToken`/`DashboardHost`) and the dashboard
starts with the monitor.
the OS supervisor keep it alive and capture logs.

## Linux (systemd user service)

`~/.config/systemd/user/ed-afk-monitor.service`:

```ini
[Unit]
Description=ED AFK Monitor
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/ED-AFK-Monitor
ExecStart=/usr/bin/python3 afk_monitor.py --plain
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now ed-afk-monitor
journalctl --user -u ed-afk-monitor -f     # logs, rotation handled by journald
```

## macOS (launchd agent)

`~/Library/LaunchAgents/com.edafkmonitor.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.edafkmonitor</string>
  <key>ProgramArguments</key><array>
    <string>/usr/local/bin/python3</string>
    <string>afk_monitor.py</string>
    <string>--plain</string>
  </array>
  <key>WorkingDirectory</key><string>~/ED-AFK-Monitor</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>NetworkState</key><true/></dict>
  <key>StandardOutPath</key><string>/tmp/ed-afk-monitor.log</string>
  <key>StandardErrorPath</key><string>/tmp/ed-afk-monitor.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.edafkmonitor.plist
```

## Windows (Task Scheduler)

1. *Create Task* → trigger **At log on** (and repeat every 5 minutes, enabled,
   duration 1 day, for keep-alive; untick *Stop task if it runs longer*).
2. Action: `python` · Arguments: `afk_monitor.py --plain` · Start in: the
   folder containing `afk_monitor.py`.
3. Settings: tick *If the task fails, restart every minute*; untick *Stop the
   task if it runs longer than*.
4. For logs, wrap the action: `cmd /c "python afk_monitor.py --plain >> logs\afk.log 2>&1"`
   and rotate `afk.log` periodically (or use ntfy/Discord alerts instead of
   reading logs).

## Docker

See the `Dockerfile` header comment: mount the Frontier journal folder
read-only plus a named volume for config, ledgers and history. This is the
simplest option for NAS boxes (Synology/QNAP container UI accepts the same
volume mapping). Containers that can't pass CLI flags can still enable the
web page: set `Dashboard = 8080` (plus `DashboardToken`/`DashboardHost`) in
the config volume and publish the port, e.g. `-p 8080:8080`.

A container can also host the **wing relay** for the whole wing: set
`ServePort = 8099` (plus optional `ServeHost`/`ServeToken`) under `[Wing]` in
the same config volume, publish it with `-p 8099:8099`, and wingmates point
their `PushURL`/`FetchURL` at `http://<this-host>:8099/wing`. With a token set,
they append `?token=<value>`; without one, keep the port off the open Internet.

## Keeping a headless instance useful

- Set `AlertSound` has no meaning without a terminal; use ntfy or Discord
  webhooks for level-3 alerts instead.
- The notification outbox retries anything that failed while the network or
  Discord was down, so brief drops are not lost alerts.
- `--dashboard <port> --dashboard-host 0.0.0.0 --dashboard-token <secret>`
  exposes the live page on the LAN for the rest of the household/wing - the
  same settings live in `[Settings]` (`Dashboard`, `DashboardHost`,
  `DashboardToken`) when flags are inconvenient.
