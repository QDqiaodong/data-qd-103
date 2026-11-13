"""End-to-end transcription, anchor contract, determinism and publish safety."""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cold_exposure.flash.anchors import parse_anchor_bytes
from cold_exposure.flash.builder import build_ring_image
from cold_exposure.flash.image import (
    FlashError,
    pack_raw_slot,
)
from cold_exposure.flash.transcribe import (
    CapturedInputs,
    capture_inputs,
    publish,
    transcribe,
    transcribe_capture,
)
from tests._flash_helpers import read_csv_rows, write_anchor_file

DEVICE = "DEV_1"
SENSOR = "SENSOR-A"

SCENARIO_1_RECORDS = [
    {"seq": 4294967294, "tick": 4294967294, "boot_id": 7, "temperature_centidegrees": 400},
    {"seq": 4294967295, "tick": 4294967295, "boot_id": 7, "temperature_centidegrees": 900},
    {"seq": 0, "tick": 0, "boot_id": 7, "temperature_centidegrees": -125},
    {"seq": 1, "tick": 2, "boot_id": 7, "temperature_centidegrees": 800},
]
SCENARIO_1_ANCHORS = [
    {"boot_id": 7, "seq": 0, "observed_at": "2026-01-01T00:00:02Z"}
]


def scenario_one_image():
    # Unused physical slot 2 holds a stale record with a corrupted CRC and a
    # temperature bit pattern outside the allowed range.
    stale = bytearray(pack_raw_slot(99, 5, 7, 30000))
    stale[20] ^= 0xFF
    return build_ring_image(
        capacity=5,
        records=SCENARIO_1_RECORDS,
        next_slot=2,
        next_seq=2,
        device_id=DEVICE,
        stale_slots={2: bytes(stale)},
    )


class TranscriptionHarness(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        self.image_path = self.dir / "image.bin"
        self.anchors_path = self.dir / "anchors.json"
        self.out_dir = self.dir / "out"
        self.image_path.write_bytes(scenario_one_image())
        write_anchor_file(self.anchors_path, DEVICE, SENSOR, SCENARIO_1_ANCHORS)

    def tearDown(self):
        self._dir.cleanup()

    def capture(self):
        return capture_inputs(self.image_path, self.anchors_path)

    def transcribe_output(self):
        return transcribe(self.image_path, self.anchors_path, self.out_dir)


class GoldenScenarioOneTests(TranscriptionHarness):
    def test_csv_times_temperatures_and_stale_slot_ignored(self):
        self.transcribe_output()
        rows = read_csv_rows(self.out_dir / "readings.csv")
        self.assertEqual(rows[0], ["sensor_id", "observed_at", "temperature_c"])
        self.assertEqual(
            rows[1:],
            [
                [SENSOR, "2026-01-01T00:00:00Z", "4.00"],
                [SENSOR, "2026-01-01T00:00:01Z", "9.00"],
                [SENSOR, "2026-01-01T00:00:02Z", "-1.25"],
                [SENSOR, "2026-01-01T00:00:04Z", "8.00"],
            ],
        )
        # CSV data record numbers are 1-based after the header.
        self.assertEqual([row[1] for row in rows[1:]], [
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z",
            "2026-01-01T00:00:02Z", "2026-01-01T00:00:04Z",
        ])

    def test_physical_slots_offsets_and_byte_evidence(self):
        csv_text, audit_text, model = transcribe_capture(self.capture())
        audit = json.loads(audit_text)
        positions = audit["ring"]["active_logical_positions"]
        expected = [
            (0, 3, 128 + 24 * 3, 4294967294),
            (1, 4, 128 + 24 * 4, 4294967295),
            (2, 0, 128, 0),
            (3, 1, 128 + 24, 1),
        ]
        for entry, (logical, slot, offset, seq) in zip(positions, expected):
            self.assertEqual((entry["logical_index"], entry["physical_slot"],
                              entry["absolute_offset"], entry["expected_seq"]),
                             (logical, slot, offset, seq))
        self.assertEqual(audit["ring"]["unused_physical_slots"], [2])
        # Each audit record carries the full 24-byte hex matching the image.
        image = self.image_path.read_bytes()
        for record in audit["records"]:
            raw = image[record["absolute_offset"]:record["absolute_offset"] + 24]
            self.assertEqual(record["slot_raw_hex"], raw.hex())
            self.assertEqual(len(record["slot_raw_hex"]), 48)
        # Audit CSV line mapping agrees with physical CSV rows.
        by_number = {r["csv_record_number"]: r for r in audit["records"]}
        self.assertEqual(by_number[1]["physical_slot"], 3)
        self.assertEqual(by_number[4]["physical_slot"], 1)
        # Canonical anchor evidence: seq=0 anchored at ...:02. The offset is
        # relative to the segment's expanded-tick origin (2**32-2 here), so
        # verify the invariant epoch = expanded_tick + offset instead of a
        # hard-coded small number.
        canonical = by_number[3]["canonical_anchor"]
        self.assertEqual(canonical["observed_at_utc"], "2026-01-01T00:00:02Z")
        anchor_epoch = int(datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc).timestamp())
        self.assertEqual(by_number[3]["expanded_tick"] + canonical["derived_offset_seconds"],
                         anchor_epoch)
        self.assertEqual(audit["device_binding"], {"device_id": DEVICE, "sensor_id": SENSOR})
        self.assertEqual(audit["headers"]["selected_header"], "header_A")

    def test_output_directory_contains_exactly_two_products(self):
        self.transcribe_output()
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()),
                         ["readings.csv", "transcription.json"])
        raw = (self.out_dir / "readings.csv").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))  # no BOM

    def test_stale_corrupt_slot_never_scanned(self):
        # The transcription succeeds even though slot 2 is deliberately bad.
        model = self.transcribe_output()
        self.assertEqual(len(model["records"]), 4)


class AnchorContractTests(TranscriptionHarness):
    def _anchors(self, anchors, **overrides):
        doc = {
            "schema_version": overrides.get("schema_version", 1),
            "device_id": overrides.get("device_id", DEVICE),
            "sensor_id": overrides.get("sensor_id", SENSOR),
            "anchors": anchors,
        }
        return json.dumps(doc).encode("utf-8")

    def _reject(self, raw):
        with self.assertRaises(FlashError):
            transcribe_capture(CapturedInputs(self.image_path.read_bytes(), raw))

    def test_duplicate_json_keys_rejected_at_every_level(self):
        raw = (
            b'{"schema_version":1,"device_id":"DEV_1","sensor_id":"SENSOR-A",'
            b'"anchors":[],"anchors":[]}'
        )
        with self.assertRaisesRegex(FlashError, "duplicate JSON object key"):
            parse_anchor_bytes(raw)
        nested = (
            b'{"schema_version":1,"device_id":"DEV_1","sensor_id":"SENSOR-A",'
            b'"anchors":[{"boot_id":7,"seq":0,"observed_at":"2026-01-01T00:00:02Z",'
            b'"seq":1}]}'
        )
        with self.assertRaisesRegex(FlashError, "duplicate JSON object key"):
            parse_anchor_bytes(nested)

    def test_boolean_and_type_rules(self):
        self._reject(self._anchors([], schema_version=True))
        self._reject(self._anchors([], schema_version=2))
        bad_anchor = [{"boot_id": True, "seq": 0, "observed_at": "2026-01-01T00:00:02Z"}]
        self._reject(self._anchors(bad_anchor))
        bad_anchor = [{"boot_id": 7, "seq": -1, "observed_at": "2026-01-01T00:00:02Z"}]
        self._reject(self._anchors(bad_anchor))
        bad_anchor = [{"boot_id": 7, "seq": 2 ** 32, "observed_at": "2026-01-01T00:00:02Z"}]
        self._reject(self._anchors(bad_anchor))

    def test_sensor_and_device_identity_rules(self):
        self._reject(self._anchors(SCENARIO_1_ANCHORS, device_id="OTHER"))
        self._reject(self._anchors(SCENARIO_1_ANCHORS, sensor_id=" x"))
        self._reject(self._anchors(SCENARIO_1_ANCHORS, sensor_id="x "))

    def test_timezone_aware_whole_second_contract(self):
        bad = [{"boot_id": 7, "seq": 0, "observed_at": "2026-01-01T00:00:02"}]
        self._reject(self._anchors(bad))
        bad = [{"boot_id": 7, "seq": 0, "observed_at": "2026-01-01T00:00:02.500000Z"}]
        self._reject(self._anchors(bad))
        # An offset expression is accepted and normalized to UTC.
        good = [{"boot_id": 7, "seq": 0, "observed_at": "2026-01-01T02:00:02+02:00"}]
        _, audit_text, _ = transcribe_capture(
            CapturedInputs(self.image_path.read_bytes(), self._anchors(good))
        )
        anchor = json.loads(audit_text)["anchors"][0]
        self.assertEqual(anchor["observed_at_utc"], "2026-01-01T00:00:02Z")
        self.assertEqual(anchor["observed_at_raw"], "2026-01-01T02:00:02+02:00")

    def test_anchor_must_reference_active_record(self):
        # seq 99 was overwritten / never active.
        bad = [{"boot_id": 7, "seq": 99, "observed_at": "2026-01-01T00:00:02Z"}]
        self._reject(self._anchors(bad))
        # boot_id 8 has no active records.
        bad = [{"boot_id": 8, "seq": 0, "observed_at": "2026-01-01T00:00:02Z"}]
        self._reject(self._anchors(bad))

    def test_duplicate_reference_and_missing_anchor_rejected(self):
        doubled = SCENARIO_1_ANCHORS + [
            {"boot_id": 7, "seq": 0, "observed_at": "2026-01-01T00:00:03Z"}
        ]
        self._reject(self._anchors(doubled))
        self._reject(self._anchors([]))

    def test_consistent_multi_anchor_canonical_is_earliest_in_segment(self):
        anchors = [
            # Array order deliberately puts a later anchor first.
            {"boot_id": 7, "seq": 1, "observed_at": "2026-01-01T00:00:04Z"},
            {"boot_id": 7, "seq": 0, "observed_at": "2026-01-01T00:00:02Z"},
        ]
        _, audit_text, _ = transcribe_capture(
            CapturedInputs(self.image_path.read_bytes(), self._anchors(anchors))
        )
        audit = json.loads(audit_text)
        canonical = [a for a in audit["anchors"] if a["is_canonical"]]
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0]["seq"], 0)  # earliest logical position
        self.assertTrue(all(a["consistent_with_canonical"] for a in audit["anchors"]))

    def test_inconsistent_offsets_rejected_no_fitting(self):
        anchors = [
            {"boot_id": 7, "seq": 0, "observed_at": "2026-01-01T00:00:02Z"},
            {"boot_id": 7, "seq": 1, "observed_at": "2026-01-01T00:00:09Z"},
        ]
        with self.assertRaisesRegex(FlashError, "inconsistent clock offsets"):
            transcribe_capture(
                CapturedInputs(self.image_path.read_bytes(), self._anchors(anchors))
            )


class CountZeroTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_empty_ring_emits_header_only_csv(self):
        image = build_ring_image(
            capacity=3, records=[], next_slot=0, next_seq=5, device_id="EMPTY"
        )
        anchors = json.dumps({
            "schema_version": 1, "device_id": "EMPTY", "sensor_id": "S0", "anchors": []
        }).encode("utf-8")
        csv_text, audit_text, model = transcribe_capture(CapturedInputs(image, anchors))
        self.assertEqual(csv_text, "sensor_id,observed_at,temperature_c\r\n")
        audit = json.loads(audit_text)
        self.assertEqual(audit["records"], [])
        self.assertEqual(audit["ring"]["unused_physical_slots"], [0, 1, 2])
        self.assertEqual(audit["csv"]["data_row_count"], 0)

    def test_empty_ring_rejects_nonempty_anchors(self):
        image = build_ring_image(
            capacity=3, records=[], next_slot=0, next_seq=5, device_id="EMPTY"
        )
        anchors = json.dumps({
            "schema_version": 1, "device_id": "EMPTY", "sensor_id": "S0",
            "anchors": [{"boot_id": 1, "seq": 0, "observed_at": "2026-01-01T00:00:00Z"}],
        }).encode("utf-8")
        with self.assertRaisesRegex(FlashError, "empty anchors array"):
            transcribe_capture(CapturedInputs(image, anchors))


class DeterminismAndCaptureTests(TranscriptionHarness):
    def test_identical_bytes_produce_identical_products_in_two_dirs(self):
        first = self.dir / "a"
        second = self.dir / "b"
        transcribe(self.image_path, self.anchors_path, first)
        transcribe(self.image_path, self.anchors_path, second)
        for name in ("readings.csv", "transcription.json"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

    def test_anchor_reordering_keeps_csv_and_canonical_but_changes_hash(self):
        anchors = [
            {"boot_id": 7, "seq": 1, "observed_at": "2026-01-01T00:00:04Z"},
            {"boot_id": 7, "seq": 0, "observed_at": "2026-01-01T00:00:02Z"},
        ]
        reordered = [anchors[1], anchors[0]]
        first = CapturedInputs(self.image_path.read_bytes(), self._anchors(anchors))
        second = CapturedInputs(self.image_path.read_bytes(), self._anchors(reordered))
        csv1, audit1, _ = transcribe_capture(first)
        csv2, audit2, _ = transcribe_capture(second)
        self.assertEqual(csv1, csv2)
        a1, a2 = json.loads(audit1), json.loads(audit2)
        self.assertEqual(
            [a["seq"] for a in a1["anchors"] if a["is_canonical"]],
            [a["seq"] for a in a2["anchors"] if a["is_canonical"]],
        )
        self.assertNotEqual(a1["inputs_sha256"]["anchors"], a2["inputs_sha256"]["anchors"])
        self.assertEqual(a1["inputs_sha256"]["image"], a2["inputs_sha256"]["image"])

    def _anchors(self, anchors, **overrides):
        doc = {
            "schema_version": 1,
            "device_id": overrides.get("device_id", DEVICE),
            "sensor_id": overrides.get("sensor_id", SENSOR),
            "anchors": anchors,
        }
        return json.dumps(doc).encode("utf-8")

    def test_capture_is_single_and_swapped_file_is_not_mixed(self):
        captured = self.capture()
        original_image_hash = json.loads(
            transcribe_capture(captured)[1]
        )["inputs_sha256"]["image"]
        # After the single capture, replace the path's contents entirely.
        other_image = build_ring_image(
            capacity=2,
            records=[{"seq": 0, "tick": 0, "boot_id": 1, "temperature_centidegrees": 100}],
            next_slot=1, next_seq=1, device_id=DEVICE,
        )
        self.image_path.write_bytes(other_image)
        _, audit_text, _ = transcribe_capture(captured)
        audit = json.loads(audit_text)
        # The audit still describes the captured five-slot image, not the new file.
        self.assertEqual(audit["inputs_sha256"]["image"], original_image_hash)
        self.assertEqual(audit["ring"]["capacity"], 5)


class PublishSafetyTests(TranscriptionHarness):
    def test_existing_dir_symlink_and_rerun_behavior(self):
        self.out_dir.mkdir()
        with self.assertRaisesRegex(FlashError, "already exists"):
            self.transcribe_output()
        shutil.rmtree(self.out_dir)
        link_target = self.dir / "link_target"
        link_target.mkdir()
        link = self.out_dir
        link.symlink_to(link_target)
        with self.assertRaisesRegex(FlashError, "symbolic link"):
            self.transcribe_output()
        link.unlink()
        # A failed run leaves no formal directory and the same target reruns fine.
        self.transcribe_output()
        with self.assertRaisesRegex(FlashError, "already exists"):
            self.transcribe_output()

    def test_inputs_unchanged_after_failure(self):
        image_before = self.image_path.read_bytes()
        anchors_before = self.anchors_path.read_bytes()
        bad = bytearray(image_before)
        # Corrupt an *active* slot (slot 3 is the first active slot).
        bad[128 + 24 * 3 + 20] ^= 0xFF
        self.image_path.write_bytes(bytes(bad))
        with self.assertRaises(FlashError):
            self.transcribe_output()
        self.assertFalse(self.out_dir.exists())
        self.assertEqual(self.image_path.read_bytes(), bytes(bad))
        self.assertEqual(self.anchors_path.read_bytes(), anchors_before)
        # Restore and rerun the same target successfully.
        self.image_path.write_bytes(image_before)
        self.transcribe_output()
        self.assertTrue((self.out_dir / "readings.csv").exists())

    def test_second_product_failure_cleans_staging_and_publishes_nothing(self):
        csv_text, audit_text, _ = transcribe_capture(self.capture())
        seen = []

        def hook(event, context):
            seen.append(event)
            if event == "second_product_written":
                raise OSError("simulated second-file failure")

        with self.assertRaises(OSError):
            publish(self.out_dir, csv_text, audit_text, on_event=hook)
        self.assertFalse(self.out_dir.exists())
        # No staging directory is left behind.
        leftovers = [p for p in self.dir.iterdir() if p.name.startswith(".out.staging.")]
        self.assertEqual(leftovers, [])
        # Same target is reusable after the failure.
        publish(self.out_dir, csv_text, audit_text)
        self.assertTrue((self.out_dir / "transcription.json").exists())

    def test_publish_failure_cleans_staging(self):
        csv_text, audit_text, _ = transcribe_capture(self.capture())

        def hook(event, context):
            if event == "before_publish":
                raise OSError("simulated rename failure")

        with self.assertRaises(OSError):
            publish(self.out_dir, csv_text, audit_text, on_event=hook)
        self.assertFalse(self.out_dir.exists())
        leftovers = [p for p in self.dir.iterdir() if p.name.startswith(".out.staging.")]
        self.assertEqual(leftovers, [])


class FileLengthTests(TranscriptionHarness):
    def test_truncated_and_trailing_byte_images_rejected(self):
        image = self.image_path.read_bytes()
        truncated = CapturedInputs(image[:-1], self.anchors_path.read_bytes())
        with self.assertRaisesRegex(FlashError, "image length"):
            transcribe_capture(truncated)
        extended = CapturedInputs(image + b"\x00", self.anchors_path.read_bytes())
        with self.assertRaisesRegex(FlashError, "image length"):
            transcribe_capture(extended)


if __name__ == "__main__":
    unittest.main()
