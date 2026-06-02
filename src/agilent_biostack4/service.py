"""Service layer that exposes the BioStack 4 driver as an
``EquipmentStatus`` source for the read-only lab dashboard.

Scope of this PR
----------------

This service is intentionally **read-only**. It implements only the
spec-mandated endpoints needed by the dashboard's polling aggregator:

* ``GET /``       - identity probe
* ``GET /health`` - liveness
* ``GET /status`` - ``EquipmentStatus`` snapshot

It does **not** expose ``/control/*``, claims, or any other write path.
That is deliberate: the BioStack 4 macros are gated on the bench
validation in ``PHYSICAL_TESTS.md`` and we do not want a stray HTTP call
to put a plate in motion before that gate is passed.

Once ``PHYSICAL_TESTS.md`` is signed off, a follow-up PR introduces
``ClaimStore`` and the ``/control/{startup, stage_plate, present_plate, ...}``
endpoints. The v1.1 claim shapes are already in :mod:`models` so the API
surface can grow without breaking compatibility.

Concurrency
-----------

The underlying :class:`BioStack4` driver is synchronous. The service
owns the driver instance and an :class:`asyncio.Lock` so that
``get_status()`` (called every 2-3 seconds by the dashboard) cannot
race with any future ``/control/*`` call.

``get_status()`` does not send any byte to the device. The transport's
``is_open()`` flag is enough to compute the spec ``equipment_status``
field; querying the device on every poll would amplify dashboard load
into real serial traffic, which we want to avoid until the bench tests
finish characterising what is safe to send back-to-back.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any

from .biostack import BioStack4
from .config import get as _cfg_get
from .config import load_config
from .models import (
    PROTOCOL_VERSION,
    ComponentStatus,
    EquipmentStatus,
    ErrorInfo,
)
from .transport import DryRunTransport, SerialTransport, Transport

logger = logging.getLogger(__name__)


# allowed_actions per equipment_status (v1.1).
#
# Empty everywhere for now: this service is read-only. The follow-up PR
# fills these in with the BioStack-specific skill names (``startup``,
# ``shutdown``, ``home``, ``stage_plate``, ``present_plate``) once /control/*
# lands. Until then the SDK falls back to its catalog's ``requires_states``.
_ALLOWED_ACTIONS_BY_STATE: dict[str, list[str]] = {
    "requires_init": [],
    "ready": [],
    "busy": [],
    "degraded": [],
    "error": [],
    "e_stop": [],
    "unknown": [],
    "dry_run": [],
}


class BioStack4Service:
    """Wraps a :class:`BioStack4` driver and produces spec-compliant
    ``EquipmentStatus`` snapshots.

    The constructor picks the transport based on ``dry_run``:

    * ``dry_run=True``  -> :class:`DryRunTransport`. Always reports
      ``equipment_status: dry_run``. Safe everywhere; how this should run
      in CI and on dashboards that do not yet have the bench wired up.
    * ``dry_run=False`` -> :class:`SerialTransport` against the configured
      COM port. The lifespan opens the port (no commands sent) and the
      service then reports ``ready`` while the port is open and
      ``requires_init`` otherwise.
    """

    def __init__(
        self,
        *,
        dry_run: bool = False,
        transport: Transport | None = None,
        config_path: str | None = None,
    ) -> None:
        cfg = load_config(config_path)
        self._config = cfg
        self.dry_run = dry_run

        if transport is not None:
            self._transport: Transport = transport
        elif dry_run:
            self._transport = DryRunTransport()
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

        self._driver = BioStack4(transport=self._transport)
        self._lock = asyncio.Lock()
        self._started_at = time.monotonic()
        self._last_error: ErrorInfo | None = None

        self.equipment_id: str = _cfg_get(cfg, "dashboard", "equipment_id", "agilent_biostack")
        self.equipment_name: str = _cfg_get(
            cfg, "dashboard", "equipment_name", "Agilent BioStack 4"
        )
        self.equipment_kind: str = "plate_stacker"
        self.equipment_version: str | None = _cfg_get(
            cfg, "dashboard", "equipment_version", None
        )

    # ---- lifecycle ----------------------------------------------------------

    async def startup(self) -> None:
        """Open the underlying transport. Never sends a command to the device.

        Raises on failure; the API layer catches and logs so the service
        stays up in ``requires_init`` for the dashboard to display.
        """
        async with self._lock:
            if self._driver.is_connected():
                return
            try:
                await asyncio.to_thread(self._driver.connect)
                self._last_error = None
            except Exception as exc:
                self._record_error(exc, "startup")
                raise

    async def shutdown(self) -> None:
        """Best-effort close. Never raises."""
        async with self._lock:
            try:
                await asyncio.to_thread(self._driver.close)
            except Exception:
                logger.exception("Error while closing BioStack 4 transport")

    # ---- status (side-effect-free) ----------------------------------------

    async def get_status(self) -> EquipmentStatus:
        """Produce a fresh status snapshot.

        Crucially, this does NOT send any byte to the BioStack. It only
        inspects the transport's open/closed state and a small in-memory
        record of the last error.
        """
        async with self._lock:
            return self._build_status()

    def _build_status(self) -> EquipmentStatus:
        now = datetime.now(timezone.utc)
        uptime = time.monotonic() - self._started_at
        host = socket.gethostname()

        connected = self._driver.is_connected()
        components: dict[str, ComponentStatus] = {
            "transport": ComponentStatus(
                connected=connected,
                state="open" if connected else "closed",
            ),
            "gripper": ComponentStatus(
                connected=connected,
                state="unknown",
                message="Position not polled in read-only mode",
            ),
        }

        details: dict[str, Any] = {}
        com_port = _cfg_get(self._config, "instrument", "com_port", "COM8")
        if not self.dry_run:
            details["com_port"] = com_port
        details["read_only"] = True
        # Bench validation (PHYSICAL_TESTS.md steps 0-5) signed off 2026-05-29;
        # the stage->present loop and graceful empty-stack exhaustion were
        # re-confirmed on real hardware 2026-06-01. The service stays read-only
        # by choice; the motion control surface is a separate follow-up, not a
        # validation gap.
        details["bench_validated"] = "2026-05-29 (re-confirmed 2026-06-01)"

        if self.dry_run:
            state: str = "dry_run"
            message: str | None = "Dry-run mode - no hardware connected"
            details["dry_run"] = True
        elif not connected:
            state = "requires_init"
            message = (
                "Serial transport not open. POST /control/startup will be "
                "added in a follow-up; restart the service to retry connection."
            )
        elif self._last_error is not None:
            state = "error"
            message = self._last_error.message
        else:
            state = "ready"
            message = "Transport open (read-only); motion control surface not yet exposed"

        required_actions: list[str] = ["startup"] if state == "requires_init" else []

        return EquipmentStatus(
            protocol_version=PROTOCOL_VERSION,
            equipment_id=self.equipment_id,
            equipment_name=self.equipment_name,
            equipment_kind=self.equipment_kind,  # type: ignore[arg-type]
            equipment_version=self.equipment_version,
            host=host,
            equipment_status=state,  # type: ignore[arg-type]
            message=message,
            required_actions=required_actions,
            allowed_actions=list(_ALLOWED_ACTIONS_BY_STATE.get(state, [])),
            device_time=now,
            uptime_seconds=uptime,
            components=components,
            last_error=self._last_error,
            details=details,
        )

    # ---- helpers -----------------------------------------------------------

    def _record_error(self, exc: Exception, code: str) -> None:
        self._last_error = ErrorInfo(
            code=code,
            message=str(exc),
            severity="error",
            timestamp=datetime.now(timezone.utc),
        )
        logger.exception("BioStack 4 error in %s", code)


__all__ = ["BioStack4Service"]
