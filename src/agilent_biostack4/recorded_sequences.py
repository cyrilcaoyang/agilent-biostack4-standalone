"""Recorded command sequences for the BioStack 4 macro workflows.

These are byte sequences lifted directly from real Gen5 captures in
``port-communication-captures/``. Sending them replays exactly what Gen5
sent, which is the most conservative starting point until each individual
command's payload semantics are decoded.

Each sequence is a tuple of ``(address, command, payload)`` triples. The
``recorded_sequences`` module never builds the wire bytes itself; the
transport layer encodes through :mod:`agilent_biostack4.frames`.

The sequences here are PROVISIONAL until ``PHYSICAL_TESTS.md`` step 2
confirms which command does drop and which does pickup.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecordedStep:
    """One command in a recorded macro sequence."""

    address: int
    command: int
    payload: bytes
    description: str


@dataclass(frozen=True)
class RecordedSequence:
    """An ordered list of recorded steps that together implement a macro."""

    name: str
    steps: tuple[RecordedStep, ...]
    source_capture: str
    notes: str = ""


STATUS = RecordedSequence(
    name="status",
    steps=(
        RecordedStep(0x01, 0xBD, b"", "status / are-you-there query"),
    ),
    source_capture="0_test_comm_com8.csv",
    notes="Single 'bd' query. Used for connection and liveness checks.",
)

HOME = RecordedSequence(
    name="home",
    steps=(
        RecordedStep(0x01, 0xBD, b"", "status check before homing"),
        RecordedStep(
            0x01,
            0xBE,
            bytes.fromhex("01 00 00 00 00 00 00 00 c8 96 5f e8 ed 00 00 00 04 00"),
            "home-context setup ('be' from 1_setup_home.csv)",
        ),
        RecordedStep(0x02, 0xC0, b"", "home all axes"),
    ),
    source_capture="1_setup_home.csv",
    notes=(
        "Replays the setup 'be' command captured immediately before "
        "Gen5's home request. Bench test should confirm whether the 'be' "
        "step is strictly required."
    ),
)

DROP_PLATE = RecordedSequence(
    name="drop_plate",
    steps=(
        RecordedStep(0x01, 0xBD, b"", "status check"),
        RecordedStep(
            0x01,
            0xBE,
            bytes.fromhex("01 00 00 00 00 00 00 00 88 9d 5f e8 ed 00 00 00 04 00"),
            "drop-context setup ('be' from 4_verify_step1.csv #11)",
        ),
        RecordedStep(0x02, 0xB9, b"", "verify/drop: place a plate at handoff (provisional)"),
    ),
    source_capture="4_verify_step1.csv",
    notes=(
        "Provisional mapping. PHYSICAL_TESTS.md step 2 must confirm that "
        "'b9' is the drop command. If the bench test shows it is actually "
        "the pickup command, swap this sequence with PICKUP_PLATE."
    ),
)

PICKUP_PLATE = RecordedSequence(
    name="pickup_plate",
    steps=(
        RecordedStep(0x01, 0xBD, b"", "status check"),
        RecordedStep(
            0x01,
            0xBE,
            bytes.fromhex("01 00 00 00 00 00 00 00 58 9d 5f e8 ed 00 00 00 04 00"),
            "pickup-context setup ('be' from 4_verify_step1.csv #14)",
        ),
        RecordedStep(
            0x02,
            0xCD,
            bytes.fromhex(
                "50 41 53 53 57 4f 52 44 e0 0e 00 00 71 02 00 00 "
                "24 aa 89 4b fb 7f 00 00 04 00"
            ),
            "verify/pickup: pick from handoff and store on output (provisional)",
        ),
    ),
    source_capture="4_verify_step1.csv",
    notes=(
        "Provisional mapping. The 'cd' payload begins with ASCII 'PASSWORD' "
        "followed by what looks like a session/timestamp tail. The exact "
        "tail bytes captured here may need to be regenerated if the device "
        "expects fresh values. PHYSICAL_TESTS.md step 2 covers this."
    ),
)
