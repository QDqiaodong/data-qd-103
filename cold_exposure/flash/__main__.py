"""Command line entry for offline synthetic CCLOG01 flash transcription.

    python -m cold_exposure.flash --image FILE --anchors FILE --output-dir DIR

On success ``DIR`` is created atomically and contains exactly ``readings.csv``
and ``transcription.json``. Any validation, write or publish failure exits
non-zero with a locatable message and leaves no formal output directory.
"""
import argparse
from pathlib import Path

from .image import FlashError
from .transcribe import transcribe


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Offline transcription of a synthetic CCLOG01 circular flash image "
            "into analyzer-ready readings.csv plus a full audit JSON"
        )
    )
    parser.add_argument("--image", required=True, type=Path, help="raw CCLOG01 binary image")
    parser.add_argument("--anchors", required=True, type=Path, help="strict UTF-8 JSON anchor document")
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="directory that must not exist; receives readings.csv and transcription.json",
    )
    args = parser.parse_args(argv)
    try:
        model = transcribe(args.image, args.anchors, args.output_dir)
    except (FlashError, ValueError, OSError) as exc:
        parser.exit(2, f"flash transcription failed: {exc}\n")
    print(
        f"transcribed {len(model['records'])} reading(s) for sensor "
        f"{model['sensor_id']} into {args.output_dir}"
    )


if __name__ == "__main__":
    main()
