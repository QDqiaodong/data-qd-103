"""Core consensus-set tests against an independent brute-force reference.

The reference in this module deliberately does not reuse the production sweep.
It evaluates every band endpoint and a representative point in every open gap
between consecutive endpoints, counting covering bands directly, then rebuilds
the maximal closed connected components independently.
"""
import unittest
from decimal import Decimal

from cold_exposure.consensus_engine import (
    classify,
    consensus_components,
    distance_bounds,
)


def reference_components(bands, quorum):
    """Independent coverage computation used only as a test oracle."""
    endpoints = sorted({point for band in bands for point in band})
    probes = []
    for point in endpoints:  # every closed endpoint
        probes.append((point, True))
    for left, right in zip(endpoints, endpoints[1:]):  # one interior point per gap
        probes.append(((left + right) / 2, False))

    covered_points = set()
    covered_gaps = set()
    for value, is_endpoint in probes:
        coverage = sum(1 for lower, upper in bands if lower <= value <= upper)
        if coverage < quorum:
            continue
        if is_endpoint:
            covered_points.add(value)
        else:
            left = endpoints[[i for i, point in enumerate(endpoints) if point < value][-1]]
            covered_gaps.add(left)

    components = []
    start = None
    for index, point in enumerate(endpoints):
        if point in covered_points:
            if start is None:
                start = point
            if point not in covered_gaps:
                components.append((start, point))
                start = None
        else:
            start = None
    return components


def decimals(pairs):
    return [(Decimal(lower), Decimal(upper)) for lower, upper in pairs]


class ReferenceConsensusTests(unittest.TestCase):
    SCENARIOS = [
        # touching closed bands create a single-point consensus at the touch
        ([("9", "11"), ("11", "13"), ("5", "5")], 2),
        # two separated components with an unsupported safe hole between them
        ([("0", "10"), ("-1", "1"), ("9", "11")], 2),
        # overlapping consensus crossing one limit
        ([("7", "9"), ("7.5", "8.5")], 2),
        # identical zero-width bands
        ([("5", "5"), ("5", "5"), ("5", "5")], 2),
        # disjoint single points cannot reach quorum
        ([("1", "1"), ("2", "2"), ("3", "3")], 2),
        # broad envelope bands whose intersection is empty at q=3 but have q=2 regions
        ([("0", "4"), ("2", "6"), ("4", "8")], 2),
        # nested bands and quorum above outer count
        ([("0", "10"), ("2", "8"), ("4", "6")], 3),
        # endpoint-only touch chains
        ([("0", "2"), ("2", "4"), ("4", "6")], 2),
        # half-open hole must not be filled: touching only at a point
        ([("0", "3"), ("3", "6"), ("0", "2"), ("4", "6")], 2),
    ]

    def test_sweep_matches_independent_reference(self):
        for bands_text, quorum in self.SCENARIOS:
            bands = decimals(bands_text)
            expected = reference_components(bands, quorum)
            actual = consensus_components(bands, quorum)
            self.assertEqual(
                actual,
                expected,
                msg=f"bands={bands_text} quorum={quorum}",
            )

    def test_spec_touching_band_trip_one_consensus(self):
        bands = decimals([("9", "11"), ("11", "13"), ("5", "5")])
        self.assertEqual(consensus_components(bands, 2), [(Decimal(11), Decimal(11))])

    def test_spec_split_components_keep_safe_hole_empty(self):
        bands = decimals([("0", "10"), ("-1", "1"), ("9", "11")])
        self.assertEqual(
            consensus_components(bands, 2),
            [(Decimal(0), Decimal(1)), (Decimal(9), Decimal(10))],
        )
        # No point of the allowed range's interior hole [2, 8] is supported.
        for value in map(Decimal, ["2", "3.5", "8"]):
            coverage = sum(1 for lower, upper in bands if lower <= value <= upper)
            self.assertLess(coverage, 2)

    def test_below_quorum_single_vote_and_zero_support(self):
        bands = decimals([("1", "1"), ("2", "2"), ("3", "3")])
        self.assertEqual(consensus_components(bands, 2), [])
        self.assertEqual(consensus_components(decimals([("0", "10")]), 2), [])

    def test_six_decimal_places_preserved(self):
        bands = decimals([("1.000001", "2.000001"), ("1.500000", "2.500000")])
        self.assertEqual(
            consensus_components(bands, 2),
            [(Decimal("1.500000"), Decimal("2.000001"))],
        )


class ClassificationTests(unittest.TestCase):
    LOW, HIGH = Decimal(2), Decimal(8)

    def test_five_classifications(self):
        self.assertEqual(classify([], self.LOW, self.HIGH), "unknown")
        self.assertEqual(
            classify([(Decimal(3), Decimal(4))], self.LOW, self.HIGH), "within"
        )
        self.assertEqual(
            classify([(Decimal("8.1"), Decimal(9))], self.LOW, self.HIGH), "above"
        )
        self.assertEqual(
            classify([(Decimal(0), Decimal("1.9"))], self.LOW, self.HIGH), "below"
        )
        self.assertEqual(
            classify([(Decimal(0), Decimal(1)), (Decimal(9), Decimal(10))],
                     self.LOW, self.HIGH),
            "mixed",
        )
        self.assertEqual(
            classify([(Decimal(7), Decimal(9))], self.LOW, self.HIGH), "mixed"
        )

    def test_limit_equality_is_within(self):
        self.assertEqual(
            classify([(Decimal(2), Decimal(8))], self.LOW, self.HIGH), "within"
        )
        self.assertEqual(
            classify([(Decimal(2), Decimal(2)), (Decimal(8), Decimal(8))],
                     self.LOW, self.HIGH),
            "within",
        )

    def test_separated_components_above_and_below_stay_definite(self):
        components = [(Decimal(0), Decimal(1)), (Decimal(9), Decimal(10))]
        self.assertEqual(classify(components, self.LOW, self.HIGH), "mixed")

    def test_distance_bounds_over_full_set(self):
        # [0,1] and [9,10] with limits [2,8]: d ranges 1..2 and 1..2.
        minimum, maximum = distance_bounds(
            [(Decimal(0), Decimal(1)), (Decimal(9), Decimal(10))],
            self.LOW, self.HIGH,
        )
        self.assertEqual(minimum, Decimal(1))
        self.assertEqual(maximum, Decimal(2))
        # [7,9]: d reaches 0 inside and 1 at the top; min must come from the set,
        # not from the envelope endpoints alone.
        minimum, maximum = distance_bounds(
            [(Decimal(7), Decimal(9))], self.LOW, self.HIGH
        )
        self.assertEqual(minimum, Decimal(0))
        self.assertEqual(maximum, Decimal(1))
        # Entirely outside above: [11,11].
        minimum, maximum = distance_bounds(
            [(Decimal(11), Decimal(11))], self.LOW, self.HIGH
        )
        self.assertEqual((minimum, maximum), (Decimal(3), Decimal(3)))


if __name__ == "__main__":
    unittest.main()
