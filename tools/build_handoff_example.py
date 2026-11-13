#!/usr/bin/env python3
"""Deterministically regenerate the synthetic hand-off marketplace example.

Everything produced here is original synthetic data invented for this project:
the carriers, trips, loggers and hand-offs are fictional and no real shipment,
device, carrier or liability decision is involved. Re-running the script with
the same arguments reproduces every file byte for byte; there is no clock
reading, randomness or machine-specific data.

Usage:
    python tools/build_handoff_example.py --output-dir examples/handoff/generated

The directory must not exist. It receives the schema-version-2 consensus
report for the three synthetic trips and a copy of the hand-off CSV, which can
then be materialized:

    python -m cold_exposure.handoff build \\
        --consensus examples/handoff/generated/consensus.json \\
        --handoffs examples/handoff/generated/handoffs.csv \\
        --output run/handoff/marketplace.db
"""
import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cold_exposure.consensus_pipeline import build_report  # noqa: E402

SOURCE_DIR = REPO_ROOT / "examples" / "handoff" / "source"
HANDOFFS = REPO_ROOT / "examples" / "handoff" / "handoffs.csv"
MAX_GAP_SECONDS = 3600


def build(output_dir):
    target = Path(output_dir)
    if target.is_symlink():
        raise SystemExit(f"refusing to write through a symbolic link: {target}")
    if target.exists():
        raise SystemExit(f"output directory already exists: {target}")
    target.mkdir(parents=True)

    _, serialized = build_report(
        SOURCE_DIR / "trips.csv",
        SOURCE_DIR / "assignments.csv",
        SOURCE_DIR / "readings.csv",
        MAX_GAP_SECONDS,
    )
    (target / "consensus.json").write_text(serialized, encoding="utf-8")
    shutil.copyfile(HANDOFFS, target / "handoffs.csv")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    target = build(args.output_dir)
    print(f"wrote hand-off example inputs to {target}")


if __name__ == "__main__":
    main()
