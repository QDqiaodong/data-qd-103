"""SQLite materialization and read-only recall queries for the genealogy.

The database is built entirely inside a hidden temporary file next to the
target, verified with the same contract checks used by the in-memory model
plus exact SQL-side recomputation, and only then installed with
``os.replace``. Any failure removes the temporary file and leaves a previous
database byte-for-byte intact. Re-running with identical inputs rebuilds an
identical database: there is no clock reading and no machine-specific data.

Every quantity is stored twice, mirroring the dual time columns of the
hand-off marketplace: as canonical six-decimal kg text (used by reports) and
as exact integer micro-kilograms (used by SQL-side arithmetic). The two
representations are cross-checked during verification. The two original CSV
documents are archived verbatim with their SHA-256 hashes, so the inputs can
be moved or deleted and the database still supports recall queries and a full
re-audit.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .errors import RecallError
from .model import (
    build_model,
    compute_recall,
    kg_text,
    read_bytes,
    topological_order,
)

SCHEMA_VERSION = 1

POLICY_NOTES = [
    "Synthetic recall evidence only: it traces batch genealogy for recall drills and never determines legal scope, liability or disposal decisions.",
    "Risk propagates to entire descendant lots regardless of flow quantities; flow kilograms are never treated as contamination mass.",
    "Remaining inventory is the declared quantity minus the recorded outflow; each affected lot is counted exactly once even under confluence or multiple recall starting points.",
]

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE ingestion_run (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    lot_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    raw_material_count INTEGER NOT NULL,
    total_declared_kg TEXT NOT NULL,
    total_flow_kg TEXT NOT NULL,
    total_remaining_kg TEXT NOT NULL,
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
    csv_header_line_number INTEGER,
    first_data_line_number INTEGER,
    record_count INTEGER NOT NULL
);

CREATE TABLE lot (
    lot_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL UNIQUE,
    declared_kg TEXT NOT NULL,
    declared_micro INTEGER NOT NULL CHECK (declared_micro > 0),
    incoming_kg TEXT NOT NULL,
    incoming_micro INTEGER NOT NULL CHECK (incoming_micro >= 0),
    outgoing_kg TEXT NOT NULL,
    outgoing_micro INTEGER NOT NULL CHECK (outgoing_micro >= 0),
    remaining_kg TEXT NOT NULL,
    remaining_micro INTEGER NOT NULL CHECK (remaining_micro >= 0),
    is_raw_material INTEGER NOT NULL CHECK (is_raw_material IN (0, 1)),
    csv_line_number INTEGER NOT NULL CHECK (csv_line_number >= 2)
);

CREATE TABLE flow (
    edge_id INTEGER PRIMARY KEY CHECK (edge_id >= 0),
    source_lot TEXT NOT NULL REFERENCES lot(lot_id),
    target_lot TEXT NOT NULL REFERENCES lot(lot_id),
    quantity_kg TEXT NOT NULL,
    quantity_micro INTEGER NOT NULL CHECK (quantity_micro > 0),
    csv_line_number INTEGER NOT NULL CHECK (csv_line_number >= 2),
    UNIQUE (source_lot, target_lot),
    CHECK (source_lot <> target_lot)
);

CREATE VIEW v_lot AS
SELECT lot_id, ordinal, declared_kg, incoming_kg, outgoing_kg, remaining_kg,
       is_raw_material, csv_line_number
FROM lot
ORDER BY ordinal;

CREATE VIEW v_flow AS
SELECT edge_id, source_lot, target_lot, quantity_kg, csv_line_number
FROM flow
ORDER BY edge_id;
"""


def _connect(path):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
                "lots_csv",
                source_paths["lots_csv"],
                1,
                2,
                len(model["lots"]),
            ),
            (
                "flows_csv",
                source_paths["flows_csv"],
                1,
                2,
                len(model["edges"]),
            ),
        ],
    )

    for ordinal, lot_id in enumerate(model["lot_order"]):
        record = model["lots"][lot_id]
        if record["ordinal"] != ordinal:
            raise RecallError("internal error: lot ordinal disagrees with topological order")
        cur.execute(
            "INSERT INTO lot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lot_id,
                ordinal,
                kg_text(record["declared_micro"]),
                record["declared_micro"],
                kg_text(record["incoming_micro"]),
                record["incoming_micro"],
                kg_text(record["outgoing_micro"]),
                record["outgoing_micro"],
                kg_text(record["remaining_micro"]),
                record["remaining_micro"],
                int(record["is_raw"]),
                record["line"],
            ),
        )

    for edge in model["edges"]:
        cur.execute(
            "INSERT INTO flow VALUES (?, ?, ?, ?, ?, ?)",
            (
                edge["edge_id"],
                edge["source"],
                edge["target"],
                kg_text(edge["quantity_micro"]),
                edge["quantity_micro"],
                edge["line"],
            ),
        )

    lots = model["lots"]
    cur.execute(
        "INSERT INTO ingestion_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            SCHEMA_VERSION,
            len(lots),
            len(model["edges"]),
            sum(1 for record in lots.values() if record["is_raw"]),
            kg_text(sum(record["declared_micro"] for record in lots.values())),
            kg_text(sum(edge["quantity_micro"] for edge in model["edges"])),
            kg_text(sum(record["remaining_micro"] for record in lots.values())),
            json.dumps(POLICY_NOTES, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()


def verify_connection(conn):
    """Run every SQL-side and model invariant; raise ``RecallError`` on breach."""
    conn.row_factory = sqlite3.Row
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RecallError("database verification failed: foreign key check found broken references")

    # 0. The stored run summary must recompute exactly from the lot/flow rows.
    run = conn.execute("SELECT * FROM ingestion_run WHERE id = 1").fetchone()
    if run is None:
        raise RecallError("database verification failed: ingestion_run summary is missing")
    if run["schema_version"] != SCHEMA_VERSION:
        raise RecallError(
            f"database verification failed: schema_version {run['schema_version']} "
            f"is not supported by this verifier ({SCHEMA_VERSION})"
        )
    if run["policy_notes"] != json.dumps(POLICY_NOTES, ensure_ascii=False, sort_keys=True):
        raise RecallError("database verification failed: policy notes were altered")

    lot_rows = {row["lot_id"]: row for row in conn.execute("SELECT * FROM lot").fetchall()}
    flow_rows = conn.execute("SELECT * FROM flow ORDER BY edge_id").fetchall()
    if run["lot_count"] != len(lot_rows):
        raise RecallError("database verification failed: ingestion_run.lot_count mismatch")
    if run["edge_count"] != len(flow_rows):
        raise RecallError("database verification failed: ingestion_run.edge_count mismatch")
    if run["raw_material_count"] != sum(1 for row in lot_rows.values() if row["is_raw_material"]):
        raise RecallError("database verification failed: ingestion_run.raw_material_count mismatch")

    # 1. Canonical kg text must agree with the exact micro-kg integers.
    for lot_id, row in lot_rows.items():
        for field in ("declared", "incoming", "outgoing", "remaining"):
            if row[f"{field}_kg"] != kg_text(row[f"{field}_micro"]):
                raise RecallError(
                    f"database verification failed: lot {lot_id!r} {field}_kg text "
                    "disagrees with its micro-kg integer"
                )
    for row in flow_rows:
        if row["quantity_kg"] != kg_text(row["quantity_micro"]):
            raise RecallError(
                f"database verification failed: edge {row['edge_id']} quantity_kg text "
                "disagrees with its micro-kg integer"
            )

    # 2. Exact SQL-side integer aggregates must reproduce every lot balance.
    incoming = {
        row["target_lot"]: row["total"]
        for row in conn.execute(
            "SELECT target_lot, SUM(quantity_micro) AS total FROM flow GROUP BY target_lot"
        )
    }
    outgoing = {
        row["source_lot"]: row["total"]
        for row in conn.execute(
            "SELECT source_lot, SUM(quantity_micro) AS total FROM flow GROUP BY source_lot"
        )
    }
    for lot_id, row in lot_rows.items():
        in_micro = incoming.get(lot_id, 0)
        out_micro = outgoing.get(lot_id, 0)
        if row["incoming_micro"] != in_micro or row["outgoing_micro"] != out_micro:
            raise RecallError(
                f"database verification failed: lot {lot_id!r} stored flow totals "
                "do not recompute from the edge table"
            )
        if row["remaining_micro"] != row["declared_micro"] - row["outgoing_micro"]:
            raise RecallError(
                f"database verification failed: lot {lot_id!r} remaining inventory "
                "is not declared minus outgoing"
            )
        if row["outgoing_micro"] > row["declared_micro"]:
            raise RecallError(
                f"database verification failed: lot {lot_id!r} is over-consumed"
            )
        if row["is_raw_material"] != (1 if in_micro == 0 else 0):
            raise RecallError(
                f"database verification failed: lot {lot_id!r} raw-material flag "
                "disagrees with its incoming edges"
            )
        if not row["is_raw_material"] and in_micro != row["declared_micro"]:
            raise RecallError(
                f"database verification failed: non-raw lot {lot_id!r} incoming "
                "total does not equal its declared quantity"
            )

    # 3. Run-level totals recompute exactly.
    if run["total_declared_kg"] != kg_text(sum(row["declared_micro"] for row in lot_rows.values())):
        raise RecallError("database verification failed: ingestion_run.total_declared_kg mismatch")
    if run["total_flow_kg"] != kg_text(sum(row["quantity_micro"] for row in flow_rows)):
        raise RecallError("database verification failed: ingestion_run.total_flow_kg mismatch")
    if run["total_remaining_kg"] != kg_text(sum(row["remaining_micro"] for row in lot_rows.values())):
        raise RecallError("database verification failed: ingestion_run.total_remaining_kg mismatch")

    # 4. Edge ids are dense in input order and lines are strictly increasing.
    if [row["edge_id"] for row in flow_rows] != list(range(len(flow_rows))):
        raise RecallError("database verification failed: edge ids are not dense from zero")
    lines = [row["csv_line_number"] for row in flow_rows]
    if any(later <= earlier for earlier, later in zip(lines, lines[1:])):
        raise RecallError("database verification failed: edge line numbers are not in input order")
    pairs = {(row["source_lot"], row["target_lot"]) for row in flow_rows}
    if len(pairs) != len(flow_rows):
        raise RecallError("database verification failed: duplicate (source, target) edge stored")

    # 5. The stored edges still form a DAG and the stored ordinals are the
    #    canonical topological order of that graph.
    edges = [
        {"source": row["source_lot"], "target": row["target_lot"]} for row in flow_rows
    ]
    try:
        order = topological_order(set(lot_rows), edges)
    except RecallError as exc:
        raise RecallError(f"database verification failed: {exc}") from exc
    for ordinal, lot_id in enumerate(order):
        if lot_rows[lot_id]["ordinal"] != ordinal:
            raise RecallError(
                "database verification failed: stored topological order is not canonical"
            )

    # 6. Stored source hashes match the archived bytes; locations resolve names.
    document_names = {
        row["name"] for row in conn.execute("SELECT name FROM source_document")
    }
    if document_names != {"lots_csv", "flows_csv"}:
        raise RecallError("database verification failed: unexpected source document set")
    sources = {}
    for row in conn.execute("SELECT name, sha256, byte_count, content FROM source_document"):
        stored = row["content"].encode("utf-8")
        if hashlib.sha256(stored).hexdigest() != row["sha256"] or row["byte_count"] != len(stored):
            raise RecallError(
                f"database verification failed: source document {row['name']!r} hash mismatch"
            )
        sources[row["name"]] = stored
    location_rows = conn.execute(
        "SELECT name, input_path, csv_header_line_number, first_data_line_number, "
        "record_count FROM source_location"
    ).fetchall()
    if {row["name"] for row in location_rows} != document_names:
        raise RecallError("database verification failed: source_location missing a document")
    for row in location_rows:
        if not row["input_path"] or row["record_count"] < 0:
            raise RecallError(
                f"database verification failed: source_location {row['name']!r} is incomplete"
            )
        if row["csv_header_line_number"] != 1 or row["first_data_line_number"] != 2:
            raise RecallError(
                f"database verification failed: source_location {row['name']!r} "
                "line markers were altered"
            )
    expected_records = {"lots_csv": len(lot_rows), "flows_csv": len(flow_rows)}
    for row in location_rows:
        if row["record_count"] != expected_records[row["name"]]:
            raise RecallError(
                f"database verification failed: source_location {row['name']!r} "
                "record count mismatch"
            )

    # 7. Rebuild the whole model from the archived documents and compare rows.
    rebuilt = build_model(sources["lots_csv"], sources["flows_csv"])
    if rebuilt["lot_order"] != [row["lot_id"] for row in sorted(lot_rows.values(), key=lambda r: r["ordinal"])]:
        raise RecallError("database verification failed: rebuilt topological order differs")
    for lot_id, record in rebuilt["lots"].items():
        row = lot_rows.get(lot_id)
        if row is None:
            raise RecallError("database verification failed: rebuilt lot missing from storage")
        expected = (
            record["ordinal"],
            kg_text(record["declared_micro"]),
            record["declared_micro"],
            kg_text(record["incoming_micro"]),
            record["incoming_micro"],
            kg_text(record["outgoing_micro"]),
            record["outgoing_micro"],
            kg_text(record["remaining_micro"]),
            record["remaining_micro"],
            int(record["is_raw"]),
            record["line"],
        )
        stored = (
            row["ordinal"],
            row["declared_kg"],
            row["declared_micro"],
            row["incoming_kg"],
            row["incoming_micro"],
            row["outgoing_kg"],
            row["outgoing_micro"],
            row["remaining_kg"],
            row["remaining_micro"],
            row["is_raw_material"],
            row["csv_line_number"],
        )
        if stored != expected:
            raise RecallError(
                f"database verification failed: rebuilt lot {lot_id!r} differs from storage"
            )
    if len(rebuilt["edges"]) != len(flow_rows):
        raise RecallError("database verification failed: rebuilt edge count mismatch")
    for edge, row in zip(rebuilt["edges"], flow_rows):
        expected = (
            edge["edge_id"],
            edge["source"],
            edge["target"],
            kg_text(edge["quantity_micro"]),
            edge["quantity_micro"],
            edge["line"],
        )
        stored = (
            row["edge_id"],
            row["source_lot"],
            row["target_lot"],
            row["quantity_kg"],
            row["quantity_micro"],
            row["csv_line_number"],
        )
        if stored != expected:
            raise RecallError("database verification failed: rebuilt flows differ from storage")

    return {
        "lots": len(lot_rows),
        "edges": len(flow_rows),
        "raw_materials": sum(1 for row in lot_rows.values() if row["is_raw_material"]),
        "total_declared_kg": run["total_declared_kg"],
        "total_remaining_kg": run["total_remaining_kg"],
    }


def _open_read_only(path, purpose):
    target = Path(path)
    if target.is_symlink():
        raise RecallError(f"database path {target} is a symbolic link; refusing to follow it")
    if not target.is_file():
        raise RecallError(f"database {target} does not exist")
    uri = target.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        run = conn.execute(
            "SELECT schema_version FROM ingestion_run WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        conn.close()
        raise RecallError(f"database {target} is not a readable genealogy database")
    if run is None:
        conn.close()
        raise RecallError(f"database {target} has no ingestion_run summary")
    if run[0] != SCHEMA_VERSION:
        conn.close()
        raise RecallError(
            f"database {target} has schema_version {run[0]}; this {purpose} "
            f"supports {SCHEMA_VERSION}"
        )
    return conn


def verify_database(path):
    """Open an existing genealogy database read-only and verify every invariant."""
    conn = _open_read_only(path, "verifier")
    try:
        return verify_connection(conn)
    finally:
        conn.close()


def recall_from_database(path, lot_ids):
    """Run a read-only recall against an existing genealogy database.

    Only the database is consulted; the original CSV files are not needed.
    """
    conn = _open_read_only(path, "recall query")
    try:
        conn.row_factory = sqlite3.Row
        lots = {
            row["lot_id"]: {
                "ordinal": row["ordinal"],
                "is_raw": bool(row["is_raw_material"]),
                "declared_micro": row["declared_micro"],
                "outgoing_micro": row["outgoing_micro"],
                "remaining_micro": row["remaining_micro"],
                "line": row["csv_line_number"],
            }
            for row in conn.execute("SELECT * FROM lot").fetchall()
        }
        edges = [
            {
                "edge_id": row["edge_id"],
                "source": row["source_lot"],
                "target": row["target_lot"],
                "quantity_micro": row["quantity_micro"],
                "line": row["csv_line_number"],
            }
            for row in conn.execute("SELECT * FROM flow ORDER BY edge_id").fetchall()
        ]
    finally:
        conn.close()
    result = compute_recall(lots, edges, lot_ids)
    return {
        "database": str(path),
        "schema_version": SCHEMA_VERSION,
        **result,
    }


def build_database(lots_path, flows_path, output_path):
    """Capture inputs, build, verify and atomically publish the SQLite database."""
    target = Path(output_path)
    if target.is_symlink():
        raise RecallError(f"output path {target} is a symbolic link; refusing to follow it")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    sources = {
        "lots_csv": read_bytes(lots_path, "lots CSV"),
        "flows_csv": read_bytes(flows_path, "flows CSV"),
    }
    # Record the invocation-supplied locations verbatim; the archived bytes,
    # not these paths, are what verification re-parses, so moving the inputs
    # afterwards is safe.
    source_paths = {
        "lots_csv": str(lots_path),
        "flows_csv": str(flows_path),
    }
    model = build_model(sources["lots_csv"], sources["flows_csv"])

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
