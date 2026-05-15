"""Standalone Python driver for the Agilent BioStack 4 microplate stacker.

Talks to the BioStack 4 directly over RS-232 (default COM8) using a
sniffed/inferred binary framed protocol. No Gen5, no reader software.

Quick start::

    from agilent_biostack4 import BioStack4

    stacker = BioStack4()
    stacker.connect()
    stacker.status()
    stacker.home()
    stacker.drop_plate()
    stacker.pickup_plate()
    stacker.close()

This package is pre-bench-validation. See ``PLAN.md`` and
``PHYSICAL_TESTS.md`` in the repository root before driving real hardware.
"""

from .biostack import BioStack4, StatusPayload
from .exceptions import (
    BioStackCommandError,
    BioStackConnectionError,
    BioStackError,
    BioStackProtocolError,
    NoPlatePickedUpError,
    StackEmptyError,
)

__all__ = [
    "BioStack4",
    "BioStackCommandError",
    "BioStackConnectionError",
    "BioStackError",
    "BioStackProtocolError",
    "NoPlatePickedUpError",
    "StackEmptyError",
    "StatusPayload",
]

__version__ = "0.1.0"
