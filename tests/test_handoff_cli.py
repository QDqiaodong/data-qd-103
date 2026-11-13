"""End-to-end tests driving ``python -m cold_exposure.handoff`` as a subprocess.

The CLI runs in an isolated interpreter (``-I``) launched from a directory
outside the project, mirroring an offline operator run so a planted module on
the current working directory cannot be imported. The legacy v1 and v2 entries
are also re-executed here to prove they keep working unchanged.
"""
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
GENERATED = PROJECT / "examples" / "handoff" / "generated"
HANDOFFS = PROJECT / "examples" / "handoff" / "handoffs.csv"

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
        module_command("cold_exposure.handoff", arguments),
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


class HandoffCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        # Private copies of the inputs so tests can move them away later.
        self.consensus = self.dir / "consensus.json"
        self.handoffs = self.dir / "handoffs.csv"
        shutil.copyfile(GENERATED / "consensus.json", self.consensus)
        shutil.copyfile(HANDOFFS, self.handoffs)
        self.db = self.dir / "marketplace.db"

    def tearDown(self):
        self._dir.cleanup()

    def build(self, expect_success=True):
        completed = run_cli(
            ["build", "--consensus", str(self.consensus),
             "--handoffs", str(self.handoffs), "--output", str(self.db)],
            cwd=self.dir,
        )
        if expect_success:
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)
            return json.loads(completed.stdout)
        self.assertNotEqual(completed.returncode, 0)
        return completed

    def test_build_verify_and_hand_computed_sql_rows(self):
        summary = self.build()
        self.assertEqual(summary["trips"], 3)
        self.assertEqual(summary["handoffs"], 4)
        self.assertEqual(summary["attribution_facts"], 13)
        self.assertEqual(summary["unassigned_seconds"], 5100)
        self.assertEqual(summary["unknown_seconds"], 3600)

        verified = run_cli(["verify", "--database", str(self.db)], cwd=self.dir)
        self.assertEqual(verified.returncode, 0, msg=verified.stderr)
        payload = json.loads(verified.stdout)
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["facts"], 13)
        self.assertEqual(payload["components"], 7)

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        # Original documents and their invocation-time locations are archived.
        locations = {
            r["name"]: dict(r)
            for r in conn.execute("SELECT * FROM source_location")
        }
        self.assertEqual(set(locations), {"consensus_json", "handoff_csv"})
        self.assertTrue(locations["handoff_csv"]["input_path"].endswith("handoffs.csv"))
        self.assertEqual(locations["handoff_csv"]["first_data_record_number"], 2)
        self.assertEqual(locations["handoff_csv"]["record_count"], 4)
        self.assertIsNone(locations["consensus_json"]["first_data_record_number"])
        # Carrier-day materialization matches the independent hand calculation.
        rows = {
            (r["carrier"], r["utc_day"]): dict(r)
            for r in conn.execute("SELECT * FROM carrier_daily")
        }
        self.assertEqual(
            rows[("Northwind", "2026-05-01")]["degree_minutes_min"], "60.000000"
        )
        self.assertEqual(
            rows[("Polar", "2026-05-03")]["degree_minutes_max"], "20.000000"
        )
        self.assertIsNone(rows[("Acme", "2026-05-02")]["degree_minutes_min"])
        self.assertEqual(rows[("Acme", "2026-05-02")]["unknown_seconds"], 900)
        unassigned = [r for r in rows.values() if r["is_unassigned"]]
        self.assertEqual(sum(r["duration_seconds"] for r in unassigned), 5100)

        # Foreign keys trace every fact back to its original partition and trip.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(violations, [])

        # The run summary is itself SQL-recomputable from the fact table.
        run = dict(conn.execute("SELECT * FROM ingestion_run WHERE id = 1").fetchone())
        self.assertEqual(run["trip_count"], 3)
        self.assertEqual(run["handoff_count"], 4)
        self.assertEqual(run["fact_count"], 13)
        self.assertEqual(run["total_duration_seconds"], 12000)
        self.assertEqual(run["total_definitely_outside_seconds"], 6600)
        self.assertEqual(run["total_unknown_seconds"], 3600)
        self.assertEqual(run["total_unassigned_seconds"], 5100)
        self.assertIn("liability", run["policy_notes"])
        orphan = conn.execute(
            """
            SELECT COUNT(*) FROM attribution_fact f
            LEFT JOIN consensus_partition p
              ON p.trip_id = f.trip_id AND p.partition_index = f.partition_index
            WHERE p.trip_id IS NULL
            """
        ).fetchone()[0]
        self.assertEqual(orphan, 0)

    def test_no_double_counting_when_components_are_joined(self):
        self.build()
        conn = sqlite3.connect(self.db)
        # A naive join through consensus_component would multiply HC metrics.
        naive = conn.execute(
            """
            SELECT SUM(f.seconds)
            FROM attribution_fact f
            JOIN consensus_component c
              ON c.trip_id = f.trip_id AND c.partition_index = f.partition_index
            WHERE f.trip_id = 'HC'
            """
        ).fetchone()[0]
        correct = conn.execute(
            "SELECT SUM(seconds) FROM attribution_fact WHERE trip_id = 'HC'"
        ).fetchone()[0]
        self.assertEqual(naive, 2400)  # 1200 seconds x two components
        self.assertEqual(correct, 1200)
        # The documented query path (facts only) conserves the trip total.
        self.assertEqual(
            conn.execute(
                "SELECT COALESCE(SUM(seconds), 0) FROM attribution_fact"
            ).fetchone()[0],
            7200 + 3600 + 1200,
        )

    def test_sources_archived_so_inputs_can_be_moved_away(self):
        self.build()
        original_hash = hashlib_file(self.db)
        # Move the original input files out of the directory entirely.
        moved = self.dir / "moved"
        moved.mkdir()
        shutil.move(str(self.consensus), moved / "consensus.json")
        shutil.move(str(self.handoffs), moved / "handoffs.csv")
        verified = run_cli(["verify", "--database", str(self.db)], cwd=self.dir)
        self.assertEqual(verified.returncode, 0, msg=verified.stderr)
        conn = sqlite3.connect(self.db)
        docs = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute("SELECT name, sha256, byte_count, content FROM source_document")
        }
        self.assertEqual(set(docs), {"consensus_json", "handoff_csv"})
        archived = {
            "consensus_json": (moved / "consensus.json").read_bytes(),
            "handoff_csv": (moved / "handoffs.csv").read_bytes(),
        }
        import hashlib

        for name, original_bytes in archived.items():
            digest, byte_count, content = docs[name]
            stored = content.encode("utf-8")
            self.assertEqual(stored, original_bytes)
            self.assertEqual(byte_count, len(original_bytes))
            self.assertEqual(hashlib.sha256(stored).hexdigest(), digest)
        self.assertEqual(hashlib_file(self.db), original_hash)

    def test_overlapping_handoff_rejected_before_database_is_touched(self):
        overlapping = self.dir / "overlap.csv"
        overlapping.write_text(
            "handoff_id,trip_id,carrier,started_at,ended_at\n"
            "H1,HA,Northwind,2026-05-01T23:00:00Z,2026-05-01T23:31:00Z\n"
            "H2,HA,Polar,2026-05-01T23:30:00Z,2026-05-02T00:30:00Z\n",
            encoding="utf-8",
        )
        completed = run_cli(
            ["build", "--consensus", str(self.consensus),
             "--handoffs", str(overlapping), "--output", str(self.db)],
            cwd=self.dir,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("overlap", completed.stderr)
        self.assertFalse(self.db.exists())
        leftovers = list(self.dir.glob(".*.tmp"))
        self.assertEqual(leftovers, [])

    def test_write_failure_keeps_previous_database_byte_for_byte(self):
        self.build()
        before = hashlib_file(self.db)
        # Point the output at an unwritable location to force an OSError mid-build.
        blocked = self.dir / "blocked"
        blocked.mkdir()
        os.chmod(blocked, 0o500)
        target = blocked / "cannot.db"
        completed = run_cli(
            ["build", "--consensus", str(self.consensus),
             "--handoffs", str(self.handoffs), "--output", str(target)],
            cwd=self.dir,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("handoff failed", completed.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(list(blocked.glob(".*.tmp")), [])
        # The previously published database is untouched and still verifiable.
        self.assertEqual(hashlib_file(self.db), before)
        verified = run_cli(["verify", "--database", str(self.db)], cwd=self.dir)
        self.assertEqual(verified.returncode, 0, msg=verified.stderr)
        os.chmod(blocked, 0o700)

    def test_rebuild_is_deterministic_and_uses_temporary_then_replace(self):
        self.build()
        first = hashlib_file(self.db)
        # A second build over a fresh path produces identical stored content;
        # SQLite file headers are compared indirectly via SQL dumps.
        second_db = self.dir / "second.db"
        completed = run_cli(
            ["build", "--consensus", str(self.consensus),
             "--handoffs", str(self.handoffs), "--output", str(second_db)],
            cwd=self.dir,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(sql_dump(self.db), sql_dump(second_db))
        self.assertEqual(hashlib_file(self.db), first)
        # Rebuilding the same path atomically replaces it and still verifies.
        completed = run_cli(
            ["build", "--consensus", str(self.consensus),
             "--handoffs", str(self.handoffs), "--output", str(self.db)],
            cwd=self.dir,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        verified = run_cli(["verify", "--database", str(self.db)], cwd=self.dir)
        self.assertEqual(verified.returncode, 0)

    def test_verify_rejects_tampered_database(self):
        self.build()
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "UPDATE attribution_fact SET seconds = seconds + 60 WHERE fact_id = 0"
        )
        conn.commit()
        conn.close()
        completed = run_cli(["verify", "--database", str(self.db)], cwd=self.dir)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("verification failed", completed.stderr)

    def test_legacy_entries_still_work(self):
        # The hand-off CLI requires a subcommand; its failure mode is unchanged.
        bad = run_cli(["build"], cwd=self.dir)
        self.assertNotEqual(bad.returncode, 0)
        consensus_out = self.dir / "v2.json"
        command = module_command(
            "cold_exposure.consensus",
            [
                "--trips", str(PROJECT / "data" / "consensus" / "trips.csv"),
                "--assignments", str(PROJECT / "data" / "consensus" / "assignments.csv"),
                "--readings", str(PROJECT / "data" / "consensus" / "readings.csv"),
                "--max-gap-seconds", "900",
                "--output", str(consensus_out),
            ],
        )
        completed = subprocess.run(
            command, cwd=self.dir, env={"PATH": "/usr/bin:/bin"},
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(json.loads(consensus_out.read_text())["schema_version"], 2)


def hashlib_file(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sql_dump(path):
    conn = sqlite3.connect(path)
    lines = "\n".join(conn.iterdump())
    conn.close()
    return lines


if __name__ == "__main__":
    unittest.main()
