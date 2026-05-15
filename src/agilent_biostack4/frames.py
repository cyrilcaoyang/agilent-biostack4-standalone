"""Binary frame codec for the BioStack 4 serial protocol.

The protocol is inferred from sniffed Gen5 traffic in
``port-communication-captures/``. See ``PROTOCOL_NOTES.md`` for the
byte-level analysis.

Request frame::

    01 <addr> <cmd> 0d 01 01 00 <plen_le16> <chk_le16>
    [<payload bytes if plen > 0>]

Response frame::

    06                                                  # 1-byte ACK
    01 00 <cmd> 0d 02 00 00 <plen_le16> <chk_le16>      # response header
    <payload bytes>                                     # always 4 bytes in captures

Checksum invariant::

    (sum(header[:9]) + sum(payload) + chk_le16) & 0xFFFF == 0

This module is pure functions over ``bytes``. No I/O. Fully unit-testable
on macOS / Linux without hardware.
"""

from __future__ import annotations

from dataclasses import dataclass

HEADER_LENGTH = 11
ACK_BYTE = 0x06
REQUEST_PREFIX = b"\x01"
REQUEST_DIRECTION = b"\x0d\x01\x01\x00"
RESPONSE_PREFIX = b"\x01\x00"
RESPONSE_DIRECTION = b"\x0d\x02\x00\x00"
SUCCESS_STATUS = b"\x00\x80\x00\x00"


@dataclass(frozen=True)
class RequestFrame:
    """A request frame ready to send to the BioStack.

    ``raw`` is the full byte sequence: 11-byte header followed by the
    payload (if any).
    """

    address: int
    command: int
    payload: bytes
    raw: bytes


@dataclass(frozen=True)
class ResponseFrame:
    """A response frame received from the BioStack.

    ``status_payload`` is the 4-byte body that carries success / failure.
    ``success`` is true when ``status_payload`` equals ``SUCCESS_STATUS``.
    """

    command: int
    status_payload: bytes
    success: bool


def compute_checksum(header_without_checksum: bytes, payload: bytes = b"") -> int:
    """Compute the 16-bit checksum so that the protocol invariant holds.

    The protocol invariant is::

        (sum(header[:9]) + sum(payload) + chk_le16) & 0xFFFF == 0

    so the checksum is the two's-complement of the data sum.
    """
    if len(header_without_checksum) != 9:
        raise ValueError(
            "header_without_checksum must be 9 bytes (the header up to but "
            f"not including the checksum); got {len(header_without_checksum)}"
        )
    total = sum(header_without_checksum) + sum(payload)
    return (-total) & 0xFFFF


def verify_checksum(header: bytes, payload: bytes = b"") -> bool:
    """Return True if the header's embedded checksum is valid for the data."""
    if len(header) != HEADER_LENGTH:
        return False
    chk = header[9] | (header[10] << 8)
    total = sum(header[:9]) + sum(payload) + chk
    return total & 0xFFFF == 0


def encode_request(address: int, command: int, payload: bytes = b"") -> RequestFrame:
    """Encode a request frame for ``command`` to ``address`` with ``payload``.

    Parameters
    ----------
    address:
        Subsystem address byte. Captures show ``0x01`` for status / setup
        commands and ``0x02`` for motion commands.
    command:
        Command byte, e.g. ``0xbd`` for status, ``0xc0`` for home, ``0xb9``
        and ``0xcd`` for the verify/drop/pickup family.
    payload:
        Optional payload bytes. Length encoded as ``plen_le16`` in the
        header. May be empty.
    """
    if not 0 <= address <= 0xFF:
        raise ValueError(f"address must fit in one byte, got {address}")
    if not 0 <= command <= 0xFF:
        raise ValueError(f"command must fit in one byte, got {command}")
    if len(payload) > 0xFFFF:
        raise ValueError(f"payload too long: {len(payload)} bytes")

    plen = len(payload)
    header_without_checksum = (
        REQUEST_PREFIX
        + bytes([address, command])
        + REQUEST_DIRECTION
        + bytes([plen & 0xFF, (plen >> 8) & 0xFF])
    )
    chk = compute_checksum(header_without_checksum, payload)
    header = header_without_checksum + bytes([chk & 0xFF, (chk >> 8) & 0xFF])
    return RequestFrame(
        address=address,
        command=command,
        payload=bytes(payload),
        raw=header + bytes(payload),
    )


def parse_response_header(header: bytes, expected_command: int | None = None) -> int:
    """Validate a response header and return the embedded payload length.

    The response header always announces a 4-byte status payload in the
    captures, but the field is parsed properly so any future variants are
    handled.

    Parameters
    ----------
    header:
        Exactly ``HEADER_LENGTH`` bytes.
    expected_command:
        If given, verify the response is for this command byte. Useful for
        catching desynchronisation when many transactions run back to back.

    Raises
    ------
    BioStackProtocolError
        Any field mismatch, including bad direction bytes or unexpected
        command echo.
    """
    from .exceptions import BioStackProtocolError

    if len(header) != HEADER_LENGTH:
        raise BioStackProtocolError(
            f"response header must be {HEADER_LENGTH} bytes, got {len(header)}: "
            f"{header.hex(' ')}"
        )
    if header[:2] != RESPONSE_PREFIX:
        raise BioStackProtocolError(
            f"response header prefix mismatch: expected 01 00, got "
            f"{header[:2].hex(' ')}"
        )
    if header[3:7] != RESPONSE_DIRECTION:
        raise BioStackProtocolError(
            f"response header direction mismatch: expected 0d 02 00 00, got "
            f"{header[3:7].hex(' ')}"
        )
    command = header[2]
    if expected_command is not None and command != expected_command:
        raise BioStackProtocolError(
            f"response command mismatch: expected 0x{expected_command:02x}, "
            f"got 0x{command:02x}"
        )
    plen = header[7] | (header[8] << 8)
    return plen


def parse_response(header: bytes, payload: bytes, expected_command: int | None = None) -> ResponseFrame:
    """Parse a complete response (header + payload) into a ``ResponseFrame``.

    Validates the checksum and the protocol-level field structure.
    """
    from .exceptions import BioStackProtocolError

    declared_plen = parse_response_header(header, expected_command=expected_command)
    if len(payload) != declared_plen:
        raise BioStackProtocolError(
            f"response payload length mismatch: header declared {declared_plen}, "
            f"received {len(payload)}"
        )
    if not verify_checksum(header, payload):
        raise BioStackProtocolError(
            f"response checksum invalid: header={header.hex(' ')}, "
            f"payload={payload.hex(' ')}"
        )
    return ResponseFrame(
        command=header[2],
        status_payload=bytes(payload),
        success=bytes(payload) == SUCCESS_STATUS,
    )
