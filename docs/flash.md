# Synthetic CCLOG01 circular flash image transcription

The `cold_exposure.flash` package transcribes a returned **flash image** into
the analyzer-ready `readings.csv` contract shared by the v1 and v2 consumers,
plus a self-contained `transcription.json` audit. It is an offline,
single-process, standard-library-only pipeline.

> **Synthetic protocol notice.** CCLOG01 is an original device protocol
> invented for this project. It is not compatible with any vendor hardware,
> firmware or file format. No real device, serial port, network, database,
> credential or shipment measurement is involved anywhere in this code.

## Why transcription is needed

The returned medium is a whole flash image, not an ordered export:

- slots are reused circularly once the medium fills, so stale, overwritten
  slots may still contain old readings or arbitrary garbage;
- the device reboots reset the per-boot seconds counter (`tick`) to an
  independent origin, so raw ticks cannot be dated without human anchors;
- sequence numbers are 32-bit and wrap.

Naively converting the file from start to end would treat expired slots as
current readings and assign wrong dates after a reboot. The transcriber
instead trusts only the header-declared valid sequence and reconstructs UTC
from manual calibration anchors.

## Image layout

All integers are unsigned little-endian unless stated. The image is exactly
`128 + capacity * 24` bytes. Two independent 64-byte header copies sit at
absolute offsets `0` (header_A) and `64` (header_B); they are followed by
`capacity` fixed 24-byte slots. Slot `i` starts at absolute offset
`128 + 24 * i`.

### Header (each 64-byte copy)

Intervals are start-inclusive, end-exclusive.

| Bytes | Field | Notes |
| --- | --- | --- |
| `[0,8)` | magic | ASCII `CCLOG01` plus LF, i.e. `b"CCLOG01\n"` |
| `[8,12)` | `generation` | uint32 |
| `[12,16)` | `capacity` | uint32, 1..1 000 000 |
| `[16,20)` | `count` | uint32, `0 <= count <= capacity` |
| `[20,24)` | `next_slot` | uint32, strictly below `capacity` |
| `[24,28)` | `next_seq` | uint32, the seq the next write will use |
| `[28,44)` | `device_id` | 1..16 ASCII letters/digits/`_`/`-`, NUL-padded; all bytes after the first NUL must be NUL; identity is case-sensitive |
| `[44,60)` | reserved | must be all zero |
| `[60,64)` | CRC32 | `zlib.crc32(data[0:60]) & 0xffffffff` |

A copy is valid only when the magic, every field, the reserved bytes and the
CRC all pass. Slot contents are never used to infer header validity.

### Header selection

The two copies are judged independently first.

- If neither is valid, transcription fails.
- If exactly one is valid it is selected, and the audit records why the other
  copy was rejected.
- If both are valid, let
  `d = (generation_B - generation_A) mod 2**32`:
  - `0 < d < 2**31` → B is newer and is selected;
  - `2**31 < d < 2**32` → A is newer (B already wrapped) and is selected;
  - `d = 2**31` → the headers cannot be ordered, transcription is rejected;
  - `d = 0` → accepted only when the first 60 bytes of the two copies are
    identical, in which case A is the canonical choice; otherwise it is a
    same-generation conflict and transcription is rejected.

Only after selection is the file length checked against the selected header's
`capacity` (`128 + capacity*24` exactly; truncation or trailing bytes fail).
If the newer valid header points at corrupt active data the whole run fails;
the transcriber never falls back to an older header to hide the damage.

### Slot (24 bytes)

| Bytes | Field | Notes |
| --- | --- | --- |
| `[0,4)` | `seq` | uint32 |
| `[4,8)` | `tick` | uint32, per-boot seconds counter, wraps at 2**32 |
| `[8,12)` | `boot_id` | uint32, identifies one device boot |
| `[12,14)` | `temperature_centidegrees` | **signed** int16 little-endian, range -10000..15000 (i.e. -100.00..150.00 °C) |
| `[14,20)` | reserved | must be all zero |
| `[20,24)` | CRC32 | over bytes `[0,20)` |

### Active ring range

`next_slot` is where the next write would go; `next_seq` is the sequence
number that write would use. For logical position `j = 0 .. count-1`:

- active physical slot: `(next_slot - count + j) mod capacity`
- expected sequence: `(next_seq - count + j) mod 2**32`

Only those `count` slots are read. The transcriber never scans other slots
for something that "looks newer"; unused regions may hold stale readings, bad
CRCs or arbitrary bytes without affecting the result. `count = 0` is legal
and means there are no valid samples.

Every active slot must pass: stored CRC over its first 20 bytes, reserved
bytes zero, `seq` exactly equal to the expected value (no sorting and no
skipping of gaps), and temperature inside `-10000..15000`. Errors name the
physical slot and its absolute offset.

## Tick expansion and boot segments

Within the active sequence, equal `boot_id` values must form one contiguous
run; an A,B,A reuse pattern is rejected. For adjacent records inside one boot
segment the tick difference is computed modulo 2**32 and must be in
`1 .. 2**31-1`. A zero difference means no time elapsed; a difference of
2**31 or more is a half-turn-or-larger jump whose elapsed time cannot be
interpreted safely — both are rejected. The segment's first raw tick is the
origin of an expanded tick axis, and each legal delta is accumulated onto it.
Each boot segment expands independently; time is never extrapolated across a
reboot.

## Anchor document

Strict UTF-8 JSON with no duplicate object keys. The top level has exactly
the keys `schema_version`, `device_id`, `sensor_id`, `anchors`.

- `schema_version` is the integer `1` (booleans are rejected);
- `device_id` must equal the selected header's device id;
- `sensor_id` is a non-empty string with no leading or trailing whitespace,
  emitted verbatim; shipment assignment is never guessed from the device id;
- each anchor object has exactly `boot_id`, `seq`, `observed_at`. The two
  integers are uint32 (booleans rejected); `observed_at` uses the project's
  existing timezone-aware, whole-UTC-second timestamp contract.

Each anchor must uniquely reference one currently active record;
`(boot_id, seq)` pairs may not repeat and must not reference an overwritten
or unknown slot. Every active boot segment needs at least one anchor; an
anchor for a boot without active records is rejected. When `count = 0` the
array must be empty.

For an anchor `a` and a record `r` of the same boot:

```
UTC(r) = UTC(a) + expanded_tick(r) - expanded_tick(a)
```

Multiple anchors of one boot must imply exactly the same offset between the
expanded axis and UTC; inconsistency is rejected outright (no least squares,
no drift fitting, no arbitrary pick). Anchors may sit in the middle of a
segment and time may be derived forwards or backwards from them; another
boot's anchor can never fill in. The canonical anchor of a boot is the one
bound to the earliest logical position in that segment, regardless of JSON
array order; the other consistent anchors are still listed.

Derived instants outside the `datetime` range are rejected. Wall-clock
monotonicity across boots is not assumed: the final CSV is sorted by UTC. If
two valid records of the same sensor land on the same UTC second — even with
identical temperatures — the run is rejected rather than silently merging.

## Outputs

The output directory must not exist beforehand and must not be a symlink. On
success it contains exactly two files.

### `readings.csv`

UTF-8 without BOM, standard CSV escaping, header
`sensor_id,observed_at,temperature_c`. Timestamps are always
`YYYY-MM-DDTHH:MM:SSZ`; temperatures are fixed two-place decimals produced
from the integer hundredths without passing through float. A zero-count
transcription still writes a legal header-only file. The file drops no
in-scope sample, adds no interpolation or boundary padding, never restores
overwritten records, and never trims a shipment window; it can be fed
unchanged to `python -m cold_exposure` (v1) and
`python -m cold_exposure.consensus` (v2).

### `transcription.json`

Deterministic, sorted-key JSON containing:

- format/schema version and the synthetic-protocol notice;
- SHA-256 of the exact captured image and anchor bytes;
- both header copies with validity, rejection reason, parsed fields and raw
  hex, plus the selected copy, its generation and the selection reason;
- the circular active range (logical position, physical slot, absolute
  offset, expected seq) and the unused physical slots;
- the device-to-sensor binding;
- per-boot segment ranges and canonical anchor identities;
- every anchor with raw and normalized UTC time, bound record, derived
  offset, canonical flag and consistency marking;
- per-record logical position, physical slot/offset, full 24-byte hex, raw
  seq/tick/boot/centidegrees, expanded tick, canonical anchor evidence,
  final UTC, rendered temperature and CSV data record number.

The audit is sufficient to recompute header selection, active-slot order,
tick expansion and every transcribed row independently. It does not store
the whole image and does not claim the device measured a real temperature.

## Determinism and failure recovery

Each input file is captured exactly once into bytes; parsing, hashing and
the audit all derive from that capture. Identical image and anchor bytes
produce byte-identical products in two fresh directories. Reordering the
anchor array changes neither the CSV nor the canonical anchors, while the
anchor SHA-256 changes honestly. No run timestamps, random ids or machine
absolute paths are written.

Both products are fully built before the output path is touched. They are
written into a private hidden staging directory next to the target and
published with one directory rename. A failure while writing either file or
while publishing removes the staging directory and leaves no formal
half-published result and no altered input; the same target can be used again
after fixing the problem. Existing files or directories are never
overwritten and symlinked output paths are refused.

## Command and Python API

```sh
python -m cold_exposure.flash \
    --image path/to/image.bin \
    --anchors path/to/anchors.json \
    --output-dir path/to/new-output-directory
```

```python
from pathlib import Path
from cold_exposure.flash import (
    CapturedInputs, capture_inputs, transcribe_capture, publish,
)

captured = capture_inputs(Path("image.bin"), Path("anchors.json"))
csv_text, audit_text, model = transcribe_capture(captured)
publish(Path("out"), csv_text, audit_text)

# Or the all-in-one helper:
from cold_exposure.flash import transcribe
transcribe("image.bin", "anchors.json", "out")
```

Synthetic example images and anchor documents can be regenerated with:

```sh
python tools/build_flash_examples.py --output-dir examples/flash/generated
```

The transcriber handles one physical device image at a time. It does not
fuse multiple loggers, define new temperature-exposure metrics, or perform
any analysis beyond transcription; v1/v2 analysis remains exclusively in the
existing analyzers.
