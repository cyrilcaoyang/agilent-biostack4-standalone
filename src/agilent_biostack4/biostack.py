"""High-level synchronous driver for the Agilent BioStack 4.

The public API is intentionally small. The lab does not need fine-grained
control - it needs reliable macro workflows that either succeed or raise
a typed exception::

    stacker = BioStack4()
    stacker.connect()
    stacker.status()         # raises if device does not ACK
    stacker.home()
    stacker.stage_plate()    # input stack -> internal handoff
    stacker.present_plate()  # handoff -> external drop-off (out of the instrument)
    stacker.close()

Internally each macro replays a sequence of frames captured from real
Gen5 traffic (see ``recorded_sequences.py``). The drop/present command
roles were confirmed on the bench 2026-05-29 (see ``PROTOCOL_NOTES.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import recorded_sequences as seqs
from .config import get as _cfg_get
from .config import load_config
from .exceptions import (
    BioStackCommandError,
    BioStackConnectionError,
    NoPlatePickedUpError,
    StackEmptyError,
)
from .transport import DryRunTransport, Response, SerialTransport, Transport


@dataclass(frozen=True)
class StatusPayload:
    """Result of a single command, surfaced to the caller.

    For macros, ``StatusPayload`` reports the *final* step's status: the
    macro raises on any earlier-step failure, so by definition the final
    step's status is what the caller sees on success.
    """

    command: int
    payload: bytes
    success: bool
    elapsed_seconds: float

    @classmethod
    def from_response(cls, response: Response) -> "StatusPayload":
        return cls(
            command=response.response.command,
            payload=response.response.status_payload,
            success=response.response.success,
            elapsed_seconds=response.elapsed_seconds,
        )


_FAILURE_EXCEPTIONS: dict[bytes, type[BioStackCommandError]] = {
    b"\x01\x80\x01\x17": NoPlatePickedUpError,
    b"\x01\x80\x00\x16": StackEmptyError,
}


def _exception_for(status_payload: bytes) -> type[BioStackCommandError]:
    return _FAILURE_EXCEPTIONS.get(status_payload, BioStackCommandError)


class BioStack4:
    """Synchronous driver for one BioStack 4 on a single COM port."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        config_path: str | None = None,
    ) -> None:
        cfg = load_config(config_path)
        self._config = cfg

        if transport is not None:
            self._transport = transport
            self._owns_transport = False
        elif _cfg_get(cfg, "service", "dry_run", False):
            self._transport = DryRunTransport()
            self._owns_transport = True
        else:
            self._transport = SerialTransport(
                port=_cfg_get(cfg, "instrument", "com_port", "COM8"),
                baudrate=_cfg_get(cfg, "instrument", "baudrate", 9600),
                bytesize=_cfg_get(cfg, "instrument", "bytesize", 8),
                parity=_cfg_get(cfg, "instrument", "parity", "N"),
                stopbits=_cfg_get(cfg, "instrument", "stopbits", 2),
                ack_timeout=_cfg_get(cfg, "instrument", "ack_timeout", 1.0),
                header_timeout=_cfg_get(cfg, "instrument", "header_timeout", 30.0),
                payload_timeout=_cfg_get(cfg, "instrument", "payload_timeout", 5.0),
            )
            self._owns_transport = True

    # -- Connection ------------------------------------------------------

    def connect(self) -> None:
        """Open the underlying transport. Raises BioStackConnectionError on failure."""
        self._transport.open()

    def close(self) -> None:
        """Close the underlying transport. Safe to call when already closed."""
        if self._owns_transport:
            self._transport.close()

    def is_connected(self) -> bool:
        return self._transport.is_open()

    def __enter__(self) -> "BioStack4":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- Macros ----------------------------------------------------------

    def status(self) -> StatusPayload:
        """Send the ``bd`` status query and return its status payload."""
        return self._run(seqs.STATUS)

    def home(self) -> StatusPayload:
        """Home all axes."""
        return self._run(seqs.HOME)

    def stage_plate(self) -> StatusPayload:
        """Move one plate from the input stack to the internal handoff position.

        Maps to the ``b9`` macro family. Bench-confirmed 2026-05-29: the
        plate ends at the internal handoff with the gripper retracted,
        ready for :meth:`present_plate` to hand it out of the instrument.
        See ``PROTOCOL_NOTES.md``.
        """
        return self._run(seqs.STAGE_PLATE)

    def present_plate(self) -> StatusPayload:
        """Pick the staged plate from the handoff and present it out of the instrument.

        Maps to the ``cd`` macro family. Bench-confirmed 2026-05-29: the
        plate is delivered to an external drop-off position OUTSIDE the
        equipment (for a robot arm / reader nest to take). This does NOT
        restack the plate internally; internal input->output transfer is a
        different command (``e2``, not yet exposed). See ``PROTOCOL_NOTES.md``.
        """
        return self._run(seqs.PRESENT_PLATE)

    # -- Internals -------------------------------------------------------

    def _run(self, sequence: seqs.RecordedSequence) -> StatusPayload:
        """Play a recorded sequence and return the final step's status.

        Any non-success status from any step raises immediately - we
        never continue a multi-step macro past a failure.
        """
        if not self._transport.is_open():
            raise BioStackConnectionError(
                "BioStack4 transport is not open; call .connect() first"
            )

        return self._run_steps(sequence.steps)

    def _run_steps(self, steps: Sequence[seqs.RecordedStep]) -> StatusPayload:
        last_response: Response | None = None
        for step in steps:
            response = self._transport.transact(step.address, step.command, step.payload)
            if not response.response.success:
                exc_cls = _exception_for(response.response.status_payload)
                raise exc_cls(step.command, response.response.status_payload)
            last_response = response
        assert last_response is not None, "RecordedSequence must contain at least one step"
        return StatusPayload.from_response(last_response)
