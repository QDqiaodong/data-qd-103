import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from cold_exposure.input import load,stamp
from cold_exposure.exposure import analyze_trip
from cold_exposure.pipeline import run

DATA=Path(__file__).resolve().parents[1]/"data"


class ExposureTests(unittest.TestCase):
    def setUp(self):
        self.trips,self.readings=load(DATA/"trips.csv",DATA/"readings.csv")

    def test_time_weighting_excursion_and_longest_streak(self):
        r=analyze_trip(self.trips[0],self.readings["S1"],600)
        self.assertEqual(r["seconds"],{"within":600,"above":1200,"below":0,"unknown":0})
        self.assertEqual(r["mean_known_c"],"8.000000")
        self.assertEqual(r["outside_degree_minutes"],"30.000000")
        self.assertEqual(r["longest_same_side_excursion_seconds"],1200)
        self.assertEqual(r["status"],"excursion")

    def test_gap_and_no_trailing_extrapolation(self):
        r=analyze_trip(self.trips[1],self.readings["S2"],600)
        self.assertEqual(r["seconds"]["unknown"],1200)
        self.assertIsNone(r["mean_known_c"])
        self.assertEqual(r["status"],"incomplete")
        r=analyze_trip(self.trips[0],self.readings["S1"][:-1],600)
        self.assertEqual(r["seconds"]["unknown"],600)

    def test_clipping_to_trip_window(self):
        t={**self.trips[0],"start":stamp("2026-04-01T00:05:00Z"),"end":stamp("2026-04-01T00:15:00Z")}
        r=analyze_trip(t,self.readings["S1"],600)
        self.assertEqual(r["seconds"]["within"],300)
        self.assertEqual(r["seconds"]["above"],300)
        self.assertEqual(r["outside_degree_minutes"],"10.000000")

    def test_limits_inclusive_empty_and_single_reading(self):
        t=self.trips[0]
        for value in [Decimal(2),Decimal(8)]:
            r=analyze_trip(t,[(t["start"],value),(t["end"],value)],1800)
            self.assertEqual(r["status"],"within_limits")
        for values in [[],[(t["start"],Decimal(5))]]:
            self.assertEqual(analyze_trip(t,values,600)["known_coverage"],0)

    def test_unknown_breaks_excursion_streak(self):
        t=self.trips[0];start=t["start"]
        values=[(start,Decimal(10)),(start+timedelta(minutes=5),Decimal(10)),(start+timedelta(minutes=20),Decimal(10)),(t["end"],Decimal(10))]
        r=analyze_trip(t,values,600)
        self.assertEqual(r["longest_same_side_excursion_seconds"],600)
        self.assertEqual(r["seconds"]["unknown"],900)

    def test_duplicate_nonfinite_and_timezone_free_input(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"readings.csv";original=(DATA/"readings.csv").read_text()
            for content in [original+original.splitlines()[1]+"\n",original.replace(",9\n",",NaN\n"),original.replace("00:20:00Z","00:20:00")]:
                path.write_text(content)
                with self.assertRaises(ValueError):load(DATA/"trips.csv",path)

    def test_repeated_reports_are_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            run(DATA/"trips.csv",DATA/"readings.csv",temp)
            before=(Path(temp)/"exposure.json").read_bytes()
            run(DATA/"trips.csv",DATA/"readings.csv",temp)
            self.assertEqual(before,(Path(temp)/"exposure.json").read_bytes())

    def test_fractional_offset_cannot_create_subsecond_utc(self):
        with self.assertRaisesRegex(ValueError,"whole-second"):
            stamp("2026-01-01T00:00:30+00:00:30.5")
        self.assertEqual(stamp("2026-01-01T00:01:00+00:01"),stamp("2026-01-01T00:00:00Z"))

    def test_invalid_gap_limit(self):
        for gap in [0,-1,86401,True]:
            with self.assertRaises(ValueError):analyze_trip(self.trips[0],[],gap)


if __name__=="__main__":unittest.main()
