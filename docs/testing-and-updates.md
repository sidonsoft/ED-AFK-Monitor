# Testing and updating the source build

The wing stack changes are in the source build. Upstream standalone executables do
not include this fork's additions. Keep the whole source folder together and use
Python 3.12 or newer. Discord support additionally needs `discord-webhook` in that
Python environment.

## Update an existing installation

1. Stop the monitor normally with Ctrl+C and allow it to finish saving and delivery.
2. Back up the source folder, `afk_monitor.toml`, `mission_ledger/`, and
   `pending_notifications/`. Keep the backup private: configuration may contain
   webhook URLs or bot tokens, and pending files contain notification text.
3. Record the current Git revision with `git rev-parse HEAD`, and check
   `git status --short`. Preserve local edits before changing branches.
4. The wing mission stack feature is merged into this fork's `main` branch. For a
   clean checkout on `main`, update with:

   ```powershell
   git switch main
   git pull --ff-only origin main
   ```

   If the installed checkout is still on `testing/wing-mission-stacks`, stop the
   monitor and switch to `main` before pulling. Run Git commands in the checkout
   used by your launcher; its Python executable can live in a different folder
   from the source it runs.
5. Keep your existing `afk_monitor.toml` and saved state. Do not replace them with the
   example config. Set `SummaryMissions = 2` in the existing `[LogLevels]` section if
   you want stacks included in notification summaries.
6. Run the checks below, then start through your existing launcher. No game restart
   is required. Confirm the startup journal and commander/profile are correct.

## Checks before a live test

Use the Python interpreter configured in your launcher for these commands:

```powershell
py -m unittest discover -s tests -v
py afk_monitor.py --missions
py afk_monitor.py --missions --json
```

The regression suite uses simulated journals and mocked transports; it does not
send notifications. The report commands read saved state without contacting
notification services. The installed wing stack build passed 102 tests on the
Windows gaming PC; automated checks also passed on Windows, macOS and Linux with
Python 3.12 and 3.13.

For a notification preview, start with `--test`; output is previewed in the terminal
instead of sent. Normal starts resume configured notification delivery, including
pending messages.

## Live verification

- Compare the number of active wing missions and total/shareable reward with the
  in-game list or Elite Tracker. Compare issuer counts and required quotas first;
  kill estimates can differ for the reasons in the [stack guide](wing-missions.md#accuracy-and-recovery).
- Confirm origin and target systems populate after journal loading. Run `--missions`
  again to read the refreshed ledger.
- Check that a qualifying local kill advances one mission per issuer, with different
  issuers advancing in parallel. Check a `MissionRedirected` event marks the objective
  complete and increases the ready-to-turn-in value.
- Check the terminal and any configured Discord/Telegram summaries. A long Discord
  block is intentionally abbreviated; the saved report and JSON contain the full list.
- Stop and restart the monitor and confirm the saved mission state is retained.

Do not infer objective completion from an estimated zero kills left alone. The game
and its completion event remain the confirmation for journal-linked missions.

## Roll back

Stop the monitor before rolling back. Switch to the previous recorded Git revision
or restore the backed-up source files. Keep the current settings and ledger unless
you specifically need the older snapshot; restoring an old ledger loses progress
recorded since that backup. Preserve the current folder before replacing it, and
restart using the same launcher and Python environment. Avoid a hard reset or clean
that could remove local settings or saved state.

The 26 October 2026 gaming-PC installation has a backup under
`Documents/Codex/2026-10-26/afk-wing-stack-test/backup-20261026-143217` in the Windows
user profile. Its `previous-revision.txt` records the preceding source revision.
This path is specific to that test installation.
