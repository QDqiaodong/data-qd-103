import argparse
import json
from pathlib import Path
from .pipeline import run


def main():
    parser=argparse.ArgumentParser(description="Audit synthetic cold-chain logger exposure")
    parser.add_argument("--trips",type=Path,default=Path("data/trips.csv"))
    parser.add_argument("--readings",type=Path,default=Path("data/readings.csv"))
    parser.add_argument("--output",type=Path,default=Path("run/report"))
    parser.add_argument("--max-gap-seconds",type=int,default=600)
    args=parser.parse_args()
    try:report=run(args.trips,args.readings,args.output,args.max_gap_seconds)
    except (ValueError,OSError) as exc:parser.exit(2,f"exposure failed: {exc}\n")
    print(json.dumps([{k:r[k] for k in ["trip_id","status","seconds","known_coverage","outside_degree_minutes"]} for r in report["trips"]],sort_keys=True))


if __name__=="__main__":main()
