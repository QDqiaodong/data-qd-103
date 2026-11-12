# Multi-logger consensus model v2

A second, independent entry point (`python -m cold_exposure.consensus`,
schema_version 2) analyzes trips carrying several redundant loggers. It does not
replace model v1 (`python -m cold_exposure`), whose files, CLI and numeric
behavior are unchanged. The consensus policy is a synthetic demonstration
strategy: it is not a statistical confidence interval, a guarantee of true
temperature, or a food/drug release criterion. Only the Python standard library
is used.

## Inputs (three UTF-8 CSV files, exact ordered headers)

- `trips.csv`: `trip_id,started_at,ended_at,min_c,max_c,min_support`
- `assignments.csv`: `trip_id,sensor_id,uncertainty_c`
- `readings.csv`: `sensor_id,observed_at,temperature_c`

Rules enforced before any output is written; every rejection names the file and
a CSV data record number (header excluded, first data row is 1) or a business
key:

- `trip_id` unique. Every `(trip_id, sensor_id)` assignment unique; an
  assignment repeated with identical content is still rejected. Every trip has
  at least one assignment; assignments must reference a known trip.
- `min_support` (the quorum `q`) is an integer from 1 up to the number of
  sensors assigned to that trip. `q` is never reduced because of gaps.
- Readings reference only sensors that appear in at least one assignment. The
  unique reading key is `(sensor_id, observed_at)` after UTC normalization, so
  the same instant written with different UTC offsets is a duplicate. Readings
  may arrive unordered; a header-only readings file is valid.
- Identifiers are non-empty with no leading/trailing whitespace. Timestamps are
  timezone-aware, normalized to whole UTC seconds, and each trip ends after it
  starts (same contract as v1).
- `min_c`, `max_c`, `temperature_c` are finite decimals in `[-100, 150]` with at
  most six decimal places, and `min_c < max_c`. `uncertainty_c` is a finite
  decimal in `[0, 20]` with at most six decimal places. These bounds are local
  to the v2 entry and never change v1 validation.
- `--max-gap-seconds` is an integer in `1..86400` (default 600).

A sensor may be assigned to several trips; `uncertainty_c` is the fixed error
half-width adopted for that sensor on that trip.

## Logger intervals and the shared partition

Each assigned logger independently forms left-held intervals from adjacent raw
samples `(a, v)` and `(b, ...)`: the value `v` is used on `[a, b)`. If the raw
gap `b - a` exceeds `max_gap_seconds`, the whole interval is unknown. The gap
is judged from the original samples, never after clipping to the trip window.
Samples outside the window may bracket a partition inside it, but nothing is
extrapolated before the first or after the last sample; zero or one reading
yields no valid interval.

All in-window logger sample times and the trip start/end are merged into one
ordered set of half-open partitions `[t_k, t_{k+1})`. Every update happening at
the same instant jointly determines the following partition. Inside one
partition, each **valid** logger contributes exactly one vote via the closed
band `[v-u, v+u]`. Per-partition sensor states are:

- `valid`: a raw adjacent pair brackets the partition and its gap is within the
  limit; the report includes the held reading, uncertainty half-width and band.
- `gap_exceeded`: a bracketing pair exists but the raw gap exceeds the limit.
- `no_bracket`: no adjacent pair brackets the instant (before the first sample,
  after the last, or with fewer than two samples).

Both `gap_exceeded` and `no_bracket` partitions retain their left/right raw
sample identities (sensor, UTC time, temperature, CSV record number) when a
bracket exists, and are explicitly null otherwise. The right sample only
closes the interval; its temperature is never used for the preceding time.

## Consensus set, components and classification

For a partition, the consensus set `S` is the set of temperatures contained in
at least `q` valid bands. The report emits every maximal closed connected
component of `S`, ordered by temperature; components that touch (including at a
single point) are merged, so closed-band endpoints touching at a point can
produce a one-temperature component. The unsupported hole between separated
components is never filled; the overall band intersection, averages and
min/max envelopes are never used as a substitute. Components are computed by an
endpoint sweep over band endpoints — no per-second scan, temperature grid or
subset enumeration.

- Fewer than `q` valid loggers: `S` is empty with reason `below_quorum`.
- At least `q` valid loggers but no point covered `q` times: empty with reason
  `no_consensus`.
- Both empty outcomes are normal analysis results, not program failures.

With the allowed range `A = [min_c, max_c]` (equality is inside):

- non-empty `S` wholly inside `A` → `within`
- wholly strictly above `max_c` → `above`
- wholly strictly below `min_c` → `below`
- otherwise → `mixed`
- empty `S` → `unknown`

The five second counts partition the trip duration exactly.

## Definite vs. possible excursions

Computed independently of the five-way classification:

- `definitely_outside_seconds`: partitions where `S` is non-empty and disjoint
  from `A` (every supported temperature is outside), counting all their seconds.
- `possibly_outside_seconds`: partitions where non-empty `S` is not contained in
  `A` (some supported temperature is outside).

Definite time is always included in possible time; the two measures are not
mutually exclusive categories. A partition with one component below `min_c` and
another above `max_c` is definitely outside but has no single defensible side,
and is classified `mixed` rather than flattened into a "possibly safe" envelope.

## Supported degree-minutes

For temperature `x`, `d(x) = max(min_c - x, x - max_c, 0)`. Over non-empty
`S` the lower bound is the minimum of `d` and the upper bound the maximum,
evaluated over the full component set (including interior limit crossings where
`d = 0`), not over an envelope. Integer partition seconds weight the sums,
which are divided by 60 into `supported_degree_minutes_min/max`, shown as
six-place strings rounded `ROUND_HALF_UP`. All temperature, distance and
exposure arithmetic stays in `decimal.Decimal`; no float and no early rounding
is used.

Unknown partitions never enter the sums and are reported separately as
`unknown_seconds`. The two degree-minute fields describe only
consensus-supported seconds; when `unknown_seconds > 0` the maximum is not a
whole-trip exposure bound. No fused point temperature is invented and unknown
time is never treated as zero. When the whole trip is unsupported the fields
are JSON `null`. Coverage is given as integer supported seconds over the total
duration (`supported_seconds`, `duration_seconds`).

## Streaks and trip status

`longest_same_side_confirmed_seconds` merges adjacent partitions whose
classification is the same definite side (`above` then `above`, or `below` then
`below`). `within`, `mixed`, `unknown` and a side switch break the streak; a
change of supporting loggers or supporting samples alone does not.

Status priority, with every count retained:

1. any definite outside second → `confirmed_excursion`
2. otherwise any possible outside second → `possible_excursion`
3. otherwise any unknown second → `incomplete`
4. otherwise → `within_limits`

## Report shape (schema_version 2)

Top level: `schema_version`, `model`, `parameters.max_gap_seconds`,
`inputs_sha256` (SHA-256 of the exact bytes hashed and parsed for each of the
three inputs), `policy_notes`, and `trips`. Each trip carries its input window,
limits, quorum, CSV record number, assignments (with uncertainty and record
numbers), all aggregate counts, and the complete ordered `partitions`. Each
partition carries its half-open window, seconds, valid logger count, quorum,
`consensus_reason` (null when a consensus exists), classification, consensus
components, the two outside flags, and one entry per assigned sensor with its
state, band and raw sample evidence — enough to recompute validity and the
q-fold coverage independently.

## Determinism and failure recovery

Each input file is captured once as bytes; the same bytes are parsed and
hashed. Output uses sorted keys, stable ordering, no current timestamp and no
machine-absolute path: identical input bytes and parameters produce
byte-identical JSON. Validation, analysis and serialization complete before the
output path is touched; the JSON is written to a hidden temporary file in the
output's own directory and installed with `os.replace`. Any read, parse,
compute, serialization or replace failure exits non-zero, leaves a previous
report untouched, and removes this run's temporary file; the command can simply
be re-run after fixing the input. The delivery is single-process offline
analysis with single-file replacement only.

## Command and Python API

```sh
python -m cold_exposure.consensus \
  --trips data/consensus/trips.csv \
  --assignments data/consensus/assignments.csv \
  --readings data/consensus/readings.csv \
  --max-gap-seconds 900 \
  --output run/consensus/report.json
```

```python
from cold_exposure.consensus_pipeline import run_consensus
report = run_consensus("trips.csv", "assignments.csv", "readings.csv",
                       output="report.json", max_gap_seconds=600)
```
