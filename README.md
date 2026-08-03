# agilent-biostack4-standalone

Standalone Python driver for the Agilent BioStack 4 microplate stacker over
RS-232. No Gen5, no Cytation, no vendor software in the loop.

The driver speaks the BioStack 4's binary framed serial protocol directly,
exposes only the two workflows the lab needs (`stage_plate`, `present_plate`),
and converts any non-success status payload from the device into a typed
Python exception.

**This repo conforms to lab status spec v1.2.** The FastAPI service
implements the read baseline (`/`, `/health`, `/status`), the cooperative
claim protocol (`/control/{claim,heartbeat,release}` with hard
`X-Claim-Token` enforcement), and a guarded motion surface
(`/control/{startup,shutdown,home,stage_plate,present_plate,handoff}`).
The wire types come from the shared
[`sdl-lab-contract`](https://github.com/AccelerationConsortium/sdl-lab-contract)
package rather than a vendored copy.

### Activity and utilization (v1.2)

`equipment_status` answers *is this stacker healthy*; `activity` answers
*is it moving a plate right now*. They are independent, and `activity` is
read off the macro-in-flight flag — never derived from `equipment_status`,
which §2.3 forbids because it would add no information.

**Primary operation** is a **plate move**: `stage_plate` (stack → carrier),
`present_plate` (carrier → instrument), or `handoff` (both, as one
commanded delivery). `home` also reports `activity: "running"` — the
carrier is moving and no second macro may start — but carries no plate, so
it is not counted as a cycle.

| Situation | `equipment_status` | `activity` |
|---|---|---|
| Transport not open | `requires_init` | `idle` |
| Open, no macro running | `ready` | `idle` |
| Plate move or homing in flight | `busy` | `running` |
| Latched (no plate at the handoff) | `error` | `running` until the macro unwinds, then `idle` |
| Dry-run | `dry_run` | observed — the simulation's real activity |

`metrics["cycles_total"]` is the spec's reserved counter (§2.3.1) and
counts completed plate moves. It matters because a stage→present cycle
takes ~21 s, comfortably inside the dashboard's 60 s poll: a sampled
`activity` series does not undercount those moves, it misses them
entirely. The poll-to-poll delta is the accountable number, and it resets
on service restart by contract.

`activity_since` is the instant the current span began, so a reader can
recover an in-flight macro's true elapsed time. While `activity` is
`running`, `allowed_actions` is empty — nothing may start a second macro.

Worth noting for the dry-run row above: the top-level state stays
`dry_run` for the process's whole life, so before v1.2 a simulated stacker
gave a reader no way to see that work was happening at all. `activity` is
where that answer now lives.

`GET /status` never takes the operation lock, so it answers immediately
even while a ~21 s macro is running — which is what makes `running`
observable in the first place.

Bench validation steps 0-5 are signed off (2026-05-29; re-confirmed
2026-06-01; see [PHYSICAL_TESTS.md](PHYSICAL_TESTS.md)).
Read [PLAN.md](PLAN.md) and [PHYSICAL_TESTS.md](PHYSICAL_TESTS.md) before
running any code path that actually opens COM8.

### The staged-plate safety invariant

The BioStack has two "out of plate" failure modes. `stage_plate` (`b9`)
on an empty input stack fails **gracefully** (`StackEmptyError`, device
stays healthy). But `present_plate` (`cd`) into an **empty handoff**
**latches** the device (`01 80 00 17`) — a sticky failure that software
cannot clear (`home` makes it worse) and that requires a physical
power-cycle. Because every macro begins with a `bd` status check that a
latched device fails, the latch locks the driver out of its own recovery.

So the API makes the latch **unreachable**: the service tracks a
`_plate_staged` flag (set after `stage_plate`, cleared after
`present_plate`, `False` on startup) and **refuses `present_plate` with
HTTP 412 unless a plate is known to be staged**. The flag is inferred
from this service's own command history (the protocol has no
handoff-occupancy readback), is lost on restart, and can desync on an
out-of-band move — but the failure direction is always toward *refusing*
`present_plate`, never toward issuing `cd` blindly. The composite
`POST /control/handoff` (stage **then** present in one server-side op) is
the recommended orchestration primitive: it is structurally incapable of
presenting into an empty handoff.

### `last_error.code` taxonomy

`last_error.code` is drawn from a stable set (see
`LAST_ERROR_CODES` in `service.py`):

| `code` | Raised by | Meaning / recovery |
| --- | --- | --- |
| `stack_empty` | `stage_plate`/`handoff` on an empty input stack | Graceful, non-latching. Add plates and retry. |
| `no_plate_picked_up` | `present_plate`/`handoff` into an empty handoff | The sticky **latch**. Device goes to `equipment_status: error`; power-cycle required. |
| `latched` | reserved | Reserved for follow-on failures observed while already latched. |
| `connect_failed` | `startup` / transport open | Serial port could not be opened. |
| `protocol_error` | malformed wire bytes | Bad ACK / header / checksum. |
| `command_error` | any other device-reported failure | Catch-all device failure status. |

Per spec §6.4, `last_error` auto-clears to `null` on the first 2xx from
any operational `/control/*` endpoint; 412 precondition refusals never
touch it (§6.3).

## Status

| Layer | State |
| --- | --- |
| Protocol notes (sniffed) | [`PROTOCOL_NOTES.md`](PROTOCOL_NOTES.md) |
| Frame codec | implemented, unit-tested |
| Dry-run transport | implemented |
| Serial transport | implemented; exercised against hardware 2026-05-29 |
| High-level workflows (`status`, `home`, `stage_plate`, `present_plate`) | implemented as recorded-sequence playback; command roles bench-confirmed 2026-05-29 |
| FastAPI service (`/`, `/health`, `/status`) | implemented; reports spec **v1.2** |
| FastAPI service (claims + guarded `/control/*`) | implemented; hard `X-Claim-Token` enforcement, staged-plate 412 interlock |
| v1.2 activity + utilization | `activity` / `activity_since` observed from the macro-in-flight flag; reserved `cycles_total` counts plate moves |
| Physical validation | steps 0-5 signed off 2026-05-29; re-confirmed 2026-06-01 (see PHYSICAL_TESTS.md) |

## Install (development)

```bash
uv venv
uv pip install -e ".[dev]"
```

## Run the unit tests

The unit tests do not touch hardware and run on macOS / Linux / Windows:

```bash
uv run pytest
```

## Try the driver in dry-run

```python
from agilent_biostack4 import BioStack4
from agilent_biostack4.transport import DryRunTransport

stacker = BioStack4(transport=DryRunTransport())
stacker.connect()
print(stacker.status())
stacker.home()
stacker.stage_plate()    # input stack -> internal handoff
stacker.present_plate()  # handoff -> external drop-off (out of the instrument)
stacker.close()
```

## Run the dashboard service

The service can run on any host (no hardware needed) and the
`ac-organic-lab` dashboard picks it up via the `agilent_biostack`
`adapter: http` entry in `equipment.yaml` (set `protocol: "1.1"`).

```bash
uv pip install -e ".[dev]"
uv run agilent-biostack4-serve --dry-run --port 8030
# then in another shell:
curl http://localhost:8030/status
```

The dry-run tile reports `equipment_status: dry_run` and advertises the
full action set. Deploy on the lab PC with `dry_run = false` in
`config.toml` and the same endpoint reports `ready` / `requires_init` /
`busy` / `error` based on the real serial transport.

To drive a plate over HTTP, acquire a claim first (the `X-Claim-Token`
is enforced on every `/control/*` call):

```bash
TOKEN=$(curl -s -XPOST localhost:8030/control/claim \
  -H 'content-type: application/json' \
  -d '{"owner":"me","session_id":"wf-1","ttl_s":30}' | python -c 'import sys,json;print(json.load(sys.stdin)["claim_token"])')

# stage then present in one latch-safe operation:
curl -XPOST localhost:8030/control/handoff -H "X-Claim-Token: $TOKEN"
curl -XPOST localhost:8030/control/release -H "X-Claim-Token: $TOKEN"
```

A tokenless `/control/*` call returns `423 Locked`; a `present_plate`
with nothing staged returns `412` (and never issues `cd`).

### Emergency override flags

`config.toml` `[service]` carries two interlock flags, both default
`true` and **emergency-only** (never ship `false`):

* `enforce_claims` — hard `X-Claim-Token` enforcement (423 on miss).
* `enforce_stage_precondition` — the staged-plate 412 interlock that
  makes the `cd` latch unreachable. Disabling it lets a remote caller
  drive the device into the sticky latch.

## Run against real hardware (driver only)

Only after [PHYSICAL_TESTS.md](PHYSICAL_TESTS.md) step 0 passes.

```bash
cp config.example.toml config.toml
# edit config.toml: com_port, dry_run = false
uv run python -c "from agilent_biostack4 import BioStack4; \
    b = BioStack4(); b.connect(); print(b.status()); b.close()"
```

## Layout

```
agilent-biostack4-standalone/
+- PLAN.md                       design contract
+- PHYSICAL_TESTS.md             bench validation sign-off
+- PROTOCOL_NOTES.md             byte-level protocol observations
+- pyproject.toml
+- config.example.toml
+- src/agilent_biostack4/        the driver package
+- tests/                        unit tests (no hardware)
+- tools/analyze_captures.py     reproducible CSV summarizer
+- port-communication-captures/  gitignored: raw sniffer CSVs and workflow notes
```

## License

MIT. See [LICENSE](LICENSE).
