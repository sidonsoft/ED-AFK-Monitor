"""Terminal hotkeys: status, mission spread and alert mute while the monitor runs.

The command layer is pure (key -> callback) so it is unit-testable; the
watcher thread only turns stdin keypresses into those commands.
"""
import threading

COMMAND_NAMES = {"status": "session status", "missions": "mission spread", "mute": "mute/unmute alerts"}
PRESET_MAPS = {
    "status,missions,mute": {"s": "status", "m": "missions", "u": "mute"},
    "1,2,3": {"1": "status", "2": "missions", "3": "mute"},
}


def parse_keymap(spec, warn=None):
    """Parse 'status=S,missions=M,mute=U'; '1,2,3' and 'status,missions,mute' presets."""
    spec = (spec or "").strip()
    if spec in PRESET_MAPS:
        return dict(PRESET_MAPS[spec])
    keymap = {}
    for part in (p.strip() for p in spec.split(",")):
        if not part or "=" not in part:
            if warn:
                warn(f"Hotkey mapping ignored: '{part}' (expected command=KEY)")
            continue
        name, _, key = part.partition("=")
        name, key = name.strip().lower(), key.strip()
        if name not in COMMAND_NAMES:
            if warn:
                warn(f"Hotkey ignored: unknown command '{name}' ({', '.join(sorted(COMMAND_NAMES))})")
            continue
        if len(key) != 1:
            if warn:
                warn(f"Hotkey mapping ignored: '{name}={key}' (one character expected)")
            continue
        keymap[key] = name
    return keymap


def run_keypress(key, callbacks, log):
    """Dispatch one keypress; unknown keys are quietly ignored."""
    target = callbacks.get("__keymap__", {}).get(key)
    if target is None:
        return
    callback = callbacks.get(target)
    if not callable(callback):
        return
    report = str(callback() or "").strip()
    if report:
        for line in report.splitlines():
            log(f"\u2328\ufe0f {line}")
    elif target != "mute":
        log(f"\u2328\ufe0f {target}: nothing to report")


def format_keymap(keymap):
    return " · ".join(f"{command} [{key}]" for key, command in sorted(keymap.items(), key=lambda kv: kv[1]))


def start_hotkeys(keymap, callbacks, stop_event):
    """Watch stdin for hotkeys on a daemon thread; returns a start-error string."""
    lookup = dict(callbacks)
    lookup["__keymap__"] = keymap

    def emit(line):
        print(line, flush=True)

    def watch():
        try:
            import msvcrt  # Windows: keyboard is always available
        except ImportError:
            msvcrt = None
        if msvcrt is not None:
            while not stop_event.is_set():
                if msvcrt.kbhit():
                    run_keypress(msvcrt.getwch(), lookup, emit)
                else:
                    stop_event.wait(0.25)
            return
        try:
            import sys
            import termios
            import tty
        except ImportError:
            return  # no usable terminal (e.g. run under a service)
        if not sys.stdin.isatty():
            return
        saved = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())
            while not stop_event.is_set():
                key = msvcrt_or_read(sys.stdin, stop_event)
                if key:
                    run_keypress(key, lookup, emit)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)

    def msvcrt_or_read(stream, event):
        import select
        ready, _, _ = select.select([stream], [], [], 0.25)
        return stream.read(1) if ready else ""

    try:
        threading.Thread(target=watch, daemon=True).start()
    except (RuntimeError, OSError) as failure:
        return f"Hotkeys unavailable: {failure}"
    return ""
