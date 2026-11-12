import csv
import hashlib
import json
from pathlib import Path
from .input import load
from .exposure import analyze_trip


def run(trips_path,readings_path,output,max_gap_seconds=600):
    trips,readings=load(trips_path,readings_path)
    results=[analyze_trip(t,readings[t["sensor_id"]],max_gap_seconds) for t in sorted(trips,key=lambda t:t["trip_id"])]
    report={"schema_version":1,"model":"left-held bounded intervals","max_gap_seconds":max_gap_seconds,
            "inputs_sha256":{name:hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in [("trips",trips_path),("readings",readings_path)]},"trips":results}
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    (output/"exposure.json").write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    with (output/"summary.csv").open("w",newline="",encoding="utf-8") as stream:
        writer=csv.DictWriter(stream,fieldnames=["trip_id","duration_seconds","known_coverage","mean_known_c","outside_degree_minutes","status"])
        writer.writeheader();writer.writerows({k:r[k] for k in writer.fieldnames} for r in results)
    return report
