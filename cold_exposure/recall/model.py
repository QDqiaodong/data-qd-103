"""Validation and in-memory model for the batch genealogy recall library.

Inputs are two UTF-8 CSV documents captured once as bytes:

- ``lots.csv``  with the exact header ``lot_id,quantity_kg``
- ``flows.csv`` with the exact header ``source_lot,target_lot,quantity_kg``

The same captured bytes feed SHA-256 hashing, parsing and the database source
archive, so the original files can be moved away after a build and every query
and re-audit still runs from the archived content alone.

Quantities are positive plain decimals with 1-12 integer digits and at most
six decimal places (for example ``12`` or ``0.000001``). They are never parsed
through ``float``: each literal is matched by a strict regular expression and
scaled into an exact integer number of micro-kilograms (1 kg = 1,000,000
micro-kg). All sums, comparisons and remainders are integer arithmetic, so no
rounding mode or precision context is involved anywhere.

Structural contract (validated in this order, every rejection names the file
and the physical CSV line, header being line 1):

1. Lots are unique; edges are unique as ``(source_lot, target_lot)`` pairs.
2. Every edge endpoint references a declared lot; self-loops are rejected.
3. The genealogy must be acyclic. Edge rows may appear in any order; a
   deterministic topological order (smallest ``lot_id`` first among the
   available lots) is computed regardless of the input row order.
4. A lot with no incoming edge is a raw material. Any other lot's incoming
   flow total must equal its declared quantity exactly.
5. Each lot's outgoing flow total must not exceed its declared quantity, so
   the remaining inventory ``declared - outgoing`` is never negative.

Recall semantics: risk propagates along edges to *entire* descendant lots.
Flow kilograms are never treated as a contamination mass, and each affected
lot contributes its remaining inventory exactly once even when several paths
or several recall starting points reach it.
"""
import csv
import heapq
import io
import re

from .errors import RecallError

LOT_HEADERS = ["lot_id", "quantity_kg"]
FLOW_HEADERS = ["source_lot", "target_lot", "quantity_kg"]

MICRO_PER_KG = 1_000_000

_QUANTITY_PATTERN = re.compile(r"^([0-9]{1,12})(?:\.([0-9]{1,6}))?$")


def kg_text(micro):
    """Canonical six-decimal-place kg text for a non-negative micro-kg count."""
    if micro < 0:
        raise RecallError(f"internal error: negative micro-kg value {micro}")
    whole, fraction = divmod(micro, MICRO_PER_KG)
    return f"{whole}.{fraction:06d}"


def parse_quantity(text, where):
    """Parse one contract quantity into exact integer micro-kilograms."""
    match = _QUANTITY_PATTERN.match(text if text is not None else "")
    if match is None:
        raise RecallError(
            f"{where}: quantity_kg must be a plain positive decimal with 1-12 "
            f"integer digits and at most six decimal places (no sign, exponent "
            f"or whitespace), got {text!r}"
        )
    micro = int(match.group(1)) * MICRO_PER_KG
    fraction = match.group(2)
    if fraction is not None:
        micro += int(fraction.ljust(6, "0"))
    if micro <= 0:
        raise RecallError(f"{where}: quantity_kg must be positive, got {text!r}")
    return micro


def read_bytes(path, label):
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except OSError as exc:
        raise RecallError(f"{label}: cannot read {path}: {exc}") from exc


def _parse_id(value, field, where):
    if not value or value != value.strip():
        raise RecallError(
            f"{where}: {field} must be non-empty without leading/trailing whitespace"
        )
    if "\n" in value or "\r" in value:
        raise RecallError(f"{where}: {field} must not contain a line break")
    return value


def _csv_rows(data, headers, file_name):
    """Yield ``(physical_line_number, row)`` for each data record."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecallError(f"{file_name}: file is not valid UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != headers:
        raise RecallError(
            f"{file_name}: CSV header must be exactly {','.join(headers)}, got "
            f"{None if reader.fieldnames is None else ','.join(reader.fieldnames)}"
        )
    rows = []
    for row in reader:
        line_number = reader.line_num
        if None in row or any(value is None or value == "" for value in row.values()):
            raise RecallError(f"{file_name}: line {line_number}: missing or extra CSV field")
        rows.append((line_number, row))
    return rows


def parse_lots_bytes(data, file_name="lots"):
    """Parse and validate ``lots.csv`` bytes into a lot-id keyed mapping."""
    lots = {}
    for line_number, row in _csv_rows(data, LOT_HEADERS, file_name):
        where = f"{file_name}: line {line_number}"
        lot_id = _parse_id(row["lot_id"], "lot_id", where)
        if lot_id in lots:
            raise RecallError(f"{where}: duplicate lot_id {lot_id!r}")
        lots[lot_id] = {
            "declared_micro": parse_quantity(row["quantity_kg"], where),
            "line": line_number,
        }
    if not lots:
        raise RecallError(f"{file_name}: at least one lot is required")
    return lots


def parse_flows_bytes(data, lots, file_name="flows"):
    """Parse and validate ``flows.csv`` bytes against the declared lots."""
    edges = []
    seen_pairs = set()
    for line_number, row in _csv_rows(data, FLOW_HEADERS, file_name):
        where = f"{file_name}: line {line_number}"
        source = _parse_id(row["source_lot"], "source_lot", where)
        target = _parse_id(row["target_lot"], "target_lot", where)
        if source == target:
            raise RecallError(f"{where}: self-loop on lot {source!r} is not allowed")
        quantity_micro = parse_quantity(row["quantity_kg"], where)
        if (source, target) in seen_pairs:
            raise RecallError(
                f"{where}: duplicate edge {source!r} -> {target!r}; split or "
                "mixed quantities of the same pair must be a single row"
            )
        seen_pairs.add((source, target))
        if source not in lots:
            raise RecallError(
                f"{where}: unknown source_lot {source!r}; the lot must be declared in lots"
            )
        if target not in lots:
            raise RecallError(
                f"{where}: unknown target_lot {target!r}; the lot must be declared in lots"
            )
        edges.append(
            {
                "edge_id": len(edges),
                "source": source,
                "target": target,
                "quantity_micro": quantity_micro,
                "line": line_number,
            }
        )
    return edges


def topological_order(lots, edges):
    """Deterministic topological order; raises ``RecallError`` on any cycle.

    Among the lots available at each step the smallest ``lot_id`` is taken,
    so the result depends only on the graph, never on the input row order.
    """
    indegree = {lot_id: 0 for lot_id in lots}
    adjacency = {lot_id: [] for lot_id in lots}
    for edge in edges:
        indegree[edge["target"]] += 1
        adjacency[edge["source"]].append(edge["target"])
    available = [lot_id for lot_id, degree in indegree.items() if degree == 0]
    heapq.heapify(available)
    order = []
    while available:
        node = heapq.heappop(available)
        order.append(node)
        for target in adjacency[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(available, target)
    if len(order) != len(lots):
        remaining = sorted(lot_id for lot_id, degree in indegree.items() if degree > 0)
        raise RecallError(
            "the genealogy contains a cycle; these lots are on or downstream of "
            "a cycle: " + ", ".join(repr(lot_id) for lot_id in remaining)
        )
    return order


def build_model(lots_data, flows_data):
    """Validate both captured CSV documents and return the in-memory model."""
    lots = parse_lots_bytes(lots_data)
    edges = parse_flows_bytes(flows_data, lots)

    # Structural soundness comes first: the genealogy must be a DAG before
    # per-lot flow balances are meaningful.
    order = topological_order(lots, edges)
    for ordinal, lot_id in enumerate(order):
        lots[lot_id]["ordinal"] = ordinal

    incoming = {lot_id: 0 for lot_id in lots}
    outgoing = {lot_id: 0 for lot_id in lots}
    for edge in edges:
        incoming[edge["target"]] += edge["quantity_micro"]
        outgoing[edge["source"]] += edge["quantity_micro"]

    for lot_id, record in lots.items():
        declared = record["declared_micro"]
        record["incoming_micro"] = incoming[lot_id]
        record["outgoing_micro"] = outgoing[lot_id]
        record["is_raw"] = incoming[lot_id] == 0
        if not record["is_raw"] and incoming[lot_id] != declared:
            raise RecallError(
                f"lot {lot_id!r} (lots: line {record['line']}): incoming flow total "
                f"{kg_text(incoming[lot_id])} kg must equal the declared quantity "
                f"{kg_text(declared)} kg; only raw materials (no incoming edges) "
                "may declare more than they receive"
            )
        if outgoing[lot_id] > declared:
            raise RecallError(
                f"lot {lot_id!r} (lots: line {record['line']}): outgoing flow total "
                f"{kg_text(outgoing[lot_id])} kg exceeds the declared quantity "
                f"{kg_text(declared)} kg"
            )
        record["remaining_micro"] = declared - outgoing[lot_id]

    return {"lots": lots, "lot_order": order, "edges": edges}


def compute_recall(lots, edges, recalled_ids):
    """Propagate a recall to entire descendant lots and sum remaining stock.

    ``lots`` maps each lot id to a record with ``ordinal``, ``is_raw``,
    ``declared_micro``, ``outgoing_micro``, ``remaining_micro`` and ``line``;
    ``edges`` is a list of records with ``source``, ``target``,
    ``quantity_micro`` and ``line`` in a stable order. Every affected lot is
    counted exactly once regardless of how many paths or starting points
    reach it; flow quantities never act as a contamination mass.
    """
    unique_recalled = sorted(set(recalled_ids))
    if not unique_recalled:
        raise RecallError("at least one recalled lot must be given")
    unknown = [lot_id for lot_id in unique_recalled if lot_id not in lots]
    if unknown:
        raise RecallError(
            "unknown lot_id(s): " + ", ".join(repr(lot_id) for lot_id in unknown)
        )

    adjacency = {lot_id: [] for lot_id in lots}
    for edge in edges:
        adjacency[edge["source"]].append(edge["target"])
    affected = set()
    stack = list(unique_recalled)
    while stack:
        node = stack.pop()
        if node in affected:
            continue
        affected.add(node)
        stack.extend(adjacency[node])

    ordered = sorted(affected, key=lambda lot_id: lots[lot_id]["ordinal"])
    total_micro = sum(lots[lot_id]["remaining_micro"] for lot_id in affected)
    return {
        "recalled_lots": unique_recalled,
        "affected_lots": [
            {
                "lot_id": lot_id,
                "recall_root": lot_id in unique_recalled,
                "is_raw_material": lots[lot_id]["is_raw"],
                "declared_kg": kg_text(lots[lot_id]["declared_micro"]),
                "outgoing_kg": kg_text(lots[lot_id]["outgoing_micro"]),
                "remaining_kg": kg_text(lots[lot_id]["remaining_micro"]),
                "lots_csv_line": lots[lot_id]["line"],
            }
            for lot_id in ordered
        ],
        "affected_edges": [
            {
                "source_lot": edge["source"],
                "target_lot": edge["target"],
                "quantity_kg": kg_text(edge["quantity_micro"]),
                "flows_csv_line": edge["line"],
            }
            for edge in edges
            if edge["source"] in affected
        ],
        "affected_lot_count": len(affected),
        "total_remaining_kg": kg_text(total_micro),
    }
