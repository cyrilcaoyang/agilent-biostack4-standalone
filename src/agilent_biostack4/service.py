"""Service layer that exposes the BioStack 4 driver over STATUS_SPEC v1.1.

This service implements the full v1.1 surface:

* ``GET /``       - identity probe
* ``GET /health`` - liveness
* ``GET /status`` - ``EquipmentStatus`` snapshot (side-effect-free)
* ``POST /control/{claim,heartbeat,release}`` - cooperative claim protocol
* ``POST /control/{startup,shutdown,home,stage_plate,present_plate,handoff}``
  - the guarded motion surface

The central safety invariant
----------------------------

The BioStack has two "out of plate" failure modes (confirmed on
hardware, see ``PROTOCOL_NOTES.md`` "Step 4"):

* ``stage_plate`` (``b9``) on an empty input stack → ``01 80 02 16``,
  **graceful, non-latching** → :class:`StackEmptyError`. The device stays
  healthy; the operator just adds plates and retries.
* ``present_plate`` (``cd``) with **no plate at the handoff** →
  ``01 80 00 17``, a **sticky latch** that software cannot clear (``home``
  makes it worse) and that requires a physical power-cycle →
  :class:`NoPlatePickedUpError`.

Because every macro begins with a ``bd`` status check and a latched
device fails ``bd``, the latch locks the driver out of its own recovery
path. So the API must make it **impossible** to issue ``present_plate``
unless a plate is known to be staged at the handoff.

We track ``_plate_staged`` in memory:

* ``True`` after a successful ``stage_plate``.
* ``False`` after a successful ``present_plate`` and on startup.
* ``present_plate`` with ``_plate_staged == False`` → HTTP 412 and is
  **omitted from ``allowed_actions``**.

The flag is *inferred from this service's own command history*, not read
back from the device (the BioStack has no handoff-occupancy readback in
our protocol). An out-of-band move can desync it, and it is lost on
restart. This is acceptable because the failure direction is always
toward *refusing* ``present_plate`` (harmless: blocks a real plate), never
toward issuing ``cd`` into an empty handoff (the disaster).

Concurrency
-----------

``get_status()`` is polled every 2-3 s by the dashboard and must never
block on a long macro (a ``home`` is ~21 s). Two locks:

* ``_op_lock`` serializes control macros (one at a time). Held across the
  blocking ``to_thread`` driver call.
* ``_state_lock`` is a short, never-blocking lock guarding the in-memory
  flag bundle (``_busy``, ``_current_action``, ``_plate_staged``,
  ``_latched``, ``_last_error``). ``get_status()`` reads these without
  touching ``_op_lock``, so it returns ``busy`` immediately while a macro
  runs. Mirrors ``filter_every_well``'s ``_move_lock`` + busy surfacing.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .biostack import BioStack4
from .claims import ClaimStore
from .config import get as _cfg_get
from .config import load_config
from .exceptions import (
    BioStackCommandError,
    BioStackConnectionError,
    BioStackProtocolError,
    NoPlatePickedUpError,
    StackEmptyError,
)
from .models import (
    PROTOCOL_VERSION,
    ClaimedBy,
    ComponentStatus,
    EquipmentStatus,
    ErrorInfo,
)
from .transport import DryRunTransport, SerialTransport, Transport

logger = logging.getLogger(__name__)


# ``last_error.code`` taxonomy for the BioStack 4. Stable set documented in
# README; each mutation site validates against it. See STATUS_SPEC §6
# ("``code`` SHOULD be a stable enum drawn from a per-repo taxonomy").
LAST_ERROR_CODES: frozenset[str] = frozenset(
    {
        "stack_empty",
        "no_plate_picked_up",
        "latched",
        "connect_failed",
        "protocol_error",
        "command_error",
    }
)


# The full set of control skill names this device exposes (flat names, to
# match the driver method names and the future plate_stacker catalog — see
# CONTROL_API_PLAN.md §8 / §10 decision 2).
_ALL_ACTIONS = ["startup", "shutdown", "home", "stage_plate", "present_plate", "handoff"]


# ---------------------------------------------------------------------------
# Control-gating exceptions. The API layer maps each to an HTTP status.
# ---------------------------------------------------------------------------


class DeviceStateError(RuntimeError):
    """Coarse equipment-state conflict (not connected / busy). Maps to 409."""


class DeviceLatchedError(DeviceStateError):
    """The device is in the sticky ``cd`` latch and needs a power-cycle.
    Maps to 409 with an actionable recovery message."""


class StagePreconditionError(RuntimeError):
    """A staged-plate precondition (§6.1) was violated. Maps to HTTP 412.

    ``body`` is the structured, shape-distinguishable 412 payload.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__(body.get("detail", "precondition failed"))
        self.body = body


class BioStack4Service:
    """Wraps a :class:`BioStack4` driver and produces spec-compliant
    ``EquipmentStatus`` snapshots plus a guarded control surface.

    The constructor picks the transport based on ``dry_run``:

    * ``dry_run=True``  -> :class:`DryRunTransport`. Always reports
      ``equipment_status: dry_run`` and advertises the full action set so
      the control surface is exercisable in CI/dev without hardware.
    * ``dry_run=False`` -> :class:`SerialTransport` against the configured
      COM port (or an injected transport for tests). Reports
      ``requires_init`` until the port is open, then ``ready`` / ``busy`` /
      ``error`` as the state machine dictates.
    """

    def __init__(
        self,
        *,
        dry_run: bool = False,
        transport: Transport | None = None,
        config_path: str | None = None,
        enforce_claims: bool = True,
        enforce_stage_precondition: bool = True,
    ) -> None:
        cfg = load_config(config_path)
        self._config = cfg
        self.dry_run = dry_run
        self.enforce_claims = enforce_claims
        self.enforce_stage_precondition = enforce_stage_precondition

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
        self.claims = ClaimStore()

        # Serializes macros; held across the blocking to_thread call.
        self._op_lock = asyncio.Lock()
        # Short, never-blocking lock for the flag bundle read by get_status().
        self._state_lock = asyncio.Lock()

        self._started_at = time.monotonic()
        self._last_error: ErrorInfo | None = None
        self._busy = False
        self._current_action: str | None = None
        self._plate_staged = False
        self._latched = False

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

        Idempotent: a no-op if already connected. Raises on failure; the
        API layer maps that to HTTP 503 and the lifespan logs + leaves the
        service in ``requires_init`` for the dashboard to display.
        """
        async with self._op_lock:
            if self._driver.is_connected():
                return
            try:
                await asyncio.to_thread(self._driver.connect)
            except Exception as exc:
                await self._record_error(exc, "connect_failed")
                raise
            async with self._state_lock:
                self._last_error = None
                self._latched = False

    async def shutdown(self) -> None:
        """Best-effort close. Never raises."""
        async with self._op_lock:
            try:
                await asyncio.to_thread(self._driver.close)
            except Exception:
                logger.exception("Error while closing BioStack 4 transport")

    # ---- control macros -----------------------------------------------------

    async def home(self) -> None:
        await self._operate("home", self._driver.home)

    async def stage_plate(self) -> None:
        await self._operate("stage_plate", self._driver.stage_plate)

    async def present_plate(self) -> None:
        await self._operate("present_plate", self._driver.present_plate)

    async def handoff(self) -> None:
        """Composite primitive: stage a plate then immediately present it.

        Because it stages immediately before presenting, a remote caller
        cannot present into an empty handoff — the latch trap (§3) is
        structurally unreachable through this verb. The granular
        ``stage_plate`` / ``present_plate`` stay available (guarded) for
        flexibility; ``handoff`` is the recommended high-level skill for
        orchestration.
        """
        async with self._op_lock:
            self._gate("handoff")
            await self._set_busy("handoff")
            try:
                await asyncio.to_thread(self._driver.stage_plate)
                async with self._state_lock:
                    self._plate_staged = True
                await asyncio.to_thread(self._driver.present_plate)
            except StackEmptyError as exc:
                await self._record_error(exc, "stack_empty")
                raise
            except NoPlatePickedUpError as exc:
                await self._record_error(exc, "no_plate_picked_up", latched=True)
                raise
            except BioStackProtocolError as exc:
                await self._record_error(exc, "protocol_error")
                raise
            except BioStackConnectionError as exc:
                await self._record_error(exc, "connect_failed")
                raise
            except BioStackCommandError as exc:
                await self._record_error(exc, "command_error")
                raise
            else:
                async with self._state_lock:
                    self._plate_staged = False
                    self._last_error = None
            finally:
                await self._clear_busy()

    async def _operate(self, action: str, fn: Callable[[], Any]) -> None:
        """Gate, run a single blocking macro in a thread, and update state.

        On success: clear ``last_error`` and update ``_plate_staged``. On
        failure: record ``last_error`` with the right ``code`` (and set the
        latch for ``no_plate_picked_up``). ``_busy`` is set/cleared around
        the call so ``/status`` surfaces ``busy`` without taking ``_op_lock``.
        """
        async with self._op_lock:
            self._gate(action)
            await self._set_busy(action)
            try:
                await asyncio.to_thread(fn)
            except StackEmptyError as exc:
                await self._record_error(exc, "stack_empty")
                raise
            except NoPlatePickedUpError as exc:
                await self._record_error(exc, "no_plate_picked_up", latched=True)
                raise
            except BioStackProtocolError as exc:
                await self._record_error(exc, "protocol_error")
                raise
            except BioStackConnectionError as exc:
                await self._record_error(exc, "connect_failed")
                raise
            except BioStackCommandError as exc:
                await self._record_error(exc, "command_error")
                raise
            else:
                await self._on_success(action)
            finally:
                await self._clear_busy()

    # ---- gating + preconditions --------------------------------------------

    def _gate(self, action: str) -> None:
        """Validate the coarse device state for ``action`` (called under
        ``_op_lock``). Raises :class:`DeviceLatchedError` /
        :class:`DeviceStateError` (→ 409) or :class:`StagePreconditionError`
        (→ 412). ``require_claim`` (→ 423) has already run in the API layer.
        """
        if self._latched:
            raise DeviceLatchedError(
                "BioStack is latched (no plate was at the handoff). Software "
                "cannot clear this: power-cycle the BioStack and inspect the "
                "carrier for a jam, then POST /control/startup."
            )
        if not self._driver.is_connected():
            raise DeviceStateError(
                "BioStack transport not open; POST /control/startup first"
            )
        if self._busy:
            raise DeviceStateError("BioStack is busy with another operation")

        block, body = self.evaluate_stage_precondition(action)
        if block:
            assert body is not None
            raise StagePreconditionError(body)

    def evaluate_stage_precondition(
        self, action: str
    ) -> tuple[bool, dict[str, Any] | None]:
        """Single source of truth for the staged-plate precondition (§6.2).

        Returns ``(should_block, body_for_412)``. Consulted by both
        ``_gate`` (to raise 412) and ``_allowed_actions`` (to omit the
        action), so the two surfaces can never disagree. Honors the
        ``enforce_stage_precondition`` override flag.

        * ``present_plate`` is blocked when **no** plate is staged (the
          latch guard — the whole point of this PR).
        * ``stage_plate`` / ``handoff`` are blocked when a plate is
          **already** staged (don't stage onto an occupied handoff).
        """
        if not self.enforce_stage_precondition:
            return (False, None)

        if action == "present_plate" and not self._plate_staged:
            return (
                True,
                {
                    "detail": "No plate staged at the handoff",
                    "plate_staged": False,
                    "required": "stage_plate first",
                },
            )
        if action in ("stage_plate", "handoff") and self._plate_staged:
            return (
                True,
                {
                    "detail": "A plate is already staged at the handoff",
                    "plate_staged": True,
                    "required": "present_plate first",
                },
            )
        return (False, None)

    def _allowed_actions(self, state: str) -> list[str]:
        """Compute ``allowed_actions`` for ``state`` (the §6.2 mirror).

        Built from the same :meth:`evaluate_stage_precondition` helper the
        control handlers consult, so ``X in allowed_actions`` iff a POST of
        ``X`` would not 412.
        """
        if state == "dry_run":
            # Advertise the full set so the surface is exercisable in CI/dev
            # (CONTROL_API_PLAN.md §10 decision 4).
            return list(_ALL_ACTIONS)
        if state == "requires_init":
            return ["startup"]
        if state in ("busy", "error", "e_stop", "degraded", "unknown"):
            return []
        # ready: shutdown + home are always offered; the staged-plate
        # precondition decides which of stage/present/handoff is offered.
        actions = ["shutdown", "home"]
        for candidate in ("stage_plate", "present_plate", "handoff"):
            block, _ = self.evaluate_stage_precondition(candidate)
            if not block:
                actions.append(candidate)
        return actions

    # ---- status (side-effect-free) -----------------------------------------

    async def get_status(self) -> EquipmentStatus:
        """Produce a fresh status snapshot.

        Crucially, this does NOT send any byte to the BioStack and does NOT
        take ``_op_lock``, so it returns immediately even while a ~21 s
        macro is running (it reports ``busy``).
        """
        claimed_by = await self.claims.current()
        async with self._state_lock:
            return self._build_status(claimed_by)

    def _build_status(self, claimed_by: ClaimedBy | None) -> EquipmentStatus:
        now = datetime.now(timezone.utc)
        uptime = time.monotonic() - self._started_at
        host = socket.gethostname()

        connected = self._driver.is_connected()

        details: dict[str, Any] = {}
        com_port = _cfg_get(self._config, "instrument", "com_port", "COM8")
        if not self.dry_run:
            details["com_port"] = com_port
        details["plate_staged"] = self._plate_staged
        details["claimed_by"] = (
            claimed_by.model_dump(mode="json") if claimed_by is not None else None
        )
        # Bench validation (PHYSICAL_TESTS.md steps 0-5) signed off 2026-05-29;
        # stage->present loop and graceful empty-stack exhaustion re-confirmed
        # on real hardware 2026-06-01.
        details["bench_validated"] = "2026-05-29 (re-confirmed 2026-06-01)"

        if self.dry_run:
            state: str = "dry_run"
            message: str | None = "Dry-run mode - no hardware connected"
            details["dry_run"] = True
        elif not connected:
            state = "requires_init"
            message = "Serial transport not open. POST /control/startup to connect."
        elif self._latched:
            state = "error"
            message = (
                "BioStack latched (no plate at the handoff). Power-cycle the "
                "device and inspect the carrier for a jam; software cannot clear this."
            )
        elif self._busy:
            state = "busy"
            message = f"Running {self._current_action}"
        else:
            state = "ready"
            message = "Transport open; ready for plate moves"

        gripper_msg = "Position not polled (no readback in protocol)"
        components: dict[str, ComponentStatus] = {
            "transport": ComponentStatus(
                connected=connected,
                state="open" if connected else "closed",
            ),
            "gripper": ComponentStatus(
                connected=connected,
                state="unknown",
                message=gripper_msg,
            ),
            "handoff": ComponentStatus(
                connected=connected,
                state="occupied" if self._plate_staged else "empty",
                message="Inferred from command history; no occupancy readback",
            ),
        }

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
            allowed_actions=self._allowed_actions(state),
            device_time=now,
            uptime_seconds=uptime,
            components=components,
            last_error=self._last_error,
            details=details,
        )

    # ---- state helpers (take _state_lock; never take _op_lock) -------------

    async def _set_busy(self, action: str) -> None:
        async with self._state_lock:
            self._busy = True
            self._current_action = action

    async def _clear_busy(self) -> None:
        async with self._state_lock:
            self._busy = False
            self._current_action = None

    async def _on_success(self, action: str) -> None:
        async with self._state_lock:
            # last_error auto-clears on the first 2xx from an operational
            # control endpoint (§6.4). Cleared before the echoed status body
            # is built.
            self._last_error = None
            if action == "stage_plate":
                self._plate_staged = True
            elif action == "present_plate":
                self._plate_staged = False
            # home / startup / shutdown leave _plate_staged untouched.

    async def _record_error(
        self, exc: Exception, code: str, *, latched: bool = False
    ) -> None:
        assert code in LAST_ERROR_CODES, f"unknown last_error code {code!r}"
        async with self._state_lock:
            self._last_error = ErrorInfo(
                code=code,
                message=str(exc),
                severity="critical" if latched else "error",
                timestamp=datetime.now(timezone.utc),
            )
            if latched:
                self._latched = True
                self._plate_staged = False
        logger.error("BioStack 4 error (%s): %s", code, exc)


__all__ = [
    "BioStack4Service",
    "DeviceLatchedError",
    "DeviceStateError",
    "LAST_ERROR_CODES",
    "StagePreconditionError",
]
