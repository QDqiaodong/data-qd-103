"""Two-boot segments, reversed wall-clock order, generation wrap and rejections."""
import json
import tempfile
import unittest
from pathlib import Path

import csv
import io

from cold_exposure.flash.builder import build_ring_image
from cold_exposure.flash.image import HALF32, FlashError, pack_header
from cold_exposure.flash.transcribe import CapturedInputs, transcribe_capture

DEVICE = "DEV_2"
SENSOR = "SENSOR-B"

# Logical order: boot 10 (three records, tick wrap) then boot 20 (three records).
BOOT_10 = [
    {"seq": 0, "tick": 4294967290, "boot_id": 10, "temperature_centidegrees": 400},
    {"seq": 1, "tick": 4294967295, "boot_id": 10, "temperature_centidegrees": 410},
    {"seq": 2, "tick": 1, "boot_id": 10, "temperature_centidegrees": 420},
]
BOOT_20 = [
    {"seq": 3, "tick": 100, "boot_id": 20, "temperature_centidegrees": 600},
    {"seq": 4, "tick": 105, "boot_id": 20, "temperature_centidegrees": 610},
    {"seq": 5, "tick": 107, "boot_id": 20, "temperature_centidegrees": 620},
]
RECORDS = BOOT_10 + BOOT_20

# Boot 20's wall clock is a day EARLIER even though its slots come later in
# the physical/logical ring.
ANCHORS = [
    {"boot_id": 10, "seq": 0, "observed_at": "2026-01-02T00:00:00Z"},
    {"boot_id": 10, "seq": 2, "observed_at": "2026-01-02T00:00:07Z"},  # consistent second anchor
    {"boot_id": 20, "seq": 4, "observed_at": "2026-01-01T00:00:00Z"},  # middle-of-segment anchor
]


def build_image(records=RECORDS, *, generation=1, alternate_header=None, capacity=8,
                next_slot=6, next_seq=6):
    return build_ring_image(
        capacity=capacity,
        records=records,
        next_slot=next_slot,
        next_seq=next_seq,
        device_id=DEVICE,
        generation=generation,
        alternate_header=alternate_header,
    )


def anchor_bytes(anchors, device=DEVICE):
    return json.dumps({
        "schema_version": 1,
        "device_id": device,
        "sensor_id": SENSOR,
        "anchors": anchors,
    }).encode("utf-8")


class MultiBootOrderingTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def transcribe(self, image, anchors):
        return transcribe_capture(CapturedInputs(image, anchor_bytes(anchors)))

    def test_wall_clock_sort_reverses_physical_order_with_full_mapping(self):
        csv_text, audit_text, model = self.transcribe(build_image(), ANCHORS)
        rows = read_csv_rows_from_text(csv_text)
        times = [row[1] for row in rows[1:]]
        # Boot 20 (logical slots 3..5) sorts ahead of boot 10 (logical 0..2).
        self.assertEqual(times, [
            "2025-12-31T23:59:55Z",  # boot20 seq3, tick 100 vs anchor tick 105
            "2026-01-01T00:00:00Z",  # boot20 seq4, the anchored record
            "2026-01-01T00:00:02Z",  # boot20 seq5
            "2026-01-02T00:00:00Z",  # boot10 seq0
            "2026-01-02T00:00:05Z",  # boot10 seq1 (4294967295 - 4294967290 = 5)
            "2026-01-02T00:00:07Z",  # boot10 seq2 (wrapped tick, expanded +7)
        ])
        audit = json.loads(audit_text)
        # Logical/physical mapping is preserved regardless of CSV sorting.
        by_logical = {entry["logical_index"]: entry for entry in audit["records"]}
        self.assertEqual(by_logical[0]["physical_slot"], 0)
        self.assertEqual(by_logical[5]["physical_slot"], 5)
        # CSV record numbers follow UTC order, not physical order.
        self.assertEqual(by_logical[3]["csv_record_number"], 1)
        self.assertEqual(by_logical[5]["csv_record_number"], 3)
        self.assertEqual(by_logical[0]["csv_record_number"], 4)
        self.assertEqual(by_logical[2]["csv_record_number"], 6)
        # Boot segments are reported independently.
        self.assertEqual([segment["boot_id"] for segment in audit["boot_segments"]], [10, 20])
        # Tick expansion across the 32-bit wrap.
        self.assertEqual(by_logical[2]["expanded_tick"], 4294967297)

    def test_two_anchors_imply_one_offset_and_canonical_is_earliest(self):
        _, audit_text, _ = self.transcribe(build_image(), ANCHORS)
        audit = json.loads(audit_text)
        boot_10 = [anchor for anchor in audit["anchors"] if anchor["boot_id"] == 10]
        self.assertEqual(len(boot_10), 2)
        offsets = {anchor["derived_offset_seconds"] for anchor in boot_10}
        self.assertEqual(len(offsets), 1)  # equivalent UTC offset
        canonical = [anchor for anchor in boot_10 if anchor["is_canonical"]]
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0]["seq"], 0)  # earliest in segment, not JSON order
        self.assertTrue(all(anchor["consistent_with_canonical"] for anchor in boot_10))
        # The middle anchor of boot 20 is its canonical one.
        boot_20 = [anchor for anchor in audit["anchors"] if anchor["boot_id"] == 20]
        self.assertEqual(len(boot_20), 1)
        self.assertTrue(boot_20[0]["is_canonical"])
        self.assertEqual(boot_20[0]["seq"], 4)

    def test_generation_wrap_selects_newer_header_b(self):
        # Primary header A is at generation 2**32-1; copy B at generation 0,
        # one modular step newer, otherwise describing the same image.
        alternate = pack_header(0, 8, 6, 6, 6, DEVICE)
        image = build_image(generation=4294967295, alternate_header=alternate)
        _, audit_text, _ = self.transcribe(image, ANCHORS)
        audit = json.loads(audit_text)
        self.assertEqual(audit["headers"]["selected_header"], "header_B")
        self.assertEqual(audit["headers"]["selected_generation"], 0)
        self.assertEqual(audit["headers"]["copies"][0]["valid"], True)
        self.assertEqual(audit["headers"]["copies"][1]["valid"], True)


class MultiBootRejectionTests(unittest.TestCase):
    def setUp(self):
        self.image = build_image()

    def _reject(self, anchors, image=None):
        with self.assertRaises(FlashError):
            transcribe_capture(CapturedInputs(image or self.image, anchor_bytes(anchors)))

    def test_conflicting_anchor_offsets(self):
        bad = [
            {"boot_id": 10, "seq": 0, "observed_at": "2026-01-02T00:00:00Z"},
            {"boot_id": 10, "seq": 2, "observed_at": "2026-01-02T00:00:08Z"},  # one second off
            {"boot_id": 20, "seq": 4, "observed_at": "2026-01-01T00:00:00Z"},
        ]
        self._reject(bad)

    def test_missing_anchor_for_one_boot(self):
        self._reject([ANCHORS[0], ANCHORS[1]])  # no boot 20 anchor

    def test_anchor_referencing_overwritten_seq(self):
        bad = ANCHORS + [
            {"boot_id": 20, "seq": 6, "observed_at": "2026-01-01T00:00:00Z"}
        ]
        self._reject(bad)

    def test_duplicate_anchor_reference(self):
        bad = ANCHORS + [
            {"boot_id": 20, "seq": 4, "observed_at": "2026-01-01T00:00:00Z"}
        ]
        self._reject(bad)

    def test_boot_reuse_pattern_rejected(self):
        reused = [
            {"seq": 0, "tick": 0, "boot_id": 10, "temperature_centidegrees": 400},
            {"seq": 1, "tick": 1, "boot_id": 20, "temperature_centidegrees": 410},
            {"seq": 2, "tick": 2, "boot_id": 10, "temperature_centidegrees": 420},
        ]
        image = build_image(reused, next_seq=3, next_slot=3, capacity=4)
        anchors = [
            {"boot_id": 10, "seq": 0, "observed_at": "2026-01-01T00:00:00Z"},
            {"boot_id": 20, "seq": 1, "observed_at": "2026-01-02T00:00:00Z"},
        ]
        self._reject(anchors, image=image)

    def test_cross_boot_same_utc_second_rejected(self):
        # Shift boot 20 so its anchored record lands exactly on boot 10's first
        # record instant (2026-01-02T00:00:00Z): tick 105 anchored there.
        bad = [
            {"boot_id": 10, "seq": 0, "observed_at": "2026-01-02T00:00:00Z"},
            {"boot_id": 20, "seq": 4, "observed_at": "2026-01-02T00:00:00Z"},
        ]
        self._reject(bad)

    def test_half_turn_tick_ambiguity_rejected(self):
        ambiguous = BOOT_10 + [
            {"seq": 3, "tick": 0, "boot_id": 20, "temperature_centidegrees": 600},
            {"seq": 4, "tick": HALF32, "boot_id": 20, "temperature_centidegrees": 610},
            {"seq": 5, "tick": HALF32 + 1, "boot_id": 20, "temperature_centidegrees": 620},
        ]
        image = build_image(ambiguous)
        with self.assertRaisesRegex(FlashError, "half-turn"):
            transcribe_capture(CapturedInputs(image, anchor_bytes(ANCHORS)))

    def test_anchor_for_unknown_boot_rejected(self):
        bad = ANCHORS + [
            {"boot_id": 99, "seq": 0, "observed_at": "2026-01-01T00:00:00Z"}
        ]
        self._reject(bad)

    def test_derived_utc_outside_datetime_range_rejected(self):
        # A huge tick expansion plus a modern anchor pushes derived epochs
        # beyond datetime.max (year 9999); this must fail rather than wrap.
        huge = [
            {"seq": 0, "tick": 0, "boot_id": 1, "temperature_centidegrees": 400},
            {"seq": 1, "tick": 100, "boot_id": 1, "temperature_centidegrees": 410},
        ]
        image = build_image(huge, next_seq=2, next_slot=2, capacity=4)
        anchors = [
            # 60 seconds before datetime.max; the next record is +100 s, so it
            # overflows the representable UTC range.
            {"boot_id": 1, "seq": 0, "observed_at": "9999-12-31T23:59:00Z"}
        ]
        with self.assertRaisesRegex(FlashError, "datetime range"):
            transcribe_capture(CapturedInputs(image, anchor_bytes(anchors)))


def read_csv_rows_from_text(text):
    import csv
    import io
    return list(csv.reader(io.StringIO(text)))


if __name__ == "__main__":
    unittest.main()
