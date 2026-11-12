"""Deterministic schema-version-2 consensus report with atomic file replace.

Validation, analysis and JSON serialization all finish before any output file
is touched. The final write goes through a temporary file in the output's own
directory and ``os.replace``: a crash or failure leaves any previous report
byte-for-byte intact, and this run's temporary file is removed.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .consensus_engine import analyze_consensus_trip, validate_gap
from .consensus_input import capture_inputs

POLICY_NOTES = [
    "Synthetic demonstration policy: neither a statistical confidence interval, a true-temperature guarantee, nor a food/drug release criterion.",
    "Each valid logger contributes one closed band [v-u, v+u]; the consensus set contains every point covered by at least q bands, emitted as maximal closed connected components.",
    "Unknown partitions (below_quorum, no_consensus, no_bracket or gap_exceeded loggers) carry no consensus and never enter degree-minute totals.",
    "supported_degree_minutes_min/max describe only consensus-supported seconds; they are not whole-trip exposure bounds when unknown_seconds is positive.",
    "Raw sample gaps are judged before trip-window clipping; values are never extrapolated before the first or after the last sample.",
]


def _trip_report(trip, readings, max_gap_seconds):
    assignments = [
        {
            "sensor_id": item["sensor_id"],
            "uncertainty_c": str(item["uncertainty"]),
            "assignment_csv_record_number": item["row"],
        }
        for item in trip["assignments"]
    ]
    result = analyze_consensus_trip(trip, trip["assignments"], readings, max_gap_seconds)
    return {
        "trip_id": trip["trip_id"],
        "started_at": trip["start"].isoformat(),
        "ended_at": trip["end"].isoformat(),
        "min_c": str(trip["low"]),
        "max_c": str(trip["high"]),
        "min_support": trip["min_support"],
        "trip_csv_record_number": trip["row"],
        "assignments": assignments,
        **result,
    }


def build_report(trips_path, assignments_path, readings_path, max_gap_seconds=600):
    """Validate and analyze; return (report dict, serialized JSON string)."""
    validate_gap(max_gap_seconds)
    captured, model = capture_inputs(trips_path, assignments_path, readings_path)
    trip_reports = [
        _trip_report(trip, model["readings"], max_gap_seconds) for trip in model["trips"]
    ]
    report = {
        "schema_version": 2,
        "model": "multi-logger bounded-band consensus",
        "parameters": {"max_gap_seconds": max_gap_seconds},
        "inputs_sha256": {
            name: hashlib.sha256(data).hexdigest() for name, data in sorted(captured.items())
        },
        "policy_notes": POLICY_NOTES,
        "trips": trip_reports,
    }
    serialized = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return report, serialized


def _atomic_write(output_path, serialized):
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix="." + output.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, output)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise


def run_consensus(
    trips_path, assignments_path, readings_path, output, max_gap_seconds=600
):
    """Full offline consensus run; returns the report mapping."""
    report, serialized = build_report(
        trips_path, assignments_path, readings_path, max_gap_seconds
    )
    _atomic_write(output, serialized)
    return report
