# BioStack 4 Physical Test Plan

Read this end-to-end **before** sending any command over `COM8`.

The code in this repository can drive real moving parts. The protocol mapping
is inferred from sniffed Gen5 captures, not from vendor documentation, and
several command interpretations are provisional. Until the tests below pass,
treat every command as unsafe.

## Hard preconditions before each bench session

1. A human is at the instrument, hand on the lab E-stop or wall power switch.
2. The BioStack stacks (input and output) are empty unless the test calls
   for plates.
3. Nothing is on or near the carrier path. No reader, no plate carrier from
   a Cytation, no aluminum foil. Just open stacker.
4. Gen5 is **closed** on the lab PC. If Gen5 is open it holds COM8 open and
   our `pyserial` `Serial()` will fail with "Access denied".
5. The lab PC's USB-serial adapter is on COM8. Re-check in Windows Device
   Manager. If it has moved (eg. after a reboot), update `config.toml`.

## Step 0 - Wire check (no motion)

Goal: confirm we can open COM8, talk to the BioStack, and read a success
status payload, all without commanding any motion.

1. `uv run python -c "from agilent_biostack4 import BioStack4; b = BioStack4(); b.connect(); print(b.status()); b.close()"`
2. Expected: `StatusPayload(command=0xbd, payload=b'\x00\x80\x00\x00', success=True)`.
3. If it raises `BioStackConnectionError`:
   - Gen5 still open?
   - COM port number changed?
   - USB cable seated?
4. If it raises `BioStackProtocolError`:
   - Some other process owns COM8 (port server, terminal program). Close it.
   - Wrong baud / line settings - re-run with a serial sniffer attached and
     compare to `PROTOCOL_NOTES.md`.

Do not proceed until step 0 is clean.

## Step 1 - Home (motion, no plates)

Goal: confirm the homing command moves the gripper / plate holder to their
home positions without binding.

1. Stacks empty. Carrier path clear.
2. `b.home()`
3. Watch the carrier move to its home pose. Listen for end-of-travel ticking
   or unusual motor noise. If anything is off, hit E-stop and pull power.
4. Expected: `StatusPayload(command=0xc0, payload=b'\x00\x80\x00\x00', success=True)`.

Repeat 3-5 times. Each call should home cleanly.

## Step 2 - Disambiguate `b9` vs `cd` (the critical test)

Goal: prove which of `b9` and `cd` is "drop one plate from input" and which
is "pick up one plate to output". Our current guess is that `b9` is drop and
`cd` is pickup, but it is a guess.

> **RESOLVED 2026-05-29.** `b9` = stage (input stack → internal handoff),
> `cd` = present (handoff → external drop-off OUTSIDE the equipment). The
> two macros implement an external hand-off, not internal restacking, so
> the API was renamed `drop_plate`→`stage_plate` and
> `pickup_plate`→`present_plate`. See `PROTOCOL_NOTES.md` "Bench
> Observations". The original procedure is kept below for the record.

This test requires putting one labeled plate in the input stack and watching
where it ends up.

1. One labeled plate in input stack.
2. Run `b.stage_plate()`.
3. Where did the plate go?
   - At the calibrated handoff position with the gripper retracted: this
     command is doing what its name says. Continue. (This is what happened.)
   - Still in the input stack: command did not move it. Inspect the
     `StatusPayload`. If `success=True`, `stage_plate` is bound to the wrong
     low-level command - swap `b9` <-> `cd` in
     `recorded_sequences.STAGE_PLATE` and `recorded_sequences.PRESENT_PLATE`,
     then retry.
   - Anywhere else: stop. Open `PROTOCOL_NOTES.md`, add the observation,
     do not retry the command until we understand what happened.
4. With the plate at the handoff position, run `b.present_plate()`.
5. Where did it end up?
   - At the external drop-off position outside the equipment: workflow is
     correct. (This is what happened.)
   - Anywhere else: same flow as step 3.

Document the answer in `PROTOCOL_NOTES.md` and update the
`Inferred meaning` column in the command table.

## Step 3 - Setup commands sufficiency

Goal: find out which of the `bd` / `be` / `eb` / `f1` setup commands are
strictly required before `b9` and `cd`. Gen5 sends all of them; we want to
know if we can drop any.

For each command in [`eb`, `f1`, `be`], do one trial where that command is
removed from the macro sequence and the macro is run again with a fresh
plate. Mark the result in `PROTOCOL_NOTES.md`. If removing a setup command
causes a failure, restore it and stop.

`bd` is always sent. We do not test removing it.

## Step 4 - Error-status mapping

Goal: collect more failing status payloads so we can map them to specific
exception subclasses.

1. Empty both stacks.
2. Run `b.present_plate()`. Expect a non-success payload (no plate at the
   handoff). Record the exact 4 bytes.
3. Run `b.stage_plate()` with the input stack empty. Record the exact 4 bytes.
4. Run `b.present_plate()` with the gripper deliberately misaligned (e.g. by
   running step 1 first then nudging the carrier with the power off - **only
   the bench operator decides if this is safe**). Record the bytes.
5. Add each failure to `PROTOCOL_NOTES.md` and promote the most common ones
   to dedicated `BioStackCommandError` subclasses.

## Step 5 - Repeated runs

Goal: confirm the driver can cycle plates end to end (external hand-off).

Note: `present_plate` delivers each plate to the *same* external drop-off
position. An operator (or the receiving robot arm) must clear the drop-off
before the next `present_plate`, or the cycle will jam. Run one plate at a
time with a person clearing the drop-off between cycles.

1. Five labeled plates in the input stack. External drop-off clear.
2. Per cycle (clear the drop-off between cycles):
   ```python
   try:
       b.stage_plate()
       b.present_plate()
   except StackEmptyError:
       pass  # input stack exhausted
   ```
3. Expected: five plates presented out one at a time in stack order, no
   exceptions until the final `stage_plate` after the input stack is empty.

## Sign-off

The driver is allowed to graduate from `adapter: mock` to `adapter: http` in
`ac-organic-lab/equipment.yaml` only after **all** of steps 0-5 have been
performed and the bench operator has initialled them here:

| Step | Date | Operator initials | Notes |
| --- | --- | --- | --- |
| 0 | 2026-05-29 | | PASS (real hardware). `bd` query: `command=0xbd, payload=00 80 00 00, success=True`, round-trip ~49 ms, COM8 opened (is_connected True). Run via explicit SerialTransport to bypass config.toml `dry_run = true`. Earlier same-day attempt was against DryRunTransport (elapsed 0.0) and did not count. |
| 1 | 2026-05-29 | | PASS (real hardware). 5 home cycles (`c0`), all `success, payload=00 80 00 00`, ~21.3 s each, consistent. Carrier homed clean, operator confirmed no binding / abnormal noise. |
| 2 | 2026-05-29 | | Command roles confirmed. `b9` = stage (input→internal handoff, ~5.5 s); `cd` = present (handoff→EXTERNAL drop-off outside the equipment, ~7.7 s) — external hand-off, not internal restacking. `PASSWORD` payload accepted (not one-shot). Team chose external-handoff intent; API renamed `drop_plate`→`stage_plate`, `pickup_plate`→`present_plate`, docstrings corrected. External-handoff repeatability still to be exercised (Step 5). See PROTOCOL_NOTES.md. |
| 3 | 2026-05-29 | | PARTIAL. Only `be` is testable (our macros never send `eb`/`f1`). `be` is NOT required for `c0`/home: `bd → c0` (no `be`) homed cleanly, `00 80 00 00`, ~21.1 s. `be` sufficiency for `b9`/`cd` still OPEN — needs a plate (empty-stack run re-triggers the no-plate latch). See PROTOCOL_NOTES.md "Step 3 setup-command sufficiency". |
| 4 | 2026-05-29 | | Mostly done. Codes captured: `cd`/no-plate-at-handoff → `01 80 00 17` (STICKY, latches; power-cycle to recover); `c0`/home while latched → `01 80 0e 02`; `b9`/empty-input-stack → `01 80 02 16` (graceful, NON-latching, device stayed healthy). Families: `…16`=stack-exhausted, `…17`=pickup-fail. KEY FINDING: macros gate on `bd`, so the `cd` latch locks out all macros incl. home — recovery is a power-cycle. Misalignment trial not done. See PROTOCOL_NOTES.md "Step 4". |
| 5 | 2026-05-29 | | PASS (loop exercised). 3× `stage_plate`→`present_plate` cycles ran clean (each plate staged input→handoff, then presented to the external drop-off), operator clearing the drop-off between cycles. 4th call was a `present_plate` (`cd`) with no plate at the handoff → `01 80 00 17` — the device **latched** (sticky; software `home`/`bd` could not clear it) and was recovered by a physical **power-cycle**. Confirms the Step 4 hazard: a `cd` with nothing at the handoff is the failure mode to avoid, distinct from the graceful `b9`/empty-input `01 80 02 16`. External-handoff loop itself is repeatable; the latch is an operator-sequencing hazard, not a loop defect. See PROTOCOL_NOTES.md "Step 4". |
