"""Multi-logger consensus over left-held bounded intervals.

The strategy here is the project's synthetic demonstration policy. It is not a
statistical confidence interval, a guarantee of true temperature, or a
food/drug release criterion.

Steps:

1. Each assigned logger independently produces left-held intervals
   ``[a, b) -> value v`` from adjacent raw samples ``(a, v)`` and ``(b, ...)``.
   If the original raw gap ``b - a`` exceeds ``max_gap_seconds`` the whole
   interval is unknown; the gap is judged on the original samples before any
   trip-window clipping. No value is extrapolated before the first sample or
   after the last one.
2. Every interval boundary and every trip boundary becomes a boundary of a
   single half-open partition of the trip; all updates at the same instant
   jointly determine the following partition.
3. Inside a partition every *valid* logger contributes at most one closed band
   ``[v-u, v+u]``. The consensus set ``S`` is every temperature contained in at
   least ``q`` valid bands, emitted as maximal closed connected components.
   Computation is an endpoint sweep over band endpoints: no time grid, no
   temperature grid, no subset enumeration.
"""
from bisect import bisect_left
from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal(0)


def validate_gap(max_gap_seconds):
    if type(max_gap_seconds) is not int or not 1 <= max_gap_seconds <= 86400:
        raise ValueError("max gap must be an integer in 1..86400 seconds")
    return max_gap_seconds


def _left_holding(readings, instant):
    """Return the raw pair holding ``instant`` under left holding, or None.

    ``readings`` is a chronologically sorted list of reading dicts. A value is
    held on ``[sample_k.time, sample_k+1.time)``. The bracketing right sample is
    always returned so evidence can show which sample closes the interval.
    """
    times = [item["observed_at"] for item in readings]
    index = bisect_left(times, instant)
    if index < len(times) and times[index] == instant:
        left_index = index
    else:
        left_index = index - 1
    if left_index < 0 or left_index + 1 >= len(times):
        return None
    return readings[left_index], readings[left_index + 1]


def logger_state(assignment, readings, instant, max_gap_seconds):
    """Evaluate one assigned logger at a partition's representative instant.

    Returns a dict describing one of three states: ``valid`` (with the held
    reading, uncertainty half-width and closed band), ``gap_exceeded`` (raw
    bracket exists but spans more than the allowed gap) or ``no_bracket``.
    """
    validate_gap(max_gap_seconds)
    pair = _left_holding(readings, instant)
    base = {
        "sensor_id": assignment["sensor_id"],
        "uncertainty_c": str(assignment["uncertainty"]),
        "state": "no_bracket",
        "left_sample": None,
        "right_sample": None,
        "temperature_c": None,
        "band": None,
    }
    if pair is None:
        return base
    left_sample, right_sample = pair
    base["left_sample"] = _sample_ref(left_sample)
    base["right_sample"] = _sample_ref(right_sample)
    gap = (right_sample["observed_at"] - left_sample["observed_at"]).total_seconds()
    if gap > max_gap_seconds:
        base["state"] = "gap_exceeded"
        return base
    uncertainty = assignment["uncertainty"]
    value = left_sample["temperature"]
    base["state"] = "valid"
    base["temperature_c"] = str(value)
    base["band"] = {"lower": str(value - uncertainty), "upper": str(value + uncertainty)}
    return base


def _sample_ref(sample):
    return {
        "sensor_id": sample["sensor_id"],
        "observed_at": sample["observed_at"].isoformat(),
        "temperature_c": str(sample["temperature"]),
        "csv_record_number": sample["row"],
    }


def consensus_components(bands, quorum):
    """Maximal closed connected components with coverage >= ``quorum``.

    ``bands`` is a list of ``(lower, upper)`` Decimal closed intervals; each
    contributes one vote at every point of its interval, including both
    endpoints. Endpoints touching (e.g. ``[9,11]`` and ``[11,13]``) therefore
    create a single-point consensus at 11.
    """
    # Sweep across distinct coordinates. Starts are applied before ends at a
    # shared coordinate so closed bands touching at a point cover that point.
    grouped = {}
    for lower, upper in bands:
        grouped.setdefault(lower, [0, 0])[0] += 1
        grouped.setdefault(upper, [0, 0])[1] += 1
    coordinates = sorted(grouped)

    covered_points = set()
    covered_gaps = set()  # coordinate x whose open gap (x, next) is fully covered
    coverage = 0
    for index, coordinate in enumerate(coordinates):
        starts, ends = grouped[coordinate]
        coverage += starts
        if coverage >= quorum:
            covered_points.add(coordinate)
        coverage -= ends
        if index + 1 < len(coordinates) and coverage >= quorum:
            covered_gaps.add(coordinate)

    components = []
    start = None
    for index, coordinate in enumerate(coordinates):
        point_covered = coordinate in covered_points
        gap_after = coordinate in covered_gaps
        if point_covered:
            if start is None:
                start = coordinate
            if not gap_after:
                components.append((start, coordinate))
                start = None
        else:
            # An uncovered point cannot lie inside a closed connected component.
            start = None
    return components


def classify(components, low, high):
    """Return one of within/above/below/mixed/unknown for a consensus set."""
    if not components:
        return "unknown"
    if all(component[0] >= low and component[1] <= high for component in components):
        return "within"
    if all(component[0] > high for component in components):
        return "above"
    if all(component[1] < low for component in components):
        return "below"
    return "mixed"


def distance_bounds(components, low, high):
    """Min/max of d(x)=max(low-x, x-high, 0) over the full consensus set."""
    def distance(x):
        return max(low - x, x - high, ZERO)

    minimum = None
    maximum = ZERO
    for lower, upper in components:
        candidates = [distance(lower), distance(upper)]
        if lower < low < upper:
            candidates.append(ZERO)
        if lower < high < upper:
            candidates.append(ZERO)
        component_min = min(candidates)
        component_max = max(candidates)
        minimum = component_min if minimum is None else min(minimum, component_min)
        maximum = max(maximum, component_max)
    return minimum, maximum


def _partition_boundaries(trip, readings, assignments):
    boundaries = {trip["start"], trip["end"]}
    assigned = {item["sensor_id"] for item in assignments}
    for sensor_id in assigned:
        for sample in readings.get(sensor_id, ()):
            moment = sample["observed_at"]
            if trip["start"] < moment < trip["end"]:
                boundaries.add(moment)
    return sorted(boundaries)


def analyze_consensus_trip(trip, assignments, readings, max_gap_seconds):
    """Build the full partition timeline and every aggregate for one trip."""
    validate_gap(max_gap_seconds)
    quorum = trip["min_support"]
    low, high = trip["low"], trip["high"]
    boundaries = _partition_boundaries(trip, readings, assignments)

    totals = {kind: 0 for kind in ["within", "above", "below", "mixed", "unknown"]}
    definitely_outside = 0
    possibly_outside = 0
    degree_seconds_min = ZERO
    degree_seconds_max = ZERO
    longest_confirmed = 0
    confirmed_streak = 0
    previous_class = None
    partitions = []

    for start, end in zip(boundaries, boundaries[1:]):
        seconds = int((end - start).total_seconds())
        representative = start  # left-held states are constant on [start, end)
        sensors = []
        valid_bands = []
        for assignment in assignments:
            state = logger_state(
                assignment, readings.get(assignment["sensor_id"], []), representative, max_gap_seconds
            )
            sensors.append(state)
            if state["state"] == "valid":
                valid_bands.append(
                    (Decimal(state["band"]["lower"]), Decimal(state["band"]["upper"]))
                )

        valid_count = len(valid_bands)
        if valid_count < quorum:
            components = []
            reason = "below_quorum"
        else:
            components = consensus_components(valid_bands, quorum)
            reason = "no_consensus" if not components else None

        kind = classify(components, low, high)
        totals[kind] += seconds

        disjoint = bool(components) and all(
            component[1] < low or component[0] > high for component in components
        )
        not_contained = bool(components) and any(
            component[0] < low or component[1] > high for component in components
        )
        if disjoint:
            definitely_outside += seconds
        if not_contained:
            possibly_outside += seconds

        if components:
            distance_min, distance_max = distance_bounds(components, low, high)
            degree_seconds_min += distance_min * seconds
            degree_seconds_max += distance_max * seconds

        if kind in {"above", "below"}:
            if kind == previous_class:
                confirmed_streak += seconds
            else:
                confirmed_streak = seconds
            longest_confirmed = max(longest_confirmed, confirmed_streak)
        else:
            confirmed_streak = 0
        previous_class = kind

        partitions.append(
            {
                "started_at": start.isoformat(),
                "ended_at": end.isoformat(),
                "seconds": seconds,
                "valid_logger_count": valid_count,
                "quorum": quorum,
                "consensus_reason": reason,
                "classification": kind,
                "consensus_components": [
                    {"lower": str(component[0]), "upper": str(component[1])}
                    for component in components
                ],
                "disjoint_from_allowed_range": disjoint,
                "not_contained_in_allowed_range": not_contained,
                "sensors": sensors,
            }
        )

    duration = int((trip["end"] - trip["start"]).total_seconds())
    assert sum(totals.values()) == duration
    supported_seconds = duration - totals["unknown"]
    if definitely_outside:
        status = "confirmed_excursion"
    elif possibly_outside:
        status = "possible_excursion"
    elif totals["unknown"]:
        status = "incomplete"
    else:
        status = "within_limits"

    return {
        "trip_id": trip["trip_id"],
        "duration_seconds": duration,
        "quorum": quorum,
        "status": status,
        "classification_seconds": totals,
        "definitely_outside_seconds": definitely_outside,
        "possibly_outside_seconds": possibly_outside,
        "supported_degree_minutes_min": _format_degree_minutes(degree_seconds_min, supported_seconds),
        "supported_degree_minutes_max": _format_degree_minutes(degree_seconds_max, supported_seconds),
        "unknown_seconds": totals["unknown"],
        "supported_seconds": supported_seconds,
        "longest_same_side_confirmed_seconds": longest_confirmed,
        "partitions": partitions,
    }


def _format_degree_minutes(degree_seconds, supported_seconds):
    # Unknown partitions never enter the weighted sums; an entirely unsupported
    # trip reports null rather than a fabricated zero exposure.
    if not supported_seconds:
        return None
    value = (degree_seconds / Decimal(60)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    return str(value)
