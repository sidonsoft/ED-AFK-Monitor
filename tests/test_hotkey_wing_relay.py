"""hotkey_wing_report's relay line, exec'd straight from the source."""
import ast
import textwrap
import unittest
from pathlib import Path

from missions import merge_spreads

ROOT = Path(__file__).resolve().parents[1]


def load_hotkey(serve_label, wings=(), errors=None, local_total=3, turn_in=250000,
              turnin_alert=0):
    if errors is None:  # a stale wingmate by default: matches the shipped report shape
        errors = ["mate.json: no updates for 30h - counts may be stale"]
    source = (ROOT / "afk_monitor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    def find(node):
        for child in getattr(node, "body", []):
            if isinstance(child, ast.FunctionDef) and child.name == "hotkey_wing_report":
                return child
            found = find(child)
            if found:
                return found

    func = find(tree)
    lines = source.splitlines()
    body = textwrap.dedent("\n".join(lines[func.lineno - 1:func.end_lineno]))
    namespace = {
        "wing_view": lambda: (list(wings), list(errors)),
        "mission_ledger": type("L", (), {"spread": lambda self: {
            "done": min(1, local_total), "total": local_total, "left": 2,
            "turn_in": turn_in, "rows": []}})(),
        "merge_spreads": merge_spreads,
        "wing_serve_label": serve_label,
        "wing_serve_dirs": [],
        "served_count": lambda dirs: 2,
        "wing_last_push": {"name": "putmate.json", "size": 4096, "at": 1761500000},
        "time": __import__("time"),
        "conf_settings": {"WingTurnInAlert": turnin_alert},
        "turnin_notice": __import__("missions").turnin_notice,
    }
    exec(body, namespace)
    return namespace["hotkey_wing_report"]()


class HotkeyEmptyWingTests(unittest.TestCase):
    def test_nothing_configured_explains_how_to_start(self):
        text = load_hotkey("", local_total=0, turn_in=0, errors=[])
        self.assertIn("No wing ledgers yet", text)
        self.assertIn("--wing-test <URL>", text)  # points at the one-shot probe
        self.assertIn("--wing-serve", text)

    def test_errors_still_take_the_report(self):
        text = load_hotkey("", local_total=0,
                           errors=["mate.json: no updates for 30h - counts may be stale"])
        self.assertIn("no updates for 30h", text)
        self.assertNotIn("No wing ledgers yet", text)


class HotkeyWingRelayTests(unittest.TestCase):
    def test_relay_address_named_when_serving(self):
        text = load_hotkey("0.0.0.0:8099")
        self.assertIn("serves the wing on 0.0.0.0:8099", text)
        self.assertIn("(2 ledgers)", text)
        self.assertIn("last push: putmate.json (4,096 bytes) at", text)

    def test_turnin_alert_shown_when_configured(self):
        text = load_hotkey("", turnin_alert=100000)
        self.assertIn("250,000 Cr of completed missions ready", text)
        text = load_hotkey("", turnin_alert=1_000_000)
        self.assertNotIn("ready to turn in", text)
        self.assertIn("250,000 Cr ready", text)
        self.assertIn("no updates for 30h", text)

    def test_quiet_when_not_serving(self):
        self.assertNotIn("serves the wing", load_hotkey(""))
