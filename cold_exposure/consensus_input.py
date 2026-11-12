"""Validation and single byte-capture loading for the consensus (v2) entry.

The three UTF-8 CSV inputs are read once into bytes: the same bytes feed both
the SHA-256 hashes and the parser. Only Python standard library types are used;
temperatures stay as ``decimal.Decimal`` and timestamps as timezone-aware
``datetime`` values normalized to whole UTC seconds.
"""
import csv
import io
from decimal import Decimal, InvalidOperation

from .input import stamp


class ConsensusInputError(ValueError):
    """An input contract violation, naming the file and a locatable record."""


TRIPS_HEADERS = ["trip_id", "started_at", "ended_at", "min_c", "max_c", "min_support"]
ASSIGNMENT_HEADERS = ["trip_id", "sensor_id", "uncertainty_c"]
READING_HEADERS = ["sensor_id", "observed_at", "temperature_c"]


def _identifier(value, kind, file_name, row_number):
    if value is None or value == "" or value != value.strip():
        raise ConsensusInputError(
            f"{file_name}: row {row_number}: {kind} must be non-empty without leading/trailing whitespace"
        )
    return value


def _bounded_decimal(value, low, high, file_name, row_number, field):
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ConsensusInputError(f"{file_name}: row {row_number}: {field} is not a finite decimal") from exc
    if not result.is_finite():
        raise ConsensusInputError(f"{file_name}: row {row_number}: {field} must be finite")
    if not low <= result <= high:
        raise ConsensusInputError(f"{file_name}: row {row_number}: {field} must be within [{low}, {high}]")
    if result.as_tuple().exponent < -6:
        raise ConsensusInputError(f"{file_name}: row {row_number}: {field} may use at most six decimal places")
    return result


def _temperature(value, file_name, row_number, field="temperature_c"):
    return _bounded_decimal(value, Decimal(-100), Decimal(150), file_name, row_number, field)


def _uncertainty(value, file_name, row_number):
    return _bounded_decimal(value, Decimal(0), Decimal(20), file_name, row_number, "uncertainty_c")


def _unsigned_int(value, file_name, row_number, field):
    if value is None or not all("0" <= character <= "9" for character in value):
        raise ConsensusInputError(f"{file_name}: row {row_number}: {field} must be a non-negative integer")
    return int(value)


def _dict_rows(data, headers, file_name):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConsensusInputError(f"{file_name}: file is not valid UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != headers:
        raise ConsensusInputError(
            f"{file_name}: CSV header must be exactly {','.join(headers)}, got "
            f"{None if reader.fieldnames is None else ','.join(reader.fieldnames)}"
        )
    rows = []
    for row_number, row in enumerate(reader, start=1):
        if None in row or any(value is None or value == "" for value in row.values()):
            raise ConsensusInputError(f"{file_name}: row {row_number}: missing or extra CSV field")
        rows.append((row_number, row))
    return rows


def _read_bytes(path, file_name):
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except OSError as exc:
        raise ConsensusInputError(f"{file_name}: cannot read {path}: {exc}") from exc


def capture_inputs(trips_path, assignments_path, readings_path):
    """Read every input exactly once and return (bytes dict, parsed model)."""
    captured = {
        "trips": _read_bytes(trips_path, "trips"),
        "assignments": _read_bytes(assignments_path, "assignments"),
        "readings": _read_bytes(readings_path, "readings"),
    }
    return captured, parse_inputs(captured)


def parse_inputs(captured):
    """Parse and cross-validate already-captured UTF-8 CSV byte strings."""
    trips = []
    trip_ids = set()
    for row_number, row in _dict_rows(captured["trips"], TRIPS_HEADERS, "trips"):
        trip_id = _identifier(row["trip_id"], "trip_id", "trips", row_number)
        if trip_id in trip_ids:
            raise ConsensusInputError(f"trips: row {row_number}: duplicate trip_id {trip_id!r}")
        try:
            start = stamp(row["started_at"])
            end = stamp(row["ended_at"])
        except ValueError as exc:
            raise ConsensusInputError(f"trips: row {row_number}: {exc}") from exc
        if end <= start:
            raise ConsensusInputError(f"trips: row {row_number}: ended_at must be later than started_at")
        low = _temperature(row["min_c"], "trips", row_number, "min_c")
        high = _temperature(row["max_c"], "trips", row_number, "max_c")
        if low >= high:
            raise ConsensusInputError(f"trips: row {row_number}: min_c must be strictly below max_c")
        min_support = _unsigned_int(row["min_support"], "trips", row_number, "min_support")
        if min_support < 1:
            raise ConsensusInputError(f"trips: row {row_number}: min_support must be at least 1")
        trip_ids.add(trip_id)
        trips.append(
            {
                "trip_id": trip_id,
                "start": start,
                "end": end,
                "low": low,
                "high": high,
                "min_support": min_support,
                "row": row_number,
            }
        )
    if not trips:
        raise ConsensusInputError("trips: at least one trip is required")

    assignments = []
    assignment_pairs = set()
    per_trip = {trip["trip_id"]: [] for trip in trips}
    for row_number, row in _dict_rows(captured["assignments"], ASSIGNMENT_HEADERS, "assignments"):
        trip_id = _identifier(row["trip_id"], "trip_id", "assignments", row_number)
        sensor_id = _identifier(row["sensor_id"], "sensor_id", "assignments", row_number)
        if trip_id not in trip_ids:
            raise ConsensusInputError(
                f"assignments: row {row_number}: unknown trip_id {trip_id!r}"
            )
        pair = (trip_id, sensor_id)
        if pair in assignment_pairs:
            raise ConsensusInputError(
                f"assignments: row {row_number}: duplicate assignment ({trip_id!r}, {sensor_id!r})"
            )
        assignment_pairs.add(pair)
        uncertainty = _uncertainty(row["uncertainty_c"], "assignments", row_number)
        assignment = {
            "trip_id": trip_id,
            "sensor_id": sensor_id,
            "uncertainty": uncertainty,
            "row": row_number,
        }
        assignments.append(assignment)
        per_trip[trip_id].append(assignment)

    assigned_sensors = set()
    for trip in trips:
        members = sorted(per_trip[trip["trip_id"]], key=lambda item: item["sensor_id"])
        if not members:
            raise ConsensusInputError(
                f"assignments: trip {trip['trip_id']!r} has at least one assigned sensor"
            )
        if not 1 <= trip["min_support"] <= len(members):
            raise ConsensusInputError(
                f"trips: row {trip['row']}: min_support {trip['min_support']} must be between 1 and "
                f"the {len(members)} assigned sensors for trip {trip['trip_id']!r}"
            )
        trip["assignments"] = members
        assigned_sensors.update(item["sensor_id"] for item in members)

    readings = {sensor_id: [] for sensor_id in assigned_sensors}
    reading_keys = set()
    for row_number, row in _dict_rows(captured["readings"], READING_HEADERS, "readings"):
        sensor_id = _identifier(row["sensor_id"], "sensor_id", "readings", row_number)
        if sensor_id not in assigned_sensors:
            raise ConsensusInputError(
                f"readings: row {row_number}: sensor_id {sensor_id!r} is not assigned to any trip"
            )
        try:
            observed_at = stamp(row["observed_at"])
        except ValueError as exc:
            raise ConsensusInputError(f"readings: row {row_number}: {exc}") from exc
        key = (sensor_id, observed_at)
        if key in reading_keys:
            raise ConsensusInputError(
                f"readings: row {row_number}: duplicate reading for {sensor_id!r} at "
                f"{observed_at.isoformat()} (equivalent UTC instants count as duplicates)"
            )
        reading_keys.add(key)
        readings[sensor_id].append(
            {
                "sensor_id": sensor_id,
                "observed_at": observed_at,
                "temperature": _temperature(row["temperature_c"], "readings", row_number),
                "row": row_number,
            }
        )
    for sensor_id in readings:
        readings[sensor_id].sort(key=lambda item: item["observed_at"])

    return {
        "trips": sorted(trips, key=lambda item: item["trip_id"]),
        "assignments": sorted(assignments, key=lambda item: (item["trip_id"], item["sensor_id"])),
        "readings": readings,
    }
