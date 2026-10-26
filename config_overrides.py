"""Command-line config overrides: --set Section.Key=value."""
import json

def parse_overrides(pairs, warn=None):
    """Parse 'Section.Key=value' strings into (section, key, typed value)."""
    overrides = []
    for pair in pairs:
        if "=" not in pair or "." not in pair.split("=", 1)[0]:
            if warn:
                warn(f"Config override ignored, expected Section.Key=value: '{pair}'")
            continue
        dotted, text = pair.split("=", 1)
        section, _, key = dotted.partition(".")
        if "." in key:
            if warn:
                warn(f"Config override ignored, expected Section.Key=value: '{pair}'")
            continue
        if not section.strip() or not key.strip():
            if warn:
                warn(f"Config override ignored, expected Section.Key=value: '{pair}'")
            continue
        overrides.append((section.strip(), key.strip(), _parse_value(text)))
    return overrides


def _parse_value(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    try:
        return json.loads(text)
    except ValueError:
        return text


def apply_overrides(conf, section, overrides, warn=None):
    """Overlay matching overrides onto a settings dict, type-checked against defaults."""
    for sec, key, value in overrides:
        if sec != section:
            continue
        if key not in conf:
            if warn:
                warn(f"Config override ignored: '{section}' has no setting '{key}'")
            continue
        current = conf[key]
        if isinstance(current, bool):
            if not isinstance(value, bool):
                if warn:
                    warn(f"Config override '{section}.{key}' expects true/false")
                continue
        elif isinstance(current, int):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                if warn:
                    warn(f"Config override '{section}.{key}' expects a number")
                continue
            if isinstance(value, float) and float(value).is_integer():
                value = int(value)
            elif isinstance(value, float):
                if warn:
                    warn(f"Config override '{section}.{key}' expects a whole number")
                continue
        elif isinstance(current, list):
            if not isinstance(value, list):
                if warn:
                    warn(f"Config override '{section}.{key}' expects a JSON list, e.g. [15,30]")
                continue
        elif not isinstance(value, str):
            value = str(value)
        conf[key] = value
