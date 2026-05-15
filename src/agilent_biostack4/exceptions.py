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
    """The pickup workflow ran but the device reported no plate was picked up.

    Provisionally bound to status payload ``01 80 01 17`` observed in
    ``4_verify_step2_failed_pickup.csv``.
    """


class StackEmptyError(BioStackCommandError):
    """A stack-to-stack move was attempted with no plates remaining.

    Provisionally bound to status payload ``01 80 00 16`` observed at the
    end of the ``move_all_plates_*`` captures.
    """
