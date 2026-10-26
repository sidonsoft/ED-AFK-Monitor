"""Persist Section.Key=value back into afk_monitor.toml, preserving comments.

Deliberately dependency-free (the app already only reads TOML via tomllib,
which cannot write). This edits line-oriented: values are rewritten in place
so all comments and layout around them survive.
"""
import os
import re
from pathlib import Path

SECTION_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*$")
KEY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=")


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _comment_suffix(line):
    """Return the trailing '# ...' comment of a key line, quote-aware, or ''."""
    in_quote = None
    for position, char in enumerate(line):
        if in_quote:
            if char == in_quote:
                in_quote = None
        elif char in "\"'":
            in_quote = char
        elif char == "#" and position > 0:
            return line[position:].rstrip()
    return ""


def set_config_text(text, section, key, value):
    """Return text with section.key set to value; '' if section/key look invalid."""
    new_line = f"{key} = {toml_value(value)}"
    lines = text.splitlines()
    section_index = None
    for index, line in enumerate(lines):
        match = SECTION_RE.match(line)
        if match and match.group(1) == section:
            section_index = index
            break
    if section_index is None:
        if not SECTION_RE.match(f"[{section}]"):
            return ""
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"[{section}]", new_line])
        return "\n".join(lines) + "\n"
    # within the section: replace an existing key or insert after the last entry
    end = len(lines)
    last_entry = section_index
    for index in range(section_index + 1, len(lines)):
        if SECTION_RE.match(lines[index]):
            end = index
            break
        if KEY_RE.match(lines[index]):
            last_entry = index
            if KEY_RE.match(lines[index]).group(1) == key:
                comment = _comment_suffix(lines[index])
                lines[index] = new_line + (f"  {comment}" if comment else "")
                return "\n".join(lines) + "\n"
    lines.insert(last_entry + 1 if last_entry != section_index else section_index + 1, new_line)
    return "\n".join(lines) + "\n"


def save_overrides(path, overrides):
    """Apply [(section, key, value)] to the file at path; returns '' or an error."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as failure:
        return f"Could not read {path}: {failure}"
    for section, key, value in overrides:
        updated = set_config_text(text, section, key, value)
        if not updated:
            return f"Invalid section name for '{section}.{key}'"
        text = updated
    temp = Path(str(path) + ".tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    except OSError as failure:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return f"Could not write {path}: {failure}"
    return ""
