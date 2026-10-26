"""Multi-instance supervisor: one command watches several journal folders.

Each instance (label=journal folder) runs its own monitor process with its
own profile, so mission ledgers, history, outbox and previews stay separated
while a single Ctrl+C stops everything. Useful for multi-CMDR rigs or
watching alpha/live installs side by side.
"""
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_RESTARTS = 3


def parse_instances(specs, warn=None):
    """Parse 'label=journal_folder' strings; validation with warnings."""
    instances = []
    seen = set()
    for spec in specs:
        label, sep, path = str(spec).partition("=")
        label, path = label.strip(), path.strip()
        if not sep or not label or not path:
            if warn:
                warn(f"Instance ignored, expected label=journal_folder: '{spec}'")
            continue
        expanded = Path(path).expanduser()
        if not expanded.is_dir():
            if warn:
                warn(f"Instance '{label}' skipped, journal folder not found: {path}")
            continue
        if label in seen:
            if warn:
                warn(f"Instance '{label}' duplicated; keeping the first definition")
            continue
        seen.add(label)
        instances.append((label, str(expanded)))
    return instances


def run_instances(instances, child_args=(), python=None, script=None, report=None,
                  max_restarts=DEFAULT_RESTARTS, env=None):
    """Start every instance, restart crashes (bounded), stop all on Ctrl+C."""
    python = python or sys.executable
    if isinstance(python, str):
        python = [python]
    script = script or str(Path(sys.argv[0]).resolve())
    report = report or (lambda line: None)
    paths = dict(instances)
    procs = {}
    restarts = {label: 0 for label in paths}

    def spawn(label):
        procs[label] = subprocess.Popen([*python, script, "-j", paths[label], "-p", label,
                                         *child_args], env=env)
        report(f"Instance {label}: watching {paths[label]}")

    for label in paths:
        spawn(label)
    if not procs:
        report("No valid instances to watch")
        return 1
    try:
        while procs:
            for label, proc in list(procs.items()):
                code = proc.poll()
                if code is None:
                    continue
                del procs[label]
                if code == 0:
                    report(f"Instance {label}: finished cleanly")
                elif restarts[label] < max_restarts:
                    restarts[label] += 1
                    report(f"Instance {label}: exited {code}; restarting ({restarts[label]}/{max_restarts})")
                    time.sleep(1)
                    spawn(label)
                else:
                    report(f"Instance {label}: exited {code}; no restarts left")
            time.sleep(0.5)
    except KeyboardInterrupt:
        # A repeated interrupt during cleanup (patched sleeps in tests) must
        # not escape: force-kill stragglers and still return cleanly.
        report("Stopping all instances...")
        for proc in procs.values():
            try:
                proc.terminate()
            except OSError:
                pass
        deadline = time.monotonic() + 5
        for proc in procs.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                pass
        for proc in procs.values():
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
    return 0
