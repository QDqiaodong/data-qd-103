"""Subprocess CLI tests for ``python -m cold_exposure.flash``.

The CLI is launched under ``python -I`` from outside the project so no planted
module can be imported, mirroring an offline operator run.
"""
import json
import tempfile
import unittest
from pathlib import Path

from cold_exposure.flash.image import (
    FlashError,
    pack_header,
    pack_slot,
)
from cold_exposure.flash.builder import build_ring_image
from tests._flash_helpers import read_csv_rows, run_flash_cli, write_anchor_file

DEVICE = "DEV_CLI"
SENSOR = "SENSOR-CLI"


def simple_image(device=DEVICE):
    records = [
        {"seq": 0, "tick": 10, "boot_id": 1, "temperature_centidegrees": 500},
        {"seq": 1, "tick": 11, "boot_id": 1, "temperature_centidegrees": 510},
        {"seq": 2, "tick": 12, "boot_id": 1, "temperature_centidegrees": 520},
    ]
    return build_ring_image(
        capacity=4, records=records, next_slot=3, next_seq=3, device_id=device
    )


def simple_anchors(path, device=DEVICE):
    write_anchor_file(
        path,
        device,
        SENSOR,
        [{"boot_id": 1, "seq": 0, "observed_at": "2026-03-01T00:00:00Z"}],
    )


class FlashCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        self.image = self.dir / "image.bin"
        self.anchors = self.dir / "anchors.json"
        self.out = self.dir / "published"
        self.image.write_bytes(simple_image())
        simple_anchors(self.anchors)

    def tearDown(self):
        self._dir.cleanup()

    def args(self, output=None):
        return [
            "--image", self.image,
            "--anchors", self.anchors,
            "--output-dir", output or self.out,
        ]

    def test_success_writes_exactly_two_products(self):
        completed = run_flash_cli(self.args())
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["readings.csv", "transcription.json"])
        rows = read_csv_rows(self.out / "readings.csv")
        self.assertEqual(rows[1][0], SENSOR)
        self.assertEqual(rows[1][1:], ["2026-03-01T00:00:00Z", "5.00"])
        self.assertEqual(rows[3][1:], ["2026-03-01T00:00:02Z", "5.20"])

    def test_deterministic_runs_in_two_directories(self):
        first = self.dir / "one"
        second = self.dir / "two"
        run_flash_cli(self.args(output=first))
        run_flash_cli(self.args(output=second))
        for name in ("readings.csv", "transcription.json"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

    def test_existing_output_refused_and_rerun_allowed_after_cleanup(self):
        self.out.mkdir()
        completed = run_flash_cli(self.args(), expect_success=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("already exists", completed.stderr)
        self.out.rmdir()
        completed = run_flash_cli(self.args())
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

    def test_symlink_output_refused(self):
        target = self.dir / "real_dir"
        target.mkdir()
        self.out.symlink_to(target)
        completed = run_flash_cli(self.args(), expect_success=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("symbolic link", completed.stderr)
        # Nothing was written through the link.
        self.assertEqual(list(target.iterdir()), [])

    def test_truncated_image_fails_with_locatable_reason(self):
        self.image.write_bytes(self.image.read_bytes()[:-3])
        completed = run_flash_cli(self.args(), expect_success=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("image length", completed.stderr)
        self.assertFalse(self.out.exists())

    def test_trailing_bytes_fail_exact_length_contract(self):
        self.image.write_bytes(self.image.read_bytes() + b"\x00\x00")
        completed = run_flash_cli(self.args(), expect_success=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("image length", completed.stderr)

    def test_bad_anchor_json_fails_nonzero(self):
        self.anchors.write_text("{not json", encoding="utf-8")
        completed = run_flash_cli(self.args(), expect_success=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("anchors", completed.stderr)
        self.assertFalse(self.out.exists())


class NewerHeaderBadDataTests(unittest.TestCase):
    """The newer valid header wins even when its active ring is corrupt."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        capacity = 5
        slots = [b"\x00" * 24 for _ in range(capacity)]
        # Shared active slots 0 and 1 carry seq 2 and 3, valid under both rings.
        slots[0] = pack_slot(2, 10, 1, 500)
        slots[1] = pack_slot(3, 11, 1, 510)
        slots[4] = pack_slot(1, 9, 1, 490)
        # Slot 3 is active only under the newer header and is deliberately bad.
        bad = bytearray(pack_slot(0, 8, 1, 480))
        bad[21] ^= 0x01
        slots[3] = bytes(bad)
        header_a = pack_header(1, capacity, 2, 2, 4, DEVICE)  # active: slots 0,1
        header_b = pack_header(2, capacity, 4, 2, 4, DEVICE)  # active: slots 3,4,0,1
        self.image_bytes = header_a + header_b + b"".join(slots)
        self.image = self.dir / "image.bin"
        self.image.write_bytes(self.image_bytes)
        self.anchors = self.dir / "anchors.json"
        # Anchors valid for the older ring (one boot, seq 2 and 3).
        write_anchor_file(
            self.anchors, DEVICE, SENSOR,
            [{"boot_id": 1, "seq": 2, "observed_at": "2026-03-01T00:00:00Z"}],
        )
        self.out = self.dir / "out"

    def tearDown(self):
        self._dir.cleanup()

    def test_newer_header_bad_active_slot_fails_without_fallback(self):
        completed = run_flash_cli([
            "--image", self.image, "--anchors", self.anchors, "--output-dir", self.out,
        ], expect_success=False)
        self.assertNotEqual(completed.returncode, 0)
        # The error names the physical slot and its absolute offset (128+24*3=200).
        self.assertIn("slot 3", completed.stderr)
        self.assertIn("offset 200", completed.stderr)
        self.assertFalse(self.out.exists())
        # Prove the older header alone would have transcribed: invalidate B.
        salvageable = bytearray(self.image_bytes)
        salvageable[64 + 60] ^= 0xFF
        self.image.write_bytes(bytes(salvageable))
        completed = run_flash_cli([
            "--image", self.image, "--anchors", self.anchors,
            "--output-dir", self.dir / "salvaged",
        ])
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        rows = read_csv_rows((self.dir / "salvaged") / "readings.csv")
        self.assertEqual(len(rows), 3)  # header + two records
        audit = json.loads(((self.dir / "salvaged") / "transcription.json").read_text())
        self.assertEqual(audit["headers"]["selected_header"], "header_A")
        self.assertIn("header_B rejected", audit["headers"]["selection_reason"])

    def test_corrupt_active_slot_error_directly_through_api(self):
        from cold_exposure.flash.transcribe import CapturedInputs, transcribe_capture
        with self.assertRaisesRegex(FlashError, r"slot 3 at absolute offset 200"):
            transcribe_capture(
                CapturedInputs(self.image_bytes, self.anchors.read_bytes())
            )


if __name__ == "__main__":
    unittest.main()
