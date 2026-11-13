"""Validation and in-memory model for the hand-off fulfillment marketplace.

Inputs are the existing schema-version-2 consensus report (UTF-8 JSON) and a
hand-off CSV (``handoff_id,trip_id,carrier,started_at,ended_at``). Both are read
once into bytes; the same bytes feed hashing, parsing and the database source
archive, so moving the original input files later never invalidates the
materialized database.

The consensus report is never trusted blindly: the partition timeline is
re-checked for full, non-overlapping coverage of every trip, partition
classifications and the definite/possible flags are recomputed with the same
endpoint-sweep engine that produced the report, and the trip totals are
re-derived from the partitions. Exposure is always attributed by cutting the
original consensus partitions; rounded whole-trip metrics are never prorated.

Only Python standard library types are used. Time stays as timezone-aware
``datetime`` values normalized to whole UTC seconds; temperatures and distances
stay as ``decimal.Decimal``.
"""
import csv
import io
import json
from datetime import timezone
from decimal import Decimal, InvalidOperation

from ..consensus_engine import classify, distance_bounds
from ..input import stamp
from .errors import HandoffError

HANDOFF_HEADERS = ["handoff_id", "trip_id", "carrier", "started_at", "ended_at"]
CLASSIFICATIONS = ("within", "above", "below", "mixed", "unknown")


def read_bytes(path, label):
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except OSError as exc:
        raise HandoffError(f"{label}: cannot read {path}: {exc}") from exc


def _require_mapping(value, where):
    if not isinstance(value, dict):
        raise HandoffError(f"consensus JSON: {where} must be a JSON object")


def _require_list(value, where):
    if not isinstance(value, list):
        raise HandoffError(f"consensus JSON: {where} must be a JSON array")


def _require_string(value, where):
    if not isinstance(value, str) or not value:
        raise HandoffError(f"consensus JSON: {where} must be a non-empty string")
    return value


def _require_int(value, where):
    if type(value) is not int:
        raise HandoffError(f"consensus JSON: {where} must be an integer")
    return value


def _require_decimal(value, where):
    if not isinstance(value, str):
        raise HandoffError(f"consensus JSON: {where} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise HandoffError(f"consensus JSON: {where} is not a finite decimal") from exc
    if not result.is_finite():
        raise HandoffError(f"consensus JSON: {where} must be finite")
    return result


def _moment(value, where):
    if not isinstance(value, str):
        raise HandoffError(f"consensus JSON: {where} must be an ISO-8601 UTC string")
    try:
        return stamp(value)
    except ValueError as exc:
        raise HandoffError(f"consensus JSON: {where}: {exc}") from exc


def _no_duplicate_keys(pairs):
    seen = set()
    result = {}
    for key, value in pairs:
        if key in seen:
            raise HandoffError(f"consensus JSON: duplicate JSON object key {key!r}")
        seen.add(key)
        result[key] = value
    return result


def parse_consensus_bytes(data):
    """Parse and cross-validate the captured v2 consensus report bytes."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandoffError("consensus JSON: file is not valid UTF-8") from exc
    try:
        report = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise HandoffError(f"consensus JSON: invalid JSON at line {exc.lineno} column {exc.colno}") from exc
    _require_mapping(report, "top level")
    if report.get("schema_version") != 2:
        raise HandoffError("consensus JSON: schema_version must be 2")
    if report.get("model") != "multi-logger bounded-band consensus":
        raise HandoffError(
            "consensus JSON: model must be 'multi-logger bounded-band consensus'"
        )
    trips_raw = report.get("trips")
    _require_list(trips_raw, "trips")
    if not trips_raw:
        raise HandoffError("consensus JSON: at least one trip is required")

    trips = {}
    order = []
    for trip_index, trip_raw in enumerate(trips_raw):
        where = f"trips[{trip_index}]"
        _require_mapping(trip_raw, where)
        trip_id = _require_string(trip_raw.get("trip_id"), f"{where}.trip_id")
        if trip_id in trips:
            raise HandoffError(f"consensus JSON: duplicate trip_id {trip_id!r}")
        start = _moment(trip_raw.get("started_at"), f"{where}.started_at")
        end = _moment(trip_raw.get("ended_at"), f"{where}.ended_at")
        if end <= start:
            raise HandoffError(f"consensus JSON: trip {trip_id!r} must end after it starts")
        low = _require_decimal(trip_raw.get("min_c"), f"{where}.min_c")
        high = _require_decimal(trip_raw.get("max_c"), f"{where}.max_c")
        if low >= high:
            raise HandoffError(f"consensus JSON: trip {trip_id!r}: min_c must be below max_c")
        quorum = _require_int(trip_raw.get("quorum"), f"{where}.quorum")
        if quorum < 1:
            raise HandoffError(f"consensus JSON: trip {trip_id!r}: quorum must be at least 1")
        duration = _require_int(
            trip_raw.get("duration_seconds"), f"{where}.duration_seconds"
        )
        if duration != int((end - start).total_seconds()):
            raise HandoffError(
                f"consensus JSON: trip {trip_id!r}: duration_seconds disagrees with the window"
            )

        partitions = _parse_partitions(trip_raw, trip_id, start, end, low, high, quorum)
        totals = {kind: 0 for kind in CLASSIFICATIONS}
        definite = 0
        possible = 0
        unknown = 0
        for partition in partitions:
            totals[partition["classification"]] += partition["seconds"]
            if partition["disjoint"]:
                definite += partition["seconds"]
            if partition["not_contained"]:
                possible += partition["seconds"]
            if partition["classification"] == "unknown":
                unknown += partition["seconds"]

        if totals != {
            key: _require_int(trip_raw["classification_seconds"].get(key),
                              f"{where}.classification_seconds.{key}")
            for key in CLASSIFICATIONS
        } if isinstance(trip_raw.get("classification_seconds"), dict) else {}:
            raise HandoffError(
                f"consensus JSON: trip {trip_id!r}: classification_seconds do not match its partitions"
            )
        if sum(totals.values()) != duration:
            raise HandoffError(
                f"consensus JSON: trip {trip_id!r}: partitions do not cover the full duration"
            )
        if _require_int(
            trip_raw.get("definitely_outside_seconds"),
            f"{where}.definitely_outside_seconds",
        ) != definite:
            raise HandoffError(
                f"consensus JSON: trip {trip_id!r}: definitely_outside_seconds disagrees with partitions"
            )
        if _require_int(
            trip_raw.get("possibly_outside_seconds"),
            f"{where}.possibly_outside_seconds",
        ) != possible:
            raise HandoffError(
                f"consensus JSON: trip {trip_id!r}: possibly_outside_seconds disagrees with partitions"
            )
        if _require_int(trip_raw.get("unknown_seconds"), f"{where}.unknown_seconds") != unknown:
            raise HandoffError(
                f"consensus JSON: trip {trip_id!r}: unknown_seconds disagrees with partitions"
            )
        if _require_int(
            trip_raw.get("supported_seconds"), f"{where}.supported_seconds"
        ) != duration - unknown:
            raise HandoffError(
                f"consensus JSON: trip {trip_id!r}: supported_seconds disagrees with partitions"
            )
        _check_degree_minutes(trip_raw, trip_id, partitions, duration - unknown, where)

        record = {
            "trip_id": trip_id,
            "start": start,
            "end": end,
            "low": low,
            "high": high,
            "quorum": quorum,
            "duration": duration,
            "partitions": partitions,
            "total_classification": totals,
            "definitely_outside_seconds": definite,
            "possibly_outside_seconds": possible,
            "unknown_seconds": unknown,
        }
        trips[trip_id] = record
        order.append(trip_id)

    parameters = report.get("parameters")
    max_gap = None
    if isinstance(parameters, dict) and type(parameters.get("max_gap_seconds")) is int:
        max_gap = parameters["max_gap_seconds"]
    return {"trips": trips, "trip_order": order, "max_gap_seconds": max_gap, "report": report}


def _parse_partitions(trip_raw, trip_id, trip_start, trip_end, low, high, quorum):
    where = f"trip {trip_id!r} partitions"
    rows_raw = trip_raw.get("partitions")
    _require_list(rows_raw, where)
    if not rows_raw:
        raise HandoffError(f"consensus JSON: {where} must not be empty")
    partitions = []
    cursor = trip_start
    for index, row_raw in enumerate(rows_raw):
        p_where = f"trip {trip_id!r} partition[{index}]"
        _require_mapping(row_raw, p_where)
        start = _moment(row_raw.get("started_at"), f"{p_where}.started_at")
        end = _moment(row_raw.get("ended_at"), f"{p_where}.ended_at")
        if start != cursor:
            raise HandoffError(
                f"consensus JSON: {p_where} leaves a gap or overlaps; expected start "
                f"{cursor.isoformat()}"
            )
        if end <= start or end > trip_end:
            raise HandoffError(f"consensus JSON: {p_where} is not a non-empty interval inside the trip")
        seconds = _require_int(row_raw.get("seconds"), f"{p_where}.seconds")
        if seconds != int((end - start).total_seconds()):
            raise HandoffError(f"consensus JSON: {p_where}.seconds disagrees with its window")
        classification = _require_string(
            row_raw.get("classification"), f"{p_where}.classification"
        )
        if classification not in CLASSIFICATIONS:
            raise HandoffError(f"consensus JSON: {p_where}.classification is unknown")
        components_raw = row_raw.get("consensus_components")
        _require_list(components_raw, f"{p_where}.consensus_components")
        components = []
        for component_index, component_raw in enumerate(components_raw):
            c_where = f"{p_where}.consensus_components[{component_index}]"
            _require_mapping(component_raw, c_where)
            lower = _require_decimal(component_raw.get("lower"), f"{c_where}.lower")
            upper = _require_decimal(component_raw.get("upper"), f"{c_where}.upper")
            if lower > upper:
                raise HandoffError(f"consensus JSON: {c_where} lower must not exceed upper")
            components.append((lower, upper))
        if _require_int(row_raw.get("quorum"), f"{p_where}.quorum") != quorum:
            raise HandoffError(f"consensus JSON: {p_where}.quorum disagrees with the trip")

        # Recompute the classification and both outside flags from the stored
        # components, using the same engine semantics as the original pipeline.
        expected_class = classify(components, low, high)
        if classification != expected_class:
            raise HandoffError(
                f"consensus JSON: {p_where}.classification {classification!r} does not match "
                f"the recomputed value {expected_class!r}"
            )
        expected_disjoint = bool(components) and all(
            component[1] < low or component[0] > high for component in components
        )
        expected_not_contained = bool(components) and any(
            component[0] < low or component[1] > high for component in components
        )
        if bool(row_raw.get("disjoint_from_allowed_range")) != expected_disjoint:
            raise HandoffError(
                f"consensus JSON: {p_where}.disjoint_from_allowed_range disagrees with its components"
            )
        if bool(row_raw.get("not_contained_in_allowed_range")) != expected_not_contained:
            raise HandoffError(
                f"consensus JSON: {p_where}.not_contained_in_allowed_range disagrees with its components"
            )

        degree_min = None
        degree_max = None
        if components:
            degree_min, degree_max = distance_bounds(components, low, high)

        partitions.append(
            {
                "index": index,
                "start": start,
                "end": end,
                "seconds": seconds,
                "classification": classification,
                "components": components,
                "disjoint": expected_disjoint,
                "not_contained": expected_not_contained,
                "degree_seconds_min": degree_min,
                "degree_seconds_max": degree_max,
            }
        )
        cursor = end
    if cursor != trip_end:
        raise HandoffError(
            f"consensus JSON: trip {trip_id!r}: partitions end at {cursor.isoformat()} "
            f"but the trip ends at {trip_end.isoformat()}"
        )
    return partitions


def _check_degree_minutes(trip_raw, trip_id, partitions, supported_seconds, where):
    """Trip degree-minute strings must equal the partition-derived sums.

    This guards the marketplace against reports whose rounded whole-trip
    metrics would not even reproduce their own partitions; attribution still
    cuts partitions directly rather than prorating these totals.
    """
    from decimal import ROUND_HALF_UP

    degree_seconds_min = Decimal(0)
    degree_seconds_max = Decimal(0)
    for partition in partitions:
        if partition["components"]:
            degree_seconds_min += partition["degree_seconds_min"] * partition["seconds"]
            degree_seconds_max += partition["degree_seconds_max"] * partition["seconds"]
    raw_min = trip_raw.get("supported_degree_minutes_min")
    raw_max = trip_raw.get("supported_degree_minutes_max")
    if supported_seconds == 0:
        if raw_min is not None or raw_max is not None:
            raise HandoffError(
                f"consensus JSON: trip {trip_id!r}: fully unsupported trip must report null degree minutes"
            )
        return
    expected_min = str((degree_seconds_min / Decimal(60)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    ))
    expected_max = str((degree_seconds_max / Decimal(60)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    ))
    if raw_min != expected_min or raw_max != expected_max:
        raise HandoffError(
            f"consensus JSON: {where}.supported_degree_minutes_min/max do not match the "
            "partition-derived sums"
        )


def _csv_rows(data, headers, file_name):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandoffError(f"{file_name}: file is not valid UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != headers:
        raise HandoffError(
            f"{file_name}: CSV header must be exactly {','.join(headers)}, got "
            f"{None if reader.fieldnames is None else ','.join(reader.fieldnames)}"
        )
    rows = []
    for row_number, row in enumerate(reader, start=1):
        if None in row or any(value is None or value == "" for value in row.values()):
            raise HandoffError(f"{file_name}: row {row_number}: missing or extra CSV field")
        rows.append((row_number, row))
    return rows


def parse_handoff_bytes(data, trips, file_name="handoffs"):
    """Parse and validate hand-off intervals against the known trip set."""
    handoffs = []
    seen_ids = set()
    per_trip = {trip_id: [] for trip_id in trips}
    for row_number, row in _csv_rows(data, HANDOFF_HEADERS, file_name):
        handoff_id = row["handoff_id"]
        if handoff_id != handoff_id.strip() or not handoff_id:
            raise HandoffError(
                f"{file_name}: row {row_number}: handoff_id must be non-empty without "
                "leading/trailing whitespace"
            )
        if handoff_id in seen_ids:
            raise HandoffError(
                f"{file_name}: row {row_number}: duplicate handoff_id {handoff_id!r}"
            )
        seen_ids.add(handoff_id)
        trip_id = row["trip_id"]
        if not trip_id or trip_id != trip_id.strip():
            raise HandoffError(
                f"{file_name}: row {row_number}: trip_id must be non-empty without "
                "leading/trailing whitespace"
            )
        if trip_id not in trips:
            raise HandoffError(
                f"{file_name}: row {row_number}: unknown trip_id {trip_id!r}; the trip must "
                "exist in the consensus report"
            )
        carrier = row["carrier"]
        if not carrier or carrier != carrier.strip():
            raise HandoffError(
                f"{file_name}: row {row_number}: carrier must be non-empty without "
                "leading/trailing whitespace"
            )
        try:
            start = stamp(row["started_at"])
            end = stamp(row["ended_at"])
        except ValueError as exc:
            raise HandoffError(f"{file_name}: row {row_number}: {exc}") from exc
        trip = trips[trip_id]
        if start < trip["start"] or end > trip["end"]:
            raise HandoffError(
                f"{file_name}: row {row_number}: hand-off {handoff_id!r} must stay inside trip "
                f"{trip_id!r} ({trip['start'].isoformat()} .. {trip['end'].isoformat()})"
            )
        if end <= start:
            raise HandoffError(
                f"{file_name}: row {row_number}: hand-off {handoff_id!r} must be a non-empty "
                "half-open interval"
            )
        record = {
            "handoff_id": handoff_id,
            "trip_id": trip_id,
            "carrier": carrier,
            "start": start,
            "end": end,
            "row": row_number,
        }
        handoffs.append(record)
        per_trip[trip_id].append(record)

    # Same-trip intervals may not overlap (touching endpoints is allowed).
    for trip_id, items in per_trip.items():
        items.sort(key=lambda item: (item["start"], item["handoff_id"]))
        for earlier, later in zip(items, items[1:]):
            if later["start"] < earlier["end"]:
                raise HandoffError(
                    f"{file_name}: hand-offs {earlier['handoff_id']!r} and {later['handoff_id']!r} "
                    f"overlap inside trip {trip_id!r}"
                )
    handoffs.sort(key=lambda item: (item["trip_id"], item["start"], item["handoff_id"]))
    return handoffs


def utc_day(moment):
    """UTC calendar day as the canonical ``YYYY-MM-DD`` text key."""
    return moment.astimezone(timezone.utc).date().isoformat()


def epoch(moment):
    return int(moment.timestamp())
