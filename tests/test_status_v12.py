"""STATUS_SPEC v1.2 conformance: observed `activity`, spans, `cycles_total`.

`equipment_status` answers "is this stacker healthy"; `activity` answers "is
it moving a plate right now". §2.3 requires the second to come from observed
hardware state, never from the first — here that is the macro-in-flight flag
the control methods already maintain.

The service was well placed for this: `get_status()` deliberately does not
take `_op_lock`, so a poll issued during a ~21 s macro answers immediately.
That is what makes `activity: "running"` observable at all, and these tests
pin it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agilent_biostack4.models import EquipmentStatus
from agilent_biostack4.service import BioStack4Service

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

# §2.3's invariant table, for the states this device reaches.
_INVARIANTS = {
    "busy": "running",
    "ready": "idle",
    "requires_init": "idle",
}

_ALL_MOTION = {"home", "stage_plate", "present_plate", "handoff"}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _service() -> BioStack4Service:
    return BioStack4Service(dry_run=True)


def test_protocol_version_is_1_2() -> None:
    status = _run(_service().get_status())
    assert status.protocol_version == "1.2"


def test_idle_service_reports_idle_with_a_real_span() -> None:
    status = _run(_service().get_status())

    assert status.activity == "idle"
    assert status.activity_since is not None
    assert status.metrics["cycles_total"].value == 0
    assert status.metrics["cycles_total"].unit == "count"


def test_activity_since_is_stable_across_polls() -> None:
    svc = _service()

    first = _run(svc.get_status())
    second = _run(svc.get_status())

    # An unchanged span must not be re-stamped by the act of polling.
    assert first.activity_since == second.activity_since


def test_status_answers_mid_macro_with_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the whole field depends on: `get_status()` never takes
    `_op_lock`, so a poll during a plate move reports the move."""

    async def scenario():
        svc = _service()
        await svc.startup()

        entered = asyncio.Event()
        release = asyncio.Event()

        def slow_stage():
            # Runs in a worker thread via asyncio.to_thread.
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(_wait(), loop).result()

        async def _wait():
            await release.wait()

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(svc._driver, "stage_plate", slow_stage)

        task = asyncio.create_task(svc.stage_plate())
        await entered.wait()

        status = await asyncio.wait_for(svc.get_status(), timeout=1.0)

        release.set()
        await task
        return status, await svc.get_status()

    mid, after = _run(scenario())

    # A simulated device reports the simulation's real activity. Note the
    # top-level state stays `dry_run` — which is exactly why v1.2 is worth
    # having: before `activity`, a dry-run device gave a reader no way to
    # see that work was happening at all, because the one state field was
    # pinned to `dry_run` for the process's whole life.
    assert mid.equipment_status == "dry_run"
    assert mid.activity == "running"
    assert mid.activity_since is not None
    # §2.3: no second concurrent macro may be advertised while one runs.
    assert not _ALL_MOTION.intersection(mid.allowed_actions)

    # Span closes when the macro does, and the move is counted.
    assert after.activity == "idle"
    assert after.activity_since > mid.activity_since
    assert after.metrics["cycles_total"].value == 1


def test_busy_implies_running_on_the_hardware_path() -> None:
    """The §2.3 invariant on the non-simulated path, where `busy` is
    reachable. Both come from the same flag, so they cannot disagree."""

    svc = BioStack4Service(dry_run=False)
    svc.dry_run = False
    svc._driver.is_connected = lambda: True  # type: ignore[method-assign]

    idle = _run(svc.get_status())
    assert idle.equipment_status == "ready"
    assert idle.activity == "idle"

    _run(svc._set_busy("stage_plate"))
    busy = _run(svc.get_status())

    assert busy.equipment_status == "busy"
    assert busy.activity == "running"
    assert busy.allowed_actions == []
    assert busy.activity_since > idle.activity_since


def test_cycles_total_counts_plate_moves_not_homing() -> None:
    """`home` moves the carrier — so it is `running` — but carries no plate,
    so it is not a completed primary operation."""

    svc = _service()
    _run(svc.startup())

    _run(svc.home())
    assert _run(svc.get_status()).metrics["cycles_total"].value == 0

    _run(svc.stage_plate())
    _run(svc.present_plate())
    assert _run(svc.get_status()).metrics["cycles_total"].value == 2


def test_handoff_counts_as_one_delivery() -> None:
    """`handoff` stages then presents internally, but it is one commanded
    plate delivery."""

    svc = _service()
    _run(svc.startup())

    _run(svc.handoff())

    assert _run(svc.get_status()).metrics["cycles_total"].value == 1


def test_cycles_total_is_monotonic_across_a_run() -> None:
    svc = _service()
    _run(svc.startup())

    seen = []
    for _ in range(3):
        _run(svc.handoff())
        seen.append(_run(svc.get_status()).metrics["cycles_total"].value)

    assert seen == sorted(seen) == [1, 2, 3]


@pytest.mark.parametrize(
    "name",
    [
        "status_requires_init.json",
        "status_ready_no_claim.json",
        "status_ready_claim_held.json",
        "status_ready_plate_staged.json",
    ],
)
def test_fixtures_are_v1_2_shaped(name: str) -> None:
    status = EquipmentStatus(**json.loads((_FIXTURES / name).read_text()))

    assert status.protocol_version == "1.2"
    assert status.activity_since is not None
    assert status.metrics["cycles_total"].unit == "count"

    required = _INVARIANTS.get(status.equipment_status)
    if required is not None:
        assert status.activity == required
    if status.activity == "running":
        assert not _ALL_MOTION.intersection(status.allowed_actions)
