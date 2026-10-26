# Roadmap — potential features

Generated 2026‑10‑26 from a review of this fork at `b5e3b3b`. Each item: value/effort in
(★ impact / † effort). Grouped by theme; suggested order: Now → Next → Later.

## Now — correctness & user-visible wins ✅ (branch `roadmap/now`)

1. ✅ **Per-channel message length policy** (★★ / †) — Discord's 2000-char cap currently drops long
   mission/summary reports silently; split or truncate with "--missions" pointer, like Telegram's
   4096 chunking.
2. ✅ **Fork-aware update check** (★★ / †) — `GITHUB_REPO` still points at upstream `PsiPab`; add a
   `Repo` config key so the fork advertises *its* releases (and keeps a link to upstream).
3. ✅ **Fix small warning-noise bugs** (★ / †) — fuel-burn divide-by-zero guard, `j.get("VictimFaction")`
   fallback for pre-2021 journal lines, Discord avatar hosted in-repo instead of a Discord
   attachment CDN URL.
4. ✅ **Ship pools as a data file** (★★ / †) — move `SHIPS_EASY`/`SHIPS_HARD` to a JSON/CSV shipped
   with the app so Frontier adds/renames don't need code edits; option to auto-refresh from game data.

## Next — insight & value tracking

5. **Session database + history** (★★★ / ††) — SQLite log of sessions (duration, kill/scan rates,
   bounties/hr, merits, mission value); `--history` trend report; answers "is this ring better
   than last week?"
6. ✅ **Projected income per hour** (★★★ / †) — combine live kill rate with mission reward/kill
   estimates (already in the ledger) to show live cr/h *including* mission turn-in value, plus
   "stack completes in ~X min at current rate" projections.
7. ✅ **Mission expiry alarms** (★★ / †) — expiry dates are already stored; warn when an incomplete
   mission or a high-value stack is about to expire while farming.
8. **Discord embeds for mission spread** (★★ / †) — the live/summary blocks are plain text;
   embeds with fields, colours, and progress bars would make wing reports far scannable.
9. **Better kill→mission attribution** (★★ / ††) — use `VictimFaction` + system match +
   `AwardingFaction` from FactionKillBond to reduce estimate error; surface confidence in the report.

## Later — platform & UX

10. **`main()` refactor + typed core** (★★ / †† for future features) — config dataclass, event
    handlers as methods, injectable transports; unlocks unit-testing without the subprocess
    bootstrap and paves the way for everything below.
11. **More channels** (★★ / † each) — ntfy, Pushover, generic JSON webhook, Home Assistant,
    email; reuse the dispatcher/outbox layer unchanged.
12. **Local web dashboard** (★★★ / ††) — serve the existing `spread()`/`export()` JSON plus live
    stats as a localhost page; read-only, token-protected share link for wingmates without
    Discord.
13. **Wing-shared ledgers** (★★ / ††) — merge ledgers from multiple wing members (a small relay or
    a shared folder/URL) so the stack view reflects the whole wing, not one CMDR.
14. **Interactive commands** (★★ / ††) — a Discord bot mode (`/stats`, `/missions`, `/mute`)
    complementing the webhook, or hotkeys on the terminal.
15. **Session export & shareable card** (★★ / †) — CSV/JSON export per session, and a rendered
    PNG summary card for posting after a farm.
16. **Packaging & ops** (★★ / ††) — `pip install ed-afk-monitor` entry point, GitHub Actions
    PyInstaller release builds, `--self-update` link, log file with rotation, service mode
    (Task Scheduler/launchd/systemd) and Docker for headless rigs.
17. **Config quality of life** (★ / †) — `tomlkit` so the app can edit config without destroying
    comments; `--set key=value` CLI; config hot-reload.
18. **Journal schema drift detector** (★ / †) — count unknown events/fields and report them in the
    summary; add a corpus of real journal fixtures to CI so game updates surface as failing tests,
    not silent warnings.
19. **Multi-instance watch mode** (★★ / ††) — one process tailing several journal folders
    (multi-CMDR setups, alpha + live), each with its own profile/outbox.
20. **Audio/desktop alerts** (★ / †) — optional local sound on level-3 events for in-room
    notification alongside remote channels.

## Explicit non-goals (keep scope tight)

- No game-state injection/automation (journal-only, stays anti-cheat safe).
- No account-wide economy (keep Elite Dangerous journal AFK farming as the product).
