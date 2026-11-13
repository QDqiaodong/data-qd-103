"""Manual time-calibration anchor contract for synthetic CCLOG01 images.

The anchor document is strict UTF-8 JSON with no duplicate object keys. It
binds one physical device (``device_id`` must equal the chosen header) to one
output ``sensor_id`` and supplies at least one human time observation per
active boot segment. Wall-clock time is never inferred across reboots.
"""
import json
from datetime import datetime, timedelta, timezone

from ..input import stamp
from .image import MOD32, FlashError

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
TOP_LEVEL_KEYS = {"schema_version", "device_id", "sensor_id", "anchors"}
ANCHOR_KEYS = {"boot_id", "seq", "observed_at"}


class _RejectDuplicateKeys:
    """``json.loads`` object hook that rejects repeated member names."""

    def __init__(self):
        self.pairs = []

    def __call__(self, ordered_pairs):
        seen = set()
        for key, _value in ordered_pairs:
            if key in seen:
                raise FlashError(f"anchors: duplicate JSON object key {key!r}")
            seen.add(key)
        return dict(ordered_pairs)


def _require_uint32(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise FlashError(f"anchors: {field} must be a uint32 integer, booleans are invalid")
    if not 0 <= value < MOD32:
        raise FlashError(f"anchors: {field} {value} is outside the uint32 range")
    return value


def parse_anchor_bytes(raw):
    """Validate anchor-document structure and return the parsed mapping.

    Timestamps become timezone-aware whole-second UTC datetimes; cross-checks
    against the chosen header and active records happen in
    :func:`bind_anchors`.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise FlashError("anchors: input must be captured bytes")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FlashError("anchors: document is not strict UTF-8") from exc
    hook = _RejectDuplicateKeys()
    try:
        document = json.loads(text, object_pairs_hook=hook)
    except json.JSONDecodeError as exc:
        raise FlashError(f"anchors: invalid JSON: {exc}") from exc
    if not isinstance(document, dict) or set(document) != TOP_LEVEL_KEYS:
        raise FlashError(
            "anchors: top level must be an object with exactly the keys "
            "schema_version, device_id, sensor_id, anchors"
        )
    schema_version = document["schema_version"]
    if isinstance(schema_version, bool) or schema_version != 1:
        raise FlashError("anchors: schema_version must be the integer 1")
    device_id = document["device_id"]
    if not isinstance(device_id, str) or not device_id:
        raise FlashError("anchors: device_id must be a non-empty string")
    sensor_id = document["sensor_id"]
    if not isinstance(sensor_id, str) or not sensor_id or sensor_id != sensor_id.strip():
        raise FlashError(
            "anchors: sensor_id must be a non-empty string without leading/trailing whitespace"
        )
    anchors = document["anchors"]
    if not isinstance(anchors, list):
        raise FlashError("anchors: anchors must be an array")
    parsed = []
    for index, item in enumerate(anchors):
        if not isinstance(item, dict) or set(item) != ANCHOR_KEYS:
            raise FlashError(
                f"anchors: entry {index} must be an object with exactly boot_id, seq, observed_at"
            )
        boot_id = _require_uint32(item["boot_id"], f"entry {index}.boot_id")
        seq = _require_uint32(item["seq"], f"entry {index}.seq")
        observed_at = item["observed_at"]
        if not isinstance(observed_at, str):
            raise FlashError(f"anchors: entry {index}.observed_at must be a string")
        try:
            moment = stamp(observed_at)
        except ValueError as exc:
            raise FlashError(f"anchors: entry {index}.observed_at: {exc}") from exc
        parsed.append(
            {
                "index": index,
                "boot_id": boot_id,
                "seq": seq,
                "observed_at_raw": observed_at,
                "observed_at_utc": moment,
            }
        )
    return {
        "schema_version": 1,
        "device_id": device_id,
        "sensor_id": sensor_id,
        "anchors": parsed,
    }


def epoch_seconds(moment):
    return int((moment - _EPOCH).total_seconds())


def utc_from_epoch(seconds):
    try:
        return _EPOCH + timedelta(seconds=seconds)
    except (OverflowError, OSError) as exc:
        raise FlashError(f"derived UTC second {seconds} is outside the datetime range") from exc


def bind_anchors(document, device_id, records, segments):
    """Cross-check anchors against active records and derive clock offsets.

    ``records`` are the active slot records in logical order, already carrying
    ``expanded_tick`` values. ``segments`` lists record-index runs per boot.
    Every active boot needs at least one anchor, every anchor must reference an
    active record of that boot, and all anchors of one boot must imply exactly
    the same offset between the expanded tick axis and UTC.
    """
    if document["device_id"] != device_id:
        raise FlashError(
            f"anchors: device_id {document['device_id']!r} does not match the selected "
            f"header device_id {device_id!r}"
        )
    anchors = document["anchors"]
    if not records:
        if anchors:
            raise FlashError("anchors: count=0 image requires an empty anchors array")
        return []

    record_by_key = {
        (record["boot_id"], record["seq"]): index for index, record in enumerate(records)
    }
    boot_of_index = {}
    for segment in segments:
        boot_id = records[segment[0]]["boot_id"]
        for record_index in segment:
            boot_of_index[record_index] = boot_id

    seen_pairs = set()
    bound = []
    for anchor in anchors:
        pair = (anchor["boot_id"], anchor["seq"])
        if pair in seen_pairs:
            raise FlashError(
                f"anchors: entry {anchor['index']} repeats (boot_id, seq)= "
                f"({anchor['boot_id']}, {anchor['seq']})"
            )
        seen_pairs.add(pair)
        if pair not in record_by_key:
            raise FlashError(
                f"anchors: entry {anchor['index']} references "
                f"(boot_id={anchor['boot_id']}, seq={anchor['seq']}) which is not a "
                "current valid record (overwritten or unknown slots cannot be anchored)"
            )
        record_index = record_by_key[pair]
        if boot_of_index[record_index] != anchor["boot_id"]:
            raise FlashError(
                f"anchors: entry {anchor['index']} boot_id {anchor['boot_id']} does not "
                f"match the record's boot_id {boot_of_index[record_index]}"
            )
        record = records[record_index]
        offset_seconds = epoch_seconds(anchor["observed_at_utc"]) - record["expanded_tick"]
        bound.append({**anchor, "record_index": record_index, "offset_seconds": offset_seconds})

    anchors_by_boot = {}
    for anchor in bound:
        anchors_by_boot.setdefault(anchor["boot_id"], []).append(anchor)

    active_boots = [records[segment[0]]["boot_id"] for segment in segments]
    unknown_boots = sorted(set(anchors_by_boot) - set(active_boots))
    if unknown_boots:
        raise FlashError(
            f"anchors: references to boot_id(s) {unknown_boots} that have no active records"
        )
    for boot_id in active_boots:
        if boot_id not in anchors_by_boot:
            raise FlashError(f"anchors: active boot_id {boot_id} has no calibration anchor")

    resolved = []
    for boot_id in active_boots:
        boot_anchors = anchors_by_boot[boot_id]
        offsets = {anchor["offset_seconds"] for anchor in boot_anchors}
        if len(offsets) != 1:
            detail = ", ".join(
                f"entry {anchor['index']} offset={anchor['offset_seconds']}"
                for anchor in boot_anchors
            )
            raise FlashError(
                f"anchors: inconsistent clock offsets for boot_id {boot_id} ({detail}); "
                "least squares, drift fitting and picking one anchor are not allowed"
            )
        # The canonical anchor is the one at the earliest logical position in
        # the segment, regardless of JSON array order.
        canonical = min(boot_anchors, key=lambda anchor: anchor["record_index"])
        for anchor in boot_anchors:
            resolved.append(
                {
                    **anchor,
                    "is_canonical": anchor is canonical,
                    "consistent_with_canonical": True,
                }
            )
    # Deterministic audit order: logical record position, then document index.
    resolved.sort(key=lambda anchor: (anchor["record_index"], anchor["index"]))
    return resolved
