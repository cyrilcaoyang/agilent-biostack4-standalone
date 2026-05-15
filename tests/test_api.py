"""HTTP smoke tests for the read-only BioStack 4 service.

The FastAPI app is started against ``DryRunTransport`` so no hardware
or platform-specific bindings are involved. Verifies:

* The spec endpoints (``/``, ``/health``, ``/status``) exist and return
  the right pydantic shape.
* ``GET /status`` reports ``equipment_status: dry_run`` in dry-run mode.
* ``GET /status`` calls do NOT cause the transport to send any frame
  (so dashboard polling cannot wake the device up).
* Hitting the endpoints from outside any claim flow works (no /control/*
  routes mounted yet).
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
    assert body["details"]["read_only"] is True
    assert body["allowed_actions"] == []
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


def test_no_control_routes_mounted(client: TestClient) -> None:
    # Read-only service: every /control/* must 404. The follow-up PR will
    # mount these once PHYSICAL_TESTS.md is signed off.
    for path in (
        "/control/startup",
        "/control/shutdown",
        "/control/home",
        "/control/drop_plate",
        "/control/pickup_plate",
    ):
        response = client.post(path, json={})
        assert response.status_code == 404, path
