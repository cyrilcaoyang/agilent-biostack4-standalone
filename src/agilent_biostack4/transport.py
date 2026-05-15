"""Transports for the BioStack 4 driver.

Two implementations behind one :class:`Transport` protocol:

* :class:`SerialTransport` - real ``pyserial`` connection. Opens the COM
  port with the settings captured from Gen5 and runs each request through
  the full ACK + header + payload read cycle.

* :class:`DryRunTransport` - in-memory simulator. Returns the success
  status payload for the small set of commands the high-level driver
  uses; raises :class:`BioStackProtocolError` for anything else. Lets us
  exercise the driver and (later) the FastAPI service without hardware.

The :class:`Response` dataclass captures the elapsed wall-clock time so
the high-level code can log slow transactions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from . import frames
from .exceptions import BioStackConnectionError, BioStackProtocolError


@dataclass(frozen=True)
class Response:
    """Result of a single round-trip."""

    request: frames.RequestFrame
    response: frames.ResponseFrame
    elapsed_seconds: float


class Transport(Protocol):
    """Wire-level interface used by :class:`BioStack4`."""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def is_open(self) -> bool: ...

    def transact(
        self,
        address: int,
        command: int,
        payload: bytes = b"",
        *,
        timeout: float | None = None,
    ) -> Response: ...


class SerialTransport:
    """`pyserial`-backed transport against a real BioStack 4.

    The defaults mirror the settings Gen5 applies to COM8 in the captures:
    9600 8N2, DTR set, RTS cleared. ``ack_timeout`` is short because the
    device acknowledges within milliseconds; ``header_timeout`` is long
    because the response header for a motion command can take many
    seconds to arrive.
    """

    def __init__(
        self,
        port: str = "COM8",
        *,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 2,
        ack_timeout: float = 1.0,
        header_timeout: float = 30.0,
        payload_timeout: float = 5.0,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.ack_timeout = ack_timeout
        self.header_timeout = header_timeout
        self.payload_timeout = payload_timeout
        self._serial: "object | None" = None

    def open(self) -> None:
        try:
            import serial  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise BioStackConnectionError(
                "pyserial is not installed; install with `pip install pyserial`"
            ) from exc

        try:
            ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.header_timeout,
                write_timeout=self.ack_timeout,
            )
        except Exception as exc:
            raise BioStackConnectionError(
                f"Could not open {self.port}: {exc}"
            ) from exc

        try:
            ser.dtr = True
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception as exc:
            ser.close()
            raise BioStackConnectionError(
                f"Could not initialize serial settings on {self.port}: {exc}"
            ) from exc

        self._serial = ser

    def close(self) -> None:
        ser = self._serial
        self._serial = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def is_open(self) -> bool:
        ser = self._serial
        return ser is not None and bool(getattr(ser, "is_open", False))

    def transact(
        self,
        address: int,
        command: int,
        payload: bytes = b"",
        *,
        timeout: float | None = None,
    ) -> Response:
        if self._serial is None:
            raise BioStackConnectionError("transport is not open; call .open() first")

        request = frames.encode_request(address, command, payload)
        ser = self._serial

        start = time.monotonic()

        ser.timeout = self.ack_timeout
        ser.write(request.raw)

        ack = ser.read(1)
        if ack != bytes([frames.ACK_BYTE]):
            raise BioStackProtocolError(
                f"expected ACK 0x{frames.ACK_BYTE:02x} from device for command "
                f"0x{command:02x}, got {ack.hex(' ') or '(timeout)'}"
            )

        ser.timeout = timeout if timeout is not None else self.header_timeout
        header = self._read_exact(ser, frames.HEADER_LENGTH, "response header")

        declared_plen = frames.parse_response_header(header, expected_command=command)

        ser.timeout = self.payload_timeout
        status_payload = self._read_exact(ser, declared_plen, "status payload")

        response = frames.parse_response(header, status_payload, expected_command=command)
        elapsed = time.monotonic() - start
        return Response(request=request, response=response, elapsed_seconds=elapsed)

    @staticmethod
    def _read_exact(ser: object, n: int, label: str) -> bytes:
        """Read exactly ``n`` bytes or raise :class:`BioStackProtocolError`."""
        buf = b""
        while len(buf) < n:
            chunk = ser.read(n - len(buf))  # type: ignore[attr-defined]
            if not chunk:
                raise BioStackProtocolError(
                    f"timed out waiting for {label} (expected {n} bytes, "
                    f"got {len(buf)}: {buf.hex(' ') or '(none)'})"
                )
            buf += chunk
        return buf


class DryRunTransport:
    """In-memory simulator usable on any OS without hardware.

    Returns the canonical success status payload for the small command
    whitelist used by the high-level workflows. Anything else raises a
    :class:`BioStackProtocolError` so tests catch surprises.
    """

    DEFAULT_SUCCESS_COMMANDS: frozenset[int] = frozenset(
        {0xBD, 0xBE, 0xC0, 0xB9, 0xCD, 0xE2, 0xBC, 0xEB, 0xF1}
    )

    def __init__(
        self,
        *,
        success_commands: frozenset[int] | None = None,
        responses: dict[int, bytes] | None = None,
    ) -> None:
        self._success_commands = (
            success_commands if success_commands is not None else self.DEFAULT_SUCCESS_COMMANDS
        )
        self._responses: dict[int, bytes] = dict(responses or {})
        self._open = False
        self.history: list[frames.RequestFrame] = []

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def is_open(self) -> bool:
        return self._open

    def set_response(self, command: int, status_payload: bytes) -> None:
        """Force ``transact(... command=...)`` to return this payload."""
        if len(status_payload) != 4:
            raise ValueError("status payload must be exactly 4 bytes")
        self._responses[command] = bytes(status_payload)

    def transact(
        self,
        address: int,
        command: int,
        payload: bytes = b"",
        *,
        timeout: float | None = None,
    ) -> Response:
        if not self._open:
            raise BioStackConnectionError("DryRunTransport is not open")

        request = frames.encode_request(address, command, payload)
        self.history.append(request)

        if command in self._responses:
            status_payload = self._responses[command]
        elif command in self._success_commands:
            status_payload = frames.SUCCESS_STATUS
        else:
            raise BioStackProtocolError(
                f"DryRunTransport has no response programmed for command "
                f"0x{command:02x}; whitelist via DryRunTransport(success_commands=...) "
                f"or set_response()"
            )

        header_without_checksum = (
            frames.RESPONSE_PREFIX
            + bytes([command])
            + frames.RESPONSE_DIRECTION
            + bytes([len(status_payload) & 0xFF, (len(status_payload) >> 8) & 0xFF])
        )
        chk = frames.compute_checksum(header_without_checksum, status_payload)
        header = header_without_checksum + bytes([chk & 0xFF, (chk >> 8) & 0xFF])

        response = frames.parse_response(header, status_payload, expected_command=command)
        return Response(request=request, response=response, elapsed_seconds=0.0)
