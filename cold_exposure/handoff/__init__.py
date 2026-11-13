"""Hand-off fulfillment marketplace (independent SQLite entry).

This subpackage attributes the existing multi-logger consensus evidence
(schema-version-2 JSON) across custody hand-offs and UTC days. It only
materializes synthetic fulfillment evidence; it never determines compensation
or liability. The legacy v1/v2 entries are unchanged.
"""
from .attribution import build_facts, build_model, check_conservation, materialize_carrier_days
from .errors import HandoffError
from .model import parse_consensus_bytes, parse_handoff_bytes, read_bytes
from .store import POLICY_NOTES, SCHEMA_VERSION, build_database, verify_database

__all__ = [
    "HandoffError",
    "POLICY_NOTES",
    "SCHEMA_VERSION",
    "build_database",
    "build_facts",
    "build_model",
    "check_conservation",
    "materialize_carrier_days",
    "parse_consensus_bytes",
    "parse_handoff_bytes",
    "read_bytes",
    "verify_database",
]
