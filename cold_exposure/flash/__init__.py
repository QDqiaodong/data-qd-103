"""Offline transcription of the synthetic CCLOG01 circular flash image.

This package implements an original, entirely synthetic device protocol
(``CCLOG01``). It is not compatible with any vendor hardware and does not talk
to a serial port, network or database. The public entry points are:

* :func:`cold_exposure.flash.transcribe.capture_inputs`
* :func:`cold_exposure.flash.transcribe.transcribe_capture`
* :func:`cold_exposure.flash.transcribe.publish`
* :func:`cold_exposure.flash.transcribe.transcribe`
* ``python -m cold_exposure.flash --image ... --anchors ... --output-dir ...``
"""
from .image import (
    HEADER_REGION_SIZE,
    HEADER_SIZE,
    SLOT_SIZE,
    FlashError,
    assemble_image,
    evaluate_headers,
    expand_segment_ticks,
    format_temperature,
    logical_positions,
    pack_header,
    pack_raw_slot,
    pack_slot,
    parse_header,
    parse_slot,
    select_header,
)
from .transcribe import (
    CapturedInputs,
    capture_inputs,
    publish,
    transcribe,
    transcribe_capture,
)

__all__ = [
    "HEADER_REGION_SIZE",
    "HEADER_SIZE",
    "SLOT_SIZE",
    "CapturedInputs",
    "FlashError",
    "assemble_image",
    "capture_inputs",
    "evaluate_headers",
    "expand_segment_ticks",
    "format_temperature",
    "logical_positions",
    "pack_header",
    "pack_raw_slot",
    "pack_slot",
    "parse_header",
    "parse_slot",
    "publish",
    "select_header",
    "transcribe",
    "transcribe_capture",
]
