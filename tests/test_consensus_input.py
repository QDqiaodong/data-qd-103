"""Validation, single-capture hashing and atomic-output failure recovery."""
import hashlib
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from cold_exposure.consensus_input import ConsensusInputError, capture_inputs
from cold_exposure.consensus_pipeline import _atomic_write, build_report, run_consensus

HEADER_TRIPS = "trip_id,started_at,ended_at,min_c,max_c,min_support\n"
HEADER_ASSIGN = "trip_id,sensor_id,uncertainty_c\n"
HEADER_READ = "sensor_id,observed_at,temperature_c\n"

VALID_TRIPS = HEADER_TRIPS + "T1,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,2,8,1\n"
VALID_ASSIGN = HEADER_ASSIGN + "T1,S1,0.5\n"
VALID_READ = (
    HEADER_READ
    + "S1,2026-05-01T00:00:00Z,5\n"
    + "S1,2026-05-01T00:10:00Z,5\n"
)


class InputHarness(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        self.paths = {
            "trips": self.dir / "trips.csv",
            "assignments": self.dir / "assignments.csv",
            "readings": self.dir / "readings.csv",
        }
        self.write(VALID_TRIPS, VALID_ASSIGN, VALID_READ)
        self.args = (self.paths["trips"], self.paths["assignments"], self.paths["readings"])

    def tearDown(self):
        self._dir.cleanup()

    def write(self, trips, assignments, readings):
        self.paths["trips"].write_text(trips, encoding="utf-8")
        self.paths["assignments"].write_text(assignments, encoding="utf-8")
        self.paths["readings"].write_text(readings, encoding="utf-8")

    def capture(self):
        return capture_inputs(
            self.paths["trips"], self.paths["assignments"], self.paths["readings"]
        )


class ValidationTests(InputHarness):
    def assert_rejected(self):
        with self.assertRaises(ConsensusInputError):
            self.capture()

    def test_valid_baseline_and_hash_uses_captured_bytes(self):
        captured, model = self.capture()
        self.assertEqual(
            captured["trips"], self.paths["trips"].read_bytes()
        )
        self.assertEqual(len(model["trips"]), 1)
        self.assertEqual(
            hashlib.sha256(captured["readings"]).hexdigest(),
            hashlib.sha256(self.paths["readings"].read_bytes()).hexdigest(),
        )

    def test_duplicate_trip_id_rejected_with_location(self):
        self.write(
            HEADER_TRIPS
            + "T1,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,2,8,1\n"
            + "T1,2026-05-01T01:00:00Z,2026-05-01T01:10:00Z,2,8,1\n",
            VALID_ASSIGN, VALID_READ,
        )
        with self.assertRaisesRegex(ConsensusInputError, "trips: row 2"):
            self.capture()

    def test_header_must_match_exactly(self):
        self.write(
            HEADER_TRIPS.replace("min_support", "quorum")
            + "T1,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,2,8,1\n",
            VALID_ASSIGN, VALID_READ,
        )
        self.assert_rejected()

    def test_duplicate_assignment_even_identical_is_rejected(self):
        self.write(
            VALID_TRIPS,
            HEADER_ASSIGN + "T1,S1,0.5\nT1,S1,0.5\n",
            VALID_READ,
        )
        self.assert_rejected()

    def test_duplicate_reading_at_equivalent_utc_instant(self):
        self.write(
            VALID_TRIPS, VALID_ASSIGN,
            HEADER_READ
            + "S1,2026-05-01T02:00:00+02:00,5\n"
            + "S1,2026-05-01T00:00:00Z,5\n"
            + "S1,2026-05-01T00:10:00Z,5\n",
        )
        self.assert_rejected()

    def test_unknown_references(self):
        self.write(
            VALID_TRIPS,
            HEADER_ASSIGN + "T9,S1,0.5\n",
            VALID_READ,
        )
        self.assert_rejected()
        self.write(
            VALID_TRIPS, VALID_ASSIGN,
            HEADER_READ + "S9,2026-05-01T00:00:00Z,5\n",
        )
        self.assert_rejected()

    def test_quorum_bounds_and_trip_without_assignment(self):
        self.write(
            HEADER_TRIPS + "T1,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,2,8,2\n",
            VALID_ASSIGN, VALID_READ,
        )
        self.assert_rejected()
        self.write(
            HEADER_TRIPS + "T1,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,2,8,0\n",
            VALID_ASSIGN, VALID_READ,
        )
        self.assert_rejected()
        self.write(
            HEADER_TRIPS
            + "T1,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,2,8,1\n"
            + "T2,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,2,8,1\n",
            VALID_ASSIGN, VALID_READ,
        )
        self.assert_rejected()

    def test_non_finite_and_precision_and_range_violations(self):
        for bad in ["NaN", "Infinity", "1.0000001", "150.1", "-100.1"]:
            self.write(
                VALID_TRIPS, VALID_ASSIGN,
                HEADER_READ + f"S1,2026-05-01T00:00:00Z,{bad}\nS1,2026-05-01T00:10:00Z,5\n",
            )
            self.assert_rejected()
        for bad in ["-0.1", "20.1", "0.0000001"]:
            self.write(
                VALID_TRIPS,
                HEADER_ASSIGN + f"T1,S1,{bad}\n",
                VALID_READ,
            )
            self.assert_rejected()
        self.write(
            HEADER_TRIPS + "T1,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,8,8,1\n",
            VALID_ASSIGN, VALID_READ,
        )
        self.assert_rejected()

    def test_identifier_whitespace_and_empty_rows(self):
        self.write(
            HEADER_TRIPS + " T1,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,2,8,1\n",
            VALID_ASSIGN, VALID_READ,
        )
        self.assert_rejected()
        self.write(VALID_TRIPS, VALID_ASSIGN, HEADER_READ)
        captured, model = self.capture()
        self.assertEqual(model["readings"]["S1"], [])

    def test_error_names_file_and_record(self):
        self.write(
            HEADER_TRIPS + "T1,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,2,8,1\n",
            HEADER_ASSIGN + "T1,S1,0.5\nT1,S1,0.5\n",
            VALID_READ,
        )
        with self.assertRaisesRegex(ConsensusInputError, "assignments: row 2"):
            self.capture()

    def test_timezone_free_and_non_whole_second_rejected(self):
        self.write(
            VALID_TRIPS, VALID_ASSIGN,
            HEADER_READ + "S1,2026-05-01T00:00:00,5\nS1,2026-05-01T00:10:00Z,5\n",
        )
        self.assert_rejected()
        self.write(
            VALID_TRIPS, VALID_ASSIGN,
            HEADER_READ + "S1,2026-05-01T00:00:00.5Z,5\nS1,2026-05-01T00:10:00Z,5\n",
        )
        self.assert_rejected()

    def test_sensor_can_serve_multiple_trips_and_six_decimals(self):
        self.write(
            HEADER_TRIPS
            + "T1,2026-05-01T00:00:00Z,2026-05-01T00:10:00Z,1.000001,8.000000,1\n"
            + "T2,2026-05-01T02:00:00Z,2026-05-01T02:10:00Z,2,8,1\n",
            HEADER_ASSIGN + "T1,S1,0.000001\nT2,S1,0\n",
            HEADER_READ
            + "S1,2026-05-01T00:10:00Z,5\nS1,2026-05-01T00:00:00Z,5\n",
        )
        captured, model = self.capture()
        self.assertEqual(len(model["trips"]), 2)
        self.assertEqual(
            [a["uncertainty"] for a in model["assignments"]],
            [Decimal("0.000001"), Decimal(0)],
        )
        self.assertEqual(model["readings"]["S1"][0]["temperature"], Decimal(5))

    def test_gap_parameter_validation(self):
        for gap in [0, -1, 86401, True, 600.0]:
            with self.assertRaises(ValueError):
                build_report(
                    self.paths["trips"], self.paths["assignments"],
                    self.paths["readings"], max_gap_seconds=gap,
                )


class AtomicReplaceTests(InputHarness):
    def test_successful_write_is_replaceable_and_deterministic(self):
        output = self.dir / "nested" / "report.json"
        args = (self.paths["trips"], self.paths["assignments"], self.paths["readings"])
        first = run_consensus(*args, output=output, max_gap_seconds=600)
        before = output.read_bytes()
        second = run_consensus(*args, output=output, max_gap_seconds=600)
        self.assertEqual(before, output.read_bytes())
        self.assertEqual(first, second)

    def test_failed_replace_keeps_old_report_and_cleans_temp(self):
        output = self.dir / "report.json"
        run_consensus(*self.args, output=output, max_gap_seconds=600)
        original_bytes = output.read_bytes()

        import cold_exposure.consensus_pipeline as module
        real_replace = module.os.replace

        def boom(source, destination):
            raise OSError("simulated replace failure")

        module.os.replace = boom
        try:
            with self.assertRaises(OSError):
                run_consensus(*self.args, output=output, max_gap_seconds=600)
        finally:
            module.os.replace = real_replace
        self.assertEqual(output.read_bytes(), original_bytes)
        self.assertEqual(list(self.dir.glob(".*.tmp")), [])

        # Re-running after the simulated fault overwrites with a valid report.
        run_consensus(*self.args, output=output, max_gap_seconds=600)
        self.assertEqual(output.read_bytes(), original_bytes)

    def test_failed_serialization_never_touches_output(self):
        output = self.dir / "report.json"
        run_consensus(*self.args, output=output, max_gap_seconds=600)
        original_bytes = output.read_bytes()
        self.write(
            VALID_TRIPS,
            HEADER_ASSIGN + "T1,S1,0.5\nT1,S1,0.5\n",
            VALID_READ,
        )
        with self.assertRaises(ConsensusInputError):
            run_consensus(*self.args, output=output, max_gap_seconds=600)
        self.assertEqual(output.read_bytes(), original_bytes)
        self.assertEqual(list(self.dir.glob(".*.tmp")), [])

    def test_atomic_write_helper_removes_temp_on_write_error(self):
        target = self.dir / "out.json"

        class FailingFile:
            def __init__(self, *args, **kwargs):
                raise OSError("simulated open failure")

        import cold_exposure.consensus_pipeline as module
        original = module.tempfile.NamedTemporaryFile
        module.tempfile.NamedTemporaryFile = FailingFile
        try:
            with self.assertRaises(OSError):
                _atomic_write(target, "{}")
        finally:
            module.tempfile.NamedTemporaryFile = original
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
