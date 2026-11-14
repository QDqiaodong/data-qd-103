"""Hand-computed model tests for the batch genealogy recall library.

Every expectation is computed by hand from the contract: whole-lot descendant
propagation, remaining = declared - outgoing, confluence and multiple starts
counted once, flow quantities never acting as contamination mass.
"""
import unittest

from cold_exposure.recall import (
    RecallError,
    build_model,
    compute_recall,
    kg_text,
    parse_quantity,
)


def csv_bytes(rows, header):
    lines = [header] + [",".join(row) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def lots_csv(rows):
    return csv_bytes(rows, "lot_id,quantity_kg")


def flows_csv(rows):
    return csv_bytes(rows, "source_lot,target_lot,quantity_kg")


def build(lots_rows, flow_rows):
    return build_model(lots_csv(lots_rows), flows_csv(flow_rows))


def recall(model, *lot_ids):
    return compute_recall(model["lots"], model["edges"], lot_ids)


def remaining_by_lot(result):
    return {row["lot_id"]: row["remaining_kg"] for row in result["affected_lots"]}


class DiamondConfluenceTests(unittest.TestCase):
    """R feeds A and B; A and B both feed C. C must be counted once."""

    def setUp(self):
        self.model = build(
            [("R", "150"), ("A", "60"), ("B", "40"), ("C", "90")],
            [("R", "A", "60"), ("R", "B", "40"), ("A", "C", "50"), ("B", "C", "40")],
        )

    def test_recall_from_raw_counts_each_lot_once(self):
        result = recall(self.model, "R")
        self.assertEqual(result["affected_lot_count"], 4)
        # R: 150-100=50; A: 60-50=10; B: 40-40=0; C: 90-0=90 (reached twice, counted once)
        self.assertEqual(
            remaining_by_lot(result),
            {"R": "50.000000", "A": "10.000000", "B": "0.000000", "C": "90.000000"},
        )
        self.assertEqual(result["total_remaining_kg"], "150.000000")

    def test_recall_from_both_branches_still_counts_c_once(self):
        result = recall(self.model, "A", "B")
        self.assertEqual(result["affected_lot_count"], 3)
        self.assertEqual(result["total_remaining_kg"], "100.000000")

    def test_flow_quantity_is_not_a_contamination_mass(self):
        # A tiny 1-gram edge still pulls the whole target lot into the recall.
        model = build(
            [("R", "10"), ("S", "5"), ("M", "15")],
            [("R", "M", "10"), ("S", "M", "5")],
        )
        result = recall(model, "S")
        self.assertEqual(remaining_by_lot(result), {"S": "0.000000", "M": "15.000000"})
        self.assertEqual(result["total_remaining_kg"], "15.000000")


class MixedLotPartialConsumptionTests(unittest.TestCase):
    """A mixed lot that is only partially consumed keeps remaining stock."""

    def setUp(self):
        self.model = build(
            [("RAW1", "100"), ("RAW2", "200"), ("MIX", "300"), ("OUT1", "120.000001")],
            [
                ("RAW1", "MIX", "100"),
                ("RAW2", "MIX", "200"),
                ("MIX", "OUT1", "120.000001"),
            ],
        )

    def test_partial_consumption_leaves_exact_micro_remainder(self):
        result = recall(self.model, "RAW2")
        # MIX remaining = 300 - 120.000001 = 179.999999 exactly (integer micro-kg).
        self.assertEqual(
            remaining_by_lot(result),
            {"RAW2": "0.000000", "MIX": "179.999999", "OUT1": "120.000001"},
        )
        self.assertEqual(result["total_remaining_kg"], "300.000000")

    def test_unaffected_upstream_sibling_is_excluded(self):
        result = recall(self.model, "RAW1")
        self.assertNotIn("RAW2", remaining_by_lot(result))
        self.assertEqual(result["affected_lot_count"], 3)

    def test_recall_is_downstream_only(self):
        result = recall(self.model, "MIX")
        self.assertEqual(
            remaining_by_lot(result),
            {"MIX": "179.999999", "OUT1": "120.000001"},
        )


class MultipleOverlappingStartsTests(unittest.TestCase):
    """B->C->D plus B->D; starting at B and C must not double count D."""

    def setUp(self):
        self.model = build(
            [("A", "10"), ("B", "10"), ("C", "6"), ("D", "10")],
            [
                ("A", "B", "10"),
                ("B", "C", "6"),
                ("B", "D", "4"),
                ("C", "D", "6"),
            ],
        )

    def test_overlapping_starts_union_without_double_counting(self):
        result = recall(self.model, "B", "C")
        self.assertEqual(result["affected_lot_count"], 3)
        self.assertEqual(
            remaining_by_lot(result),
            {"B": "0.000000", "C": "0.000000", "D": "10.000000"},
        )
        self.assertEqual(result["total_remaining_kg"], "10.000000")

    def test_duplicate_start_is_deduplicated(self):
        result = recall(self.model, "D", "D")
        self.assertEqual(result["recalled_lots"], ["D"])
        self.assertEqual(result["affected_lot_count"], 1)
        self.assertEqual(result["total_remaining_kg"], "10.000000")

    def test_recall_root_flags(self):
        result = recall(self.model, "B", "C")
        roots = {row["lot_id"]: row["recall_root"] for row in result["affected_lots"]}
        self.assertEqual(roots, {"B": True, "C": True, "D": False})


class UnrelatedLotsTests(unittest.TestCase):
    def test_independent_chains_do_not_cross_contaminate(self):
        model = build(
            [("M1", "10"), ("M2", "10"), ("O1", "7"), ("O2", "7")],
            [("M1", "M2", "10"), ("O1", "O2", "7")],
        )
        result = recall(model, "M1")
        self.assertEqual(remaining_by_lot(result), {"M1": "0.000000", "M2": "10.000000"})
        result = recall(model, "O2")
        self.assertEqual(remaining_by_lot(result), {"O2": "7.000000"})

    def test_unknown_recall_ids_are_rejected_and_sorted(self):
        model = build([("A", "1")], [])
        with self.assertRaises(RecallError) as caught:
            recall(model, "Z9", "A", "Z1")
        message = str(caught.exception)
        self.assertIn("unknown lot_id(s): 'Z1', 'Z9'", message)

    def test_no_recall_id_is_rejected(self):
        model = build([("A", "1")], [])
        with self.assertRaises(RecallError):
            recall(model)


class CycleRejectionTests(unittest.TestCase):
    def test_self_loop_rejected_at_parse(self):
        with self.assertRaises(RecallError) as caught:
            build([("A", "1")], [("A", "A", "1")])
        self.assertIn("self-loop", str(caught.exception))
        self.assertIn("line 2", str(caught.exception))

    def test_two_cycle_rejected(self):
        with self.assertRaises(RecallError) as caught:
            build(
                [("A", "2"), ("B", "2")],
                [("A", "B", "2"), ("B", "A", "2")],
            )
        self.assertIn("cycle", str(caught.exception))
        self.assertIn("'A'", str(caught.exception))
        self.assertIn("'B'", str(caught.exception))

    def test_three_cycle_with_out_of_order_rows_rejected(self):
        with self.assertRaises(RecallError) as caught:
            build(
                [("C", "3"), ("A", "3"), ("B", "3")],
                [("C", "A", "3"), ("B", "C", "3"), ("A", "B", "3")],
            )
        self.assertIn("cycle", str(caught.exception))

    def test_cycle_report_includes_downstream_lots(self):
        with self.assertRaises(RecallError) as caught:
            build(
                [("A", "2"), ("B", "2"), ("C", "2"), ("D", "2")],
                [("A", "B", "2"), ("B", "A", "2"), ("B", "C", "2"), ("C", "D", "2")],
            )
        message = str(caught.exception)
        # C and D are not on the cycle but can never be ordered; they are named.
        for lot_id in ("'A'", "'B'", "'C'", "'D'"):
            self.assertIn(lot_id, message)


class BalanceRejectionTests(unittest.TestCase):
    def test_over_consumption_rejected(self):
        with self.assertRaises(RecallError) as caught:
            build(
                [("A", "10"), ("B", "6"), ("C", "5")],
                [("A", "B", "6"), ("A", "C", "5")],
            )
        self.assertIn("exceeds", str(caught.exception))

    def test_exact_full_consumption_accepted(self):
        model = build([("A", "10"), ("B", "10")], [("A", "B", "10")])
        self.assertEqual(model["lots"]["A"]["remaining_micro"], 0)

    def test_inflow_below_declared_rejected_for_non_raw(self):
        with self.assertRaises(RecallError) as caught:
            build([("A", "10"), ("B", "10")], [("A", "B", "9")])
        self.assertIn("must equal the declared quantity", str(caught.exception))

    def test_inflow_above_declared_rejected_for_non_raw(self):
        with self.assertRaises(RecallError) as caught:
            build([("A", "11"), ("B", "10")], [("A", "B", "11")])
        self.assertIn("must equal the declared quantity", str(caught.exception))

    def test_raw_material_may_declare_more_than_it_receives(self):
        model = build([("A", "10"), ("B", "3")], [("A", "B", "3")])
        self.assertTrue(model["lots"]["A"]["is_raw"])
        self.assertFalse(model["lots"]["B"]["is_raw"])


class QuantityFormatTests(unittest.TestCase):
    def test_valid_quantities_parse_to_exact_micro_kg(self):
        cases = {
            "1": 1_000_000,
            "0.000001": 1,
            "0007.5": 7_500_000,
            "999999999999.999999": 999_999_999_999_999_999,
            "0.1": 100_000,
        }
        for text, micro in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_quantity(text, "test"), micro)

    def test_invalid_quantities_rejected(self):
        for text in (
            "0", "0.000000", "-1", "+1", "1.1234567", "0.0000005",
            "abc", "1e3", "1.5E-2", "1.5 ", " 1.5", "", ".5", "1.",
            "1,5", "NaN", "Infinity", "1_000", "1234567890123",
        ):
            with self.subTest(text=text):
                with self.assertRaises(RecallError):
                    parse_quantity(text, "test")

    def test_micro_precision_sums_are_exact(self):
        # Three 0.000001 flows sum to exactly 0.000003; no float drift is possible.
        model = build(
            [("R1", "0.000001"), ("R2", "0.000001"), ("R3", "0.000001"), ("T", "0.000003")],
            [("R1", "T", "0.000001"), ("R2", "T", "0.000001"), ("R3", "T", "0.000001")],
        )
        self.assertEqual(model["lots"]["T"]["incoming_micro"], 3)
        self.assertEqual(kg_text(model["lots"]["R1"]["remaining_micro"]), "0.000000")

    def test_kg_text_canonical_form(self):
        self.assertEqual(kg_text(0), "0.000000")
        self.assertEqual(kg_text(1), "0.000001")
        self.assertEqual(kg_text(7_500_000), "7.500000")
        self.assertEqual(kg_text(999_999_999_999_999_999), "999999999999.999999")


class StructureTests(unittest.TestCase):
    def test_duplicate_lot_rejected(self):
        with self.assertRaises(RecallError) as caught:
            build([("A", "1"), ("A", "2")], [])
        self.assertIn("duplicate lot_id 'A'", str(caught.exception))

    def test_duplicate_edge_rejected(self):
        with self.assertRaises(RecallError) as caught:
            build(
                [("A", "2"), ("B", "2")],
                [("A", "B", "1"), ("A", "B", "1")],
            )
        self.assertIn("duplicate edge", str(caught.exception))

    def test_unknown_source_rejected(self):
        with self.assertRaises(RecallError) as caught:
            build([("A", "1")], [("ZZ", "A", "1")])
        self.assertIn("unknown source_lot 'ZZ'", str(caught.exception))

    def test_unknown_target_rejected(self):
        with self.assertRaises(RecallError) as caught:
            build([("A", "1")], [("A", "ZZ", "1")])
        self.assertIn("unknown target_lot 'ZZ'", str(caught.exception))

    def test_header_mismatch_rejected(self):
        with self.assertRaises(RecallError) as caught:
            build_model(b"lot,quantity_kg\nA,1\n", flows_csv([]))
        self.assertIn("CSV header must be exactly lot_id,quantity_kg", str(caught.exception))

    def test_missing_and_extra_fields_rejected(self):
        with self.assertRaises(RecallError):
            build_model(b"lot_id,quantity_kg\nA\n", flows_csv([]))
        with self.assertRaises(RecallError):
            build_model(b"lot_id,quantity_kg\nA,1,x\n", flows_csv([]))

    def test_non_utf8_rejected(self):
        with self.assertRaises(RecallError) as caught:
            build_model(b"lot_id,quantity_kg\nA\xff,1\n", flows_csv([]))
        self.assertIn("not valid UTF-8", str(caught.exception))

    def test_empty_lots_rejected_but_empty_flows_accepted(self):
        with self.assertRaises(RecallError) as caught:
            build_model(lots_csv([]), flows_csv([]))
        self.assertIn("at least one lot", str(caught.exception))
        model = build([("A", "1"), ("B", "2")], [])
        self.assertEqual(model["edges"], [])
        result = recall(model, "A")
        self.assertEqual(result["affected_lot_count"], 1)

    def test_id_whitespace_and_line_breaks_rejected(self):
        with self.assertRaises(RecallError):
            build([(" A", "1")], [])
        with self.assertRaises(RecallError):
            build([("A ", "1")], [])
        with self.assertRaises(RecallError):
            build_model(b'lot_id,quantity_kg\n"A\nB",1\n', flows_csv([]))


class OutOfOrderAndEvidenceTests(unittest.TestCase):
    def test_out_of_order_rows_accepted_with_canonical_order(self):
        # Children declared before parents; edges in reverse topological order.
        model = build(
            [("C", "2"), ("B", "2"), ("A", "4")],
            [("B", "C", "2"), ("A", "B", "2")],
        )
        self.assertEqual(model["lot_order"], ["A", "B", "C"])
        self.assertEqual([model["lots"][x]["ordinal"] for x in ("A", "B", "C")], [0, 1, 2])

    def test_topo_order_is_row_order_independent(self):
        forward = build([("A", "4"), ("B", "2"), ("C", "2")], [("A", "B", "2"), ("A", "C", "2")])
        backward = build([("C", "2"), ("B", "2"), ("A", "4")], [("A", "C", "2"), ("A", "B", "2")])
        self.assertEqual(forward["lot_order"], backward["lot_order"])

    def test_evidence_traces_to_physical_input_lines(self):
        model = build(
            [("B", "2"), ("A", "4"), ("C", "2")],
            [("B", "C", "2"), ("A", "B", "2")],
        )
        # Header is line 1; data rows follow in file order.
        self.assertEqual(model["lots"]["B"]["line"], 2)
        self.assertEqual(model["lots"]["A"]["line"], 3)
        self.assertEqual(model["lots"]["C"]["line"], 4)
        self.assertEqual(model["edges"][0]["line"], 2)
        self.assertEqual(model["edges"][1]["line"], 3)
        result = recall(model, "A")
        lines = {row["lot_id"]: row["lots_csv_line"] for row in result["affected_lots"]}
        self.assertEqual(lines, {"A": 3, "B": 2, "C": 4})
        edge_lines = [(e["source_lot"], e["flows_csv_line"]) for e in result["affected_edges"]]
        self.assertEqual(edge_lines, [("B", 2), ("A", 3)])

    def test_blank_lines_do_not_shift_line_evidence(self):
        data = b"lot_id,quantity_kg\nA,1\n\nB,2\n"
        model = build_model(data, flows_csv([]))
        self.assertEqual(model["lots"]["A"]["line"], 2)
        self.assertEqual(model["lots"]["B"]["line"], 4)


if __name__ == "__main__":
    unittest.main()
