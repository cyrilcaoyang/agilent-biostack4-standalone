"""HTTP smoke tests for the BioStack 4 spec endpoints.

The FastAPI app is started against ``DryRunTransport`` so no hardware
or platform-specific bindings are involved. Verifies:

* The spec endpoints (``/``, ``/health``, ``/status``) exist and return
  the right pydantic shape.
* ``GET /status`` reports ``equipment_status: dry_run`` in dry-run mode
  and advertises the full action set (so the surface is exercisable).
* ``GET /status`` calls do NOT cause the transport to send any frame
  (so dashboard polling cannot wake the device up).

Claim + control routes are covered in ``test_api_control.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agilent_biostack4.api import create_app
from agilent_biostack4.service import BioStack4Service
from agilent_biostack4.transport import DryRunTransport


@pytest.fixture()
def transport() -> DryRunTransport:
    return DryRunTransport()


@pytest.fixture()
def client(transport: DryRunTransport) -> TestClient:
    service = BioStack4Service(dry_run=True, transport=transport)
    app = create_app(service=service)
    with TestClient(app) as test_client:
        yield test_client


def test_probe_returns_identity(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["equipment_id"] == "agilent_biostack"
    assert body["equipment_name"] == "Agilent BioStack 4"
    assert body["protocol_version"] == "1.1"


def test_health_returns_healthy(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_status_dry_run_envelope(client: TestClient) -> None:
    response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["protocol_version"] == "1.1"
    assert body["equipment_id"] == "agilent_biostack"
    assert body["equipment_kind"] == "plate_stacker"
    assert body["equipment_status"] == "dry_run"
    assert body["details"]["dry_run"] is True
    assert body["details"]["claimed_by"] is None
    assert body["details"]["plate_staged"] is False
    # dry_run advertises the full action set so the surface is exercisable.
    assert set(body["allowed_actions"]) == {
        "startup",
        "shutdown",
        "home",
        "stage_plate",
        "present_plate",
        "handoff",
    }
    assert "transport" in body["components"]
    assert body["components"]["transport"]["connected"] is True


def test_status_does_not_send_frames(
    client: TestClient, transport: DryRunTransport
) -> None:
    # Capture history baseline (lifespan may have opened transport but
    # the driver doesn't send any frame during connect()).
    baseline = list(transport.history)
    for _ in range(3):
        assert client.get("/status").status_code == 200
    assert transport.history == baseline


def test_control_routes_mounted_and_claim_gated(client: TestClient) -> None:
    # Control routes are mounted (no 404) and hard-gated: a tokenless POST
    # returns 423 Locked, never 404.
    for path in (
        "/control/startup",
        "/control/shutdown",
        "/control/home",
        "/control/stage_plate",
        "/control/present_plate",
        "/control/handoff",
    ):
        response = client.post(path, json={})
        assert response.status_code == 423, path
