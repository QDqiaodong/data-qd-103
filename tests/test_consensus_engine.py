"""Partition timeline, gap/bracket semantics and aggregate behavior."""
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from cold_exposure.consensus_engine import analyze_consensus_trip
from cold_exposure.consensus_pipeline import run_consensus

UTC = timezone.utc


def instant(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def make_trip(trip_id="T1", start="2026-06-01T00:00:00Z",
              end="2026-06-01T00:10:00Z", low="2", high="8", q=1):
    return {
        "trip_id": trip_id, "start": instant(start), "end": instant(end),
        "low": Decimal(low), "high": Decimal(high), "min_support": q,
        "row": 1, "assignments": [],
    }


def assign(sensor_id, uncertainty="0"):
    return {"trip_id": "T1", "sensor_id": sensor_id,
            "uncertainty": Decimal(uncertainty), "row": 1}


def sample(sensor_id, time_text, value):
    return {"sensor_id": sensor_id, "observed_at": instant(time_text),
            "temperature": Decimal(value), "row": 1}


def attach(trip, pairs):
    trip["assignments"] = [assign(sensor_id) for sensor_id, _ in pairs]
    readings = {}
    for sensor_id, values in pairs:
        readings[sensor_id] = [
            sample(sensor_id, time_text, value) for time_text, value in values
        ]
    return trip, readings


class GapAndBracketTests(unittest.TestCase):
    BASE = "2026-06-01T00:{:02d}:00Z"

    def test_raw_gap_exceeds_limit_even_when_trip_covers_only_part(self):
        # Raw samples 20 minutes apart; the trip only covers 5 minutes of it.
        trip = make_trip(start=self.BASE.format(0), end=self.BASE.format(5))
        trip, readings = attach(trip, [("S1", [(self.BASE.format(0), 5),
                                               (self.BASE.format(20), 5)])])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        self.assertEqual(result["classification_seconds"]["unknown"], 300)
        state = result["partitions"][0]["sensors"][0]
        self.assertEqual(state["state"], "gap_exceeded")
        self.assertIsNotNone(state["left_sample"])
        self.assertIsNotNone(state["right_sample"])

    def test_gap_equal_to_limit_is_valid(self):
        trip = make_trip(start=self.BASE.format(0), end=self.BASE.format(10))
        trip, readings = attach(trip, [("S1", [(self.BASE.format(0), 5),
                                               (self.BASE.format(10), 5)])])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        self.assertEqual(result["classification_seconds"]["within"], 600)
        self.assertEqual(result["partitions"][0]["sensors"][0]["state"], "valid")

    def test_outside_window_brackets_hold_without_extrapolation(self):
        trip = make_trip(start=self.BASE.format(5), end=self.BASE.format(15))
        trip, readings = attach(trip, [
            ("S1", [(self.BASE.format(0), 5), (self.BASE.format(10), 5),
                    (self.BASE.format(20), 5)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        first, second = result["partitions"]
        # [05:00,10:00) is bracketed by samples at 00:00 and 10:00.
        self.assertEqual(first["sensors"][0]["state"], "valid")
        self.assertEqual(first["sensors"][0]["left_sample"]["observed_at"],
                         instant(self.BASE.format(0)).isoformat())
        # [10:00,15:00) closes at the 20:00 raw sample whose temperature must
        # not be used for the held value.
        self.assertEqual(second["sensors"][0]["state"], "valid")
        self.assertEqual(second["sensors"][0]["temperature_c"], "5")
        self.assertEqual(second["sensors"][0]["right_sample"]["observed_at"],
                         instant(self.BASE.format(20)).isoformat())

    def test_no_extrapolation_before_first_or_after_last_sample(self):
        trip = make_trip(start=self.BASE.format(0), end=self.BASE.format(10))
        trip, readings = attach(trip, [
            ("S1", [(self.BASE.format(3), 5), (self.BASE.format(7), 5)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        states = [p["sensors"][0]["state"] for p in result["partitions"]]
        self.assertEqual(states[0], "no_bracket")
        self.assertEqual(states[-1], "no_bracket")
        self.assertIn("valid", states)
        for partition in result["partitions"]:
            if partition["sensors"][0]["state"] == "no_bracket":
                self.assertIsNone(partition["sensors"][0]["left_sample"])

    def test_empty_and_single_reading_are_fully_unknown(self):
        trip = make_trip()
        for values in ([], [(self.BASE.format(0), 5)]):
            trip_local, readings = attach(make_trip(), [("S1", values)])
            result = analyze_consensus_trip(trip_local, trip_local["assignments"],
                                            readings, 600)
            self.assertEqual(result["unknown_seconds"], 600)
            self.assertEqual(result["status"], "incomplete")
            self.assertIsNone(result["supported_degree_minutes_min"])

    def test_different_utc_offsets_normalize_to_same_partition(self):
        trip = make_trip()
        trip, readings = attach(trip, [
            ("S1", [("2026-06-01T02:00:00+02:00", 5),
                    ("2026-06-01T02:10:00+02:00", 5)]),
            ("S2", [(self.BASE.format(0), 5), (self.BASE.format(10), 5)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        self.assertEqual(len(result["partitions"]), 1)
        self.assertEqual(result["classification_seconds"]["within"], 600)
        self.assertTrue(all(s["state"] == "valid"
                            for s in result["partitions"][0]["sensors"]))


class QuorumReasonsTests(unittest.TestCase):
    BASE = "2026-06-01T00:{:02d}:00Z"

    def _trip_with_three(self, values, q=2):
        trip = make_trip(q=q)
        pairs = [(sensor, [(self.BASE.format(0), value),
                           (self.BASE.format(10), value)])
                 for sensor, value in zip(["S1", "S2", "S3"], values)]
        return attach(trip, pairs)

    def test_three_distinct_points_no_consensus(self):
        trip, readings = self._trip_with_three(["1", "2", "3"])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        partition = result["partitions"][0]
        self.assertEqual(partition["consensus_reason"], "no_consensus")
        self.assertEqual(partition["valid_logger_count"], 3)
        self.assertEqual(result["status"], "incomplete")

    def test_one_valid_logger_is_below_quorum(self):
        trip = make_trip(q=2)
        trip, readings = attach(trip, [
            ("S1", [(self.BASE.format(0), 5), (self.BASE.format(10), 5)]),
            ("S2", [(self.BASE.format(0), 5), (self.BASE.format(20), 5)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        partition = result["partitions"][0]
        self.assertEqual(partition["consensus_reason"], "below_quorum")
        self.assertEqual(partition["valid_logger_count"], 1)
        states = {s["sensor_id"]: s["state"] for s in partition["sensors"]}
        self.assertEqual(states, {"S1": "valid", "S2": "gap_exceeded"})


class StreakAndStatusTests(unittest.TestCase):
    BASE = "2026-06-01T00:{:02d}:00Z"

    def test_supporter_change_keeps_above_streak(self):
        trip = make_trip(end=self.BASE.format(10))
        trip, readings = attach(trip, [
            ("S1", [(self.BASE.format(0), 10), (self.BASE.format(10), 10)]),
            ("S2", [(self.BASE.format(0), 10), (self.BASE.format(5), 10),
                    (self.BASE.format(10), 10)]),
            ("S3", [(self.BASE.format(5), 10), (self.BASE.format(10), 10)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        self.assertEqual(len(result["partitions"]), 2)
        self.assertEqual([p["classification"] for p in result["partitions"]],
                         ["above", "above"])
        self.assertEqual(result["longest_same_side_confirmed_seconds"], 600)
        self.assertEqual(result["definitely_outside_seconds"], 600)

    def test_side_switch_mixed_and_unknown_break_streak(self):
        # Side switch: above then below.
        trip = make_trip(end=self.BASE.format(10))
        trip, readings = attach(trip, [
            ("S1", [(self.BASE.format(0), 10), (self.BASE.format(5), 0),
                    (self.BASE.format(10), 0)]),
            ("S2", [(self.BASE.format(0), 10), (self.BASE.format(5), 0),
                    (self.BASE.format(10), 0)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        self.assertEqual(result["longest_same_side_confirmed_seconds"], 300)

        # Unknown breaks the above streak: the second half loses its bracket.
        trip, readings = attach(make_trip(q=2, end=self.BASE.format(10)), [
            ("S1", [(self.BASE.format(0), 10), (self.BASE.format(5), 10),
                    (self.BASE.format(16), 10)]),
            ("S2", [(self.BASE.format(0), 10), (self.BASE.format(5), 10),
                    (self.BASE.format(10), 10)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        kinds = [p["classification"] for p in result["partitions"]]
        self.assertEqual(kinds, ["above", "unknown"])
        self.assertEqual(result["longest_same_side_confirmed_seconds"], 300)

        # Within breaks the above streak.
        trip, readings = attach(make_trip(end=self.BASE.format(10)), [
            ("S1", [(self.BASE.format(0), 10), (self.BASE.format(5), 5),
                    (self.BASE.format(10), 5)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        kinds = [p["classification"] for p in result["partitions"]]
        self.assertEqual(kinds, ["above", "within"])
        self.assertEqual(result["longest_same_side_confirmed_seconds"], 300)

    def test_status_priority(self):
        # Possible but not definite: a band [7,9] straddles the upper limit.
        trip = make_trip(end=self.BASE.format(10))
        trip["assignments"] = [assign("S1", uncertainty="1")]
        readings = {"S1": [sample("S1", self.BASE.format(0), 8),
                           sample("S1", self.BASE.format(10), 8)]}
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        self.assertEqual(result["status"], "possible_excursion")
        self.assertEqual(result["possibly_outside_seconds"], 600)
        self.assertEqual(result["definitely_outside_seconds"], 0)

    def test_seconds_partition_the_full_duration(self):
        trip = make_trip(end=self.BASE.format(10))
        trip, readings = attach(trip, [
            ("S1", [(self.BASE.format(0), 10), (self.BASE.format(5), 0),
                    (self.BASE.format(10), 0)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        self.assertEqual(sum(result["classification_seconds"].values()), 600)
        self.assertEqual(result["supported_seconds"] + result["unknown_seconds"], 600)


class ExactLimitTests(unittest.TestCase):
    BASE = "2026-06-01T00:{:02d}:00Z"

    def test_round_half_up_on_seventh_decimal(self):
        # d = 8.00005 - 8 = 0.00005 over 3 s: 0.00005*3/60 = 0.0000025,
        # which ROUND_HALF_UP raises to 0.000003 (half-even would give 0.000002).
        trip = make_trip(start="2026-06-01T00:00:00Z", end="2026-06-01T00:00:03Z")
        trip, readings = attach(trip, [
            ("S1", [("2026-06-01T00:00:00Z", "8.00005"),
                    ("2026-06-01T00:00:03Z", "8.00005")]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        self.assertEqual(result["supported_degree_minutes_min"], "0.000003")
        self.assertEqual(result["supported_degree_minutes_max"], "0.000003")

    def test_consensus_equal_to_limits_and_zero_uncertainty(self):
        # Zero-width consensus points sitting exactly on each limit are within.
        trip = make_trip(q=2)
        trip, readings = attach(trip, [
            ("S1", [(self.BASE.format(0), 2), (self.BASE.format(10), 2)]),
            ("S2", [(self.BASE.format(0), 2), (self.BASE.format(10), 2)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        self.assertEqual(result["classification_seconds"]["within"], 600)
        self.assertEqual(result["status"], "within_limits")
        self.assertEqual(result["possibly_outside_seconds"], 0)
        self.assertEqual(result["supported_degree_minutes_max"], "0.000000")

        trip = make_trip(q=2)
        trip, readings = attach(trip, [
            ("S1", [(self.BASE.format(0), 8), (self.BASE.format(10), 8)]),
            ("S2", [(self.BASE.format(0), 8), (self.BASE.format(10), 8)]),
        ])
        result = analyze_consensus_trip(trip, trip["assignments"], readings, 600)
        self.assertEqual(result["classification_seconds"]["within"], 600)
        self.assertEqual(result["supported_degree_minutes_min"], "0.000000")


class PipelineTimelineTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _write(self, trips, assignments, readings):
        paths = {}
        for name, content in [("trips", trips), ("assignments", assignments),
                              ("readings", readings)]:
            path = self.dir / f"{name}.csv"
            path.write_text(content, encoding="utf-8")
            paths[name] = path
        return paths

    def test_partition_boundaries_merge_async_sensors(self):
        paths = self._write(
            "trip_id,started_at,ended_at,min_c,max_c,min_support\n"
            "T1,2026-06-01T00:00:00Z,2026-06-01T00:10:00Z,2,8,1\n",
            "trip_id,sensor_id,uncertainty_c\nT1,S1,0\nT1,S2,0\n",
            "sensor_id,observed_at,temperature_c\n"
            "S1,2026-06-01T00:00:00Z,5\nS1,2026-06-01T00:04:00Z,5\n"
            "S1,2026-06-01T00:10:00Z,5\n"
            "S2,2026-06-01T00:00:00Z,5\nS2,2026-06-01T00:07:00Z,5\n"
            "S2,2026-06-01T00:10:00Z,5\n",
        )
        report = run_consensus(paths["trips"], paths["assignments"], paths["readings"],
                               output=self.dir / "r.json", max_gap_seconds=600)
        trip = report["trips"][0]
        boundaries = [p["started_at"][11:19] for p in trip["partitions"]]
        self.assertEqual(boundaries, ["00:00:00", "00:04:00", "00:07:00"])
        self.assertEqual(sum(p["seconds"] for p in trip["partitions"]), 600)
        for partition in trip["partitions"]:
            self.assertEqual(len(partition["sensors"]), 2)


if __name__ == "__main__":
    unittest.main()
