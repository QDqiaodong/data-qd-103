"""Piecewise-constant exposure, bounded gaps, no fabricated trailing sample."""
from decimal import Decimal


def segments(trip,readings,max_gap_seconds):
    if type(max_gap_seconds) is not int or not 1<=max_gap_seconds<=86400:raise ValueError("max gap must be 1..86400 seconds")
    start,end=trip["start"],trip["end"]
    parts=[];cursor=start
    def append(a,b,value):
        if b<=a:return
        kind="unknown" if value is None else "below" if value<trip["low"] else "above" if value>trip["high"] else "within"
        parts.append({"start":a,"end":b,"value":value,"kind":kind})
    for (a,value),(b,_) in zip(readings,readings[1:]):
        left,right=max(a,start),min(b,end)
        if right<=left:continue
        if left>cursor:append(cursor,left,None)
        append(left,right,value if (b-a).total_seconds()<=max_gap_seconds else None)
        cursor=right
    if cursor<end:append(cursor,end,None)
    return parts


def analyze_trip(trip,readings,max_gap_seconds):
    parts=segments(trip,readings,max_gap_seconds)
    totals={kind:0 for kind in ["within","above","below","unknown"]}
    weighted=Decimal(0);degree_seconds=Decimal(0)
    longest=0;streak=0;previous_kind=None;previous_end=None
    evidence=[]
    for part in parts:
        seconds=int((part["end"]-part["start"]).total_seconds())
        kind=part["kind"];value=part["value"]
        totals[kind]+=seconds
        if value is not None:
            weighted+=value*seconds
            degree_seconds+=max(trip["low"]-value,value-trip["high"],Decimal(0))*seconds
        if kind in {"above","below"}:
            streak=streak+seconds if kind==previous_kind and previous_end==part["start"] else seconds
            longest=max(longest,streak)
        else:streak=0
        previous_kind,previous_end=kind,part["end"]
        evidence.append({"started_at":part["start"].isoformat(),"ended_at":part["end"].isoformat(),"seconds":seconds,"kind":kind,"temperature_c":None if value is None else str(value)})
    duration=int((trip["end"]-trip["start"]).total_seconds())
    assert sum(totals.values())==duration
    known=duration-totals["unknown"]
    status="excursion" if totals["above"]+totals["below"] else "incomplete" if totals["unknown"] else "within_limits"
    def fmt(value):return format(value.quantize(Decimal("0.000001")),"f")
    return {"trip_id":trip["trip_id"],"sensor_id":trip["sensor_id"],"duration_seconds":duration,"seconds":totals,
            "known_coverage":known/duration,"mean_known_c":fmt(weighted/known) if known else None,
            "outside_degree_minutes":fmt(degree_seconds/60),"longest_same_side_excursion_seconds":longest,
            "status":status,"segments":evidence}
