"""The committed status fixtures parse as ``EquipmentStatus`` and carry the
shapes the v1.1 conformance checklist requires:

* ``requires_init``
* ``ready`` with no claim
* ``ready`` with a claim held
* the precondition-blocked shape (``allowed_actions`` lacking ``present_plate``
  when no plate is staged, and lacking ``stage_plate``/``handoff`` when one is)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agilent_biostack4.models import EquipmentStatus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "name",
    [
        "status_requires_init",
        "status_ready_no_claim",
        "status_ready_claim_held",
        "status_ready_plate_staged",
    ],
)
def test_fixture_parses_as_equipment_status(name: str) -> None:
    body = json.loads((FIXTURES / f"{name}.json").read_text())
    status = EquipmentStatus.model_validate(body)
    assert status.protocol_version == "1.1"


def test_requires_init_shape() -> None:
    body = json.loads((FIXTURES / "status_requires_init.json").read_text())
    assert body["equipment_status"] == "requires_init"
    assert body["allowed_actions"] == ["startup"]
    assert body["details"]["claimed_by"] is None


def test_ready_no_claim_shape() -> None:
    body = json.loads((FIXTURES / "status_ready_no_claim.json").read_text())
    assert body["equipment_status"] == "ready"
    assert body["details"]["claimed_by"] is None
    # Not staged: present_plate is omitted (the latch guard).
    assert "present_plate" not in body["allowed_actions"]
    assert "stage_plate" in body["allowed_actions"]


def test_ready_claim_held_shape() -> None:
    body = json.loads((FIXTURES / "status_ready_claim_held.json").read_text())
    assert body["details"]["claimed_by"]["session_id"] == "wf-1"


def test_precondition_blocked_shape() -> None:
    body = json.loads((FIXTURES / "status_ready_plate_staged.json").read_text())
    assert body["details"]["plate_staged"] is True
    # Staged: present_plate offered; stage_plate/handoff omitted.
    assert "present_plate" in body["allowed_actions"]
    assert "stage_plate" not in body["allowed_actions"]
    assert "handoff" not in body["allowed_actions"]
