"""Unit tests for hand-off validation, attribution facts and conservation.

The expected numbers below are hand-computed independently from the stored
consensus report; the synthetic example contains a midnight-crossing confirmed
excursion with a mid-trip carrier change, unassigned holes, an entirely
unknown trip and a multi-component trip.
"""
import unittest
from decimal import Decimal
from pathlib import Path

from cold_exposure.handoff import (
    HandoffError,
    build_model,
    materialize_carrier_days,
    parse_consensus_bytes,
    parse_handoff_bytes,
)
from cold_exposure.handoff.attribution import build_facts, check_conservation
from cold_exposure.handoff.model import read_bytes

PROJECT = Path(__file__).resolve().parents[1]
GENERATED = PROJECT / "examples" / "handoff" / "generated"
HANDOFFS_CSV = PROJECT / "examples" / "handoff" / "handoffs.csv"


def load_model():
    return build_model(
        read_bytes(GENERATED / "consensus.json", "consensus JSON"),
        read_bytes(HANDOFFS_CSV, "handoffs"),
    )


class HandoffModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = load_model()

    def test_trips_partitions_and_handoffs_loaded(self):
        self.assertEqual(self.model["trip_order"], ["HA", "HB", "HC"])
        self.assertEqual([h["handoff_id"] for h in self.model["handoffs"]],
                         ["H1", "H2", "H3", "H4"])
        # HA has three partitions, HB one unknown partition, HC two mixed ones.
        self.assertEqual(
            {trip_id: len(trip["partitions"])
             for trip_id, trip in self.model["trips"].items()},
            {"HA": 3, "HB": 1, "HC": 2},
        )

    def test_hand_computed_fact_slices(self):
        facts = [
            (f["trip_id"], f["handoff_id"], f["carrier"], f["utc_day"],
             f["seconds"], f["classification"])
            for f in self.model["facts"]
        ]
        expected = [
            ("HA", "H1", "Northwind", "2026-05-01", 1800, "above"),
            ("HA", None, None, "2026-05-01", 900, "above"),
            ("HA", "H2", "Polar", "2026-05-01", 900, "above"),
            ("HA", "H2", "Polar", "2026-05-02", 1800, "within"),
            ("HA", None, None, "2026-05-02", 1800, "above"),
            ("HB", None, None, "2026-05-02", 900, "unknown"),
            ("HB", "H3", "Acme", "2026-05-02", 900, "unknown"),
            ("HB", "H3", "Acme", "2026-05-03", 900, "unknown"),
            ("HB", None, None, "2026-05-03", 900, "unknown"),
            ("HC", None, None, "2026-05-03", 300, "mixed"),
            ("HC", "H4", "Polar", "2026-05-03", 300, "mixed"),
            ("HC", "H4", "Polar", "2026-05-03", 300, "mixed"),
            ("HC", None, None, "2026-05-03", 300, "mixed"),
        ]
        self.assertEqual(facts, expected)

    def test_facts_cover_every_trip_without_gaps(self):
        check_conservation(
            self.model, self.model["facts"], self.model["carrier_days"]
        )
        for trip_id, trip in self.model["trips"].items():
            own = [f for f in self.model["facts"] if f["trip_id"] == trip_id]
            self.assertEqual(sum(f["seconds"] for f in own), trip["duration"])
            cursor = trip["start"]
            for fact in own:
                self.assertEqual(fact["start"], cursor)
                self.assertGreater(fact["end"], fact["start"])
                cursor = fact["end"]
            self.assertEqual(cursor, trip["end"])

    def test_unknown_seconds_are_never_zero_filled(self):
        for fact in self.model["facts"]:
            if fact["classification"] == "unknown":
                self.assertEqual(fact["unknown_seconds"], fact["seconds"])
                self.assertIsNone(fact["degree_seconds_min"])
                self.assertIsNone(fact["degree_seconds_max"])
            else:
                self.assertEqual(fact["unknown_seconds"], 0)
                self.assertIsNotNone(fact["degree_seconds_min"])

    def test_exact_degree_seconds_conserved_per_partition(self):
        for trip_id, trip in self.model["trips"].items():
            for partition in trip["partitions"]:
                own = [
                    f for f in self.model["facts"]
                    if f["trip_id"] == trip_id and f["partition_index"] == partition["index"]
                ]
                if partition["classification"] == "unknown":
                    self.assertTrue(all(f["degree_seconds_min"] is None for f in own))
                    continue
                lower = sum(f["degree_seconds_min"] for f in own)
                upper = sum(f["degree_seconds_max"] for f in own)
                self.assertEqual(
                    lower, partition["degree_seconds_min"] * partition["seconds"]
                )
                self.assertEqual(
                    upper, partition["degree_seconds_max"] * partition["seconds"]
                )

    def test_hand_computed_carrier_daily_rows(self):
        rows = {
            (r["carrier"], r["utc_day"]): r for r in self.model["carrier_days"]
        }
        northwind = rows[("Northwind", "2026-05-01")]
        self.assertEqual(northwind["duration_seconds"], 1800)
        self.assertEqual(northwind["definitely_outside_seconds"], 1800)
        self.assertEqual(northwind["unknown_seconds"], 0)
        self.assertEqual(northwind["degree_minutes_min"], Decimal("60.000000"))
        self.assertEqual(northwind["trip_count"], 1)

        polar_day1 = rows[("Polar", "2026-05-01")]
        self.assertEqual(polar_day1["duration_seconds"], 900)
        self.assertEqual(polar_day1["degree_minutes_max"], Decimal("30.000000"))

        polar_day2 = rows[("Polar", "2026-05-02")]
        self.assertEqual(polar_day2["duration_seconds"], 1800)
        self.assertEqual(polar_day2["definitely_outside_seconds"], 0)
        self.assertEqual(polar_day2["degree_minutes_min"], Decimal("0.000000"))

        polar_day3 = rows[("Polar", "2026-05-03")]
        self.assertEqual(polar_day3["duration_seconds"], 600)
        self.assertEqual(polar_day3["definitely_outside_seconds"], 600)
        self.assertEqual(polar_day3["degree_minutes_min"], Decimal("10.000000"))
        self.assertEqual(polar_day3["degree_minutes_max"], Decimal("20.000000"))

        # Acme holds only unknown time: real unknown counts, NULL exposure bounds.
        for day in ("2026-05-02", "2026-05-03"):
            acme = rows[("Acme", day)]
            self.assertEqual(acme["unknown_seconds"], 900)
            self.assertEqual(acme["supported_seconds"], 0)
            self.assertIsNone(acme["degree_minutes_min"])
            self.assertIsNone(acme["degree_minutes_max"])
            self.assertTrue(acme["is_unassigned"] is False)

        unassigned_day2 = rows[(None, "2026-05-02")]
        self.assertTrue(unassigned_day2["is_unassigned"])
        self.assertEqual(unassigned_day2["duration_seconds"], 2700)
        self.assertEqual(unassigned_day2["unknown_seconds"], 900)
        self.assertEqual(unassigned_day2["definitely_outside_seconds"], 1800)
        self.assertEqual(unassigned_day2["trip_count"], 2)
        self.assertEqual(unassigned_day2["handoff_count"], 0)

    def test_daily_totals_reconcile_to_trip_totals(self):
        daily = self.model["carrier_days"]
        self.assertEqual(sum(r["duration_seconds"] for r in daily), 7200 + 3600 + 1200)
        self.assertEqual(
            sum(r["definitely_outside_seconds"] for r in daily),
            5400 + 0 + 1200,
        )
        self.assertEqual(sum(r["unknown_seconds"] for r in daily), 3600)
        # Trips counted distinctly per carrier-day, never via joined rows.
        self.assertEqual(
            sum(r["trip_count"] for r in daily if not r["is_unassigned"]), 6
        )

    def test_midnight_crossing_handoff_is_split_by_utc_day(self):
        # H2 (Polar) and H3 (Acme) each span a UTC midnight; one hand-off row
        # must therefore produce facts on two distinct UTC days.
        for handoff_id, days in (("H2", {"2026-05-01", "2026-05-02"}),
                                 ("H3", {"2026-05-02", "2026-05-03"})):
            own = [f for f in self.model["facts"] if f["handoff_id"] == handoff_id]
            self.assertEqual({f["utc_day"] for f in own}, days)
            handoff = next(h for h in self.model["handoffs"] if h["handoff_id"] == handoff_id)
            self.assertEqual(
                sum(f["seconds"] for f in own),
                int((handoff["end"] - handoff["start"]).total_seconds()),
            )

    def test_multi_component_partition_keeps_distinct_bounds(self):
        hc = self.model["trips"]["HC"]
        partition = hc["partitions"][0]
        self.assertEqual(
            partition["components"], [(Decimal("0.0"), Decimal(1)), (Decimal("9.0"), Decimal("10.0"))]
        )
        self.assertTrue(partition["disjoint"])
        self.assertTrue(partition["not_contained"])
        # d(x) over [0,1] is 2..1 and over [9,10] is 1..2: min 1, max 2.
        self.assertEqual(partition["degree_seconds_min"], Decimal(1))
        self.assertEqual(partition["degree_seconds_max"], Decimal(2))
        # Whole-trip bounds are partition-derived, never prorated:
        # 1200s * (1, 2) / 60 -> (20, 40) degree-minutes.
        total_min = sum(
            f["degree_seconds_min"] for f in self.model["facts"] if f["trip_id"] == "HC"
        ) / Decimal(60)
        total_max = sum(
            f["degree_seconds_max"] for f in self.model["facts"] if f["trip_id"] == "HC"
        ) / Decimal(60)
        self.assertEqual(total_min, Decimal(20))
        self.assertEqual(total_max, Decimal(40))


class HandoffValidationTests(unittest.TestCase):
    def setUp(self):
        self.consensus = read_bytes(GENERATED / "consensus.json", "consensus JSON")
        self.parsed = parse_consensus_bytes(self.consensus)

    def parse(self, text):
        return parse_handoff_bytes(text.encode("utf-8"), self.parsed["trips"])

    def test_duplicate_handoff_id_rejected(self):
        text = (
            "handoff_id,trip_id,carrier,started_at,ended_at\n"
            "X,HA,Acme,2026-05-01T23:00:00Z,2026-05-01T23:10:00Z\n"
            "X,HA,Acme,2026-05-01T23:10:00Z,2026-05-01T23:20:00Z\n"
        )
        with self.assertRaisesRegex(HandoffError, "duplicate handoff_id"):
            self.parse(text)

    def test_unknown_trip_rejected(self):
        text = (
            "handoff_id,trip_id,carrier,started_at,ended_at\n"
            "X,NOPE,Acme,2026-05-01T23:00:00Z,2026-05-01T23:10:00Z\n"
        )
        with self.assertRaisesRegex(HandoffError, "unknown trip_id"):
            self.parse(text)

    def test_overlap_inside_same_trip_rejected_but_touching_allowed(self):
        touching = (
            "handoff_id,trip_id,carrier,started_at,ended_at\n"
            "X,HA,Acme,2026-05-01T23:00:00Z,2026-05-01T23:10:00Z\n"
            "Y,HA,Acme,2026-05-01T23:10:00Z,2026-05-01T23:20:00Z\n"
        )
        rows = self.parse(touching)
        self.assertEqual([r["handoff_id"] for r in rows], ["X", "Y"])
        overlapping = (
            "handoff_id,trip_id,carrier,started_at,ended_at\n"
            "X,HA,Acme,2026-05-01T23:00:00Z,2026-05-01T23:10:01Z\n"
            "Y,HA,Acme,2026-05-01T23:10:00Z,2026-05-01T23:20:00Z\n"
        )
        with self.assertRaisesRegex(HandoffError, "overlap"):
            self.parse(overlapping)

    def test_overlap_across_different_trips_allowed(self):
        text = (
            "handoff_id,trip_id,carrier,started_at,ended_at\n"
            "X,HB,Acme,2026-05-02T23:45:00Z,2026-05-03T00:00:00Z\n"
            "Y,HC,Acme,2026-05-03T00:00:00Z,2026-05-03T00:05:00Z\n"
        )
        self.assertEqual(len(self.parse(text)), 2)

    def test_interval_outside_trip_empty_or_non_integer_second_rejected(self):
        outside = (
            "handoff_id,trip_id,carrier,started_at,ended_at\n"
            "X,HA,Acme,2026-05-01T22:59:00Z,2026-05-01T23:10:00Z\n"
        )
        with self.assertRaisesRegex(HandoffError, "must stay inside trip"):
            self.parse(outside)
        empty = (
            "handoff_id,trip_id,carrier,started_at,ended_at\n"
            "X,HA,Acme,2026-05-01T23:10:00Z,2026-05-01T23:10:00Z\n"
        )
        with self.assertRaisesRegex(HandoffError, "non-empty"):
            self.parse(empty)
        fractional = (
            "handoff_id,trip_id,carrier,started_at,ended_at\n"
            "X,HA,Acme,2026-05-01T23:00:00.5Z,2026-05-01T23:10:00Z\n"
        )
        with self.assertRaisesRegex(HandoffError, "whole-second"):
            self.parse(fractional)

    def test_bad_header_and_missing_field_rejected(self):
        with self.assertRaisesRegex(HandoffError, "CSV header"):
            self.parse("handoff_id,trip_id,carrier,started_at\n")
        with self.assertRaisesRegex(HandoffError, "missing or extra CSV field"):
            self.parse(
                "handoff_id,trip_id,carrier,started_at,ended_at\n"
                "X,HA,Acme,2026-05-01T23:00:00Z,\n"
            )

    def test_empty_handoff_file_is_all_unassigned(self):
        rows = self.parse("handoff_id,trip_id,carrier,started_at,ended_at\n")
        self.assertEqual(rows, [])
        model = dict(self.parsed)
        model["handoffs"] = []
        model["trip_order"] = self.parsed["trip_order"]
        model["report"] = self.parsed["report"]
        model["max_gap_seconds"] = self.parsed["max_gap_seconds"]
        model["trips"] = self.parsed["trips"]
        facts = build_facts(model)
        self.assertTrue(all(f["is_unassigned"] for f in facts))
        daily = materialize_carrier_days(facts)
        self.assertTrue(all(r["is_unassigned"] for r in daily))
        check_conservation(model, facts, daily)


class ConsensusReportValidationTests(unittest.TestCase):
    def test_wrong_schema_version_rejected(self):
        import json

        report = json.loads(read_bytes(GENERATED / "consensus.json", "consensus JSON"))
        report["schema_version"] = 1
        with self.assertRaisesRegex(HandoffError, "schema_version must be 2"):
            parse_consensus_bytes(json.dumps(report).encode("utf-8"))

    def test_tampered_partition_coverage_rejected(self):
        import json

        report = json.loads(read_bytes(GENERATED / "consensus.json", "consensus JSON"))
        trip = next(t for t in report["trips"] if t["trip_id"] == "HA")
        trip["partitions"][0]["ended_at"] = "2026-05-01T23:59:00Z"
        trip["partitions"][0]["seconds"] = 3540
        with self.assertRaises(HandoffError):
            parse_consensus_bytes(json.dumps(report).encode("utf-8"))

    def test_tampered_classification_rejected(self):
        import json

        report = json.loads(read_bytes(GENERATED / "consensus.json", "consensus JSON"))
        trip = next(t for t in report["trips"] if t["trip_id"] == "HA")
        trip["partitions"][0]["classification"] = "within"
        with self.assertRaisesRegex(HandoffError, "classification"):
            parse_consensus_bytes(json.dumps(report).encode("utf-8"))

    def test_duplicate_json_keys_rejected(self):
        raw = read_bytes(GENERATED / "consensus.json", "consensus JSON").decode("utf-8")
        tampered = raw.replace('"trip_id": "HA"', '"trip_id": "HA", "trip_id": "HA"', 1)
        with self.assertRaisesRegex(HandoffError, "duplicate JSON object key"):
            parse_consensus_bytes(tampered.encode("utf-8"))

    def test_tampered_whole_trip_degree_minutes_rejected(self):
        import json

        report = json.loads(read_bytes(GENERATED / "consensus.json", "consensus JSON"))
        trip = next(t for t in report["trips"] if t["trip_id"] == "HA")
        trip["supported_degree_minutes_max"] = "999.999999"
        with self.assertRaisesRegex(HandoffError, "supported_degree_minutes"):
            parse_consensus_bytes(json.dumps(report).encode("utf-8"))

    def test_non_utf8_rejected(self):
        with self.assertRaisesRegex(HandoffError, "not valid UTF-8"):
            parse_consensus_bytes(b"\xff\xfe\x00")


if __name__ == "__main__":
    unittest.main()
