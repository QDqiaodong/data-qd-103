"""Timezone-aware trip windows and finite logger readings."""
import csv
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation


def stamp(value):
    moment=datetime.fromisoformat(value.replace("Z","+00:00"))
    if moment.tzinfo is None:raise ValueError("timezone and whole-second precision required")
    moment=moment.astimezone(timezone.utc)
    if moment.microsecond:raise ValueError("whole-second UTC precision required")
    return moment


def decimal(value):
    try:result=Decimal(value)
    except InvalidOperation as exc:raise ValueError("invalid temperature") from exc
    if not result.is_finite() or not -100<=result<=150:raise ValueError("temperature outside [-100,150] Celsius")
    return result


def rows(path,headers):
    with open(path,encoding="utf-8",newline="") as stream:
        reader=csv.DictReader(stream)
        if reader.fieldnames!=headers:raise ValueError("CSV columns differ from contract")
        result=list(reader)
    if any(None in r or any(not v for v in r.values()) for r in result):raise ValueError("missing CSV fields")
    return result


def load(trips_path,readings_path):
    trips=[];seen=set()
    for r in rows(trips_path,["trip_id","sensor_id","started_at","ended_at","min_c","max_c"]):
        start,end=stamp(r["started_at"]),stamp(r["ended_at"])
        low,high=decimal(r["min_c"]),decimal(r["max_c"])
        if r["trip_id"] in seen or end<=start or low>=high:raise ValueError("invalid trip identity/window/limits")
        seen.add(r["trip_id"])
        trips.append({"trip_id":r["trip_id"],"sensor_id":r["sensor_id"],"start":start,"end":end,"low":low,"high":high})
    if not trips:raise ValueError("no trips")
    sensors={r["sensor_id"] for r in trips}
    readings={s:[] for s in sensors};seen=set()
    for r in rows(readings_path,["sensor_id","observed_at","temperature_c"]):
        key=(r["sensor_id"],stamp(r["observed_at"]))
        if key[0] not in sensors or key in seen:raise ValueError("unknown sensor or duplicate reading")
        seen.add(key)
        readings[key[0]].append((key[1],decimal(r["temperature_c"])))
    return trips,{s:sorted(values) for s,values in readings.items()}
