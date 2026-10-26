# README report previews

The README images show simulated mission data rendered by the current
`MissionLedger.report_text()` and `MissionLedger.report_live()` functions. They are
documentation previews rather than captures of a live game or Discord account.
The terminal image shows the mission section of a session summary. The Discord
frame illustrates message styling; its mission text comes from the real formatter.

The fixture uses five wing missions from three issuers, 25 matching local kills,
and one journal-confirmed objective completion. This demonstrates serial and parallel
progress, stack differences, shareable rewards and ready-to-turn-in value. No user
configuration, mission ledger, webhook or network service is accessed by the fixture.

## Regenerate

From the repository root, use Python 3.12+ to generate the HTML and exact report text:

```sh
python scripts/build-readme-previews.py
```

To capture the PNGs, use Node.js with the optional Playwright package and its Chromium
browser available in your development environment:

```sh
node scripts/capture-readme-previews.cjs
```

Playwright is a documentation dependency only; it is not required to run AFK Monitor.
If using an existing Chrome installation, set `PREVIEW_CHROME` to its executable path
before running the capture script. The script uses a fresh browser session.

Generated HTML/text go to `work/readme-previews/`; the capture script writes
`images/wing-stacks-terminal.png` and `images/wing-stacks-discord.png`.
Both scripts accept custom paths: `--output-dir` for the Python builder, then an
input directory and optional image output directory for the capture script.

Inspect both PNGs before committing them. Check labels, wrapping, clipping and that
the numbers agree between the two views. Keep the simulated-data labels visible and
update this fixture when the report fields change. Commit the scripts and PNGs;
generated HTML/text are temporary working files and need not be committed.
