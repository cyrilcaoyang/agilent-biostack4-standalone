# BioStack 4 — v1.1 Control API PR Plan

Status: **planning** (not started). Bench sign-off (`PHYSICAL_TESTS.md` steps
0–5) is complete and the failure-code mapping was fixed + re-confirmed on real
hardware 2026-06-01, so the gate that held this PR back is now cleared.

This document is the implementation plan for adding the `/control/*` write
surface and the STATUS_SPEC v1.1 claim protocol to the BioStack 4 service. It
is meant to be picked up cold — read it top to bottom before writing code.

---

## 1. Goal

Turn the read-only v1.0 service into a v1.1 device that can actually move
plates over HTTP, **without ever being able to drive the device into the
`01 80 00 17` latch** (the sticky failure that requires a physical
power-cycle — see `PROTOCOL_NOTES.md` "Step 4").

Concretely, when this PR lands:

- `GET /` and `GET /status` report `protocol_version: "1.1"`.
- `POST /control/{claim,heartbeat,release}` implement the cooperative claim
  protocol with **hard `X-Claim-Token` enforcement** (HTTP 423 on miss).
- `POST /control/{startup,shutdown,home,stage_plate,present_plate}` wrap the
  driver macros, gated by claims and by a **staged-plate precondition** that
  makes the latch trap unreachable through the API.
- `allowed_actions` is state-driven and **mirrors** the 412/precondition
  refusals exactly (STATUS_SPEC §6.2).
- `details.claimed_by` is populated while a claim is held.

Non-goals (explicitly out of scope for this PR):

- Internal stack-to-stack transfer (`e2`/`bc`) — untested on hardware, not
  exposed (see `PROTOCOL_NOTES.md` "Future work").
- Return-a-plate-to-the-output-stack — no captured command exists yet.
- Any async/cancellable "stop mid-motion" — the macros are blocking and the
  device has no safe abort; see §6.

---

## 2. Prior art to mirror

The fleet already has three v1.1 device repos. **`agilent-plateloc-server` is
the closest template** and lives on this same PC:

- `../agilent-plateloc-server/src/agilent_plateloc_server/claims.py` —
  `ClaimStore` (single-holder, TTL, idempotent re-claim, `secrets.compare_digest`).
  This repo **already vendors the identical claim models** (`ClaimRequest`,
  `ClaimResponse`, `ClaimRejection`, `ClaimedBy` in `models.py`), so this file
  can be copied in almost verbatim — only the `from .models import …` line and
  the TTL bounds need a glance.
- Plateloc's `service.py` / API layer — for the 412 precondition pattern
  (`evaluate_<name>_interlock()` called from both `/control/*` and the
  `allowed_actions` builder) and the `last_error` auto-clear-on-2xx policy.
- `filter_every_well` — for the `_move_lock` + `busy` surfacing pattern (a
  blocking motion that must show up as `equipment_status: busy` on `/status`
  without blocking the poll). See `ac-organic-lab/docs/EQUIPMENT_INTEGRATION.md`
  §8.

Contract references (mirrored under `../ac-organic-lab/docs/`):
`STATUS_SPEC.md` §5 (claims), §6 (preconditions/412, `allowed_actions` mirror,
`last_error` auto-clear), and §9 v1.1 conformance checklist.

---

## 3. The safety model (read this first — it drives the design)

The BioStack has **two** "out of plate" failure modes, confirmed on hardware:

| Trigger | Payload | Behaviour |
|---|---|---|
| `stage_plate` (`b9`) on empty **input stack** | `01 80 02 16` | ✅ graceful, **non-latching**, device stays healthy → `StackEmptyError` |
| `present_plate` (`cd`) with **no plate at the handoff** | `01 80 00 17` | ☠️ **latches** — sticky, software can't clear it, `home` makes it worse (`01 80 0e 02`), recovery is a **physical power-cycle** → `NoPlatePickedUpError` |

The latch is catastrophic for an unattended control surface because **every
macro begins with a `bd` status check** and a latched device fails `bd`, so the
driver locks itself out of its own recovery path (`PROTOCOL_NOTES.md` "Driver
design implications" #1).

**Therefore the central invariant of this PR is:**

> The API must make it **impossible** to issue `present_plate` unless a plate
> is known to be staged at the handoff.

Implementation: the service tracks a `_plate_staged: bool` flag in memory.

- Set `True` after a successful `stage_plate`.
- Set `False` after a successful `present_plate`.
- Initialised `False` on startup (conservative: if a plate is physically at the
  handoff after a restart, `present_plate` is blocked until re-staged — blocking
  a real plate is harmless; *not* blocking an empty handoff is the disaster).
- `present_plate` with `_plate_staged == False` → **HTTP 412** with a structured
  body (shape below), and `present_plate` is **omitted from `allowed_actions`**.

Known limitation to document in code + README: the flag is **inferred from the
service's own command history**, not read from the device (the BioStack has no
handoff-occupancy readback in our protocol). An out-of-band move (someone driving
the device by other means, a crash mid-macro) can desync it. The flag is lost on
restart. This is acceptable because the failure direction is always toward
*refusing* `present_plate`, never toward issuing it blindly.

### Optional hardening — a composite `handoff` action

Strongly consider exposing a single `POST /control/handoff` that runs
`stage_plate` **then** `present_plate` as one server-side operation. Because it
always stages immediately before presenting, a remote caller literally cannot
present into an empty handoff. The granular `stage_plate`/`present_plate` stay
available (guarded) for flexibility, but `handoff` becomes the recommended
high-level skill for orchestration. **Decision deferred to §10.**

---

## 4. Files to change

New:

- `src/agilent_biostack4/claims.py` — copy from plateloc, adjust imports.
- `tests/test_claims.py` — claim store unit tests (portable, no hardware).
- `tests/test_api_control.py` — TestClient tests for `/control/*` against
  `DryRunTransport`.

Modified:

- `src/agilent_biostack4/models.py` — flip `PROTOCOL_VERSION` `"1.0"` → `"1.1"`;
  add any control request bodies that need their own schema (most macros take no
  args; `home`/`stage_plate`/`present_plate` are parameterless — a shared empty
  `ControlResponse`/status-echo body is enough).
- `src/agilent_biostack4/service.py` — the bulk of the work (§5, §6).
- `src/agilent_biostack4/api.py` — add the claim + control routes; wire
  `X-Claim-Token` dependency; map `ClaimStore`/precondition errors to HTTP.
- `src/agilent_biostack4/__main__.py` + `api.py` description strings — drop the
  "read-only / control not exposed" language.
- `README.md` — "conforms to lab status spec **v1.1**"; document the claim
  protocol, the `last_error.code` taxonomy, and the staged-plate precondition.
- `config.example.toml` / `config.toml` — add `[service] enforce_claims = true`
  and (optional) `enforce_stage_precondition = true` override flags (§6.5 of the
  spec); document they are emergency-only.
- `pyproject.toml` — bump `version`.

Cross-repo (separate PR, **not** in this repo — `ac-organic-lab` is a read-only
mirror on this PC, owned by the central server; see §8):

- `skills/src/lab_skills/skill_catalog/plate_stacker.py` — **new**; register the
  SkillDefs whose names must exactly match this service's `allowed_actions`.
- `equipment.yaml` — flip the `agilent_biostack` entry `protocol: "1.0"` →
  `"1.1"`.

---

## 5. Concurrency model (must change — the current one will flap the dashboard)

Today `get_status()` holds `self._lock`, and `startup/shutdown` hold the same
lock. If a control macro held that lock for the duration of a `home`
(~21 s), every 2–3 s `/status` poll would block for 21 s → the dashboard tile
flaps to `unknown`. **This must be restructured before adding long control
calls.**

Proposed model:

- `self._op_lock: asyncio.Lock` — serializes control operations (one macro at a
  time). Held for the whole macro, including the blocking `to_thread` call.
- Lightweight in-memory state read by `get_status()` **without** taking
  `_op_lock`: `_connected` (from `driver.is_connected()`), `_busy: bool`,
  `_current_action: str | None`, `_plate_staged: bool`, `_last_error`. Guard
  these with a short, never-blocking `self._state_lock` (or rely on the GIL for
  simple flag reads — but a tiny lock is cleaner).
- A control handler: acquire `_op_lock`; set `_busy=True`, `_current_action=…`;
  run `await asyncio.to_thread(driver.<macro>)`; on success clear `_busy`, update
  `_plate_staged`, clear `_last_error`; on failure record `_last_error`, set the
  state; always release `_op_lock`.
- `get_status()` reports `busy` whenever `_busy` is True, regardless of the lock.

This mirrors `filter_every_well`'s `_move_lock` + busy surfacing.

---

## 6. Control endpoints + the state machine

Skill names (must match the future catalog — see §8). Parameterless unless noted.

| Endpoint | Skill name | Precondition (→ else refuse) |
|---|---|---|
| `POST /control/startup` | `startup` | device not already connected (else no-op 200) |
| `POST /control/shutdown` | `shutdown` | — |
| `POST /control/home` | `home` | not `busy`; **not latched** (see note) |
| `POST /control/stage_plate` | `stage_plate` | `ready`, not `busy`, `_plate_staged == False` |
| `POST /control/present_plate` | `present_plate` | `ready`, not `busy`, **`_plate_staged == True`** |
| `POST /control/handoff` *(optional, §10)* | `handoff` | `ready`, not `busy` |

State → `allowed_actions` (this table **is** the §6.2 mirror; implement via one
helper consulted by both `/status` and each `/control/*` handler):

| `equipment_status` | `_plate_staged` | `allowed_actions` |
|---|---|---|
| `requires_init` | — | `["startup"]` |
| `dry_run` | — | the full set (so the surface is testable in CI) |
| `ready` | `False` | `["shutdown", "home", "stage_plate"]` |
| `ready` | `True` | `["shutdown", "home", "present_plate"]` |
| `busy` | — | `[]` |
| `error` (incl. latched) | — | `[]` |

Notes:

- `stage_plate` is omitted when `_plate_staged == True` (don't stage onto an
  occupied handoff). `present_plate` is omitted when `_plate_staged == False`
  (the latch guard). Exactly one of the two is offered in `ready`.
- **Latched / error recovery is not an API operation.** Once the device latches
  (`01 80 00 17` or the `01 80 0e 02` it produces if you home while latched),
  `bd` fails so no macro can run. The service should surface `equipment_status:
  error` with `last_error` and an actionable message ("power-cycle the BioStack;
  inspect the carrier for a jam"), and otherwise refuse everything. Do **not**
  try to auto-recover by homing — that made it worse on the bench.
- Re: `home` "not latched": every macro starts with `bd`; a latched device fails
  the `bd` step and the macro raises. That's fine (it surfaces error), but it
  means `home` cannot be a recovery path. If we ever want software recovery,
  it needs a `bd`-less reset sequence captured from Gen5 — out of scope here.

### 412 body shapes (distinguishable by shape, per §6.1)

Staged-plate precondition on `present_plate`:

```json
{
  "detail": "No plate staged at the handoff",
  "plate_staged": false,
  "required": "stage_plate first"
}
```

Recovery here is operator-driven (call `stage_plate`), so **no `retry_after_s`**
and no `Retry-After` header (matches the plateloc stage-interlock precedent).

### `last_error` policy (spec §6.3 / §6.4)

- 412 precondition refusals **must not** populate `last_error`.
- `last_error` auto-clears to `null` on the first 2xx from any **operational**
  control endpoint (startup/home/stage/present) — not on claim/heartbeat/
  release/read. Clear it *before* building the echoed status body.
- Define a `last_error.code` taxonomy for the BioStack and document it in the
  README. Proposed `frozenset`: `{"stack_empty", "no_plate_picked_up",
  "latched", "connect_failed", "protocol_error", "command_error"}`. Map driver
  exceptions → codes in the control handler (`StackEmptyError` → `stack_empty`,
  `NoPlatePickedUpError` → `no_plate_picked_up` + set the latched state, etc.).

---

## 7. Claim protocol (§5 of the spec)

- Copy `claims.py` from plateloc. Instantiate one `ClaimStore` in
  `BioStack4Service` (or in `create_app` and inject — match how plateloc does
  it).
- `POST /control/claim` → `ClaimResponse` (200) / `ClaimRejection` (409 with
  `claimed_by`, `retry_after_s`). 503 if `requires_init`/booting (optional).
- `POST /control/heartbeat` → 204 (or 200 with `expires_at`); 401 on unknown/
  expired token.
- `POST /control/release` → 204, idempotent.
- **Hard enforcement:** a FastAPI dependency reads `X-Claim-Token` and calls
  `ClaimStore.validate`; missing/stale → **HTTP 423** *before* any precondition
  check (so tokenless calls 423 ahead of 412 — match plateloc's ordering).
  Gate this on `[service] enforce_claims` (default `true`); document the flag as
  emergency-only.
- `details.claimed_by` ← `await store.current()` in `_build_status()` (read it
  outside `_op_lock`).
- Lifespan teardown calls `store.force_clear()`.

---

## 8. Cross-repo coordination (do not skip — it's a correctness gap if missed)

`allowed_actions` strings **must** equal the SDK catalog's `Skill.name` values
for `kind: plate_stacker`. Today there is **no `plate_stacker` entry** in
`SKILL_REGISTRY` (confirmed against `ac-organic-lab/docs/ROADMAP.md` — the
catalog spans 8 kinds, plate_stacker not among them). So:

1. This repo's PR ships the device surface and picks the skill names.
2. A **companion PR in the central `ac-organic-lab` repo** adds
   `skill_catalog/plate_stacker.py` with matching SkillDefs and flips
   `equipment.yaml` `protocol` to `"1.1"`. **`ac-organic-lab/` on this PC is a
   read-only mirror — make that change on the central server, not here**
   (CLAUDE.md working rules).
3. Keep the names identical across the two PRs. Suggested set:
   `startup`, `shutdown`, `home`, `stage_plate`, `present_plate`
   (+ `handoff` if §10 says yes). Decide dotted vs. flat **once** and use it in
   both places (the rest of the catalog uses dotted families like `stage.in`;
   `stage_plate`/`present_plate` are this device's idiom — pick deliberately).

Until the catalog PR lands, the SDK falls back to `requires_states`, so the
device is still usable; the names just won't resolve to typed skills.

---

## 9. Test plan

Portable (DryRunTransport / TestClient — no hardware, run in CI):

- **Claim store** (`test_claims.py`): acquire → token; idempotent re-acquire same
  session; 409 for a different session; heartbeat extends; expired token → 401;
  release idempotent; `current()` shape.
- **Claim API** (`test_api_control.py`): tokenless `/control/*` → 423; valid
  token → 2xx; 423 fires **ahead of** 412 (tokenless `present_plate` with no
  staged plate returns 423, not 412).
- **Control happy paths** against `DryRunTransport`: startup → ready;
  stage_plate → `_plate_staged True`, allowed_actions swaps; present_plate →
  `_plate_staged False`.
- **Precondition**: `present_plate` while `_plate_staged == False` → 412 with the
  documented body shape; `last_error` untouched.
- **Mirror invariant** (property-style, per §6.2): for every reachable
  (`equipment_status`, `_plate_staged`) combo, `X in allowed_actions` iff a
  hypothetical `POST /control/X` would *not* 412. One parametrized test.
- **`last_error` lifecycle**: force a driver failure (`DryRunTransport.set_response`
  a failure code) → `last_error` set with the right `code`; next successful
  operational 2xx clears it; a 412 in between does **not** clear it.
- **busy surfacing**: simulate a slow macro (patch `to_thread`/transport to
  block) and assert `/status` returns `busy` *without* blocking for the macro
  duration.
- **Status fixtures**: add `tests/fixtures/status_*.json` for `ready` no-claim,
  `ready` claim-held, `requires_init`, and the precondition-blocked shape
  (`allowed_actions` lacking `present_plate`) — per the v1.1 conformance
  checklist.

Hardware (manual, gated by a person at the bench — extend `PHYSICAL_TESTS.md`):

- Full claim/enforcement round-trip from the dashboard tile once it exists.
- One `stage_plate` → `present_plate` over HTTP with a real plate.
- Confirm a tokenless `curl` → 423 and a no-staged-plate `present_plate` → 412
  (and that the 412 path never actually issues `cd`).

---

## 10. Open decisions (resolve before/while implementing)

1. **Composite `handoff` action?** (§3.) Recommended **yes** — it's the only
   way to give orchestration a present-a-plate primitive that's structurally
   incapable of latching. Keep granular stage/present too, guarded.
2. **Skill-name style** — flat (`stage_plate`) vs. dotted (`stack.stage`)?
   Must agree with the catalog PR (§8). Lean flat to match the existing
   `api.py` docstring and driver method names.
3. **`startup`/`shutdown` exposure** — the lifespan already opens the port. Is a
   `/control/startup` worth it beyond a reconnect-after-error affordance? Likely
   yes (matches the fleet and gives the dashboard a button), but it's near-no-op
   in the happy path.
4. **Dry-run `allowed_actions`** — advertise the full set in `dry_run` state (so
   the surface is exercisable in CI/dev) vs. empty. Plan assumes full set; the
   tile shows `dry_run` regardless.

---

## 11. Suggested commit sequence

1. `claims.py` + `test_claims.py` + flip `PROTOCOL_VERSION`; no routes yet.
   (Green tests, no behaviour change on the wire beyond version string.)
2. Concurrency refactor in `service.py` (`_op_lock`/`_state_lock`, busy flag,
   `_plate_staged`), still no routes — pure internal restructure with tests.
3. Claim routes + `X-Claim-Token` dependency + `details.claimed_by` + 423
   enforcement.
4. Control routes (startup/shutdown/home/stage/present [+handoff]) with the
   precondition helper, `allowed_actions` mirror, `last_error` taxonomy + clear.
5. Docs: README v1.1, config flags, `last_error` taxonomy table; status fixtures.
6. (Central repo, separate PR) `skill_catalog/plate_stacker.py` + `equipment.yaml`
   `protocol: "1.1"`.

---

## 12. v1.1 conformance checklist (copy into the PR description, tick on done)

- [ ] `protocol_version: "1.1"` on `/` and `/status`.
- [ ] `/control/{claim,heartbeat,release}` per §5.
- [ ] `allowed_actions` populated, state-driven, **mirrors** 412 refusals.
- [ ] `details.claimed_by` populated while held; cleared on release/expiry.
- [ ] `X-Claim-Token` enforced on `/control/*` (423 on miss) — or README says advisory.
- [ ] Preconditions: 412 with shape-distinguishable body; `allowed_actions`
      omits the gated action; single helper feeds both surfaces.
- [ ] 412 does not mutate `last_error`; `last_error` auto-clears on operational 2xx.
- [ ] Override flags (`enforce_claims`, `enforce_stage_precondition`) default true.
- [ ] Fixtures: ready/no-claim, ready/claim-held, requires_init, precondition-blocked.
- [ ] README says "conforms to lab status spec v1.1".
- [ ] `equipment.yaml` entry `protocol: "1.1"` (central repo).
