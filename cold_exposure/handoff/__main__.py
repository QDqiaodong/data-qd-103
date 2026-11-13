"""Command line entry for the hand-off fulfillment marketplace.

    python -m cold_exposure.handoff build \
        --consensus run/consensus/report.json \
        --handoffs data/handoffs/handoffs.csv \
        --output run/handoff/marketplace.db

    python -m cold_exposure.handoff verify --database run/handoff/marketplace.db

The legacy v1 (``python -m cold_exposure``) and v2
(``python -m cold_exposure.consensus``) entries are untouched.
"""
import argparse
import json
from pathlib import Path

from .errors import HandoffError
from .store import build_database, verify_database


def _build(args):
    model = build_database(args.consensus, args.handoffs, args.output)
    summary = {
        "database": str(args.output),
        "trips": len(model["trips"]),
        "handoffs": len(model["handoffs"]),
        "attribution_facts": len(model["facts"]),
        "carrier_days": len(model["carrier_days"]),
        "unassigned_seconds": sum(
            fact["seconds"] for fact in model["facts"] if fact["is_unassigned"]
        ),
        "unknown_seconds": sum(
            fact["unknown_seconds"] for fact in model["facts"]
        ),
    }
    print(json.dumps(summary, sort_keys=True))


def _verify(args):
    result = verify_database(args.database)
    print(json.dumps({"database": str(args.database), "verified": True, **result},
                     sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Cold-chain hand-off fulfillment marketplace: attribute the existing v2 "
            "consensus evidence across custody hand-offs into a SQLite database "
            "(synthetic fulfillment evidence, not a liability ruling)"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="build (or rebuild) the marketplace database atomically"
    )
    build.add_argument("--consensus", required=True, type=Path,
                       help="existing schema-version-2 consensus report JSON")
    build.add_argument("--handoffs", required=True, type=Path,
                       help="UTF-8 CSV: handoff_id,trip_id,carrier,started_at,ended_at")
    build.add_argument("--output", required=True, type=Path,
                       help="SQLite database path (temporary build, then atomic replace)")
    build.set_defaults(handler=_build)

    verify = subparsers.add_parser(
        "verify", help="re-audit an existing marketplace database in read-only mode"
    )
    verify.add_argument("--database", required=True, type=Path,
                        help="SQLite database previously produced by 'build'")
    verify.set_defaults(handler=_verify)

    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (HandoffError, ValueError, OSError) as exc:
        parser.exit(2, f"handoff failed: {exc}\n")


if __name__ == "__main__":
    main()
