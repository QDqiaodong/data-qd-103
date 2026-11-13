"""Binary-level CCLOG01 protocol tests: headers, ring order, ticks, selection.

The ring-order oracle below is written independently from the production
``logical_positions`` helper; expected slots and sequences are derived with a
plain hand-maintained pointer walk, never by calling the production selector.
"""
import struct
import unittest
import zlib

from cold_exposure.flash.image import (
    HALF32,
    HEADER_REGION_SIZE,
    MOD32,
    SLOT_SIZE,
    FlashError,
    evaluate_headers,
    expand_segment_ticks,
    format_temperature,
    logical_positions,
    pack_header,
    pack_raw_slot,
    pack_slot,
    parse_header,
    parse_slot,
    select_header,
    slot_offset,
)
from tests._flash_helpers import corrupt_byte


def reference_ring(capacity, count, next_slot, next_seq):
    """Independent oracle: walk the write pointer backwards count times."""
    slot = next_slot
    walked = []
    for _ in range(count):
        slot = slot - 1
        if slot < 0:
            slot = capacity - 1
        walked.append(slot)
    slots = list(reversed(walked))
    # Independent sequence oracle: plain unsigned subtraction modulo 2**32.
    seqs = [((next_seq - count) + j) % MOD32 for j in range(count)]
    return list(zip(slots, seqs))


class CrcVectorTests(unittest.TestCase):
    def test_public_crc32_check_vector(self):
        # The canonical zlib/CRC-32 check value for ASCII "123456789".
        self.assertEqual(zlib.crc32(b"123456789") & 0xFFFFFFFF, 0xCBF43926)

    def test_packed_header_and_slot_match_zlib(self):
        header = pack_header(7, 5, 4, 2, 2, "DEV_1")
        self.assertEqual(
            struct.unpack_from("<I", header, 60)[0],
            zlib.crc32(header[:60]) & 0xFFFFFFFF,
        )
        slot = pack_slot(1, 2, 3, 400)
        self.assertEqual(len(slot), SLOT_SIZE)
        self.assertEqual(
            struct.unpack_from("<I", slot, 20)[0],
            zlib.crc32(slot[:20]) & 0xFFFFFFFF,
        )


class RingOracleTests(unittest.TestCase):
    def test_ring_order_matches_independent_oracle(self):
        cases = [
            (5, 4, 2, 2),
            (1, 1, 0, 0),
            (1, 0, 0, 12345),
            (7, 7, 0, 0),
            (13, 9, 4, 4294967290),
            (100, 50, 99, 2 ** 31),
            (1000, 1, 0, 2 ** 32 - 1),
            (10, 10, 3, 7),
        ]
        for capacity, count, next_slot, next_seq in cases:
            with self.subTest(case=(capacity, count, next_slot, next_seq)):
                self.assertEqual(
                    logical_positions(capacity, count, next_slot, next_seq),
                    reference_ring(capacity, count, next_slot, next_seq),
                )

    def test_spec_example_slot_order_and_wrapped_sequences(self):
        # Required scenario 1: capacity=5 count=4 next_slot=2 next_seq=2.
        self.assertEqual(
            logical_positions(5, 4, 2, 2),
            [(3, 4294967294), (4, 4294967295), (0, 0), (1, 1)],
        )


class HeaderValidationTests(unittest.TestCase):
    def _header(self, **overrides):
        values = dict(
            generation=1, capacity=5, count=4, next_slot=2, next_seq=2, device_id="DEV_1"
        )
        values.update(overrides)
        return pack_header(**values)

    def test_valid_header_round_trip(self):
        raw = self._header()
        fields, reason = parse_header(raw, "header_A")
        self.assertIsNone(reason)
        self.assertEqual(fields["device_id"], "DEV_1")
        self.assertEqual((fields["generation"], fields["capacity"], fields["count"]), (1, 5, 4))

    def test_magic_crc_reserved_and_field_failures(self):
        bad_magic = bytearray(self._header())
        bad_magic[0] = ord("X")
        self.assertFalse(parse_header(bytes(bad_magic), "header_A")[0])
        bad_crc = corrupt_byte(self._header(), 60)
        fields, reason = parse_header(bad_crc, "header_A")
        self.assertIsNone(fields)
        self.assertIn("CRC32", reason)
        bad_reserved = corrupt_byte(self._header(), 50)
        self.assertIsNone(parse_header(bad_reserved, "header_A")[0])
        self.assertIsNone(parse_header(self._header(capacity=0), "header_A")[0])
        self.assertIsNone(parse_header(self._header(capacity=1_000_001), "header_A")[0])
        self.assertIsNone(parse_header(self._header(count=6), "header_A")[0])
        self.assertIsNone(parse_header(self._header(next_slot=5), "header_A")[0])

    def test_device_id_rules(self):
        self.assertIsNotNone(parse_header(self._header(device_id="a-Z_9"), "header_A")[0])
        with self.assertRaises(FlashError):
            pack_header(1, 5, 0, 0, 0, "")
        raw = bytearray(self._header())
        raw[28:44] = b"AB\x00CD" + b"\x00" * 11  # NUL followed by non-NUL
        raw[60:64] = struct.pack("<I", zlib.crc32(bytes(raw[:60])) & 0xFFFFFFFF)
        self.assertIsNone(parse_header(bytes(raw), "header_A")[0])
        raw2 = bytearray(self._header())
        raw2[28] = ord("$")
        raw2[60:64] = struct.pack("<I", zlib.crc32(bytes(raw2[:60])) & 0xFFFFFFFF)
        self.assertIsNone(parse_header(bytes(raw2), "header_A")[0])
        # Device identity is case sensitive: both parse, different decoded text.
        upper = parse_header(self._header(device_id="Sensor"), "header_A")[0]
        lower = parse_header(self._header(device_id="sensor"), "header_A")[0]
        self.assertNotEqual(upper["device_id"], lower["device_id"])


def image_with_headers(header_a, header_b, capacity=5):
    return header_a + header_b + b"\x00" * (capacity * SLOT_SIZE)


class HeaderSelectionTests(unittest.TestCase):
    def _header(self, generation, **overrides):
        values = dict(
            capacity=5, count=0, next_slot=0, next_seq=0, device_id="DEV_1"
        )
        values.update(overrides)
        return pack_header(generation=generation, **values)

    def _evaluate(self, header_a, header_b):
        return evaluate_headers(image_with_headers(header_a, header_b))

    def test_generation_wrap_picks_newer_zero_header(self):
        # generation 4294967295 vs 0: after modular wrap 0 is one step newer.
        copies = self._evaluate(self._header(4294967295), self._header(0))
        chosen, reason = select_header(copies)
        self.assertEqual(chosen["name"], "header_B")
        self.assertIn("newer", reason)

    def test_forward_generation_distance_picks_b(self):
        copies = self._evaluate(self._header(10), self._header(40))
        self.assertEqual(select_header(copies)[0]["name"], "header_B")

    def test_large_distance_means_b_is_older(self):
        copies = self._evaluate(self._header(10), self._header(5))
        chosen, _ = select_header(copies)
        self.assertEqual(chosen["name"], "header_A")

    def test_half_turn_generation_is_ambiguous(self):
        copies = self._evaluate(self._header(0), self._header(HALF32))
        with self.assertRaisesRegex(FlashError, "2\\*\\*31"):
            select_header(copies)

    def test_same_generation_identical_and_conflict(self):
        copies = self._evaluate(self._header(7), self._header(7))
        chosen, reason = select_header(copies)
        self.assertEqual(chosen["name"], "header_A")
        self.assertIn("canonical", reason)
        conflicting = self._header(7, next_seq=1)
        copies = self._evaluate(self._header(7), conflicting)
        with self.assertRaisesRegex(FlashError, "same-generation header conflict"):
            select_header(copies)

    def test_one_bad_header_falls_back_with_audit_reason(self):
        good = self._header(3)
        bad = corrupt_byte(good, 60)
        copies = self._evaluate(good, bad)
        chosen, reason = select_header(copies)
        self.assertEqual(chosen["name"], "header_A")
        self.assertIn("header_B rejected", reason)
        # And the reverse: A bad, B good.
        copies = self._evaluate(bad, good)
        chosen, reason = select_header(copies)
        self.assertEqual(chosen["name"], "header_B")
        self.assertIn("header_A rejected", reason)

    def test_both_headers_bad_fails(self):
        bad_a = corrupt_byte(self._header(1), 0)
        bad_b = corrupt_byte(self._header(2), 1)
        copies = self._evaluate(bad_a, bad_b)
        with self.assertRaisesRegex(FlashError, "no valid header copy"):
            select_header(copies)


class SlotValidationTests(unittest.TestCase):
    def test_valid_slot_and_offsets(self):
        raw = pack_slot(4294967295, 4294967290, 9, -125)
        record = parse_slot(raw, 7, 4294967295)
        self.assertEqual(record["offset"], slot_offset(7))
        self.assertEqual(record["offset"], HEADER_REGION_SIZE + 7 * SLOT_SIZE)
        self.assertEqual(record["temperature_centidegrees"], -125)

    def test_seq_mismatch_crc_reserved_and_temperature_bounds(self):
        raw = pack_slot(5, 1, 2, 400)
        with self.assertRaisesRegex(FlashError, "expected 6"):
            parse_slot(raw, 0, 6)
        with self.assertRaisesRegex(FlashError, "CRC32"):
            parse_slot(corrupt_byte(raw, 21), 0, 5)
        with self.assertRaisesRegex(FlashError, "reserved"):
            parse_slot(corrupt_byte(raw, 14), 0, 5)
        # Boundary temperatures are valid.
        for value in (-10000, 15000):
            parse_slot(pack_slot(5, 1, 2, value), 0, 5)
        # Out-of-range bit patterns rejected with slot and offset.
        too_low = pack_raw_slot(5, 1, 2, -10001)
        with self.assertRaisesRegex(FlashError, r"slot 3 at absolute offset 200"):
            parse_slot(too_low, 3, 5)
        too_high = pack_raw_slot(5, 1, 2, 15001)
        with self.assertRaisesRegex(FlashError, "temperature_centidegrees"):
            parse_slot(too_high, 3, 5)

    def test_big_endian_reinterpretation_breaks_validation(self):
        raw = pack_slot(1, 2, 3, 400)
        be = struct.pack(">IIIh", 1, 2, 3, 400) + raw[14:]
        # Reusing the little-endian CRC cannot make the big-endian bytes valid.
        with self.assertRaises(FlashError):
            parse_slot(be, 0, 1)

    def test_temperature_rendering_never_uses_float(self):
        cases = {
            -10000: "-100.00",
            -125: "-1.25",
            -5: "-0.05",
            0: "0.00",
            400: "4.00",
            15000: "150.00",
        }
        for value, text in cases.items():
            self.assertEqual(format_temperature(value), text)


class TickExpansionTests(unittest.TestCase):
    def _record(self, slot, tick, boot_id):
        return {"slot": slot, "offset": slot_offset(slot), "tick": tick, "boot_id": boot_id}

    def test_wrapping_deltas_expand_monotonically(self):
        records = [
            self._record(3, 4294967294, 7),
            self._record(4, 4294967295, 7),
            self._record(0, 0, 7),
            self._record(1, 2, 7),
        ]
        expand_segment_ticks(records, [0, 1, 2, 3])
        self.assertEqual([r["expanded_tick"] for r in records],
                         [4294967294, 4294967295, 4294967296, 4294967298])

    def test_zero_and_half_turn_deltas_rejected(self):
        records = [self._record(0, 10, 1), self._record(1, 10, 1)]
        with self.assertRaisesRegex(FlashError, "zero tick delta"):
            expand_segment_ticks(records, [0, 1])
        records = [self._record(0, 0, 1), self._record(1, HALF32, 1)]
        with self.assertRaisesRegex(FlashError, "half-turn"):
            expand_segment_ticks(records, [0, 1])
        records = [self._record(0, 0, 1), self._record(1, HALF32 + 5, 1)]
        with self.assertRaisesRegex(FlashError, "half-turn"):
            expand_segment_ticks(records, [0, 1])
        # The largest legal delta is accepted.
        records = [self._record(0, 0, 1), self._record(1, HALF32 - 1, 1)]
        expand_segment_ticks(records, [0, 1])
        self.assertEqual(records[1]["expanded_tick"], HALF32 - 1)

    def test_segments_restart_per_boot_and_reuse_is_rejected(self):
        from cold_exposure.flash.image import build_segments
        records = [
            self._record(0, 0, 1), self._record(1, 1, 1),
            self._record(2, 999, 2), self._record(3, 1000, 2),
        ]
        segments = build_segments(records)
        self.assertEqual(segments, [[0, 1], [2, 3]])
        for indices in segments:
            expand_segment_ticks(records, indices)
        # Second boot restarts its own axis at the segment's first raw tick.
        self.assertEqual(records[2]["expanded_tick"], 999)
        reused = [
            self._record(0, 0, 1), self._record(1, 1, 2), self._record(2, 2, 1)
        ]
        with self.assertRaisesRegex(FlashError, "non-contiguous"):
            build_segments(reused)


if __name__ == "__main__":
    unittest.main()
