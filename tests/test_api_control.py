"""TestClient tests for the v1.1 claim + control surface.

All tests run against ``DryRunTransport`` with ``dry_run=False`` so the
state machine flows ``requires_init`` -> (lifespan opens the port) ->
``ready`` and the macros execute against the in-memory simulator. No
hardware is involved.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from agilent_biostack4.api import create_app
from agilent_biostack4.frames import SUCCESS_STATUS
from agilent_biostack4.service import BioStack4Service
from agilent_biostack4.transport import DryRunTransport

# Failure payloads, keyed on the macro's terminal command byte (see
# recorded_sequences + the _FAILURE_EXCEPTIONS map in biostack.py).
_STACK_EMPTY = b"\x01\x80\x02\x16"  # b9 on empty input stack -> StackEmptyError
_LATCH = b"\x01\x80\x00\x17"  # cd into empty handoff -> NoPlatePickedUpError
_B9 = 0xB9
_CD = 0xCD


def _make(transport: DryRunTransport | None = None, **kwargs) -> BioStack4Service:
    return BioStack4Service(
        dry_run=False,
        transport=transport if transport is not None else DryRunTransport(),
        **kwargs,
    )


def _claim(client: TestClient) -> dict[str, str]:
    """Acquire a claim and return the ``X-Claim-Token`` header dict."""
    r = client.post(
        "/control/claim",
        json={"owner": "agent:test", "session_id": "wf-1", "ttl_s": 30},
    )
    assert r.status_code == 200, r.text
    return {"X-Claim-Token": r.json()["claim_token"]}


# ---------------------------------------------------------------------------
# Claim protocol
# ---------------------------------------------------------------------------


def test_claim_lifecycle() -> None:
    app = create_app(service=_make())
    with TestClient(app) as client:
        hdr = _claim(client)
        # heartbeat extends; release is idempotent.
        assert client.post("/control/heartbeat", headers=hdr).status_code == 200
        assert client.post("/control/release", headers=hdr).status_code == 204
        assert client.post("/control/release", headers=hdr).status_code == 204
        # heartbeat after release -> 401.
        assert client.post("/control/heartbeat", headers=hdr).status_code == 401


def test_claim_conflict_for_second_session() -> None:
    app = create_app(service=_make())
    with TestClient(app) as client:
        _claim(client)
        r = client.post(
            "/control/claim",
            json={"owner": "agent:b", "session_id": "wf-2", "ttl_s": 30},
        )
        assert r.status_code == 409
        body = r.json()
        # Top-level rejection body (not wrapped in {"detail": ...}).
        assert body["claimed_by"]["session_id"] == "wf-1"
        assert "Retry-After" in r.headers


def test_claimed_by_published_on_status() -> None:
    app = create_app(service=_make())
    with TestClient(app) as client:
        assert client.get("/status").json()["details"]["claimed_by"] is None
        _claim(client)
        claimed = client.get("/status").json()["details"]["claimed_by"]
        assert claimed["owner"] == "agent:test"
        assert claimed["session_id"] == "wf-1"


# ---------------------------------------------------------------------------
# X-Claim-Token enforcement: 423, and 423 ahead of 412
# ---------------------------------------------------------------------------


def test_tokenless_control_is_locked() -> None:
    app = create_app(service=_make())
    with TestClient(app) as client:
        for path in (
            "/control/startup",
            "/control/home",
            "/control/stage_plate",
            "/control/present_plate",
            "/control/handoff",
        ):
            r = client.post(path)
            assert r.status_code == 423, path
            assert "claimed_by" in r.json()


def test_423_fires_ahead_of_412() -> None:
    # Tokenless present_plate with NO staged plate must 423 (claim gate),
    # not 412 (precondition) — claim enforcement runs first.
    app = create_app(service=_make())
    with TestClient(app) as client:
        r = client.post("/control/present_plate")
        assert r.status_code == 423


# ---------------------------------------------------------------------------
# Control happy paths + staged-plate flag transitions
# ---------------------------------------------------------------------------


def test_stage_then_present_round_trip() -> None:
    app = create_app(service=_make())
    with TestClient(app) as client:
        hdr = _claim(client)

        # Fresh: ready, not staged -> stage_plate offered, present_plate not.
        allowed = set(client.get("/status").json()["allowed_actions"])
        assert "stage_plate" in allowed and "present_plate" not in allowed

        assert client.post("/control/stage_plate", headers=hdr).status_code == 200
        body = client.get("/status").json()
        assert body["details"]["plate_staged"] is True
        allowed = set(body["allowed_actions"])
        # allowed_actions swaps: now present_plate, no stage_plate/handoff.
        assert "present_plate" in allowed
        assert "stage_plate" not in allowed and "handoff" not in allowed

        assert client.post("/control/present_plate", headers=hdr).status_code == 200
        body = client.get("/status").json()
        assert body["details"]["plate_staged"] is False
        assert "stage_plate" in set(body["allowed_actions"])


def test_handoff_round_trip_resets_staged() -> None:
    app = create_app(service=_make())
    with TestClient(app) as client:
        hdr = _claim(client)
        assert client.post("/control/handoff", headers=hdr).status_code == 200
        # handoff stages then presents -> ends not staged.
        assert client.get("/status").json()["details"]["plate_staged"] is False


def test_home_does_not_change_staged() -> None:
    app = create_app(service=_make())
    with TestClient(app) as client:
        hdr = _claim(client)
        client.post("/control/stage_plate", headers=hdr)
        assert client.post("/control/home", headers=hdr).status_code == 200
        assert client.get("/status").json()["details"]["plate_staged"] is True


# ---------------------------------------------------------------------------
# Staged-plate precondition (412)
# ---------------------------------------------------------------------------


def test_present_without_staged_returns_412_and_does_not_mutate_last_error() -> None:
    app = create_app(service=_make())
    with TestClient(app) as client:
        hdr = _claim(client)
        r = client.post("/control/present_plate", headers=hdr)
        assert r.status_code == 412
        body = r.json()
        assert body == {
            "detail": "No plate staged at the handoff",
            "plate_staged": False,
            "required": "stage_plate first",
        }
        # 412 refusals MUST NOT populate last_error (§6.3).
        assert client.get("/status").json()["last_error"] is None


def test_stage_when_already_staged_returns_412() -> None:
    app = create_app(service=_make())
    with TestClient(app) as client:
        hdr = _claim(client)
        client.post("/control/stage_plate", headers=hdr)
        r = client.post("/control/stage_plate", headers=hdr)
        assert r.status_code == 412
        assert r.json()["plate_staged"] is True


def test_present_with_precondition_disabled_runs() -> None:
    # Emergency override: with the precondition off, present_plate is not
    # blocked even when nothing is staged.
    app = create_app(service=_make(enforce_stage_precondition=False))
    with TestClient(app) as client:
        hdr = _claim(client)
        # No 412; the macro runs against the dry-run transport.
        assert client.post("/control/present_plate", headers=hdr).status_code == 200


# ---------------------------------------------------------------------------
# Mirror invariant (§6.2): X in allowed_actions iff a POST would NOT 412
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("staged", [False, True])
def test_allowed_actions_mirrors_412(staged: bool) -> None:
    service = _make()
    service._plate_staged = staged
    allowed = set(service._allowed_actions("ready"))
    for action in ("stage_plate", "present_plate", "handoff"):
        would_412, _ = service.evaluate_stage_precondition(action)
        assert (action in allowed) == (not would_412), (action, staged)


# ---------------------------------------------------------------------------
# last_error lifecycle (§6.3 / §6.4)
# ---------------------------------------------------------------------------


def test_last_error_set_then_cleared_by_next_success() -> None:
    transport = DryRunTransport()
    transport.set_response(_B9, _STACK_EMPTY)  # stage_plate will fail
    app = create_app(service=_make(transport))
    with TestClient(app) as client:
        hdr = _claim(client)

        # Failure records last_error with the right code; device stays ready.
        r = client.post("/control/stage_plate", headers=hdr)
        assert r.status_code == 409
        body = client.get("/status").json()
        assert body["last_error"]["code"] == "stack_empty"
        assert body["equipment_status"] == "ready"

        # A 412 in between must NOT clear last_error.
        assert client.post("/control/present_plate", headers=hdr).status_code == 412
        assert client.get("/status").json()["last_error"]["code"] == "stack_empty"

        # Next successful operational 2xx clears it.
        transport.set_response(_B9, SUCCESS_STATUS)
        assert client.post("/control/stage_plate", headers=hdr).status_code == 200
        assert client.get("/status").json()["last_error"] is None


def test_latch_puts_device_in_error_and_blocks_everything() -> None:
    transport = DryRunTransport()
    transport.set_response(_CD, _LATCH)  # present_plate latches
    app = create_app(service=_make(transport))
    with TestClient(app) as client:
        hdr = _claim(client)
        client.post("/control/stage_plate", headers=hdr)  # stage so present is allowed
        r = client.post("/control/present_plate", headers=hdr)
        assert r.status_code == 409  # latch surfaced
        body = client.get("/status").json()
        assert body["equipment_status"] == "error"
        assert body["last_error"]["code"] == "no_plate_picked_up"
        assert body["allowed_actions"] == []
        # Everything is now refused with 409 (latched), even home.
        assert client.post("/control/home", headers=hdr).status_code == 409


# ---------------------------------------------------------------------------
# busy surfacing: /status returns busy without blocking on the macro
# ---------------------------------------------------------------------------


def test_status_reports_busy_during_slow_macro() -> None:
    service = _make()
    started = threading.Event()
    release = threading.Event()

    def slow_home() -> None:
        started.set()
        assert release.wait(timeout=5.0), "release event never set"

    app = create_app(service=service)
    with TestClient(app) as client:
        hdr = _claim(client)
        # Replace the bound driver method with a blocking stub.
        service._driver.home = slow_home  # type: ignore[method-assign]

        result: dict[str, int] = {}

        def fire() -> None:
            result["code"] = client.post("/control/home", headers=hdr).status_code

        worker = threading.Thread(target=fire)
        worker.start()
        try:
            assert started.wait(timeout=5.0), "macro never started"
            # Macro is mid-flight (holds _op_lock); /status must not block and
            # must report busy.
            body = client.get("/status").json()
            assert body["equipment_status"] == "busy"
            assert body["message"] == "Running home"
            assert body["allowed_actions"] == []
        finally:
            release.set()
            worker.join(timeout=5.0)
        assert result["code"] == 200
        # After completion the device is ready again.
        assert client.get("/status").json()["equipment_status"] == "ready"
