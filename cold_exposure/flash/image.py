"""Binary layout, header selection, ring ordering and tick expansion.

The synthetic ``CCLOG01`` image is exactly
``128 + capacity * 24`` bytes, all integers little-endian. Two 64-byte header
copies live at offsets 0 and 64, followed by ``capacity`` fixed 24-byte slots;
slot ``i`` starts at absolute offset ``128 + 24 * i``.

Only the Python standard library is used. Temperature integers are converted
to decimal text without ever going through :class:`float`.
"""
import struct
import zlib
from decimal import Decimal

MAGIC = b"CCLOG01\n"
HEADER_SIZE = 64
HEADER_REGION_SIZE = 128
SLOT_SIZE = 24
HEADER_COPY_OFFSETS = (0, HEADER_SIZE)
MOD32 = 1 << 32
HALF32 = 1 << 31
MAX_TICK_DELTA = HALF32 - 1
MIN_TEMPERATURE_CENTIDEGREES = -10000
MAX_TEMPERATURE_CENTIDEGREES = 15000
MIN_CAPACITY = 1
MAX_CAPACITY = 1_000_000


class FlashError(ValueError):
    """A locatable protocol or anchor-contract violation."""


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
def _decode_device_id(raw):
    first_nul = raw.find(b"\x00")
    if first_nul < 0:
        first_nul = len(raw)
    else:
        if any(byte != 0 for byte in raw[first_nul:]):
            raise FlashError("device_id: bytes after the first NUL must all be NUL")
    text = raw[:first_nul]
    if not text:
        raise FlashError("device_id: must contain at least one character")
    for byte in text:
        if not (
            0x30 <= byte <= 0x39  # 0-9
            or 0x41 <= byte <= 0x5A  # A-Z
            or 0x61 <= byte <= 0x7A  # a-z
            or byte in (0x5F, 0x2D)  # _ -
        ):
            raise FlashError(
                "device_id: only ASCII letters, digits, '_' and '-' are allowed"
            )
    return text.decode("ascii")


def parse_header(raw, copy_name):
    """Validate one 64-byte header copy.

    Returns ``(fields, reason)`` where ``fields`` is None for an invalid copy
    and ``reason`` explains why; for a valid copy ``fields`` is a mapping and
    ``reason`` is None. Validity is judged solely from the header bytes, never
    from slot contents.
    """
    if len(raw) != HEADER_SIZE:
        return None, f"{copy_name}: header copy is not {HEADER_SIZE} bytes"
    if raw[0:8] != MAGIC:
        return None, f"{copy_name}: magic is not {MAGIC!r}"
    (
        generation,
        capacity,
        count,
        next_slot,
        next_seq,
    ) = struct.unpack_from("<IIIII", raw, 8)
    try:
        device_id = _decode_device_id(raw[28:44])
    except FlashError as exc:
        return None, f"{copy_name}: {exc}"
    if any(byte != 0 for byte in raw[44:60]):
        return None, f"{copy_name}: reserved bytes [44,60) must be zero"
    stored_crc = struct.unpack_from("<I", raw, 60)[0]
    actual_crc = zlib.crc32(raw[0:60]) & 0xFFFFFFFF
    if stored_crc != actual_crc:
        return (
            None,
            f"{copy_name}: CRC32 mismatch (stored 0x{stored_crc:08x}, "
            f"computed 0x{actual_crc:08x})",
        )
    if not MIN_CAPACITY <= capacity <= MAX_CAPACITY:
        return None, f"{copy_name}: capacity {capacity} outside [1, 1000000]"
    if count > capacity:
        return None, f"{copy_name}: count {count} exceeds capacity {capacity}"
    if next_slot >= capacity:
        return (
            None,
            f"{copy_name}: next_slot {next_slot} is not below capacity {capacity}",
        )
    fields = {
        "copy": copy_name,
        "generation": generation,
        "capacity": capacity,
        "count": count,
        "next_slot": next_slot,
        "next_seq": next_seq,
        "device_id": device_id,
        "crc32": stored_crc,
    }
    return fields, None


def evaluate_headers(image):
    """Independently judge both header copies from the captured image bytes."""
    copies = []
    for index, offset in enumerate(HEADER_COPY_OFFSETS):
        raw = image[offset : offset + HEADER_SIZE]
        fields, reason = parse_header(raw, f"header_{chr(65 + index)}")
        copies.append(
            {
                "name": f"header_{chr(65 + index)}",
                "offset": offset,
                "valid": fields is not None,
                "reason": reason,
                "fields": fields,
                "raw_hex": raw.hex(),
            }
        )
    return copies


def select_header(copies):
    """Choose the authoritative header per the generation ordering rule."""
    valid = [copy for copy in copies if copy["valid"]]
    if not valid:
        reasons = "; ".join(
            f"{copy['name']} invalid: {copy['reason']}" for copy in copies
        )
        raise FlashError(f"no valid header copy: {reasons}")
    if len(valid) == 1:
        chosen = valid[0]
        other = next(copy for copy in copies if not copy["valid"])
        return chosen, (
            f"only {chosen['name']} is valid; {other['name']} rejected because "
            f"{other['reason']}"
        )
    copy_a, copy_b = copies[0], copies[1]
    fields_a, fields_b = copy_a["fields"], copy_b["fields"]
    delta = (fields_b["generation"] - fields_a["generation"]) % MOD32
    if delta == 0:
        prefix_a = bytes.fromhex(copy_a["raw_hex"])[:60]
        prefix_b = bytes.fromhex(copy_b["raw_hex"])[:60]
        if prefix_a == prefix_b:
            return copy_a, "identical generation and identical first 60 bytes; canonical choice is header_A"
        raise FlashError(
            "same-generation header conflict: both copies are valid with "
            "generation "
            f"{fields_a['generation']} but their first 60 bytes differ"
        )
    if delta == HALF32:
        raise FlashError(
            "generation difference is exactly 2**31; the two valid headers cannot be ordered"
        )
    if 0 < delta < HALF32:
        chosen = copy_b
    else:  # HALF32 < delta < MOD32 means B is older after modular wrap
        chosen = copy_a
    return (
        chosen,
        f"modular generation distance d=(B-A) mod 2**32={delta}; {chosen['name']} "
        "is the newer copy",
    )


def expected_image_size(capacity):
    return HEADER_REGION_SIZE + capacity * SLOT_SIZE


# ---------------------------------------------------------------------------
# Slots and ring ordering
# ---------------------------------------------------------------------------
def logical_positions(capacity, count, next_slot, next_seq):
    """Return ``(slot, expected_seq)`` pairs in logical sample order.

    For ``j = 0 .. count-1`` the active slot is
    ``(next_slot - count + j) mod capacity`` and its expected 32-bit sequence
    number is ``(next_seq - count + j) mod 2**32``.
    """
    return [
        (
            (next_slot - count + j) % capacity,
            (next_seq - count + j) % MOD32,
        )
        for j in range(count)
    ]


def slot_offset(slot):
    return HEADER_REGION_SIZE + slot * SLOT_SIZE


def parse_slot(raw, slot, expected_seq):
    """Validate one active 24-byte slot and return its business fields."""
    offset = slot_offset(slot)
    if len(raw) != SLOT_SIZE:
        raise FlashError(
            f"slot {slot} at absolute offset {offset}: truncated slot bytes"
        )
    seq, tick, boot_id, temperature = struct.unpack_from("<IIIh", raw, 0)
    if any(byte != 0 for byte in raw[14:20]):
        raise FlashError(
            f"slot {slot} at absolute offset {offset}: reserved bytes [14,20) must be zero"
        )
    stored_crc = struct.unpack_from("<I", raw, 20)[0]
    actual_crc = zlib.crc32(raw[0:20]) & 0xFFFFFFFF
    if stored_crc != actual_crc:
        raise FlashError(
            f"slot {slot} at absolute offset {offset}: CRC32 mismatch "
            f"(stored 0x{stored_crc:08x}, computed 0x{actual_crc:08x})"
        )
    if seq != expected_seq:
        raise FlashError(
            f"slot {slot} at absolute offset {offset}: seq {seq} does not match "
            f"expected {expected_seq}; reordering or skipping gaps is not allowed"
        )
    if not MIN_TEMPERATURE_CENTIDEGREES <= temperature <= MAX_TEMPERATURE_CENTIDEGREES:
        raise FlashError(
            f"slot {slot} at absolute offset {offset}: temperature_centidegrees "
            f"{temperature} outside [-10000, 15000]"
        )
    return {
        "slot": slot,
        "offset": offset,
        "seq": seq,
        "tick": tick,
        "boot_id": boot_id,
        "temperature_centidegrees": temperature,
        "crc32": stored_crc,
        "raw_hex": raw.hex(),
    }


def build_segments(records):
    """Split the active records into contiguous same-boot segments.

    A boot id may only appear in one contiguous run; an A,B,A reuse pattern is
    rejected. Returns lists of record indices per segment.
    """
    segments = []
    seen_boots = set()
    current_boot = None
    for index, record in enumerate(records):
        boot_id = record["boot_id"]
        if boot_id != current_boot:
            if boot_id in seen_boots:
                raise FlashError(
                    f"slot {record['slot']} at absolute offset {record['offset']}: "
                    f"boot_id {boot_id} is reused in a non-contiguous segment "
                    "(A,B,A pattern); one boot must form one contiguous run"
                )
            segments.append([])
            current_boot = boot_id
            seen_boots.add(boot_id)
        segments[-1].append(index)
    return segments


def expand_segment_ticks(records, indices):
    """Expand wrapping tick counters into a monotonic per-boot tick axis.

    Adjacent deltas are taken modulo 2**32 and must lie in
    ``[1, 2**31-1]``; zero differences and half-turn-or-larger jumps cannot be
    interpreted safely. The first raw tick of each boot segment is the origin.
    """
    first = records[indices[0]]
    first["expanded_tick"] = first["tick"]
    for left_index, right_index in zip(indices, indices[1:]):
        left, right = records[left_index], records[right_index]
        delta = (right["tick"] - left["tick"]) % MOD32
        if delta == 0:
            raise FlashError(
                f"slot {right['slot']} at absolute offset {right['offset']}: "
                "zero tick delta inside one boot segment"
            )
        if delta > MAX_TICK_DELTA:
            raise FlashError(
                f"slot {right['slot']} at absolute offset {right['offset']}: tick "
                f"delta {delta} is at least the 2**31 half-turn; the elapsed time "
                "cannot be determined safely"
            )
        right["expanded_tick"] = left["expanded_tick"] + delta


def format_temperature(centidegrees):
    """Render hundredths of a degree Celsius as fixed two-place decimal text."""
    sign = "-" if centidegrees < 0 else ""
    value = abs(centidegrees)
    return f"{sign}{value // 100}.{value % 100:02d}"


def decimal_temperature(centidegrees):
    """Exact :class:`decimal.Decimal` without passing through float."""
    return Decimal(centidegrees) / Decimal(100)


# ---------------------------------------------------------------------------
# Builders (used by tests and by the reproducible example-image script)
# ---------------------------------------------------------------------------
def pack_slot(seq, tick, boot_id, temperature_centidegrees, *, reserved=b"\x00" * 6):
    raw = struct.pack(
        "<IIIh",
        seq & 0xFFFFFFFF,
        tick & 0xFFFFFFFF,
        boot_id & 0xFFFFFFFF,
        temperature_centidegrees,
    ) + reserved
    if len(raw) != 20:
        raise FlashError("slot reserved region must make the prefix exactly 20 bytes")
    raw += struct.pack("<I", zlib.crc32(raw) & 0xFFFFFFFF)
    return raw


def pack_raw_slot(seq, tick, boot_id, raw_temperature_int16, *, reserved=b"\x00" * 6):
    """Pack arbitrary int16 temperature bits (test fixture helper).

    Unlike :func:`pack_slot`, no range check is applied and the CRC is still
    computed correctly; callers can corrupt it afterwards to build stale data.
    """
    raw = struct.pack(
        "<IIIh",
        seq & 0xFFFFFFFF,
        tick & 0xFFFFFFFF,
        boot_id & 0xFFFFFFFF,
        raw_temperature_int16,
    ) + reserved
    if len(raw) != 20:
        raise FlashError("slot reserved region must make the prefix exactly 20 bytes")
    raw += struct.pack("<I", zlib.crc32(raw) & 0xFFFFFFFF)
    return raw


def pack_header(generation, capacity, count, next_slot, next_seq, device_id):
    text = device_id.encode("ascii")
    if not 1 <= len(text) <= 16:
        raise FlashError("device_id must be 1..16 ASCII characters")
    device_field = text + b"\x00" * (16 - len(text))
    prefix = MAGIC + struct.pack(
        "<IIIII",
        generation & 0xFFFFFFFF,
        capacity,
        count,
        next_slot,
        next_seq & 0xFFFFFFFF,
    )
    prefix += device_field + b"\x00" * 16
    if len(prefix) != 60:  # pragma: no cover - struct layout guard
        raise AssertionError("header prefix must be 60 bytes")
    return prefix + struct.pack("<I", zlib.crc32(prefix) & 0xFFFFFFFF)


def assemble_image(header, slots, *, alternate_header=None):
    """Build a complete image from a header mapping and a full slot array.

    ``slots`` must contain exactly ``capacity`` 24-byte entries indexed by
    physical slot. ``alternate_header`` overrides the copy at offset 64, which
    lets tests exercise header-selection failures deterministically.
    """
    capacity = header["capacity"]
    if len(slots) != capacity:
        raise FlashError("assemble_image requires exactly capacity slots")
    primary = pack_header(
        header["generation"],
        capacity,
        header["count"],
        header["next_slot"],
        header["next_seq"],
        header["device_id"],
    )
    if alternate_header is None:
        alternate = primary
    else:
        alternate = alternate_header
    image = primary + alternate + b"".join(slots)
    expected = expected_image_size(capacity)
    if len(image) != expected:  # pragma: no cover - layout guard
        raise AssertionError(f"image must be {expected} bytes, got {len(image)}")
    return image
