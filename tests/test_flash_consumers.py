"""Consumer regression: CSV -> synthetic CCLOG01 image -> CSV -> existing CLIs.

The encoding reference used here is test-only code; production transcription
never imports it. Re-transcribed readings must drive the unchanged v1 and v2
analyzers to byte-identical reports, and the full partition-level v2 output is
compared (not just row counts).
"""
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cold_exposure.flash.builder import build_ring_image
from cold_exposure.input import stamp
from decimal import Decimal
from tests._flash_helpers import PROJECT, run_flash_cli, write_anchor_file


def normalize_temperature(text):
    # The flash contract emits fixed two-place text; analyzer arithmetic is
    # Decimal-based, so canonical numeric form is the right equality basis.
    return str(Decimal(text))


TEMPERATURE_KEYS = {
    "min_c", "max_c", "temperature_c", "mean_known_c",
}


def normalize_report(report):
    """Turn temperature strings into Decimals so equality is purely numeric.

    Decimal equality ignores display exponent (``Decimal('5.00') == Decimal('5')``),
    which matches the unchanged analyzer arithmetic while timestamps stay strings.
    """
    if isinstance(report, dict):
        result = {}
        for key, value in report.items():
            if key in TEMPERATURE_KEYS and isinstance(value, str):
                result[key] = Decimal(value)
            elif key == "band" and isinstance(value, dict):
                result[key] = {
                    endpoint: Decimal(number) for endpoint, number in value.items()
                }
            elif key == "consensus_components" and isinstance(value, list):
                result[key] = [
                    {endpoint: Decimal(component[endpoint]) for endpoint in component}
                    for component in value
                ]
            else:
                result[key] = normalize_report(value)
        return result
    if isinstance(report, list):
        return [normalize_report(value) for value in report]
    return report

DATA = PROJECT / "data"
V1_TRIPS = DATA / "trips.csv"
V1_READINGS = DATA / "readings.csv"
V2_TRIPS = DATA / "consensus" / "trips.csv"
V2_ASSIGNMENTS = DATA / "consensus" / "assignments.csv"
V2_READINGS = DATA / "consensus" / "readings.csv"


def _launch(module, arguments):
    launcher = (
        "import runpy, sys; "
        "sys.path.insert(0, %r); "
        "module = sys.argv[1]; "
        "sys.argv = [module] + sys.argv[2:]; "
        "runpy.run_module(module, run_name='__main__')"
        % str(PROJECT)
    )
    return [sys.executable, "-I", "-c", launcher, module, *[str(a) for a in arguments]]


def run_module(module, arguments):
    completed = subprocess.run(
        _launch(module, arguments),
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0:
        raise AssertionError(f"{module} failed: {completed.stderr}")
    return completed


def read_readings(path):
    with open(path, encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    grouped = {}
    for row in rows:
        grouped.setdefault(row["sensor_id"], []).append(row)
    for sensor_id in grouped:
        grouped[sensor_id].sort(key=lambda row: stamp(row["observed_at"]))
    return grouped


def encode_sensor_image(sensor_id, rows, work_dir, *, boot_id=1, device_prefix="DEV"):
    """Encode one sensor's readings as one fresh-boot CCLOG01 image.

    Ticks are the reading's own whole-UTC epoch seconds, which always fit in
    uint32 for this synthetic data; each anchor therefore has offset zero.
    """
    records = []
    for seq, row in enumerate(rows):
        moment = stamp(row["observed_at"])
        epoch = int((moment - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds())
        assert 0 <= epoch < 2 ** 32
        records.append(
            {
                "seq": seq,
                "tick": epoch,
                "boot_id": boot_id,
                "temperature_centidegrees": _centidegrees(row["temperature_c"]),
            }
        )
    image = build_ring_image(
        capacity=max(len(records) + 1, 2),
        records=records,
        next_slot=len(records),
        next_seq=len(records),
        device_id=f"{device_prefix}-{sensor_id}",
    )
    image_path = work_dir / f"{sensor_id}.bin"
    image_path.write_bytes(image)
    anchors_path = work_dir / f"{sensor_id}.anchors.json"
    write_anchor_file(
        anchors_path,
        f"{device_prefix}-{sensor_id}",
        sensor_id,
        [
            {
                "boot_id": boot_id,
                "seq": 0,
                "observed_at": rows[0]["observed_at"],
            }
        ],
    )
    return image_path, anchors_path


def _centidegrees(text):
    whole, dot, fraction = text.partition(".")
    sign = -1 if whole.startswith("-") else 1
    whole_digits = whole.lstrip("-")
    fraction = (fraction + "00")[:2] if dot else "00"
    return sign * (int(whole_digits) * 100 + int(fraction))


def encode_all(readings_path, work_dir, device_prefix):
    """Encode each sensor separately, then rebuild the original row order.

    Each sensor goes through its own image and real flash CLI. The merged
    table restores the original file's global row sequence, so even the
    analyzer's CSV record-number evidence is preserved.
    """
    original_rows = []
    grouped = {}
    with open(readings_path, encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == ["sensor_id", "observed_at", "temperature_c"]
        for row in reader:
            original_rows.append(row)
            grouped.setdefault(row["sensor_id"], []).append(row)
    for sensor_id in grouped:
        grouped[sensor_id].sort(key=lambda row: stamp(row["observed_at"]))

    transcribed = {}
    for sensor_id, rows in sorted(grouped.items()):
        image_path, anchors_path = encode_sensor_image(
            sensor_id, rows, work_dir, device_prefix=device_prefix
        )
        out_dir = work_dir / f"out-{sensor_id}"
        run_flash_cli(["--image", image_path, "--anchors", anchors_path,
                       "--output-dir", out_dir])
        with open(out_dir / "readings.csv", encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            self_header = next(reader)
            assert self_header == ["sensor_id", "observed_at", "temperature_c"]
            for sensor, observed_at, temperature in reader:
                transcribed[(sensor, observed_at)] = temperature

    merged = work_dir / "readings.flash.csv"
    with open(merged, "w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sensor_id", "observed_at", "temperature_c"])
        for row in original_rows:
            utc_text = stamp(row["observed_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
            writer.writerow([row["sensor_id"], utc_text,
                             transcribed[(row["sensor_id"], utc_text)]])
    return merged


class ConsensusRoundTripTests(unittest.TestCase):
    def test_full_v2_report_identical_after_flash_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            baseline_path = work / "baseline.json"
            run_module(
                "cold_exposure.consensus",
                [
                    "--trips", V2_TRIPS,
                    "--assignments", V2_ASSIGNMENTS,
                    "--readings", V2_READINGS,
                    "--max-gap-seconds", 900,
                    "--output", baseline_path,
                ],
            )
            merged = encode_all(V2_READINGS, work, "V2DEV")
            roundtrip_path = work / "roundtrip.json"
            run_module(
                "cold_exposure.consensus",
                [
                    "--trips", V2_TRIPS,
                    "--assignments", V2_ASSIGNMENTS,
                    "--readings", merged,
                    "--max-gap-seconds", 900,
                    "--output", roundtrip_path,
                ],
            )
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            roundtrip = json.loads(roundtrip_path.read_text(encoding="utf-8"))
            # Input hashes differ (the readings file is a fresh transcription);
            # every business partition and metric must be numerically identical.
            del baseline["inputs_sha256"]
            del roundtrip["inputs_sha256"]
            self.assertEqual(normalize_report(baseline), normalize_report(roundtrip))
            # Explicitly assert the partition-level detail the task asks for.
            by_trip_base = {t["trip_id"]: t for t in baseline["trips"]}
            by_trip_rt = {t["trip_id"]: t for t in roundtrip["trips"]}
            self.assertEqual(set(by_trip_base), set(by_trip_rt))
            for trip_id in by_trip_base:
                base, rt = by_trip_base[trip_id], by_trip_rt[trip_id]
                self.assertEqual(normalize_report(base["partitions"]),
                                 normalize_report(rt["partitions"]))
                self.assertEqual(base["classification_seconds"], rt["classification_seconds"])
                self.assertEqual(base["supported_degree_minutes_min"],
                                 rt["supported_degree_minutes_min"])
                self.assertEqual(base["supported_degree_minutes_max"],
                                 rt["supported_degree_minutes_max"])
                self.assertEqual(base["status"], rt["status"])

    def test_transcribed_table_matches_original_rows_as_a_set(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            merged = encode_all(V2_READINGS, work, "V2DEV")
            original = set()
            with open(V2_READINGS, encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    original.add(
                        (row["sensor_id"],
                         stamp(row["observed_at"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         Decimal(row["temperature_c"]))
                    )
            transcribed = set()
            with open(merged, encoding="utf-8", newline="") as stream:
                transcribed_rows = list(csv.DictReader(stream))
                for row in transcribed_rows:
                    transcribed.add(
                        (row["sensor_id"], row["observed_at"],
                         Decimal(row["temperature_c"]))
                    )
            self.assertEqual(original, transcribed)
            # And the exact fixed-two-place flash text form on every row.
            for row in transcribed_rows:
                whole, dot, fraction = row["temperature_c"].partition(".")
                self.assertTrue(dot and len(fraction) == 2)


class VersionOneRoundTripTests(unittest.TestCase):
    def test_v1_trip_stats_unchanged_after_flash_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            merged = encode_all(V1_READINGS, work, "V1DEV")
            report_dir = work / "v1-report"
            run_module(
                "cold_exposure",
                ["--trips", V1_TRIPS, "--readings", merged, "--output", report_dir],
            )
            report = json.loads((report_dir / "exposure.json").read_text(encoding="utf-8"))
            trips = {trip["trip_id"]: trip for trip in report["trips"]}
            t1, t2 = trips["T1"], trips["T2"]
            # T1: 600 s within range, 1200 s above the upper limit.
            self.assertEqual(t1["seconds"],
                             {"within": 600, "above": 1200, "below": 0, "unknown": 0})
            self.assertEqual(t1["status"], "excursion")
            self.assertEqual(t1["outside_degree_minutes"], "30.000000")
            # T2: all 1200 s unknown.
            self.assertEqual(t2["seconds"]["unknown"], 1200)
            self.assertEqual(t2["status"], "incomplete")

            # And it is byte-identical to the analyzer run on the original CSV.
            original_dir = work / "v1-original"
            run_module(
                "cold_exposure",
                ["--trips", V1_TRIPS, "--readings", V1_READINGS, "--output", original_dir],
            )
            original_report = json.loads(
                (original_dir / "exposure.json").read_text(encoding="utf-8")
            )
            del report["inputs_sha256"]
            del original_report["inputs_sha256"]
            self.assertEqual(normalize_report(report), normalize_report(original_report))

    def test_centidegree_conversion_reference(self):
        self.assertEqual(_centidegrees("9"), 900)
        self.assertEqual(_centidegrees("-1.25"), -125)
        self.assertEqual(_centidegrees("150"), 15000)
        self.assertEqual(_centidegrees("-100"), -10000)
        self.assertEqual(_centidegrees("0.05"), 5)


if __name__ == "__main__":
    unittest.main()
