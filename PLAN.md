# Plan: `agilent-biostack4-standalone`

A standalone Python driver and (eventual) REST service for the Agilent BioStack 4
microplate stacker, talking directly to the stacker over RS-232 (`COM8` on the
lab PC) without Gen5 or any reader software in the loop.

This document is the design contract. It is the source of truth before any
hardware is touched.

## Goals

1. Drive the BioStack 4 over COM8 in standalone mode from Python.
2. Expose only the two macro workflows the lab actually needs:
   - `stage_plate()` - take one plate from the input stack and place it at the
     internal handoff position. (Was `drop_plate`; `b9`.)
   - `present_plate()` - pick the staged plate up from the handoff and present
     it to an external drop-off position outside the equipment, for a robot
     arm / reader nest to take. (Was `pickup_plate`; `cd`. Bench-confirmed
     2026-05-29 that this presents the plate OUT, not onto an internal output
     stack — internal input->output transfer is `e2`, not yet exposed.)
3. Surface non-success device responses as structured Python exceptions, not
   silent failures.
4. Match STATUS_SPEC v1.1 conventions so a follow-up PR can graduate
   this driver into the `ac-organic-lab` dashboard with `adapter: http`.
5. Stay safe-by-default: no active commands fire without an explicit call from
   a human, and the first real bench session is gated by `PHYSICAL_TESTS.md`.

Non-goals for this PR:

- Fine-grained Z jog control as a public API.
- Re-implementing Gen5's calibration UI.
- Multi-stack or multi-stacker support.
- Anything reader-side (Cytation, Synergy, etc.).

## What we know about the protocol

From sniffed COM8 traffic captured under Gen5 in
`port-communication-captures/` (see `PROTOCOL_NOTES.md` for byte-level
details):

- Serial: `9600 8N2`, DTR set, RTS cleared, software flow chars `0x11`/`0x13`.
- Binary, framed protocol. Not ASCII. Not `STATUS\r`.
- Request: 11-byte header `01 <addr> <cmd> 0d 01 01 00 <plen_le16> <chk_le16>`,
  optionally followed by an N-byte payload.
- Response: ACK `0x06`, then an 11-byte response header
  `01 00 <cmd> 0d 02 00 00 04 00 <chk_le16>`, then a 4-byte status payload.
- Success payload: `00 80 00 00`.
- Observed failure payloads: `01 80 00 16` (no more plates / terminal) and
  `01 80 01 17` (failed pickup with no plate present).
- Checksum invariant: `(sum(header[:9]) + sum(payload) + chk_le16) & 0xFFFF == 0`.

Commands observed in real captures, with their best current interpretation:

| Command | Address | Payload | Inferred meaning |
| --- | --- | --- | --- |
| `bd` | `01` | none | Status / "are you there" query. |
| `be` | `01` | 18 bytes | Setup / context (payload varies per run). |
| `eb` | `01` | 12 bytes | Setup / context. |
| `f1` | `01` | 9 bytes | Setup / context. |
| `c0` | `02` | none | Home all axes. |
| `b0` | `01` | none | Save-position related. |
| `c1` | `02` | 18 bytes | Alignment / save-position. |
| `ad` | `02` | 20 bytes | Alignment. |
| `ae` | `02` | 22 bytes | Z move-down by a 16-bit count in the payload. |
| `b9` | `02` | none | Verify / drop related. |
| `cd` | `02` | 26 bytes | Verify / pickup related (payload begins ASCII `PASSWORD`). |
| `e2` | `02` | none | Move one plate input -> output (stack-to-stack). |
| `bc` | `02` | none | Move one plate output -> input (stack-to-stack). |

We do not yet have a one-to-one mapping from `b9`/`cd` to "drop" vs "pickup".
That has to be resolved on the bench (see `PHYSICAL_TESTS.md`).

## Architecture

Layered, each layer testable on its own without hardware.

```
+----------------------------------------------------+
| biostack.BioStack4         high-level workflow API |
+----------------------------------------------------+
| recorded_sequences          captured macro frames  |
+----------------------------------------------------+
| transport.SerialTransport    pyserial + ACK/READ   |
| transport.DryRunTransport    in-memory simulator   |
+----------------------------------------------------+
| frames                       encode/decode/checksum|
+----------------------------------------------------+
```

### `frames`

Pure byte-level: encode a request, decode a response, validate checksum. No
I/O. Fully unit-testable on macOS/Linux. The capture analyzer in
`tools/analyze_captures.py` continues to be the empirical reference.

### `transport`

Owns the serial port. Two implementations behind one `Transport` protocol:

- `SerialTransport` - real `pyserial`. Opens COM with the exact settings
  observed in the captures and runs a request through the full ACK + header +
  payload read cycle.
- `DryRunTransport` - returns success payloads for a fixed command whitelist
  and raises `BioStackProtocolError` for unknown commands. Lets us run the
  `BioStack4` driver and (later) the FastAPI service on a laptop without the
  stacker.

### `recorded_sequences`

The drop / pickup macros are not yet fully decoded byte-for-byte. The safest
first cut is to send the same frame sequences Gen5 sent during a successful
capture and to surface the device's own status payload back to the caller.

This module contains those byte sequences as immutable `tuple[bytes, ...]`
literals, named after the workflows they implement. They are the ground
truth for what we send; the high-level `BioStack4` class only orchestrates
playback and error handling on top.

### `biostack.BioStack4`

Public synchronous driver. Standard STATUS_SPEC sync driver shape:

```python
from agilent_biostack4 import BioStack4

stacker = BioStack4()             # reads com_port from config.toml
stacker.connect()
stacker.status()                  # raises if device does not ACK 'bd'
stacker.home()                    # 'c0' to address 02
stacker.stage_plate()             # macro: take from input stack, place at internal handoff
stacker.present_plate()           # macro: pick from handoff, present out to external drop-off
stacker.close()
```

Each macro:

1. Plays back its captured frame sequence through the transport.
2. Inspects every response status payload.
3. Raises the appropriate exception on any non-success payload, with the
   command byte and raw payload preserved in the exception fields.

### `exceptions`

```
BioStackError
+- BioStackConnectionError      # COM port open / serial layer failures
+- BioStackProtocolError        # malformed frame, bad checksum, bad ACK
+- BioStackCommandError         # device returned a 01 80 .. status payload
   +- NoPlatePickedUpError      # specifically 01 80 01 17 (provisional)
   +- StackEmptyError           # specifically 01 80 00 16 (provisional)
```

Specific subclasses are populated as we confirm error semantics during the
physical-test phase. Until then, `BioStackCommandError` is the catch-all.

### `models`

Copy of the `lab-status-contract` v1.1 shapes (vendored from ac-organic-lab's
`docs/STATUS_SPEC.md`). Needed when the FastAPI service is added in a
follow-up so the dashboard's existing v1.0/v1.1 aggregator picks the
BioStack up by changing `adapter: mock` to `adapter: http` in
`ac-organic-lab/equipment.yaml`.

### `config`

Standard STATUS_SPEC TOML loader pattern. Defaults to
`COM8`, `9600 8N2`, the captured serial flow chars, and a 30-second
movement timeout. Real values live in `config.toml`, gitignored;
`config.example.toml` is the committed template.

## Public API surface, locked

```python
class BioStack4:
    def __init__(self, *, transport: Transport | None = None) -> None: ...

    # Connection
    def connect(self) -> None: ...
    def close(self) -> None: ...
    def is_connected(self) -> bool: ...

    # Lifecycle / safety
    def status(self) -> StatusPayload: ...   # raises if device does not ACK
    def home(self) -> StatusPayload: ...

    # Workflows the lab actually needs
    def stage_plate(self) -> StatusPayload: ...
    def present_plate(self) -> StatusPayload: ...
```

`StatusPayload` is a small `dataclass` with the four-byte status, the
originating command, and a `success: bool` flag.

`Transport` is a `typing.Protocol`:

```python
class Transport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def is_open(self) -> bool: ...
    def transact(self, request: bytes, *, timeout: float = ...) -> Response: ...
```

`Response` is a dataclass with the response header, status payload, and
elapsed time.

## Testing strategy

Three tiers, in increasing order of risk:

### Tier 1: unit (no hardware, no OS dependency)

- `tests/test_frames.py`
  - Encodes the known captured requests and asserts the bytes match exactly.
  - Decodes the known captured responses and asserts checksum + fields.
  - Property tests for checksum invariant.
- `tests/test_biostack_dryrun.py`
  - Drives `BioStack4` with `DryRunTransport`, runs `status -> home ->
    stage_plate -> present_plate`, asserts the exact request frames that get
    sent and that no exception is raised.
  - Negative tests: `DryRunTransport` simulates each known failure status
    payload and asserts the matching exception class.

These run on macOS/Linux CI without any hardware.

### Tier 2: dry-run integration

- `agilent-biostack4-serve --dry-run` (added in a follow-up PR) brings up the
  FastAPI service end-to-end against `DryRunTransport`. Same shape as
  `agilent-plateloc-serve --dry-run`. No `/control/*` route fires anything
  outside of the simulator.

### Tier 3: physical (gated)

`PHYSICAL_TESTS.md` lists every step that must succeed at the bench before we
declare the driver functional. No code path that drives real hardware lands
without an explicit hardware engineer sign-off on those tests.

## Risk register

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| `b9` vs `cd` ordering is reversed from our guess | high | First physical test runs each command in isolation with a plate visible. |
| `be`/`eb`/`f1` setup payload encodes per-session state and stale bytes confuse the device | medium | Capture a "send only the action command, no setup" run during the physical tests; if it fails, replay full sequence. |
| The 26-byte `cd` payload `PASSWORD ...` includes a one-shot token | medium | First-pass driver replays the exact captured payload; if rejected, capture a fresh payload immediately before sending. |
| Sending a calibration command (`c1`, `ad`, `ae`) by mistake overwrites factory alignment | high | Calibration commands are not exposed in the public API at all. They live only inside `recorded_sequences` and only the workflow macros call them. |
| Active command sent without a person at the instrument jams a plate | high | All bench tests require a human present, plates clear, and the panel-mount E-stop within arm's reach. The driver also prints a one-line `[BIOSTACK] sending <cmd>` line before every write. |

## File layout (after this PR)

```
agilent-biostack4-standalone/
├── .gitignore
├── LICENSE
├── PLAN.md
├── PHYSICAL_TESTS.md
├── PROTOCOL_NOTES.md
├── README.md
├── config.example.toml
├── port-communication-captures/        (gitignored)
├── pyproject.toml
├── src/
│   └── agilent_biostack4/
│       ├── __init__.py
│       ├── biostack.py
│       ├── config.py
│       ├── exceptions.py
│       ├── frames.py
│       ├── models.py
│       ├── recorded_sequences.py
│       └── transport.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_frames.py
│   └── test_biostack_dryrun.py
└── tools/
    └── analyze_captures.py
```

`service.py`, `claims.py`, `api.py`, and `__main__.py` are explicitly NOT in
this PR; they get a follow-up once the bench tests pass.

## Out-of-scope follow-up PRs

1. STATUS_SPEC v1.1 service: `service.py`, `claims.py`, `api.py`,
   `__main__.py`, `--dry-run` entrypoint.
2. `equipment.yaml` migration: flip `agilent_biostack` from
   `adapter: mock` to `adapter: http` once the service is up.
3. Decoding `be`/`eb`/`f1` payloads, building a parametric API for
   re-calibration. Only after we have a real reason to recalibrate from
   Python.
