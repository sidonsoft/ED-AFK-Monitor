"""hotkey_wing_report's relay line, exec'd straight from the source."""
import ast
import textwrap
import unittest
from pathlib import Path

from missions import merge_spreads

ROOT = Path(__file__).resolve().parents[1]


def load_hotkey(serve_label):
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
        "wing_view": lambda: ([], ["mate.json: no updates for 30h - counts may be stale"]),
        "mission_ledger": type("L", (), {"spread": lambda self: {
            "done": 1, "total": 3, "left": 2, "turn_in": 250000, "rows": []}})(),
        "merge_spreads": merge_spreads,
        "wing_serve_label": serve_label,
    }
    exec(body, namespace)
    return namespace["hotkey_wing_report"]()


class HotkeyWingRelayTests(unittest.TestCase):
    def test_relay_address_named_when_serving(self):
        text = load_hotkey("0.0.0.0:8099")
        self.assertIn("serves the wing on 0.0.0.0:8099", text)
        self.assertIn("250,000 Cr ready", text)
        self.assertIn("no updates for 30h", text)

    def test_quiet_when_not_serving(self):
        self.assertNotIn("serves the wing", load_hotkey(""))
