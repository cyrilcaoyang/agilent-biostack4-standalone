"""End-to-end tests for ``BioStack4`` against ``DryRunTransport``.

These run on macOS / Linux / Windows without any hardware. They lock down:

1. The workflow methods replay the exact captured request bytes.
2. Success paths return the canonical success payload.
3. Failure paths raise the expected exception subclasses.
"""

from __future__ import annotations

import pytest

from agilent_biostack4 import (
    BioStack4,
    BioStackCommandError,
    NoPlatePickedUpError,
    StackEmptyError,
)
from agilent_biostack4 import recorded_sequences as seqs
from agilent_biostack4 import frames
from agilent_biostack4.exceptions import BioStackConnectionError
from agilent_biostack4.transport import DryRunTransport


def _make_stacker() -> tuple[BioStack4, DryRunTransport]:
    transport = DryRunTransport()
    stacker = BioStack4(transport=transport)
    return stacker, transport


def test_status_returns_success_payload():
    stacker, transport = _make_stacker()
    stacker.connect()
    try:
        status = stacker.status()
    finally:
        stacker.close()
    assert status.command == 0xBD
    assert status.success is True
    assert status.payload == frames.SUCCESS_STATUS
    assert len(transport.history) == 1
    assert transport.history[0].command == 0xBD


def test_home_replays_recorded_sequence():
    stacker, transport = _make_stacker()
    with stacker:
        stacker.home()
    sent_commands = [(req.address, req.command) for req in transport.history]
    expected = [(step.address, step.command) for step in seqs.HOME.steps]
    assert sent_commands == expected


def test_stage_then_present_workflow():
    stacker, transport = _make_stacker()
    with stacker:
        stage = stacker.stage_plate()
        present = stacker.present_plate()
    assert stage.success is True
    assert present.success is True
    sent_commands = [(req.address, req.command) for req in transport.history]
    expected = [
        *[(step.address, step.command) for step in seqs.STAGE_PLATE.steps],
        *[(step.address, step.command) for step in seqs.PRESENT_PLATE.steps],
    ]
    assert sent_commands == expected


def test_present_failure_raises_no_plate_picked_up():
    stacker, transport = _make_stacker()
    transport.set_response(0xCD, b"\x01\x80\x01\x17")
    with stacker, pytest.raises(NoPlatePickedUpError) as excinfo:
        stacker.present_plate()
    assert excinfo.value.command == 0xCD
    assert excinfo.value.status_payload == b"\x01\x80\x01\x17"


def test_stack_empty_failure_raises_stack_empty():
    stacker, transport = _make_stacker()
    transport.set_response(0xCD, b"\x01\x80\x00\x16")
    with stacker, pytest.raises(StackEmptyError):
        stacker.present_plate()


def test_b9_empty_input_stack_payload_raises_stack_empty():
    """``01 80 02 16`` is the real-hardware ``stage_plate`` empty-stack code.

    Confirmed on the bench 2026-06-01; the 3rd byte (``02``) differs from the
    ``move_all`` terminal ``00 16``, so all-four-byte matching used to miss it
    and fall back to the base exception.
    """
    stacker, transport = _make_stacker()
    transport.set_response(0xB9, b"\x01\x80\x02\x16")
    with stacker, pytest.raises(StackEmptyError) as excinfo:
        stacker.stage_plate()
    assert excinfo.value.command == 0xB9
    assert excinfo.value.status_payload == b"\x01\x80\x02\x16"


def test_cd_no_plate_at_handoff_payload_raises_no_plate_picked_up():
    """``01 80 00 17`` is the real-hardware ``present_plate`` no-plate latch code.

    Confirmed on the bench 2026-06-01; the 3rd byte (``00``) differs from the
    old failed-pickup capture ``01 17``, so it used to miss the subclass.
    """
    stacker, transport = _make_stacker()
    transport.set_response(0xCD, b"\x01\x80\x00\x17")
    with stacker, pytest.raises(NoPlatePickedUpError) as excinfo:
        stacker.present_plate()
    assert excinfo.value.command == 0xCD
    assert excinfo.value.status_payload == b"\x01\x80\x00\x17"


def test_unknown_failure_payload_falls_back_to_base_exception():
    stacker, transport = _make_stacker()
    transport.set_response(0xCD, b"\x01\x80\xff\xfe")
    with stacker, pytest.raises(BioStackCommandError) as excinfo:
        stacker.present_plate()
    assert type(excinfo.value) is BioStackCommandError
    assert excinfo.value.status_payload == b"\x01\x80\xff\xfe"


def test_macro_aborts_on_first_failed_step():
    stacker, transport = _make_stacker()
    transport.set_response(0xBD, b"\x01\x80\x00\x16")
    with stacker, pytest.raises(StackEmptyError):
        stacker.stage_plate()
    assert len(transport.history) == 1
    assert transport.history[0].command == 0xBD


def test_calling_macros_without_connect_raises():
    stacker, _ = _make_stacker()
    with pytest.raises(BioStackConnectionError):
        stacker.status()
