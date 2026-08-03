"""Lab equipment status spec shapes — from the shared package.

This module used to be a near-verbatim vendored copy of the unified status
contract in the ac-organic-lab monorepo (``docs/STATUS_SPEC.md``). That
shared package now exists (``sdl-lab-contract``, versioned so its
major.minor equals the spec revision), so the types are imported and
re-exported here and every ``from .models import ...`` in this repo keeps
working unchanged.

Conformance: the BioStack 4 service implements **lab status spec v1.2** —
the read baseline (``/``, ``/health``, ``/status``), the claim protocol
(``/control/{claim,heartbeat,release}``), a guarded ``/control/*`` motion
surface, and v1.2 ``activity`` / ``activity_since`` observed from the
macro-in-flight flag plus the reserved ``cycles_total`` metric. See the
README for what "primary operation" means for a plate stacker.

Kept local: :class:`ClaimRequest`, because this device validates incoming
claim bodies more strictly (field lengths, TTL bounds) than the wire
contract requires.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sdl_lab_contract import (
    Activity,
    ClaimedBy,
    ClaimRejection,
    ClaimResponse,
    ComponentStatus,
    EquipmentKind,
    EquipmentState,
    EquipmentStatus,
    ErrorInfo,
    HealthResponse,
    MetricValue,
    ProbeResponse,
)

# The version THIS device speaks. Deliberately overrides the package
# default ("1.0" — the honest reading of a device that does not say).
PROTOCOL_VERSION = "1.2"


class ClaimRequest(BaseModel):
    """Body of ``POST /control/claim`` — device-side strict validation.

    Same shape as ``sdl_lab_contract.ClaimRequest``; this device
    additionally bounds the field lengths and clamps the requested TTL.
    """

    owner: str = Field(min_length=1, max_length=120)
    session_id: str = Field(min_length=1, max_length=120)
    ttl_s: float = Field(default=30.0, ge=1.0, le=600.0)


__all__ = [
    "Activity",
    "ClaimedBy",
    "ClaimRejection",
    "ClaimRequest",
    "ClaimResponse",
    "ComponentStatus",
    "EquipmentKind",
    "EquipmentState",
    "EquipmentStatus",
    "ErrorInfo",
    "HealthResponse",
    "MetricValue",
    "PROTOCOL_VERSION",
    "ProbeResponse",
]
