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


class CliDocumentationTests(unittest.TestCase):
    def test_every_cli_flag_is_documented(self):
        """README's launch table must list every option argparse offers;
        a flag nobody documented is a flag nobody finds."""
        import subprocess, sys
        result = subprocess.run([sys.executable, str(ROOT / "afk_monitor.py"), "--help"],
                                capture_output=True, text=True, timeout=30)
        flags = set(re.findall(r"^\s+(--\w[\w-]*)", result.stdout, re.M))
        self.assertGreater(len(flags), 10, "too few flags parsed from --help")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        missing = [flag for flag in sorted(flags) if flag not in readme]
        self.assertEqual(missing, [])
        documented = set(re.findall(r"^(--[a-z][\w-]*)", readme, re.M))
        invented = sorted(flag for flag in documented if flag not in flags)
        self.assertEqual(invented, [], "README lists flags argparse does not offer")


class VersionFlagTests(unittest.TestCase):
    def test_version_flag_reports_the_built_version(self):
        import subprocess, sys
        result = subprocess.run([sys.executable, str(ROOT / "afk_monitor.py"), "--version"],
                                capture_output=True, text=True, timeout=30, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        import sys as _s
        self.assertIn(f"Python {_s.version.split()[0]}", result.stdout)
        version = re.search(r"VERSION = (\d+)", (ROOT / "afk_monitor.py").read_text(encoding="utf-8")).group(1)
        self.assertIn(version, result.stdout)
        self.assertIn("ED AFK Monitor", result.stdout)


class ServeDirExpansionTests(unittest.TestCase):
    def test_serve_dir_expands_home_from_flag_or_config(self):
        # the exact line the relay startup uses, exec'd against stubs:
        # if afk stops calling expanduser here, this test fails
        line = next(l.strip() for l in (ROOT / "afk_monitor.py").read_text(encoding="utf-8")
                    .splitlines() if "serve_dir = " in l and "expanduser" in l)
        ns = {"os": __import__("os"),
              "args": type("A", (), {"wing_serve_dir": "~/shared"})(),
              "conf_wing": {"ServeDir": "~/ignored-when-flag-set"}}
        exec(line, ns)
        self.assertEqual(ns["serve_dir"],
                         __import__("os").path.expanduser("~/shared"))
        ns["args"].wing_serve_dir = ""
        exec(line, ns)
        self.assertEqual(ns["serve_dir"],
                         __import__("os").path.expanduser("~/ignored-when-flag-set"))
