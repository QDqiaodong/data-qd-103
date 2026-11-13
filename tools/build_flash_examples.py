#!/usr/bin/env python3
"""Deterministically regenerate the small synthetic CCLOG01 example images.

Everything written by this script is original synthetic data invented for this
project: the CCLOG01 protocol is not compatible with any vendor device, and no
real hardware, serial link, network, database or shipment measurement is
involved. Re-running the script with the same arguments reproduces the files
byte for byte; there is no clock reading, randomness or machine-specific data.

Usage:
    python tools/build_flash_examples.py --output-dir examples/flash/generated

The directory must not exist. Each image has a matching strict UTF-8 anchor
document so the whole set can be transcribed directly:

    python -m cold_exposure.flash \\
        --image examples/flash/generated/scenario1.bin \\
        --anchors examples/flash/generated/scenario1.anchors.json \\
        --output-dir run/flash/scenario1
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cold_exposure.flash.builder import build_ring_image  # noqa: E402
from cold_exposure.flash.image import pack_raw_slot  # noqa: E402


def anchor_document(device_id, sensor_id, anchors):
    return json.dumps(
        {
            "schema_version": 1,
            "device_id": device_id,
            "sensor_id": sensor_id,
            "anchors": anchors,
        },
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"


def scenario_one():
    """The headline ring example: capacity 5, four active slots, one stale slot.

    Ring state next_slot=2 / next_seq=2 makes the active order slots
    3, 4, 0, 1 with wrapped sequences 4294967294, 4294967295, 0, 1. Slot 2 is
    stale: an out-of-range temperature bit pattern plus a corrupted CRC, which
    the transcriber must never scan.
    """
    records = [
        {"seq": 4294967294, "tick": 4294967294, "boot_id": 7, "temperature_centidegrees": 400},
        {"seq": 4294967295, "tick": 4294967295, "boot_id": 7, "temperature_centidegrees": 900},
        {"seq": 0, "tick": 0, "boot_id": 7, "temperature_centidegrees": -125},
        {"seq": 1, "tick": 2, "boot_id": 7, "temperature_centidegrees": 800},
    ]
    stale = bytearray(pack_raw_slot(seq=99, tick=5, boot_id=7, raw_temperature_int16=30000))
    stale[20] ^= 0xFF  # deliberately wrong CRC on the unused slot
    image = build_ring_image(
        capacity=5,
        records=records,
        next_slot=2,
        next_seq=2,
        device_id="SYNTH-DEV-01",
        stale_slots={2: bytes(stale)},
    )
    anchors = anchor_document(
        "SYNTH-DEV-01",
        "SYNTH-SENSOR-01",
        [{"boot_id": 7, "seq": 0, "observed_at": "2026-01-01T00:00:02Z"}],
    )
    return "scenario1", image, anchors


def multiboot():
    """Two boot segments whose wall-clock order is the reverse of ring order."""
    records = [
        {"seq": 0, "tick": 4294967290, "boot_id": 10, "temperature_centidegrees": 400},
        {"seq": 1, "tick": 4294967295, "boot_id": 10, "temperature_centidegrees": 410},
        {"seq": 2, "tick": 1, "boot_id": 10, "temperature_centidegrees": 420},
        {"seq": 3, "tick": 100, "boot_id": 20, "temperature_centidegrees": 600},
        {"seq": 4, "tick": 105, "boot_id": 20, "temperature_centidegrees": 610},
        {"seq": 5, "tick": 107, "boot_id": 20, "temperature_centidegrees": 620},
    ]
    image = build_ring_image(
        capacity=8,
        records=records,
        next_slot=6,
        next_seq=6,
        device_id="SYNTH-DEV-02",
    )
    anchors = anchor_document(
        "SYNTH-DEV-02",
        "SYNTH-SENSOR-02",
        [
            # Two consistent anchors for boot 10; the middle record for boot 20.
            {"boot_id": 10, "seq": 0, "observed_at": "2026-01-02T00:00:00Z"},
            {"boot_id": 10, "seq": 2, "observed_at": "2026-01-02T00:00:07Z"},
            {"boot_id": 20, "seq": 4, "observed_at": "2026-01-01T00:00:00Z"},
        ],
    )
    return "multiboot", image, anchors


def empty_ring():
    """A valid count=0 image: transcription yields a header-only CSV."""
    image = build_ring_image(
        capacity=3,
        records=[],
        next_slot=0,
        next_seq=5,
        device_id="SYNTH-DEV-EMPTY",
    )
    anchors = anchor_document("SYNTH-DEV-EMPTY", "SYNTH-SENSOR-EMPTY", [])
    return "empty", image, anchors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.output_dir.exists() or args.output_dir.is_symlink():
        parser.exit(2, f"refusing to overwrite existing path: {args.output_dir}\n")
    args.output_dir.mkdir(parents=True)
    for name, image, anchors in (scenario_one(), multiboot(), empty_ring()):
        (args.output_dir / f"{name}.bin").write_bytes(image)
        (args.output_dir / f"{name}.anchors.json").write_text(anchors, encoding="utf-8")
    notice = (
        "# Synthetic CCLOG01 examples\n\n"
        "Generated by tools/build_flash_examples.py. All bytes are original\n"
        "synthetic data for a fictional protocol; they are not a vendor image,\n"
        "a real device dump or a real shipment record.\n"
    )
    (args.output_dir / "README.md").write_text(notice, encoding="utf-8")
    print(f"wrote {args.output_dir}")


if __name__ == "__main__":
    main()
