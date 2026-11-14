"""End-to-end tests driving ``python -m cold_exposure.recall`` as a subprocess.

The CLI runs in an isolated interpreter (``-I``) launched from a directory
outside the project, mirroring an offline operator run so a planted module on
the current working directory cannot be imported. The legacy v1, v2 and
hand-off entries are also re-executed here to prove they keep working
unchanged.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
LOTS = PROJECT / "examples" / "recall" / "lots.csv"
FLOWS = PROJECT / "examples" / "recall" / "flows.csv"

_LAUNCHER = (
    "import runpy, sys; "
    "sys.path.insert(0, %r); "
    "module = sys.argv[1]; "
    "sys.argv = [module] + sys.argv[2:]; "
    "runpy.run_module(module, run_name='__main__')"
    % str(PROJECT)
)


def module_command(module, arguments):
    return [sys.executable, "-I", "-c", _LAUNCHER, module, *arguments]


def run_cli(arguments, cwd):
    return subprocess.run(
        module_command("cold_exposure.recall", arguments),
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sql_dump(path):
    conn = sqlite3.connect(path)
    lines = "\n".join(conn.iterdump())
    conn.close()
    return lines


class RecallCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        # Private copies of the inputs so tests can move them away later.
        self.lots = self.dir / "lots.csv"
        self.flows = self.dir / "flows.csv"
        shutil.copyfile(LOTS, self.lots)
        shutil.copyfile(FLOWS, self.flows)
        self.db = self.dir / "genealogy.db"

    def tearDown(self):
        self._dir.cleanup()

    def build(self, expect_success=True):
        completed = run_cli(
            ["build", "--lots", str(self.lots),
             "--flows", str(self.flows), "--output", str(self.db)],
            cwd=self.dir,
        )
        if expect_success:
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)
            return json.loads(completed.stdout)
        self.assertNotEqual(completed.returncode, 0)
        return completed

    def recall(self, *lot_ids, expect_success=True):
        arguments = ["recall", "--database", str(self.db)]
        for lot_id in lot_ids:
            arguments += ["--lot", lot_id]
        completed = run_cli(arguments, cwd=self.dir)
        if expect_success:
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)
            return json.loads(completed.stdout)
        self.assertNotEqual(completed.returncode, 0)
        return completed

    def test_build_verify_and_hand_computed_recalls(self):
        summary = self.build()
        self.assertEqual(summary["lots"], 11)
        self.assertEqual(summary["edges"], 11)
        self.assertEqual(summary["raw_materials"], 4)
        self.assertEqual(summary["total_declared_kg"], "7151.250000")
        self.assertEqual(summary["total_remaining_kg"], "2800.500000")

        verified = run_cli(["verify", "--database", str(self.db)], cwd=self.dir)
        self.assertEqual(verified.returncode, 0, msg=verified.stderr)
        payload = json.loads(verified.stdout)
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["lots"], 11)
        self.assertEqual(payload["edges"], 11)

        # Diamond confluence: BLEND-C is reached via BASE-A and BASE-B but
        # contributes its remaining 199.5 kg exactly once.
        result = self.recall("RAW-MILK")
        self.assertEqual(result["affected_lot_count"], 7)
        self.assertEqual(result["total_remaining_kg"], "2250.250000")
        remaining = {row["lot_id"]: row["remaining_kg"] for row in result["affected_lots"]}
        self.assertEqual(
            remaining,
            {
                "RAW-MILK": "600.000000",
                "BASE-A": "200.000000",
                "BASE-B": "0.000000",
                "BLEND-C": "199.500000",
                "PACK-C1": "600.000000",
                "PACK-C2": "400.500000",
                "FLAVORED-D": "250.250000",
            },
        )
        # Evidence traces to physical input lines (header is line 1).
        lines = {row["lot_id"]: row["lots_csv_line"] for row in result["affected_lots"]}
        self.assertEqual(lines["RAW-MILK"], 3)
        self.assertEqual(lines["BLEND-C"], 2)
        edge_lines = {
            (row["source_lot"], row["target_lot"]): row["flows_csv_line"]
            for row in result["affected_edges"]
        }
        self.assertEqual(edge_lines[("RAW-MILK", "BASE-A")], 4)
        self.assertEqual(edge_lines[("BASE-A", "BLEND-C")], 10)
        # The unrelated oat chain is untouched.
        self.assertNotIn("PACK-OATS", remaining)

        # Multiple overlapping starting points: union, still no double count.
        result = self.recall("BASE-B", "BLEND-C")
        self.assertEqual(result["affected_lot_count"], 5)
        self.assertEqual(result["total_remaining_kg"], "1450.250000")
        self.assertEqual(result["recalled_lots"], ["BASE-B", "BLEND-C"])

        # Both raw starts: 8 lots, 2250.25 + 200 (RAW-SUGAR remainder).
        result = self.recall("RAW-MILK", "RAW-SUGAR")
        self.assertEqual(result["affected_lot_count"], 8)
        self.assertEqual(result["total_remaining_kg"], "2450.250000")

        # Unrelated chain recalls only itself.
        result = self.recall("PACK-OATS")
        self.assertEqual(result["affected_lot_count"], 1)
        self.assertEqual(result["total_remaining_kg"], "300.000000")

    def test_database_rows_and_archived_sources(self):
        self.build()
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        locations = {
            row["name"]: dict(row)
            for row in conn.execute("SELECT * FROM source_location")
        }
        self.assertEqual(set(locations), {"lots_csv", "flows_csv"})
        self.assertEqual(locations["lots_csv"]["record_count"], 11)
        self.assertEqual(locations["lots_csv"]["csv_header_line_number"], 1)
        self.assertEqual(locations["lots_csv"]["first_data_line_number"], 2)
        self.assertEqual(locations["flows_csv"]["record_count"], 11)
        documents = {
            row["name"]: row["content"]
            for row in conn.execute("SELECT name, content FROM source_document")
        }
        self.assertEqual(documents["lots_csv"].encode("utf-8"), self.lots.read_bytes())
        self.assertEqual(documents["flows_csv"].encode("utf-8"), self.flows.read_bytes())
        # Exact SQL-side balance spot checks on the micro-kg integers.
        blend = dict(conn.execute("SELECT * FROM lot WHERE lot_id = 'BLEND-C'").fetchone())
        self.assertEqual(blend["declared_micro"], 1_200_000_000)
        self.assertEqual(blend["outgoing_micro"], 1_000_500_000)
        self.assertEqual(blend["remaining_micro"], 199_500_000)
        self.assertEqual(blend["remaining_kg"], "199.500000")
        recomputed = conn.execute(
            "SELECT SUM(quantity_micro) FROM flow WHERE target_lot = 'BLEND-C'"
        ).fetchone()[0]
        self.assertEqual(recomputed, blend["incoming_micro"])
        run = dict(conn.execute("SELECT * FROM ingestion_run WHERE id = 1").fetchone())
        self.assertEqual(run["schema_version"], 1)
        self.assertIn("liability", run["policy_notes"])
        conn.close()

    def test_sources_moved_away_recall_and_verify_still_work(self):
        self.build()
        before = hash_file(self.db)
        moved = self.dir / "moved"
        moved.mkdir()
        shutil.move(str(self.lots), moved / "lots.csv")
        shutil.move(str(self.flows), moved / "flows.csv")
        result = self.recall("RAW-MILK")
        self.assertEqual(result["total_remaining_kg"], "2250.250000")
        verified = run_cli(["verify", "--database", str(self.db)], cwd=self.dir)
        self.assertEqual(verified.returncode, 0, msg=verified.stderr)
        # Recall and verify are read-only: the database bytes never change.
        self.assertEqual(hash_file(self.db), before)

    def test_unknown_recall_lot_rejected_and_database_untouched(self):
        self.build()
        before = hash_file(self.db)
        completed = self.recall("RAW-MILK", "NOPE", expect_success=False)
        self.assertIn("unknown lot_id(s): 'NOPE'", completed.stderr)
        self.assertEqual(hash_file(self.db), before)

    def test_cycle_rejected_before_database_is_touched(self):
        self.flows.write_text(
            "source_lot,target_lot,quantity_kg\n"
            "BASE-A,BLEND-C,700\n"
            "BLEND-C,BASE-A,700\n",
            encoding="utf-8",
        )
        completed = self.build(expect_success=False)
        self.assertIn("cycle", completed.stderr)
        self.assertFalse(self.db.exists())
        self.assertEqual(list(self.dir.glob(".*.tmp")), [])

    def test_over_consumption_rejected_before_database_is_touched(self):
        self.flows.write_text(
            "source_lot,target_lot,quantity_kg\n"
            "RAW-MILK,BASE-A,800\n"
            "RAW-MILK,BASE-B,600\n"
            "RAW-MILK,PACK-C1,700\n",
            encoding="utf-8",
        )
        completed = self.build(expect_success=False)
        self.assertIn("exceeds", completed.stderr)
        self.assertFalse(self.db.exists())

    def test_inflow_mismatch_rejected(self):
        self.flows.write_text(
            "source_lot,target_lot,quantity_kg\n"
            "RAW-MILK,BASE-A,700\n",
            encoding="utf-8",
        )
        completed = self.build(expect_success=False)
        self.assertIn("must equal the declared quantity", completed.stderr)
        self.assertFalse(self.db.exists())

    def test_write_failure_keeps_previous_database_byte_for_byte(self):
        self.build()
        before = hash_file(self.db)
        blocked = self.dir / "blocked"
        blocked.mkdir()
        os.chmod(blocked, 0o500)
        target = blocked / "cannot.db"
        try:
            completed = run_cli(
                ["build", "--lots", str(self.lots),
                 "--flows", str(self.flows), "--output", str(target)],
                cwd=self.dir,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("recall failed", completed.stderr)
            self.assertFalse(target.exists())
            self.assertEqual(list(blocked.glob(".*.tmp")), [])
        finally:
            os.chmod(blocked, 0o700)
        self.assertEqual(hash_file(self.db), before)
        verified = run_cli(["verify", "--database", str(self.db)], cwd=self.dir)
        self.assertEqual(verified.returncode, 0, msg=verified.stderr)
        # A corrected rerun over the same path succeeds.
        summary = self.build()
        self.assertEqual(summary["lots"], 11)

    def test_rebuild_is_deterministic_and_rerun_replaces_atomically(self):
        self.build()
        first = hash_file(self.db)
        second_db = self.dir / "second.db"
        completed = run_cli(
            ["build", "--lots", str(self.lots),
             "--flows", str(self.flows), "--output", str(second_db)],
            cwd=self.dir,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(sql_dump(self.db), sql_dump(second_db))
        self.assertEqual(hash_file(self.db), first)
        # Rebuilding the same path atomically replaces it and still verifies.
        completed = run_cli(
            ["build", "--lots", str(self.lots),
             "--flows", str(self.flows), "--output", str(self.db)],
            cwd=self.dir,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        verified = run_cli(["verify", "--database", str(self.db)], cwd=self.dir)
        self.assertEqual(verified.returncode, 0, msg=verified.stderr)

    def test_verify_rejects_tampered_database(self):
        self.build()
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "UPDATE lot SET remaining_micro = remaining_micro + 1 WHERE lot_id = 'RAW-MILK'"
        )
        conn.commit()
        conn.close()
        completed = run_cli(["verify", "--database", str(self.db)], cwd=self.dir)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("verification failed", completed.stderr)

    def test_recall_requires_at_least_one_lot(self):
        self.build()
        completed = run_cli(["recall", "--database", str(self.db)], cwd=self.dir)
        self.assertNotEqual(completed.returncode, 0)

    def test_legacy_entries_still_work(self):
        # v1 exposure pipeline.
        v1_out = self.dir / "v1"
        completed = subprocess.run(
            module_command(
                "cold_exposure",
                [
                    "--trips", str(PROJECT / "data" / "trips.csv"),
                    "--readings", str(PROJECT / "data" / "readings.csv"),
                    "--output", str(v1_out),
                ],
            ),
            cwd=self.dir, env={"PATH": "/usr/bin:/bin"},
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertTrue((v1_out / "exposure.json").exists())
        # v2 consensus pipeline.
        v2_out = self.dir / "v2.json"
        completed = subprocess.run(
            module_command(
                "cold_exposure.consensus",
                [
                    "--trips", str(PROJECT / "data" / "consensus" / "trips.csv"),
                    "--assignments", str(PROJECT / "data" / "consensus" / "assignments.csv"),
                    "--readings", str(PROJECT / "data" / "consensus" / "readings.csv"),
                    "--max-gap-seconds", "900",
                    "--output", str(v2_out),
                ],
            ),
            cwd=self.dir, env={"PATH": "/usr/bin:/bin"},
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(json.loads(v2_out.read_text())["schema_version"], 2)
        # Hand-off marketplace build + verify.
        handoff_db = self.dir / "marketplace.db"
        generated = PROJECT / "examples" / "handoff" / "generated"
        completed = subprocess.run(
            module_command(
                "cold_exposure.handoff",
                [
                    "build",
                    "--consensus", str(generated / "consensus.json"),
                    "--handoffs", str(generated / "handoffs.csv"),
                    "--output", str(handoff_db),
                ],
            ),
            cwd=self.dir, env={"PATH": "/usr/bin:/bin"},
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        completed = subprocess.run(
            module_command("cold_exposure.handoff", ["verify", "--database", str(handoff_db)]),
            cwd=self.dir, env={"PATH": "/usr/bin:/bin"},
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)


if __name__ == "__main__":
    unittest.main()
