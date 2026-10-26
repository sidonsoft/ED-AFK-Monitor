import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from instance_runner import parse_instances, run_instances

EXIT0 = "import sys; sys.exit(0)"
EXIT2 = "import sys; sys.stderr.write('boom\\n'); sys.exit(2)"


class ParseInstancesTests(unittest.TestCase):
    def test_valid_and_invalid_specs(self):
        with TemporaryDirectory() as folder:
            warnings = []
            instances = parse_instances([f"one={folder}", "broken", f"two={folder}/nope",
                                         f"one={folder}"], warnings.append)
            self.assertEqual([label for label, _ in instances], ["one"])
            self.assertEqual(len(warnings), 3)

    def test_windows_drive_paths_survive(self):
        with TemporaryDirectory() as folder:
            instances = parse_instances([f"cmdr={folder}"])
            self.assertEqual(len(instances), 1)


class RunInstancesTests(unittest.TestCase):
    def test_clean_exits_report_once_each(self):
        with TemporaryDirectory() as folder:
            reports = []
            code = run_instances([("a", folder)], (), python=[sys.executable, "-c", EXIT0],
                                 script="ignored", report=reports.append)
            self.assertEqual(code, 0)
            self.assertEqual(sum(1 for line in reports if "finished cleanly" in line), 1)

    def test_crashes_restart_bounded(self):
        with TemporaryDirectory() as folder:
            reports = []
            code = run_instances([("a", folder)], (), python=[sys.executable, "-c", EXIT2],
                                 script="ignored", report=reports.append, max_restarts=1)
            self.assertEqual(code, 0)
            starts = [line for line in reports if "watching" in line]
            self.assertEqual(len(starts), 2)  # initial + one restart
            self.assertIn("no restarts left", "\n".join(reports))

    def test_no_valid_instances_returns_error(self):
        reports = []
        self.assertEqual(run_instances([], (), report=reports.append), 1)
