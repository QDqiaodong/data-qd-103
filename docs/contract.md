# Cold-chain exposure model v1

trips.csv has unique trip_id, nonempty sensor_id, timezone-aware whole-second
started_at/ended_at, min_c < max_c. Readings have unique sensor/time keys,
finite Celsius [-100,150], no unknown sensors. Rows may be unordered; empty
reading exports (header only) are valid and produce unknown coverage.
A sensor may serve more than one trip; each trip is evaluated independently.
This synthetic analysis policy is not a food/drug release authorization.

Model: left-held sample over [sample time, next sample time), clipped to the
trip's half-open window. An interval whose original sample-to-sample duration
exceeds max_gap_seconds is entirely unknown. There is no interpolation and no
extrapolation before the first or after the final reading. Thus sparse readings
cannot silently imply complete safe coverage. Threshold equality is within.

Every output second is classified within/above/below/unknown; totals must equal
trip duration. Mean temperature is weighted by known seconds only. Degree-minutes
sum time*(distance outside nearest limit)/60 over known intervals. Consecutive
same-side excursions merge; unknown/within or a change of side breaks the streak.
Any observed out-of-range interval gives excursion; no excursion but unknown time
gives incomplete; only full known compliant coverage gives within_limits.

JSON retains every clipped interval, its classification, values and input hashes;
CSV is an observable machine-readable summary (import identifiers as text in a
spreadsheet). Single-process full analysis, deterministic six-place Decimal
metric display, Python 28-digit arithmetic. Input errors occur before new report
files are written. Multi-file crash atomicity and live streaming are not provided.
