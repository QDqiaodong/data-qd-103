"""Deterministic in-memory builders for synthetic CCLOG01 test images.

These helpers deliberately stay independent of the production ring-selection
code: callers place every slot themselves. They are used by the test suite and
by ``tools/build_flash_examples.py`` only; the production parser never imports
this module.
"""
from .image import (
    MOD32,
    assemble_image,
    expected_image_size,
    pack_header,
    pack_slot,
)

__all__ = [
    "MOD32",
    "assemble_image",
    "build_ring_image",
    "expected_image_size",
    "pack_header",
    "pack_slot",
]


def build_ring_image(
    *,
    capacity,
    records,
    next_slot,
    next_seq,
    device_id,
    generation=1,
    stale_slots=None,
    alternate_header=None,
):
    """Place logical ``records`` around a ring and return a complete image.

    ``records`` is a list of business tuples/dicts in logical order; each item
    carries ``seq, tick, boot_id, temperature_centidegrees``. Physical slot
    placement is recomputed from ``capacity``, the record count and
    ``next_slot`` using simple modular arithmetic here in the builder rather
    than the production selection function.
    """
    count = len(records)
    if count > capacity:
        raise ValueError("count cannot exceed capacity")
    slots = [b"\x00" * 24 for _ in range(capacity)]
    for logical_index, item in enumerate(records):
        slot = (next_slot - count + logical_index) % capacity
        slots[slot] = pack_slot(
            item["seq"],
            item["tick"],
            item["boot_id"],
            item["temperature_centidegrees"],
        )
    for slot, raw in (stale_slots or {}).items():
        if not 0 <= slot < capacity:
            raise ValueError("stale slot out of range")
        slots[slot] = raw
    header = {
        "generation": generation,
        "capacity": capacity,
        "count": count,
        "next_slot": next_slot,
        "next_seq": next_seq,
        "device_id": device_id,
    }
    return assemble_image(header, slots, alternate_header=alternate_header)
