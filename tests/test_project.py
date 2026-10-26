import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProjectPackagingTests(unittest.TestCase):
    def modules(self):
        return sorted(path.stem for path in ROOT.glob("*.py"))

    def test_pyproject_version_matches_monitor_version(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        source = (ROOT / "afk_monitor.py").read_text(encoding="utf-8")
        version = re.search(r"^VERSION = (\d+)$", source, re.M).group(1)
        self.assertEqual(pyproject["project"]["version"], version)

    def test_pyproject_ships_every_module(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(pyproject["tool"]["setuptools"]["py-modules"]), self.modules())

    def test_dockerfile_copies_every_module_and_data(self):
        copied = set()
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
        for chunk in re.findall(r"^COPY (.+)$", dockerfile, re.M):
            copied.update(chunk.split())
        for module in self.modules():
            self.assertIn(f"{module}.py", copied)
        self.assertIn("ships.json", copied)
