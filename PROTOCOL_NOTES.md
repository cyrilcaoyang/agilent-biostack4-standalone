# BioStack 4 Serial Protocol Notes

Initial notes from Gen5 COM8 sniffer exports in `port-communication-captures/`.

Raw captures are intentionally git-ignored. These notes record only derived protocol observations.

## Serial Settings

Gen5 configures COM8 as:

- Baud: `9600` (`80 25 00 00`, little-endian `0x2580`)
- Line control: `02 00 08`
  - Windows `SERIAL_LINE_CONTROL`: stop bits `2`, parity `0`, word length `8`
  - Interpreted as `9600 8N2`
- DTR: set
- RTS: cleared
- XON/XOFF characters: `11 13`

This is not an ASCII command protocol.

## Frame Shape

Gen5 writes binary command frames, not text such as `STATUS\r`.

Request header:

```text
01 <address> <command> 0d 01 01 00 <payload_len_le16> <checksum_le16>
```

If `payload_len` is non-zero, Gen5 immediately writes that many payload bytes as a second write operation.

Checksum hypothesis:

```text
sum(header_without_checksum + payload + checksum_le16) & 0xffff == 0
```

The checksum is little-endian two's-complement over the first 9 header bytes plus the payload.

Response behavior:

- Device ACK byte is `06`.
- Fast commands may return `06` prepended to the response frame in one read.
- Motion commands often return `06` first, then a delayed response frame after the movement completes.

Response header:

```text
01 00 <command> 0d 02 00 00 <payload_len_le16> <checksum_le16>
```

Observed response payload length is `4`.

Observed success payload:

```text
00 80 00 00
```

Observed terminal/error payload when repeating "move all" after plates are exhausted:

```text
01 80 00 16
```

Observed failed pickup payload from `4_verify_step2_failed_pickup.csv`:

```text
01 80 01 17
```

Treat non-success status meanings as provisional until confirmed against more captures.

## Observed Commands

| Command | Address | Payload | Observed action |
| --- | --- | --- | --- |
| `bd` | `01` | none | Common status/query before and after actions |
| `be` | `01` | 18 bytes | Common setup/config command; payload changes between runs |
| `eb` | `01` | 12 bytes | Common setup/config command |
| `f1` | `01` | 9 bytes | Common setup/config command |
| `b0` | `01` | none | Observed during save-position flow |
| `ad` | `02` | 20 bytes | Alignment command |
| `ae` | `02` | 22 bytes | Z move-down command; requested distance appears at payload bytes 2-3 as little-endian |
| `b9` | `02` | none | Verify/drop. **Bench-confirmed 2026-05-29:** moves one plate from the input stack to the internal handoff position, gripper retracted. ~5.5 s. |
| `c1` | `02` | 18 bytes | Alignment/save-position command |
| `c0` | `02` | none | Home all axes |
| `cd` | `02` | 26 bytes | Verify pickup; payload begins with ASCII `PASSWORD`. **Bench-confirmed 2026-05-29:** picks the plate from the internal handoff and presents it to an EXTERNAL drop-off position OUTSIDE the equipment — it does NOT store to the output stack. ~7.7 s. The captured `PASSWORD` payload was accepted as-is (not a one-shot token). |
| `e2` | `02` | none | Move one plate from input stack to output stack |
| `bc` | `02` | none | Move one plate from output stack to input stack |

The "move all" captures repeat the one-plate command until the device reports a non-success payload.

## Calibration Workflow Notes

The alignment captures map to this Gen5 workflow:

0. Test COM8 connection: `bd`.
1. Home gripper and plate holder: setup/status commands followed by `c0`.
2. Align Z axis to the drop/pickup location: `c1`, `ad`, and repeated `ae` move-down commands.
3. Save Z location: `b0` plus `c1`.
4. Verify drop/pickup: `b9` then `cd`.

For `ae`, the second little-endian 16-bit value in the payload matches the requested move-down amount:

| Capture | Payload bytes | Value |
| --- | --- | --- |
| `2_movedown_1.csv` | `01 00` | `1` |
| `2_movedown_5.csv` | `05 00` | `5` |
| `2_movedown_20.csv` | `14 00` | `20` |
| `2_movedown_100.csv` | `64 00` | `100` |
| `2_movedown_400.csv` | `90 01` | `400` |

`4_verify_step1.csv` succeeded with status `00 80 00 00`. `4_verify_step2_failed_pickup.csv` returned `01 80 01 17` when no plate was picked up.

## Standalone Control Assessment

The captures support standalone COM8 control without Gen5 or Cytation. Gen5 appears to be a serial client that opens COM8, applies standard serial settings, writes binary frames, and waits for ACK/status frames from the BioStack.

This is enough evidence to build a standalone driver for known operations:

- connection/status check
- home all axes
- stack-to-stack single-plate moves
- repeated "move all" behavior
- Z alignment move-down by numeric step
- save/verify calibration flows, with caution around the `PASSWORD` payload in `cd`

The remaining unknowns are the complete status/error code table, exact meaning of setup/config payloads, and which calibration commands are safe to expose as normal operations.

## Mechanical Workflow Notes

For `move_plate_from_input_to_output_1`, the plate carrier moves to the input stack, takes the bottom plate, moves to the output stack, and inserts the plate into the bottom slot.

For `move_plate_from_output_to_input_1`, the same bottom-plate transfer happens in the opposite direction: output stack to input stack.

So `e2` and `bc` should be treated as stack-to-stack transfer commands, not as commands that present a plate to an external reader nest or arbitrary handoff position.

## Bench Observations (2026-05-29, first hardware session)

Driver run against real COM8 hardware (via an explicit `SerialTransport`;
`config.toml` is pinned `dry_run = true` for the dashboard service, so the
default `BioStack4()` constructor still simulates — bench runs must pass a
`SerialTransport` explicitly or flip the flag).

- **Step 0 (wire check):** PASS. `bd` → `00 80 00 00`, ~49 ms round-trip.
- **Step 1 (home):** PASS. `c0` ×5, all `00 80 00 00`, ~21.3 s each, clean
  homing confirmed by operator.
- **Step 2 (command-role disambiguation):** mapping confirmed; the two
  motions implement an *external hand-off*, so the API was renamed (below).
  - `stage_plate()` → `b9` (was `drop_plate`): plate moved input stack →
    internal handoff, gripper retracted. `success`, ~5.5 s.
  - `present_plate()` → `cd` (was `pickup_plate`): plate moved handoff →
    **external drop-off position outside the equipment**, NOT into an
    output stack. `success`, ~7.7 s. The captured `PASSWORD` payload was
    accepted, so it is not a one-shot session token (de-risks that PLAN.md
    item).

### Resolution (2026-05-29): external hand-off

`b9`/`cd` are the calibration **verify drop/pickup** pair (Gen5 workflow
step 4), and physically they implement *input-stack → handoff → external
hand-off out*. They are NOT internal restacking.

Team decision: the lab wants the **external hand-off** behaviour (present a
plate out for the xArm / a reader nest). Accordingly:

- `drop_plate` → renamed `stage_plate` (`b9`): input stack → internal handoff.
- `pickup_plate` → renamed `present_plate` (`cd`): handoff → external drop-off.
- Docstrings corrected to describe the external hand-off; the old
  "store onto the output stack" wording was wrong and is removed.

Internal input→output restacking (`e2`) and output→input (`bc`) remain
**UNTESTED** on hardware and are not exposed in the public API. If internal
restacking is ever needed, `e2`/`bc` are the commands to characterise — not
the `b9`/`cd` verify pair.

**Future work (requested 2026-05-29): return a plate to the output stack.**
The lab will eventually want the BioStack to take a plate back and store it
on the output stack. Note this is a *handoff → output stack* motion, which is
NOT covered by any command observed so far: `b9`/`cd` are the external
hand-off pair, and `e2`/`bc` are *internal stack-to-stack* (input↔output) and
never visit the handoff/external position. Getting this capability requires a
fresh Gen5 capture of exactly that operation (then replay as a new recorded
sequence), or bench characterisation of `e2`/`bc` if a fully internal
transfer turns out to be sufficient. Until then it is unsupported.

Step 2's original "land in the output stack" pass criterion no longer
applies; the criterion is now "plate is presented to the external drop-off".
The sign-off row reflects that the behaviour is understood and the API
renamed. The external-handoff loop was subsequently exercised in Step 5
(3× `stage_plate`→`present_plate` cycles, clean), with the only failure
being an operator-sequencing one: a 4th `present_plate` on an empty
handoff latched the device (`01 80 00 17`, power-cycle to recover) — the
Step 4 hazard, not a loop defect.

### Step 3 setup-command sufficiency (2026-05-29, partial)

Our macros only ever send `bd` + `be` + the action (the captured `eb`/`f1`
are never replayed), so the only setup command to test is `be`.

- **`be` is NOT required for `c0` (home).** Sending `bd → c0` (no `be`)
  homed cleanly: `00 80 00 00`, ~21.1 s — identical to the full
  `bd → be → c0` sequence. The `be` frame is cargo-culted from the Gen5
  capture and can be dropped from `HOME`.
- **`be` sufficiency for `b9`/`cd` (the plate actions) is still OPEN** — it
  needs a plate in the input stack to test a *successful* action with `be`
  removed (running `b9` into an empty stack only yields the `01 80 02 16`
  exhaustion code, which is inconclusive for `be`-sufficiency). Defer until a
  plate is loaded.

### Step 4 error codes + recovery finding (2026-05-29)

Bench session, input stack empty / handoff empty:

| Trigger | Status payload | Notes |
| --- | --- | --- |
| `cd` (`present_plate`) with no plate at the handoff | `01 80 00 17` | "No plate to pick up." Distinct from the previously-captured `01 80 01 17` (3rd byte `00` vs `01`) and `01 80 00 16`. Not yet mapped to a subclass. |
| `bd` (status) immediately after the above failure | `01 80 00 17` | **The error is sticky** — `bd` echoes the latched error on the next call (even after closing/reopening the port). |
| `c0` (`home`) while latched | `01 80 0e 02` | Home ran only ~2.4 s (vs ~21 s healthy) then faulted with a NEW code; this code then became the latched state. Homing does NOT clear the `01 80 00 17` latch and pushed the device into a further fault. |
| `b9` (`stage_plate`) with the input stack empty | `01 80 02 16` | Captured cleanly during Step 5 end-of-run (`b9` from a healthy state). **NOT sticky** — the follow-up `bd` returned `00 80 00 00`; the device stayed healthy, no power-cycle needed. This is the graceful "ran out of plates" signal for the stage→present loop. |

**Status-code families (working interpretation).** The 3rd byte appears to
be a sub-field and the 4th byte the primary code:

- `01 80 ?? 16` = **stack exhausted / no plate to take** (`00 16` move-all
  terminal, `02 16` `b9` on empty input). Non-latching, recoverable.
- `01 80 ?? 17` = **pickup/grip failure at the handoff** (`00 17` `cd` with
  no plate, `01 17` failed pickup from the old capture). The `cd` `00 17`
  case **latches** the device (see below).
- `01 80 0e 02` = motion/home fault (only seen while already latched).

So the exception mapping should likely key on the 4th byte (`16` →
`StackEmptyError`, `17` → `NoPlatePickedUpError`) rather than matching all
four bytes, which currently misses `02 16` and `00 17`.

**Recovery:** the `cd`/`01 80 00 17` latch could not be cleared by software. A **physical power-cycle**
of the BioStack cleared it — confirmed 2026-05-29: after power-on (allow a
few seconds to boot; the first `bd` may time out with no ACK while booting),
`bd` returned `00 80 00 00` again. Inspect the carrier/gripper for a jam
before powering back on.

**Driver design implications (do before exposing `/control/*`):**

1. Every macro currently begins with a `bd` status check and treats a
   non-success `bd` as a fatal abort. Once the device latches an error,
   `bd` fails, so *no* macro — including `home` — can run. The driver locks
   itself out of its own recovery path. `home` (and any future reset) must
   be able to run without a passing `bd` pre-check.
2. The dangerous case is specifically the **`cd` "no plate at the handoff"**
   failure (`01 80 00 17`), which **latches** the device with no software
   recovery (`c0`/home makes it worse — see `01 80 0e 02`); a power-cycle is
   required. By contrast, stack exhaustion on `b9` (`01 80 02 16`, the normal
   end of a stage→present run) is **graceful and non-latching** — Step 5
   confirmed the device stays healthy. So a stage→present loop that ends when
   the input stack empties is safe; what must be avoided/handled is calling
   `present_plate` when no plate is actually at the handoff. Finding a
   software clear for the `cd` latch (likely via a fresh Gen5 capture) is
   still worthwhile before an unattended control surface, but it is no longer
   blocking for the basic loop.
3. `01 80 0e 02` is unmapped and appears to be a motion/home fault. Capture
   more instances before assigning a subclass.

## Safety Notes

Do not send active commands to the BioStack unless someone is physically present, the moving parts are clear, and a plate jam can be handled immediately.

The first implementation should be a decoder/replayer in dry-run mode. Active command sending should require an explicit unsafe flag and should start with status-only commands.
