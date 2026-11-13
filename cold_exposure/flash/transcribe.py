"""Capture, transcribe and atomically publish one synthetic CCLOG01 image.

Each input file is read exactly once into bytes; parsing, SHA-256 hashing and
the audit trail all derive from that single capture. Both products are fully
generated before the output directory is touched: they are written into a
private hidden staging directory next to the target and installed with one
directory rename, so a failure never leaves a formal half-published result.
"""
import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
from pathlib import Path

from .anchors import bind_anchors, parse_anchor_bytes, utc_from_epoch
from .image import (
    SLOT_SIZE,
    FlashError,
    build_segments,
    evaluate_headers,
    expected_image_size,
    expand_segment_ticks,
    format_temperature,
    logical_positions,
    parse_slot,
    select_header,
    slot_offset,
)

SCHEMA_VERSION = 1
CSV_NAME = "readings.csv"
AUDIT_NAME = "transcription.json"
CSV_HEADER = ["sensor_id", "observed_at", "temperature_c"]
SYNTHETIC_NOTICE = (
    "CCLOG01 is an original synthetic device protocol invented for this project; "
    "it is not compatible with any vendor hardware and no real device, serial link, "
    "network or database is involved."
)


class CapturedInputs:
    """The one and only byte capture used for parsing and hashing."""

    def __init__(self, image, anchors, image_path=None, anchors_path=None):
        self.image = bytes(image)
        self.anchors = bytes(anchors)
        self.image_path = image_path
        self.anchors_path = anchors_path


def _read_bytes(path, label):
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except OSError as exc:
        raise FlashError(f"{label}: cannot read {path}: {exc}") from exc


def capture_inputs(image_path, anchors_path):
    """Capture both inputs exactly once."""
    return CapturedInputs(
        _read_bytes(image_path, "image"),
        _read_bytes(anchors_path, "anchors"),
        image_path=str(image_path),
        anchors_path=str(anchors_path),
    )


def _utc_text(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _active_records(captured, header):
    capacity = header["capacity"]
    expected_length = expected_image_size(capacity)
    if len(captured.image) != expected_length:
        raise FlashError(
            f"image length is {len(captured.image)} bytes but the selected header "
            f"declares capacity={capacity}, requiring exactly {expected_length} bytes "
            "(128 + capacity*24)"
        )
    positions = logical_positions(
        capacity, header["count"], header["next_slot"], header["next_seq"]
    )
    records = []
    for logical_index, (slot, expected_seq) in enumerate(positions):
        offset = slot_offset(slot)
        raw = captured.image[offset : offset + SLOT_SIZE]
        record = parse_slot(raw, slot, expected_seq)
        record["logical_index"] = logical_index
        records.append(record)
    return records, positions


def transcribe_capture(captured):
    """Parse one captured pair and return ``(csv_text, audit_text, model)``."""
    if not isinstance(captured, CapturedInputs):
        raise FlashError("transcribe_capture requires a CapturedInputs instance")
    image_sha = hashlib.sha256(captured.image).hexdigest()
    anchors_sha = hashlib.sha256(captured.anchors).hexdigest()

    header_copies = evaluate_headers(captured.image)
    chosen, selection_reason = select_header(header_copies)
    header = chosen["fields"]

    records, positions = _active_records(captured, header)
    segments = build_segments(records)
    for indices in segments:
        expand_segment_ticks(records, indices)

    anchor_document = parse_anchor_bytes(captured.anchors)
    bound_anchors = bind_anchors(
        anchor_document, header["device_id"], records, segments
    )

    canonical_by_boot = {
        anchor["boot_id"]: anchor
        for anchor in bound_anchors
        if anchor["is_canonical"]
    }

    # Derive UTC per record from its own boot segment only.
    for record in records:
        anchor = canonical_by_boot[record["boot_id"]]
        epoch_seconds = record["expanded_tick"] + anchor["offset_seconds"]
        moment = utc_from_epoch(epoch_seconds)
        record["observed_at_utc"] = moment
        record["canonical_anchor"] = anchor

    # Wall-clock monotonicity across boots is not assumed; sort for the CSV.
    csv_order = sorted(range(len(records)), key=lambda index: records[index]["observed_at_utc"])
    seen_utc = set()
    for record_index in csv_order:
        moment = records[record_index]["observed_at_utc"]
        if moment in seen_utc:
            raise FlashError(
                f"sensor {anchor_document['sensor_id']!r}: two valid records fall on the "
                f"same UTC second {_utc_text(moment)}; silent merging is not allowed"
            )
        seen_utc.add(moment)
    for csv_record_number, record_index in enumerate(csv_order, start=1):
        records[record_index]["csv_record_number"] = csv_record_number

    csv_text = _render_csv(anchor_document["sensor_id"], records, csv_order)
    audit = _build_audit(
        captured,
        image_sha,
        anchors_sha,
        header_copies,
        chosen,
        selection_reason,
        header,
        records,
        positions,
        segments,
        anchor_document,
        bound_anchors,
        csv_order,
    )
    audit_text = json.dumps(audit, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    model = {
        "records": records,
        "csv_order": csv_order,
        "sensor_id": anchor_document["sensor_id"],
        "audit": audit,
    }
    return csv_text, audit_text, model


def _render_csv(sensor_id, records, csv_order):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    for record_index in csv_order:
        record = records[record_index]
        writer.writerow(
            [
                sensor_id,
                _utc_text(record["observed_at_utc"]),
                format_temperature(record["temperature_centidegrees"]),
            ]
        )
    return buffer.getvalue()


def _header_audit(copy):
    entry = {
        "name": copy["name"],
        "offset": copy["offset"],
        "raw_hex": copy["raw_hex"],
        "valid": copy["valid"],
        "invalid_reason": copy["reason"],
    }
    if copy["valid"]:
        entry["fields"] = {
            "generation": copy["fields"]["generation"],
            "capacity": copy["fields"]["capacity"],
            "count": copy["fields"]["count"],
            "next_slot": copy["fields"]["next_slot"],
            "next_seq": copy["fields"]["next_seq"],
            "device_id": copy["fields"]["device_id"],
            "crc32": f"0x{copy['fields']['crc32']:08x}",
        }
    else:
        entry["fields"] = None
    return entry


def _anchor_audit(anchor):
    return {
        "document_entry_index": anchor["index"],
        "boot_id": anchor["boot_id"],
        "seq": anchor["seq"],
        "observed_at_raw": anchor["observed_at_raw"],
        "observed_at_utc": _utc_text(anchor["observed_at_utc"]),
        "bound_logical_index": anchor["record_index"],
        "derived_offset_seconds": anchor["offset_seconds"],
        "is_canonical": anchor["is_canonical"],
        "consistent_with_canonical": anchor["consistent_with_canonical"],
    }


def _build_audit(
    captured,
    image_sha,
    anchors_sha,
    header_copies,
    chosen,
    selection_reason,
    header,
    records,
    positions,
    segments,
    anchor_document,
    bound_anchors,
    csv_order,
):
    active_slots = {slot for slot, _ in positions}
    unused_slots = sorted(slot for slot in range(header["capacity"]) if slot not in active_slots)
    segment_audit = []
    for segment in segments:
        boot_id = records[segment[0]]["boot_id"]
        canonical = next(
            anchor for anchor in bound_anchors
            if anchor["boot_id"] == boot_id and anchor["is_canonical"]
        )
        segment_audit.append(
            {
                "boot_id": boot_id,
                "logical_index_range": [segment[0], segment[-1]],
                "record_count": len(segment),
                "canonical_anchor_entry_index": canonical["index"],
            }
        )

    record_audit = []
    for record in records:
        anchor = record["canonical_anchor"]
        record_audit.append(
            {
                "logical_index": record["logical_index"],
                "physical_slot": record["slot"],
                "absolute_offset": record["offset"],
                "slot_raw_hex": record["raw_hex"],
                "seq": record["seq"],
                "tick": record["tick"],
                "boot_id": record["boot_id"],
                "temperature_centidegrees": record["temperature_centidegrees"],
                "expanded_tick": record["expanded_tick"],
                "canonical_anchor": {
                    "document_entry_index": anchor["index"],
                    "observed_at_raw": anchor["observed_at_raw"],
                    "observed_at_utc": _utc_text(anchor["observed_at_utc"]),
                    "derived_offset_seconds": anchor["offset_seconds"],
                },
                "observed_at_utc": _utc_text(record["observed_at_utc"]),
                "temperature_c": format_temperature(record["temperature_centidegrees"]),
                "csv_record_number": record["csv_record_number"],
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "protocol": "CCLOG01 synthetic circular flash image",
        "synthetic_notice": SYNTHETIC_NOTICE,
        "inputs_sha256": {"image": image_sha, "anchors": anchors_sha},
        "device_binding": {
            "device_id": header["device_id"],
            "sensor_id": anchor_document["sensor_id"],
        },
        "headers": {
            "copies": [_header_audit(copy) for copy in header_copies],
            "selected_header": chosen["name"],
            "selected_generation": header["generation"],
            "selection_reason": selection_reason,
        },
        "ring": {
            "capacity": header["capacity"],
            "count": header["count"],
            "next_slot": header["next_slot"],
            "next_seq": header["next_seq"],
            "active_logical_positions": [
                {
                    "logical_index": logical_index,
                    "physical_slot": slot,
                    "absolute_offset": slot_offset(slot),
                    "expected_seq": expected_seq,
                }
                for logical_index, (slot, expected_seq) in enumerate(positions)
            ],
            "unused_physical_slots": unused_slots,
        },
        "boot_segments": segment_audit,
        "anchors": [_anchor_audit(anchor) for anchor in bound_anchors],
        "records": record_audit,
        "csv": {
            "file_name": CSV_NAME,
            "header": CSV_HEADER,
            "data_row_count": len(csv_order),
            "sort_order": "observed_at_utc ascending; wall-clock monotonicity across boots is not assumed",
            "encoding": "UTF-8 without BOM; standard CSV escaping; fixed two-place Celsius",
        },
        "audit_scope": (
            "Sufficient to independently recompute header selection, active slot "
            "ordering, tick expansion and every transcribed row. It does not store the "
            "full image and does not attest that any device measured a real temperature."
        ),
    }


def publish(output_dir, csv_text, audit_text, *, on_event=None):
    """Write both products in a private staging dir, then publish by rename."""
    target = Path(output_dir)
    if target.is_symlink():
        raise FlashError(f"output path {target} is a symbolic link; refusing to follow it")
    if target.exists():
        raise FlashError(f"output path {target} already exists; refusing to overwrite")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    staging_path = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.staging.", dir=parent)
    )

    def _notify(event):
        if on_event is not None:
            on_event(event, {"target": target, "staging": staging_path})

    try:
        _notify("staging_ready")
        csv_path = staging_path / CSV_NAME
        with open(csv_path, "w", encoding="utf-8", newline="") as stream:
            stream.write(csv_text)
        _notify("first_product_written")
        audit_path = staging_path / AUDIT_NAME
        with open(audit_path, "w", encoding="utf-8", newline="") as stream:
            stream.write(audit_text)
        _notify("second_product_written")
        _notify("before_publish")
        os.replace(staging_path, target)
        _notify("published")
    except BaseException:
        shutil.rmtree(staging_path, ignore_errors=True)
        raise
    return target


def transcribe(image_path, anchors_path, output_dir, *, on_event=None):
    """Convenience all-in-one entry: capture, transcribe, stage and publish."""
    captured = capture_inputs(image_path, anchors_path)
    csv_text, audit_text, model = transcribe_capture(captured)
    publish(output_dir, csv_text, audit_text, on_event=on_event)
    return model
