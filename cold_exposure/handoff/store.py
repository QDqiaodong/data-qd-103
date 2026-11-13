"""SQLite materialization for the hand-off fulfillment marketplace.

The database is built entirely inside a hidden temporary file next to the
target, validated with the same conservation checks used by the in-memory
model plus SQL-side recomputation, and only then installed with
``os.replace``. Any failure removes the temporary file and leaves a previous
database byte-for-byte intact. Re-running with identical inputs rebuilds an
identical database: there is no clock reading and no machine-specific data.

Foreign keys trace every attribution fact back to its original consensus
partition (and through it the trip); consensus components live in their own
table so aggregating the fact table can never multiply a metric by the number
of components. The two original input documents are stored verbatim with their
SHA-256 hashes, so the inputs can be moved or deleted and the database still
supports a full SQL-only re-audit.
"""
import hashlib
import os
import sqlite3
import tempfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .attribution import build_model
from .errors import HandoffError
from .model import epoch, read_bytes

SCHEMA_VERSION = 1

POLICY_NOTES = [
    "Synthetic fulfillment evidence only: it attributes recorded exposure across custody hand-offs and never assigns compensation or legal liability.",
    "Attribution cuts the original v2 consensus partitions; rounded whole-trip metrics are never prorated across carriers or days.",
    "Seconds without a covering hand-off remain explicitly unassigned; unknown consensus seconds are never zero-filled.",
    "Degree-minute exposure bounds are NULL for a carrier-day with no consensus-supported second; an entirely unknown group never reports a fabricated zero.",
]

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE ingestion_run (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    max_gap_seconds INTEGER,
    trip_count INTEGER NOT NULL,
    handoff_count INTEGER NOT NULL,
    fact_count INTEGER NOT NULL,
    carrier_day_count INTEGER NOT NULL,
    total_duration_seconds INTEGER NOT NULL,
    total_definitely_outside_seconds INTEGER NOT NULL,
    total_possibly_outside_seconds INTEGER NOT NULL,
    total_unknown_seconds INTEGER NOT NULL,
    total_unassigned_seconds INTEGER NOT NULL,
    policy_notes TEXT NOT NULL
);

CREATE TABLE source_document (
    name TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    content TEXT NOT NULL
);

CREATE TABLE source_location (
    name TEXT PRIMARY KEY REFERENCES source_document(name),
    input_path TEXT NOT NULL,
    csv_header_record_number INTEGER,
    first_data_record_number INTEGER,
    record_count INTEGER NOT NULL
);

CREATE TABLE trip (
    trip_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    started_epoch INTEGER NOT NULL,
    ended_at TEXT NOT NULL,
    ended_epoch INTEGER NOT NULL,
    min_c TEXT NOT NULL,
    max_c TEXT NOT NULL,
    quorum INTEGER NOT NULL,
    duration_seconds INTEGER NOT NULL,
    definitely_outside_seconds INTEGER NOT NULL,
    possibly_outside_seconds INTEGER NOT NULL,
    unknown_seconds INTEGER NOT NULL
);

CREATE TABLE consensus_partition (
    trip_id TEXT NOT NULL REFERENCES trip(trip_id),
    partition_index INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    started_epoch INTEGER NOT NULL,
    ended_at TEXT NOT NULL,
    ended_epoch INTEGER NOT NULL,
    seconds INTEGER NOT NULL,
    classification TEXT NOT NULL,
    disjoint_from_allowed_range INTEGER NOT NULL,
    not_contained_in_allowed_range INTEGER NOT NULL,
    degree_seconds_min TEXT,
    degree_seconds_max TEXT,
    PRIMARY KEY (trip_id, partition_index)
);

CREATE TABLE consensus_component (
    trip_id TEXT NOT NULL,
    partition_index INTEGER NOT NULL,
    component_index INTEGER NOT NULL,
    lower_c TEXT NOT NULL,
    upper_c TEXT NOT NULL,
    PRIMARY KEY (trip_id, partition_index, component_index),
    FOREIGN KEY (trip_id, partition_index)
        REFERENCES consensus_partition(trip_id, partition_index) ON DELETE CASCADE
);

CREATE TABLE handoff (
    handoff_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trip(trip_id),
    carrier TEXT NOT NULL,
    csv_record_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    started_epoch INTEGER NOT NULL,
    ended_at TEXT NOT NULL,
    ended_epoch INTEGER NOT NULL
);

CREATE TABLE attribution_fact (
    fact_id INTEGER PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trip(trip_id),
    partition_index INTEGER NOT NULL,
    handoff_id TEXT REFERENCES handoff(handoff_id),
    carrier TEXT,
    is_unassigned INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    started_epoch INTEGER NOT NULL,
    ended_at TEXT NOT NULL,
    ended_epoch INTEGER NOT NULL,
    utc_day TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    classification TEXT NOT NULL,
    definitely_outside_seconds INTEGER NOT NULL,
    possibly_outside_seconds INTEGER NOT NULL,
    unknown_seconds INTEGER NOT NULL,
    degree_seconds_min TEXT,
    degree_seconds_max TEXT,
    FOREIGN KEY (trip_id, partition_index)
        REFERENCES consensus_partition(trip_id, partition_index),
    CHECK (
        (is_unassigned = 1 AND handoff_id IS NULL AND carrier IS NULL)
        OR
        (is_unassigned = 0 AND handoff_id IS NOT NULL AND carrier IS NOT NULL)
    ),
    CHECK (seconds > 0),
    CHECK (ended_epoch > started_epoch)
);

CREATE INDEX idx_fact_trip ON attribution_fact(trip_id, started_epoch);
CREATE INDEX idx_fact_day_carrier ON attribution_fact(utc_day, carrier);
CREATE INDEX idx_fact_partition ON attribution_fact(trip_id, partition_index);

CREATE TABLE carrier_daily (
    carrier_day_id INTEGER PRIMARY KEY,
    carrier TEXT,
    is_unassigned INTEGER NOT NULL,
    utc_day TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    definitely_outside_seconds INTEGER NOT NULL,
    possibly_outside_seconds INTEGER NOT NULL,
    unknown_seconds INTEGER NOT NULL,
    supported_seconds INTEGER NOT NULL,
    degree_minutes_min TEXT,
    degree_minutes_max TEXT,
    trip_count INTEGER NOT NULL,
    handoff_count INTEGER NOT NULL,
    UNIQUE (is_unassigned, carrier, utc_day)
);

CREATE VIEW v_carrier_daily AS
SELECT carrier, is_unassigned, utc_day, duration_seconds,
       definitely_outside_seconds, possibly_outside_seconds,
       unknown_seconds, supported_seconds,
       degree_minutes_min, degree_minutes_max, trip_count, handoff_count
FROM carrier_daily
ORDER BY is_unassigned, carrier, utc_day;

CREATE VIEW v_attribution_fact AS
SELECT fact_id, trip_id, partition_index, handoff_id, carrier, is_unassigned,
       started_at, ended_at, utc_day, seconds, classification,
       definitely_outside_seconds, possibly_outside_seconds, unknown_seconds,
       degree_seconds_min, degree_seconds_max
FROM attribution_fact
ORDER BY trip_id, started_epoch, fact_id;
"""


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _decimal_text(value):
    return None if value is None else str(value)


def _connect(path):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _record_count(data):
    """Number of CSV data records (header excluded) in captured UTF-8 bytes."""
    import csv
    import io

    return max(sum(1 for _ in csv.reader(io.StringIO(data.decode("utf-8")))) - 1, 0)


def populate_database(conn, model, sources, source_paths):
    """Load a fully validated model plus captured source bytes into ``conn``."""
    conn.executescript(DDL)
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO source_document(name, sha256, byte_count, content) VALUES (?, ?, ?, ?)",
        [
            (
                name,
                hashlib.sha256(data).hexdigest(),
                len(data),
                data.decode("utf-8"),
            )
            for name, data in sorted(sources.items())
        ],
    )
    cur.executemany(
        "INSERT INTO source_location VALUES (?, ?, ?, ?, ?)",
        [
            (
                "handoff_csv",
                source_paths["handoff_csv"],
                1,
                2,
                _record_count(sources["handoff_csv"]),
            ),
            (
                "consensus_json",
                source_paths["consensus_json"],
                None,
                None,
                len(model["trips"]),
            ),
        ],
    )

    for ordinal, trip_id in enumerate(model["trip_order"]):
        trip = model["trips"][trip_id]
        cur.execute(
            "INSERT INTO trip VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trip_id,
                ordinal,
                _iso(trip["start"]),
                epoch(trip["start"]),
                _iso(trip["end"]),
                epoch(trip["end"]),
                str(trip["low"]),
                str(trip["high"]),
                trip["quorum"],
                trip["duration"],
                trip["definitely_outside_seconds"],
                trip["possibly_outside_seconds"],
                trip["unknown_seconds"],
            ),
        )
        for partition in trip["partitions"]:
            cur.execute(
                "INSERT INTO consensus_partition VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trip_id,
                    partition["index"],
                    _iso(partition["start"]),
                    epoch(partition["start"]),
                    _iso(partition["end"]),
                    epoch(partition["end"]),
                    partition["seconds"],
                    partition["classification"],
                    int(partition["disjoint"]),
                    int(partition["not_contained"]),
                    _decimal_text(
                        None
                        if partition["classification"] == "unknown"
                        else partition["degree_seconds_min"]
                    ),
                    _decimal_text(
                        None
                        if partition["classification"] == "unknown"
                        else partition["degree_seconds_max"]
                    ),
                ),
            )
            for component_index, (lower, upper) in enumerate(partition["components"]):
                cur.execute(
                    "INSERT INTO consensus_component VALUES (?, ?, ?, ?, ?)",
                    (trip_id, partition["index"], component_index, str(lower), str(upper)),
                )

    for handoff in model["handoffs"]:
        cur.execute(
            "INSERT INTO handoff VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                handoff["handoff_id"],
                handoff["trip_id"],
                handoff["carrier"],
                handoff["row"],
                _iso(handoff["start"]),
                epoch(handoff["start"]),
                _iso(handoff["end"]),
                epoch(handoff["end"]),
            ),
        )

    for fact_id, fact in enumerate(model["facts"]):
        cur.execute(
            "INSERT INTO attribution_fact VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact_id,
                fact["trip_id"],
                fact["partition_index"],
                fact["handoff_id"],
                fact["carrier"],
                int(fact["is_unassigned"]),
                _iso(fact["start"]),
                epoch(fact["start"]),
                _iso(fact["end"]),
                epoch(fact["end"]),
                fact["utc_day"],
                fact["seconds"],
                fact["classification"],
                fact["definitely_outside_seconds"],
                fact["possibly_outside_seconds"],
                fact["unknown_seconds"],
                _decimal_text(fact["degree_seconds_min"]),
                _decimal_text(fact["degree_seconds_max"]),
            ),
        )

    for row in model["carrier_days"]:
        cur.execute(
            "INSERT INTO carrier_daily VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                None,
                row["carrier"],
                int(row["is_unassigned"]),
                row["utc_day"],
                row["duration_seconds"],
                row["definitely_outside_seconds"],
                row["possibly_outside_seconds"],
                row["unknown_seconds"],
                row["supported_seconds"],
                _decimal_text(row["degree_minutes_min"]),
                _decimal_text(row["degree_minutes_max"]),
                row["trip_count"],
                row["handoff_count"],
            ),
        )

    import json

    total_duration = sum(fact["seconds"] for fact in model["facts"])
    total_definite = sum(fact["definitely_outside_seconds"] for fact in model["facts"])
    total_possible = sum(fact["possibly_outside_seconds"] for fact in model["facts"])
    total_unknown = sum(fact["unknown_seconds"] for fact in model["facts"])
    total_unassigned = sum(
        fact["seconds"] for fact in model["facts"] if fact["is_unassigned"]
    )
    cur.execute(
        "INSERT INTO ingestion_run VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            SCHEMA_VERSION,
            model["max_gap_seconds"],
            len(model["trips"]),
            len(model["handoffs"]),
            len(model["facts"]),
            len(model["carrier_days"]),
            total_duration,
            total_definite,
            total_possible,
            total_unknown,
            total_unassigned,
            json.dumps(POLICY_NOTES, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()


def _scalar(conn, sql, parameters=()):
    return conn.execute(sql, parameters).fetchone()[0]


def verify_connection(conn):
    """Run every SQL-side invariant; raise ``HandoffError`` on the first breach."""
    conn.row_factory = sqlite3.Row
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise HandoffError("database verification failed: foreign key check found broken references")

    # 0. The stored run summary must recompute exactly from the fact table.
    run = conn.execute("SELECT * FROM ingestion_run WHERE id = 1").fetchone()
    if run is None:
        raise HandoffError("database verification failed: ingestion_run summary is missing")
    recomputed_summary = conn.execute(
        """
        SELECT COUNT(DISTINCT trip_id) AS trip_count,
               (SELECT COUNT(*) FROM handoff) AS handoff_count,
               COUNT(*) AS fact_count,
               (SELECT COUNT(*) FROM carrier_daily) AS carrier_day_count,
               COALESCE(SUM(seconds), 0) AS total_duration_seconds,
               COALESCE(SUM(definitely_outside_seconds), 0) AS total_definitely_outside_seconds,
               COALESCE(SUM(possibly_outside_seconds), 0) AS total_possibly_outside_seconds,
               COALESCE(SUM(unknown_seconds), 0) AS total_unknown_seconds,
               COALESCE(SUM(CASE WHEN is_unassigned = 1 THEN seconds ELSE 0 END), 0)
                   AS total_unassigned_seconds
        FROM attribution_fact
        """
    ).fetchone()
    for column in recomputed_summary.keys():
        if run[column] != recomputed_summary[column]:
            raise HandoffError(
                f"database verification failed: ingestion_run.{column} summary mismatch"
            )
    if run["trip_count"] != _scalar(conn, "SELECT COUNT(*) FROM trip"):
        raise HandoffError("database verification failed: ingestion_run.trip_count mismatch")

    # 1. Facts cover each trip exactly once: half-open, non-empty, no gap/overlap.
    coverage = conn.execute(
        """
        SELECT t.trip_id, t.started_epoch, t.ended_epoch, t.duration_seconds,
               COALESCE(SUM(f.seconds), 0) AS fact_seconds,
               MIN(f.started_epoch) AS first_start, MAX(f.ended_epoch) AS last_end
        FROM trip t LEFT JOIN attribution_fact f ON f.trip_id = t.trip_id
        GROUP BY t.trip_id
        """
    ).fetchall()
    for row in coverage:
        if row["fact_seconds"] != row["duration_seconds"]:
            raise HandoffError(
                f"database verification failed: trip {row['trip_id']!r} facts sum to "
                f"{row['fact_seconds']}s but the trip lasts {row['duration_seconds']}s"
            )
        if row["first_start"] != row["started_epoch"] or row["last_end"] != row["ended_epoch"]:
            raise HandoffError(
                f"database verification failed: trip {row['trip_id']!r} facts do not span its window"
            )
    gaps = conn.execute(
        """
        SELECT later.trip_id, later.fact_id
        FROM attribution_fact later
        JOIN attribution_fact earlier
          ON earlier.trip_id = later.trip_id
         AND earlier.started_epoch = (
             SELECT MAX(started_epoch) FROM attribution_fact
             WHERE trip_id = later.trip_id AND started_epoch < later.started_epoch
         )
        WHERE earlier.ended_epoch != later.started_epoch
        """
    ).fetchall()
    if gaps:
        raise HandoffError("database verification failed: attribution facts contain a gap or overlap")

    # 2. Every fact sits inside its referenced original partition.
    outside = conn.execute(
        """
        SELECT COUNT(*) FROM attribution_fact f
        JOIN consensus_partition p
          ON p.trip_id = f.trip_id AND p.partition_index = f.partition_index
        WHERE f.started_epoch < p.started_epoch OR f.ended_epoch > p.ended_epoch
        """
    ).fetchone()[0]
    if outside:
        raise HandoffError("database verification failed: facts escape their source partitions")

    # 3. Additive metrics conserved from facts back to the trip table.
    for metric in (
        "definitely_outside_seconds",
        "possibly_outside_seconds",
        "unknown_seconds",
    ):
        bad = conn.execute(
            f"""
            SELECT t.trip_id FROM trip t
            JOIN attribution_fact f ON f.trip_id = t.trip_id
            GROUP BY t.trip_id HAVING SUM(f.{metric}) != t.{metric}
            """
        ).fetchall()
        if bad:
            raise HandoffError(f"database verification failed: {metric} not conserved for a trip")

    # 4. Exact degree-second bounds conserved per original partition.
    partition_check = conn.execute(
        """
        SELECT p.trip_id, p.partition_index, p.seconds,
               p.degree_seconds_min AS pmin, p.degree_seconds_max AS pmax,
               SUM(COALESCE(f.degree_seconds_min, 0)) AS fmin,
               SUM(COALESCE(f.degree_seconds_max, 0)) AS fmax,
               SUM(CASE WHEN f.degree_seconds_min IS NULL THEN 1 ELSE 0 END) AS null_facts
        FROM consensus_partition p
        JOIN attribution_fact f
          ON f.trip_id = p.trip_id AND f.partition_index = p.partition_index
        GROUP BY p.trip_id, p.partition_index
        """
    ).fetchall()
    for row in partition_check:
        if row["pmin"] is None:
            if row["null_facts"] == 0:
                raise HandoffError(
                    f"database verification failed: partition {row['trip_id']!r}/"
                    f"{row['partition_index']} lost its unknown marking"
                )
            continue
        if row["null_facts"]:
            raise HandoffError(
                f"database verification failed: supported partition {row['trip_id']!r}/"
                f"{row['partition_index']} contains unknown facts"
            )
        if Decimal(row["fmin"]) != Decimal(row["pmin"]) * row["seconds"] or \
                Decimal(row["fmax"]) != Decimal(row["pmax"]) * row["seconds"]:
            raise HandoffError(
                f"database verification failed: degree-second bounds not conserved for "
                f"{row['trip_id']!r}/{row['partition_index']}"
            )

    # 5. Carrier-day materialization recomputes exactly from facts.
    recomputed = conn.execute(
        """
        SELECT is_unassigned, carrier, utc_day,
               SUM(seconds) AS duration_seconds,
               SUM(definitely_outside_seconds) AS definitely_outside_seconds,
               SUM(possibly_outside_seconds) AS possibly_outside_seconds,
               SUM(unknown_seconds) AS unknown_seconds,
               SUM(seconds - unknown_seconds) AS supported_seconds,
               SUM(degree_seconds_min) AS degree_minutes_min_num,
               SUM(degree_seconds_max) AS degree_minutes_max_num,
               COUNT(DISTINCT trip_id) AS trip_count,
               COUNT(DISTINCT handoff_id) AS handoff_count
        FROM attribution_fact
        GROUP BY is_unassigned, carrier, utc_day
        """
    ).fetchall()
    stored = {
        (row["is_unassigned"], row["carrier"], row["utc_day"]): row
        for row in conn.execute("SELECT * FROM carrier_daily").fetchall()
    }
    if len(stored) != len(recomputed):
        raise HandoffError("database verification failed: carrier_daily row count mismatch")
    for row in recomputed:
        key = (row["is_unassigned"], row["carrier"], row["utc_day"])
        saved = stored.get(key)
        if saved is None:
            raise HandoffError(f"database verification failed: missing carrier_daily row {key}")
        for column in (
            "duration_seconds",
            "definitely_outside_seconds",
            "possibly_outside_seconds",
            "unknown_seconds",
            "supported_seconds",
            "trip_count",
            "handoff_count",
        ):
            if row[column] != saved[column]:
                raise HandoffError(
                    f"database verification failed: carrier_daily.{column} mismatch for {key}"
                )
        if row["supported_seconds"] == 0:
            if saved["degree_minutes_min"] is not None or saved["degree_minutes_max"] is not None:
                raise HandoffError(
                    f"database verification failed: all-unknown carrier-day {key} must report NULL bounds"
                )
        else:
            expected_min = str(
                (Decimal(row["degree_minutes_min_num"]) / Decimal(60)).quantize(
                    Decimal("0.000001"), rounding=ROUND_HALF_UP
                )
            )
            expected_max = str(
                (Decimal(row["degree_minutes_max_num"]) / Decimal(60)).quantize(
                    Decimal("0.000001"), rounding=ROUND_HALF_UP
                )
            )
            if saved["degree_minutes_min"] != expected_min or \
                    saved["degree_minutes_max"] != expected_max:
                raise HandoffError(
                    f"database verification failed: degree-minute bounds mismatch for {key}"
                )

    # 6. Stored source hashes match the archived bytes; locations resolve names.
    document_names = {
        row["name"] for row in conn.execute("SELECT name FROM source_document")
    }
    if document_names != {"consensus_json", "handoff_csv"}:
        raise HandoffError("database verification failed: unexpected source document set")
    for row in conn.execute("SELECT name, sha256, byte_count, content FROM source_document"):
        digest = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
        if digest != row["sha256"] or row["byte_count"] != len(row["content"].encode("utf-8")):
            raise HandoffError(
                f"database verification failed: source document {row['name']!r} hash mismatch"
            )
    location_rows = conn.execute(
        "SELECT name, input_path, record_count FROM source_location"
    ).fetchall()
    if {row["name"] for row in location_rows} != document_names:
        raise HandoffError("database verification failed: source_location missing a document")
    for row in location_rows:
        if not row["input_path"] or row["record_count"] < 0:
            raise HandoffError(
                f"database verification failed: source_location {row['name']!r} is incomplete"
            )

    # 7. Rebuild the whole model from the archived documents and compare rows.
    sources = {
        row["name"]: row["content"].encode("utf-8")
        for row in conn.execute("SELECT name, content FROM source_document")
    }
    rebuilt = build_model(sources["consensus_json"], sources["handoff_csv"])
    stored_facts = conn.execute(
        "SELECT trip_id, partition_index, handoff_id, carrier, is_unassigned, "
        "started_epoch, ended_epoch, utc_day, seconds, classification, "
        "definitely_outside_seconds, possibly_outside_seconds, unknown_seconds, "
        "degree_seconds_min, degree_seconds_max FROM attribution_fact ORDER BY fact_id"
    ).fetchall()
    if len(stored_facts) != len(rebuilt["facts"]):
        raise HandoffError("database verification failed: rebuilt fact count mismatch")
    for saved, fact in zip(stored_facts, rebuilt["facts"]):
        expected = (
            fact["trip_id"], fact["partition_index"], fact["handoff_id"], fact["carrier"],
            int(fact["is_unassigned"]), epoch(fact["start"]), epoch(fact["end"]),
            fact["utc_day"], fact["seconds"], fact["classification"],
            fact["definitely_outside_seconds"], fact["possibly_outside_seconds"],
            fact["unknown_seconds"], _decimal_text(fact["degree_seconds_min"]),
            _decimal_text(fact["degree_seconds_max"]),
        )
        if tuple(saved) != expected:
            raise HandoffError("database verification failed: rebuilt attribution facts differ")
    return {
        "trips": _scalar(conn, "SELECT COUNT(*) FROM trip"),
        "handoffs": _scalar(conn, "SELECT COUNT(*) FROM handoff"),
        "partitions": _scalar(conn, "SELECT COUNT(*) FROM consensus_partition"),
        "components": _scalar(conn, "SELECT COUNT(*) FROM consensus_component"),
        "facts": _scalar(conn, "SELECT COUNT(*) FROM attribution_fact"),
        "carrier_days": _scalar(conn, "SELECT COUNT(*) FROM carrier_daily"),
    }


def verify_database(path):
    """Open an existing marketplace database read-only and verify every invariant."""
    target = Path(path)
    if target.is_symlink():
        raise HandoffError(f"database path {target} is a symbolic link; refusing to follow it")
    if not target.is_file():
        raise HandoffError(f"database {target} does not exist")
    uri = Path(target).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        return verify_connection(conn)
    finally:
        conn.close()


def build_database(consensus_path, handoff_path, output_path):
    """Capture inputs, build, verify and atomically publish the SQLite database."""
    target = Path(output_path)
    if target.is_symlink():
        raise HandoffError(f"output path {target} is a symbolic link; refusing to follow it")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    sources = {
        "consensus_json": read_bytes(consensus_path, "consensus JSON"),
        "handoff_csv": read_bytes(handoff_path, "handoffs"),
    }
    # Record the invocation-supplied location verbatim; the archived bytes, not
    # this path, are what verification re-parses, so moving the inputs is safe.
    source_paths = {
        "consensus_json": str(consensus_path),
        "handoff_csv": str(handoff_path),
    }
    model = build_model(sources["consensus_json"], sources["handoff_csv"])

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=parent,
            prefix="." + target.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        conn = _connect(temporary_path)
        try:
            populate_database(conn, model, sources, source_paths)
            verify_connection(conn)
        finally:
            conn.close()
        os.replace(temporary_path, target)
    except BaseException:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise
    return model
