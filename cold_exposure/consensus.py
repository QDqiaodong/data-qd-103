"""Command line entry for offline multi-logger consensus analysis.

Run as ``python -m cold_exposure.consensus --trips ... --assignments ...
--readings ... --max-gap-seconds 600 --output report.json``.
"""
import argparse
import json
from pathlib import Path

from .consensus_input import ConsensusInputError
from .consensus_pipeline import run_consensus


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline redundant-logger consensus (synthetic demonstration policy)"
    )
    parser.add_argument("--trips", type=Path, required=True, help="UTF-8 CSV of trip windows and q")
    parser.add_argument("--assignments", type=Path, required=True, help="UTF-8 CSV of sensor assignments")
    parser.add_argument("--readings", type=Path, required=True, help="UTF-8 CSV of logger readings")
    parser.add_argument(
        "--max-gap-seconds",
        type=int,
        default=600,
        help="maximum allowed raw sample gap, integer in 1..86400 (default 600)",
    )
    parser.add_argument("--output", type=Path, required=True, help="JSON report path (atomic replace)")
    args = parser.parse_args(argv)
    try:
        report = run_consensus(
            args.trips, args.assignments, args.readings, args.output, args.max_gap_seconds
        )
    except (ConsensusInputError, ValueError, OSError) as exc:
        parser.exit(2, f"consensus failed: {exc}\n")
    print(
        json.dumps(
            [
                {
                    "trip_id": trip["trip_id"],
                    "status": trip["status"],
                    "classification_seconds": trip["classification_seconds"],
                    "definitely_outside_seconds": trip["definitely_outside_seconds"],
                    "possibly_outside_seconds": trip["possibly_outside_seconds"],
                }
                for trip in report["trips"]
            ],
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
