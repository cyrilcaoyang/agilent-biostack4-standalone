"""Unit tests for the single-holder claim store (portable; no hardware).

The store is async; these tests drive it via ``asyncio.run`` so no
``pytest-asyncio`` plugin is required (matching the repo's existing
dependency-free test setup). Each test constructs its ``ClaimStore``
inside the coroutine so the store's ``asyncio.Lock`` binds to that run's
event loop.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agilent_biostack4 import claims as claims_mod
from agilent_biostack4.claims import ClaimConflict, ClaimStore, UnknownClaim
from agilent_biostack4.models import ClaimRequest


def _req(session_id: str = "s1", owner: str = "agent:test", ttl_s: float = 30.0) -> ClaimRequest:
    return ClaimRequest(owner=owner, session_id=session_id, ttl_s=ttl_s)


def test_acquire_returns_token_and_interval() -> None:
    async def inner() -> None:
        store = ClaimStore()
        resp = await store.acquire(_req())
        assert resp.claim_token
        assert resp.heartbeat_interval_s >= claims_mod._MIN_HEARTBEAT_S
        assert await store.is_claimed() is True

    asyncio.run(inner())


def test_reacquire_same_session_is_idempotent() -> None:
    async def inner() -> None:
        store = ClaimStore()
        first = await store.acquire(_req(session_id="s1"))
        second = await store.acquire(_req(session_id="s1"))
        # Same session keeps its token (the SDK may call acquire repeatedly).
        assert first.claim_token == second.claim_token

    asyncio.run(inner())


def test_acquire_conflict_for_different_session() -> None:
    async def inner() -> None:
        store = ClaimStore()
        await store.acquire(_req(session_id="s1", owner="agent:a"))
        with pytest.raises(ClaimConflict) as excinfo:
            await store.acquire(_req(session_id="s2", owner="agent:b"))
        assert excinfo.value.claimed_by.session_id == "s1"
        assert excinfo.value.claimed_by.owner == "agent:a"
        assert excinfo.value.retry_after_s >= 0.0

    asyncio.run(inner())


def test_heartbeat_extends_expiry() -> None:
    async def inner() -> None:
        store = ClaimStore()
        first = await store.acquire(_req(ttl_s=30.0))
        refreshed = await store.heartbeat(first.claim_token)
        assert refreshed.claim_token == first.claim_token
        assert refreshed.expires_at >= first.expires_at

    asyncio.run(inner())


def test_heartbeat_unknown_token_raises() -> None:
    async def inner() -> None:
        store = ClaimStore()
        await store.acquire(_req())
        with pytest.raises(UnknownClaim):
            await store.heartbeat("not-the-token")
        with pytest.raises(UnknownClaim):
            await store.heartbeat(None)

    asyncio.run(inner())


def test_expired_claim_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def inner() -> None:
        store = ClaimStore()
        resp = await store.acquire(_req(ttl_s=5.0))

        # Jump wall-clock past expiry by patching the module clock.
        real_now = claims_mod._utcnow()
        monkeypatch.setattr(
            claims_mod, "_utcnow", lambda: real_now + timedelta(seconds=10)
        )
        assert await store.is_claimed() is False
        assert await store.validate(resp.claim_token) is False
        # A fresh session can now claim.
        with_new = await store.acquire(_req(session_id="s2"))
        assert with_new.claim_token

    asyncio.run(inner())


def test_release_is_idempotent() -> None:
    async def inner() -> None:
        store = ClaimStore()
        resp = await store.acquire(_req())
        await store.release(resp.claim_token)
        assert await store.is_claimed() is False
        # Releasing again (or with a stale token) never raises.
        await store.release(resp.claim_token)
        await store.release("anything")

    asyncio.run(inner())


def test_release_wrong_token_does_not_drop_others_claim() -> None:
    async def inner() -> None:
        store = ClaimStore()
        await store.acquire(_req(session_id="s1"))
        await store.release("wrong-token")
        assert await store.is_claimed() is True

    asyncio.run(inner())


def test_current_shape() -> None:
    async def inner() -> None:
        store = ClaimStore()
        assert await store.current() is None
        await store.acquire(_req(session_id="s1", owner="agent:x"))
        current = await store.current()
        assert current is not None
        assert current.session_id == "s1"
        assert current.owner == "agent:x"
        assert current.expires_at is not None

    asyncio.run(inner())


def test_validate_matches_only_live_token() -> None:
    async def inner() -> None:
        store = ClaimStore()
        resp = await store.acquire(_req())
        assert await store.validate(resp.claim_token) is True
        assert await store.validate("nope") is False
        assert await store.validate(None) is False

    asyncio.run(inner())
