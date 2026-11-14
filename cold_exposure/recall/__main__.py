"""Command line entry for the batch genealogy recall library.

    python -m cold_exposure.recall build \
        --lots examples/recall/lots.csv \
        --flows examples/recall/flows.csv \
        --output run/recall/genealogy.db

    python -m cold_exposure.recall recall \
        --database run/recall/genealogy.db --lot RAW-MILK [--lot RAW-SUGAR]

    python -m cold_exposure.recall verify --database run/recall/genealogy.db

The legacy v1 (``python -m cold_exposure``), v2
(``python -m cold_exposure.consensus``), flash and hand-off entries are
untouched.
"""
import argparse
import json
import sqlite3
from pathlib import Path

from .errors import RecallError
from .model import kg_text
from .store import build_database, recall_from_database, verify_database


def _build(args):
    model = build_database(args.lots, args.flows, args.output)
    lots = model["lots"]
    summary = {
        "database": str(args.output),
        "lots": len(lots),
        "edges": len(model["edges"]),
        "raw_materials": sum(1 for record in lots.values() if record["is_raw"]),
        "total_declared_kg": kg_text(
            sum(record["declared_micro"] for record in lots.values())
        ),
        "total_remaining_kg": kg_text(
            sum(record["remaining_micro"] for record in lots.values())
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def _recall(args):
    result = recall_from_database(args.database, args.lot)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def _verify(args):
    result = verify_database(args.database)
    print(
        json.dumps(
            {"database": str(args.database), "verified": True, **result},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Cold-chain batch genealogy recall library: validate split/mix "
            "genealogies into a SQLite database and run read-only whole-lot "
            "recall queries (synthetic recall evidence, not a liability ruling)"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="build (or rebuild) the genealogy database atomically"
    )
    build.add_argument("--lots", required=True, type=Path,
                       help="UTF-8 CSV: lot_id,quantity_kg")
    build.add_argument("--flows", required=True, type=Path,
                       help="UTF-8 CSV: source_lot,target_lot,quantity_kg")
    build.add_argument("--output", required=True, type=Path,
                       help="SQLite database path (temporary build, then atomic replace)")
    build.set_defaults(handler=_build)

    recall = subparsers.add_parser(
        "recall", help="read-only recall: affected lots and remaining inventory"
    )
    recall.add_argument("--database", required=True, type=Path,
                        help="SQLite database previously produced by 'build'")
    recall.add_argument("--lot", action="append", required=True, metavar="LOT_ID",
                        help="recalled lot id; repeat for several starting points")
    recall.set_defaults(handler=_recall)

    verify = subparsers.add_parser(
        "verify", help="re-audit an existing genealogy database in read-only mode"
    )
    verify.add_argument("--database", required=True, type=Path,
                        help="SQLite database previously produced by 'build'")
    verify.set_defaults(handler=_verify)

    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (RecallError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"recall failed: {exc}\n")


if __name__ == "__main__":
    main()
