"""Shared helpers for the synthetic CCLOG01 flash transcription tests."""
import csv
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]

# Same isolated-subprocess launcher pattern as the consensus CLI tests.
_LAUNCHER = (
    "import runpy, sys; "
    "sys.path.insert(0, %r); "
    "module = sys.argv[1]; "
    "sys.argv = [module] + sys.argv[2:]; "
    "runpy.run_module(module, run_name='__main__')"
    % str(PROJECT)
)


def flash_cli_command(arguments):
    return [sys.executable, "-I", "-c", _LAUNCHER, "cold_exposure.flash", *arguments]


def run_flash_cli(arguments, expect_success=True):
    command = flash_cli_command([str(value) for value in arguments])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    if expect_success:
        assert completed.returncode == 0, completed.stderr
    return completed


def write_anchor_file(path, device_id, sensor_id, anchors, schema_version=1):
    document = {
        "schema_version": schema_version,
        "device_id": device_id,
        "sensor_id": sensor_id,
        "anchors": anchors,
    }
    Path(path).write_bytes(json.dumps(document, ensure_ascii=False).encode("utf-8"))


def read_csv_rows(path):
    with open(path, encoding="utf-8", newline="") as stream:
        return list(csv.reader(stream))


def corrupt_byte(data, index, xor=0xFF):
    data = bytearray(data)
    data[index] ^= xor
    return bytes(data)
