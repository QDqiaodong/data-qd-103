"""Attribution facts and carrier-day materialization.

For every trip the original consensus partitions are cut against two extra
boundary sets:

* the hand-off intervals (seconds not covered by any hand-off stay
  *unassigned* and are never silently given to a carrier);
* every UTC midnight between the trip start and end.

The resulting atomic attribution facts therefore split *both* along hand-off
and UTC-midnight boundaries, keep a foreign key back to the original consensus
partition, and cover each trip exactly once: adjacent facts are half-open,
non-empty and non-overlapping, and their union is the whole trip window. All
additive metrics (seconds, unknown seconds, definite/possible seconds and the
exact degree-second bounds) are conserved under every grouping. Nothing is
derived by prorating rounded whole-trip metrics.
"""
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from .model import utc_day


def _midnights(start, end):
    """All UTC midnights strictly inside the half-open window ``[start, end)``."""
    cursor = datetime.combine(start.astimezone(timezone.utc).date(),
                              time.min, tzinfo=timezone.utc)
    while cursor <= start:
        cursor += timedelta(days=1)
    result = []
    while cursor < end:
        result.append(cursor)
        cursor += timedelta(days=1)
    return result


def _handoff_at(handoffs, instant):
    """Return the hand-off (half-open) covering ``instant`` or None."""
    for handoff in handoffs:
        if handoff["start"] <= instant < handoff["end"]:
            return handoff
    return None


def build_facts(model):
    """Cut every consensus partition into hand-off/day attribution facts."""
    facts = []
    for trip_id in model["trip_order"]:
        trip = model["trips"][trip_id]
        handoffs = [item for item in model["handoffs"] if item["trip_id"] == trip_id]
        boundaries = {trip["start"], trip["end"]}
        for partition in trip["partitions"]:
            boundaries.add(partition["start"])
            boundaries.add(partition["end"])
        for handoff in handoffs:
            boundaries.add(handoff["start"])
            boundaries.add(handoff["end"])
        for midnight in _midnights(trip["start"], trip["end"]):
            boundaries.add(midnight)
        timeline = sorted(boundaries)

        partition_index = 0
        for start, end in zip(timeline, timeline[1:]):
            seconds = int((end - start).total_seconds())
            while partition_index < len(trip["partitions"]) and \
                    trip["partitions"][partition_index]["end"] <= start:
                partition_index += 1
            partition = trip["partitions"][partition_index]
            if not (partition["start"] <= start and end <= partition["end"]):
                raise RuntimeError(
                    f"internal error: attribution slice [{start.isoformat()}, {end.isoformat()}) "
                    f"is not contained in a consensus partition"
                )
            handoff = _handoff_at(handoffs, start)
            if handoff is not None and end > handoff["end"]:
                raise RuntimeError("internal error: hand-off boundary was not cut")

            unknown = partition["classification"] == "unknown"
            degree_seconds_min = None
            degree_seconds_max = None
            if not unknown:
                degree_seconds_min = partition["degree_seconds_min"] * seconds
                degree_seconds_max = partition["degree_seconds_max"] * seconds
            facts.append(
                {
                    "trip_id": trip_id,
                    "partition_index": partition["index"],
                    "start": start,
                    "end": end,
                    "seconds": seconds,
                    "utc_day": utc_day(start),
                    "handoff_id": None if handoff is None else handoff["handoff_id"],
                    "carrier": None if handoff is None else handoff["carrier"],
                    "is_unassigned": handoff is None,
                    "classification": partition["classification"],
                    "definitely_outside_seconds": seconds if partition["disjoint"] else 0,
                    "possibly_outside_seconds": seconds if partition["not_contained"] else 0,
                    "unknown_seconds": seconds if unknown else 0,
                    "degree_seconds_min": degree_seconds_min,
                    "degree_seconds_max": degree_seconds_max,
                }
            )
    return facts


def materialize_carrier_days(facts):
    """Aggregate facts by (carrier or unassigned, UTC day).

    Unknown seconds are kept as real counts (never zero-filled). Degree-minute
    bounds stay NULL whenever the group has no supported second: an entirely
    unknown group reports NULL rather than a fabricated zero exposure.
    """
    groups = {}
    order = []
    for fact in facts:
        key = (fact["carrier"], fact["utc_day"])
        if key not in groups:
            groups[key] = {
                "carrier": fact["carrier"],
                "is_unassigned": fact["is_unassigned"],
                "utc_day": fact["utc_day"],
                "duration_seconds": 0,
                "definitely_outside_seconds": 0,
                "possibly_outside_seconds": 0,
                "unknown_seconds": 0,
                "supported_seconds": 0,
                "degree_seconds_min": Decimal(0),
                "degree_seconds_max": Decimal(0),
                "trip_ids": set(),
                "handoff_ids": set(),
            }
            order.append(key)
        group = groups[key]
        group["duration_seconds"] += fact["seconds"]
        group["definitely_outside_seconds"] += fact["definitely_outside_seconds"]
        group["possibly_outside_seconds"] += fact["possibly_outside_seconds"]
        group["unknown_seconds"] += fact["unknown_seconds"]
        group["supported_seconds"] += fact["seconds"] - fact["unknown_seconds"]
        group["trip_ids"].add(fact["trip_id"])
        if fact["handoff_id"] is not None:
            group["handoff_ids"].add(fact["handoff_id"])
        if fact["degree_seconds_min"] is not None:
            group["degree_seconds_min"] += fact["degree_seconds_min"]
            group["degree_seconds_max"] += fact["degree_seconds_max"]

    rows = []
    for key in order:
        group = groups[key]
        if group["supported_seconds"]:
            degree_minutes_min = (group["degree_seconds_min"] / Decimal(60)).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            degree_minutes_max = (group["degree_seconds_max"] / Decimal(60)).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
        else:
            degree_minutes_min = None
            degree_minutes_max = None
        rows.append(
            {
                "carrier": group["carrier"],
                "is_unassigned": group["is_unassigned"],
                "utc_day": group["utc_day"],
                "duration_seconds": group["duration_seconds"],
                "definitely_outside_seconds": group["definitely_outside_seconds"],
                "possibly_outside_seconds": group["possibly_outside_seconds"],
                "unknown_seconds": group["unknown_seconds"],
                "supported_seconds": group["supported_seconds"],
                "degree_minutes_min": degree_minutes_min,
                "degree_minutes_max": degree_minutes_max,
                "trip_count": len(group["trip_ids"]),
                "trip_ids": sorted(group["trip_ids"]),
                "handoff_count": len(group["handoff_ids"]),
            }
        )
    rows.sort(key=lambda row: (row["is_unassigned"], row["carrier"] or "", row["utc_day"]))
    return rows


def check_conservation(model, facts, daily_rows):
    """In-process conservation checks mirroring the SQL-side verification."""
    expected_seconds = sum(
        trip["duration"] for trip in model["trips"].values()
    )
    if sum(fact["seconds"] for fact in facts) != expected_seconds:
        raise RuntimeError("internal error: attribution facts do not conserve trip duration")
    for trip_id, trip in model["trips"].items():
        own = [fact for fact in facts if fact["trip_id"] == trip_id]
        if sum(fact["seconds"] for fact in own) != trip["duration"]:
            raise RuntimeError(f"internal error: trip {trip_id!r} is not fully covered by facts")
        for metric in (
            "definitely_outside_seconds",
            "possibly_outside_seconds",
            "unknown_seconds",
        ):
            if sum(fact[metric] for fact in own) != trip[metric]:
                raise RuntimeError(f"internal error: trip {trip_id!r} loses {metric} in attribution")
        supported = trip["duration"] - trip["unknown_seconds"]
        if sum(fact["seconds"] - fact["unknown_seconds"] for fact in own) != supported:
            raise RuntimeError(f"internal error: trip {trip_id!r} supported seconds not conserved")
    if sum(row["duration_seconds"] for row in daily_rows) != expected_seconds:
        raise RuntimeError("internal error: carrier-day rows do not conserve duration")
    # Per-partition exact degree-second conservation (supported partitions only).
    partition_sums = {}
    for fact in facts:
        key = (fact["trip_id"], fact["partition_index"])
        if fact["degree_seconds_min"] is None:
            continue
        current = partition_sums.setdefault(key, [Decimal(0), Decimal(0)])
        current[0] += fact["degree_seconds_min"]
        current[1] += fact["degree_seconds_max"]
    for trip_id, trip in model["trips"].items():
        for partition in trip["partitions"]:
            if partition["classification"] == "unknown":
                continue
            key = (trip_id, partition["index"])
            lower, upper = partition_sums[key]
            if lower != partition["degree_seconds_min"] * partition["seconds"]:
                raise RuntimeError(
                    f"internal error: trip {trip_id!r} partition {partition['index']} "
                    "degree-second lower bound not conserved"
                )
            if upper != partition["degree_seconds_max"] * partition["seconds"]:
                raise RuntimeError(
                    f"internal error: trip {trip_id!r} partition {partition['index']} "
                    "degree-second upper bound not conserved"
                )


def build_model(consensus_bytes, handoff_bytes, handoff_file_name="handoffs"):
    """Parse both captures, validate, cut facts and materialize carrier days."""
    from .model import parse_consensus_bytes, parse_handoff_bytes

    parsed = parse_consensus_bytes(consensus_bytes)
    handoffs = parse_handoff_bytes(handoff_bytes, parsed["trips"], handoff_file_name)
    model = {
        "trips": parsed["trips"],
        "trip_order": parsed["trip_order"],
        "max_gap_seconds": parsed["max_gap_seconds"],
        "report": parsed["report"],
        "handoffs": handoffs,
    }
    facts = build_facts(model)
    daily_rows = materialize_carrier_days(facts)
    check_conservation(model, facts, daily_rows)
    model["facts"] = facts
    model["carrier_days"] = daily_rows
    return model
