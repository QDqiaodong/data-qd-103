"""End-to-end tests that drive ``python -m cold_exposure.consensus``.

The CLI is launched as a real subprocess from a directory outside the project,
with isolated mode (``-I``), so the exercise mirrors an offline operator run and
cannot pick up planted modules on the current working directory.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "data" / "consensus"

# On this interpreter -I also removes the current directory from sys.path, so
# the package is located through an explicit path insert inside an isolated
# interpreter. PYTHONPATH and the current directory cannot inject modules.
_LAUNCHER = (
    "import runpy, sys; "
    "sys.path.insert(0, %r); "
    "module = sys.argv[1]; "
    "sys.argv = [module] + sys.argv[2:]; "
    "runpy.run_module(module, run_name='__main__')"
    % str(PROJECT)
)


def module_command(module, arguments):
    # With -c, sys.argv is ['-c', module, *arguments].
    return [sys.executable, "-I", "-c", _LAUNCHER, module, *arguments]


class CliEndToEndTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        self.output = self.dir / "report.json"

    def tearDown(self):
        self._dir.cleanup()

    def run_cli(self, gap=900, expect_success=True):
        command = module_command(
            "cold_exposure.consensus",
            [
                "--trips", str(DATA / "trips.csv"),
                "--assignments", str(DATA / "assignments.csv"),
                "--readings", str(DATA / "readings.csv"),
                "--max-gap-seconds", str(gap),
                "--output", str(self.output),
            ],
        )
        completed = subprocess.run(
            command, cwd=self.dir, env={"PATH": "/usr/bin:/bin"},
            capture_output=True, text=True,
        )
        if expect_success:
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)
            return json.loads(self.output.read_text(encoding="utf-8"))
        self.assertNotEqual(completed.returncode, 0)
        return completed

    def trip(self, report, trip_id):
        return next(trip for trip in report["trips"] if trip["trip_id"] == trip_id)

    def test_report_envelope_and_deterministic_bytes(self):
        report = self.run_cli()
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["parameters"]["max_gap_seconds"], 900)
        self.assertEqual(set(report["inputs_sha256"]),
                         {"trips", "assignments", "readings"})
        first = self.output.read_bytes()
        self.run_cli()
        self.assertEqual(first, self.output.read_bytes())
        # No machine-specific content leaked into the report.
        text = self.output.read_text(encoding="utf-8")
        self.assertNotIn(str(self.dir), text)

    def test_t1_single_point_consensus_excursion(self):
        trip = self.trip(self.run_cli(), "T1")
        partition = trip["partitions"][0]
        self.assertEqual(trip["duration_seconds"], 900)
        self.assertEqual(partition["consensus_components"],
                         [{"lower": "11", "upper": "11"}])
        self.assertEqual(trip["classification_seconds"]["above"], 900)
        self.assertEqual(trip["definitely_outside_seconds"], 900)
        self.assertEqual(trip["possibly_outside_seconds"], 900)
        self.assertEqual(trip["supported_degree_minutes_min"], "45.000000")
        self.assertEqual(trip["supported_degree_minutes_max"], "45.000000")
        self.assertEqual(trip["longest_same_side_confirmed_seconds"], 900)
        self.assertEqual(trip["status"], "confirmed_excursion")

    def test_t2_split_components_keep_hole_empty(self):
        trip = self.trip(self.run_cli(), "T2")
        components = trip["partitions"][0]["consensus_components"]
        self.assertEqual(components,
                         [{"lower": "0", "upper": "1"}, {"lower": "9", "upper": "10"}])
        self.assertEqual(trip["classification_seconds"]["mixed"], 600)
        self.assertEqual(trip["definitely_outside_seconds"], 600)
        self.assertEqual(trip["possibly_outside_seconds"], 600)
        self.assertEqual(trip["supported_degree_minutes_min"], "10.000000")
        self.assertEqual(trip["supported_degree_minutes_max"], "20.000000")
        self.assertEqual(trip["longest_same_side_confirmed_seconds"], 0)

    def test_t3_partial_overlap_possible_only(self):
        trip = self.trip(self.run_cli(), "T3")
        self.assertEqual(trip["partitions"][0]["consensus_components"],
                         [{"lower": "7", "upper": "9"}])
        self.assertEqual(trip["definitely_outside_seconds"], 0)
        self.assertEqual(trip["possibly_outside_seconds"], 600)
        self.assertEqual(trip["supported_degree_minutes_min"], "0.000000")
        self.assertEqual(trip["supported_degree_minutes_max"], "10.000000")

    def test_reasons_and_assignment_evidence(self):
        report = self.run_cli()
        t10 = self.trip(report, "T10")
        self.assertEqual(t10["partitions"][0]["consensus_reason"], "no_consensus")
        t11 = self.trip(report, "T11")
        self.assertEqual(t11["partitions"][0]["consensus_reason"], "below_quorum")
        t12 = self.trip(report, "T12")
        self.assertEqual(t12["status"], "incomplete")
        t13 = self.trip(report, "T13")
        self.assertEqual(t13["status"], "incomplete")
        t8 = self.trip(report, "T8")
        self.assertEqual([p["classification"] for p in t8["partitions"]],
                         ["above", "below"])
        self.assertEqual(t8["longest_same_side_confirmed_seconds"], 360)
        self.assertEqual(t8["definitely_outside_seconds"], 720)

        # Every assigned logger is listed on every partition, including invalid.
        t1 = self.trip(report, "T1")
        for partition in t1["partitions"]:
            self.assertEqual(
                sorted(s["sensor_id"] for s in partition["sensors"]),
                sorted(a["sensor_id"] for a in t1["assignments"]),
            )
            valid = next(s for s in partition["sensors"] if s["state"] == "valid")
            self.assertIn("band", valid)
            self.assertIsNotNone(valid["left_sample"]["csv_record_number"])
            self.assertIsNotNone(valid["right_sample"]["csv_record_number"])
            self.assertNotEqual(
                valid["left_sample"]["csv_record_number"],
                valid["right_sample"]["csv_record_number"],
            )

    def test_gap_threshold_changes_result(self):
        strict = self.trip(self.run_cli(gap=600), "T1")
        self.assertEqual(strict["status"], "incomplete")
        relaxed = self.trip(self.run_cli(gap=900), "T1")
        self.assertEqual(relaxed["status"], "confirmed_excursion")

    def test_invalid_input_fails_with_nonzero_and_no_output(self):
        bad_assignments = self.dir / "bad_assignments.csv"
        bad_assignments.write_text(
            "trip_id,sensor_id,uncertainty_c\nT1,A,1\nT1,A,1\n",
            encoding="utf-8",
        )
        command = module_command(
            "cold_exposure.consensus",
            [
                "--trips", str(DATA / "trips.csv"),
                "--assignments", str(bad_assignments),
                "--readings", str(DATA / "readings.csv"),
                "--output", str(self.output),
            ],
        )
        completed = subprocess.run(
            command, cwd=self.dir, env={"PATH": "/usr/bin:/bin"},
            capture_output=True, text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("assignments", completed.stderr)
        self.assertFalse(self.output.exists())

    def test_bad_gap_argument_rejected(self):
        completed = self.run_cli(gap=0, expect_success=False)
        self.assertIn("consensus failed", completed.stderr)


class VersionOneRegressionTests(unittest.TestCase):
    def test_legacy_cli_and_values_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp) / "report"
            command = module_command(
                "cold_exposure",
                [
                    "--trips", str(PROJECT / "data" / "trips.csv"),
                    "--readings", str(PROJECT / "data" / "readings.csv"),
                    "--output", str(output_dir),
                ],
            )
            completed = subprocess.run(
                command, cwd=temp, env={"PATH": "/usr/bin:/bin"},
                capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)
            report = json.loads((output_dir / "exposure.json").read_text())
            self.assertEqual(report["schema_version"], 1)
            t1 = next(t for t in report["trips"] if t["trip_id"] == "T1")
            self.assertEqual(t1["seconds"],
                             {"within": 600, "above": 1200, "below": 0, "unknown": 0})
            self.assertEqual(t1["mean_known_c"], "8.000000")
            self.assertEqual(t1["outside_degree_minutes"], "30.000000")
            self.assertEqual(t1["longest_same_side_excursion_seconds"], 1200)
            t2 = next(t for t in report["trips"] if t["trip_id"] == "T2")
            self.assertEqual(t2["seconds"]["unknown"], 1200)


if __name__ == "__main__":
    unittest.main()
