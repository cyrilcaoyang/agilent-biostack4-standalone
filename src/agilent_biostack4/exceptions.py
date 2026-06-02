"""Exception hierarchy for the BioStack 4 driver.

The mapping between specific failure status payloads and exception
subclasses is provisional until bench testing (see ``PHYSICAL_TESTS.md``)
confirms each one. Until then ``BioStackCommandError`` is the catch-all.
"""

from __future__ import annotations


class BioStackError(Exception):
    """Base class for every error raised by this driver."""


class BioStackConnectionError(BioStackError):
    """Could not open or maintain the serial connection to the BioStack."""


class BioStackProtocolError(BioStackError):
    """The bytes coming off the wire did not match the expected protocol.

    Examples: missing ACK, malformed response header, bad checksum,
    unexpected payload length.
    """


class BioStackCommandError(BioStackError):
    """The device acknowledged the command but reported a failure status.

    The original command byte and the raw 4-byte status payload are
    preserved as attributes for diagnostics.
    """

    def __init__(self, command: int, status_payload: bytes, message: str | None = None):
        self.command = command
        self.status_payload = bytes(status_payload)
        text = message or (
            f"BioStack reported failure for command 0x{command:02x}: "
            f"status_payload={self.status_payload.hex(' ')}"
        )
        super().__init__(text)


class NoPlatePickedUpError(BioStackCommandError):
    """A pickup ran but the device reported no plate was picked up.

    Bound to status payloads whose primary code byte (4th byte) is ``17``:
    ``01 80 01 17`` (``4_verify_step2_failed_pickup.csv``) and ``01 80 00 17``
    (``cd``/``present_plate`` with no plate at the handoff — the sticky latch,
    confirmed on hardware 2026-06-01). See ``PROTOCOL_NOTES.md`` "Step 4".
    """


class StackEmptyError(BioStackCommandError):
    """A move was attempted with no plate available to take.

    Bound to status payloads whose primary code byte (4th byte) is ``16``:
    ``01 80 00 16`` (``move_all_plates_*`` terminal) and ``01 80 02 16``
    (``b9``/``stage_plate`` on an empty input stack — graceful, non-latching;
    confirmed on hardware 2026-06-01). See ``PROTOCOL_NOTES.md`` "Step 4".
    """
