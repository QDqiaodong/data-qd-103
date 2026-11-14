"""Batch genealogy recall library (independent SQLite entry).

This subpackage validates a split/mix batch genealogy (``lots.csv`` +
``flows.csv``), archives the original documents and the derived balances in
SQLite, and answers read-only recall queries: from one or more recalled lots,
risk propagates along the flow edges to *entire* descendant lots, and the
remaining inventory (declared minus recorded outflow) of every affected lot
is reported exactly once. The legacy v1/v2, flash and hand-off entries are
unchanged.
"""
from .errors import RecallError
from .model import (
    build_model,
    compute_recall,
    kg_text,
    parse_flows_bytes,
    parse_lots_bytes,
    parse_quantity,
    read_bytes,
    topological_order,
)
from .store import (
    POLICY_NOTES,
    SCHEMA_VERSION,
    build_database,
    recall_from_database,
    verify_database,
)

__all__ = [
    "POLICY_NOTES",
    "RecallError",
    "SCHEMA_VERSION",
    "build_database",
    "build_model",
    "compute_recall",
    "kg_text",
    "parse_flows_bytes",
    "parse_lots_bytes",
    "parse_quantity",
    "read_bytes",
    "recall_from_database",
    "topological_order",
    "verify_database",
]
