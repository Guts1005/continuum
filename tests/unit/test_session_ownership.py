"""
Fix B — a session id is a name, not an authorization.

Holding the id currently grants the data. That makes every read a bearer-token
read against a token that appears in logs, URLs and screenshots. These tests
pin the replacement: the stored owner is compared against a *principal* the
application binds from its own verified credential.

Three things are being nailed down here, and they are easy to conflate:

1. WHERE the check lives.  ``AgentRunner`` is not the only door — ``LLMClient``
   reads and writes session history directly (llm/client.py:335, :480) and does
   not even accept a ``user_id``. Both doors funnel through ``SessionClient``,
   so that is the seam.

2. WHERE the identity comes from.  ``core.context._user_id`` already exists but
   is populated by *telemetry* from a caller-supplied argument. Wiring that into
   an access decision would let the attacker fill in the answer to the security
   question, so the principal is a separate variable with a narrow setter.

3. HOW it rolls out.  This changes behaviour for existing users, so the check
   runs in three modes and only ``enforce`` raises.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from continuum.session.client import SessionClient
from continuum.session.config import SessionConfig
from continuum.session.exceptions import SessionOwnershipError
from continuum.session.ownership import evaluate_ownership
from continuum.session.principal import bind_principal, get_principal
from continuum.session.providers.memory import MemorySessionProvider
from continuum.session.types import ChatMessage, SessionMetadata


def _meta(user_id: str | None) -> SessionMetadata:
    now = datetime.now(UTC)
    return SessionMetadata(
        session_id="sess-1", user_id=user_id, created_at=now, last_accessed_at=now
    )


# ── 2. the principal, and the trap next to it ─────────────────────────────────


class TestPrincipal:
    def test_unbound_by_default(self):
        assert get_principal() is None

    def test_bind_and_restore(self):
        with bind_principal("alice"):
            assert get_principal() == "alice"
        assert get_principal() is None

    def test_nesting_restores_the_outer_value(self):
        with bind_principal("alice"):
            with bind_principal("bob"):
                assert get_principal() == "bob"
            assert get_principal() == "alice"

    def test_restored_even_when_the_body_raises(self):
        with pytest.raises(RuntimeError):
            with bind_principal("alice"):
                raise RuntimeError("boom")
        assert get_principal() is None

    @pytest.mark.asyncio
    async def test_concurrent_tasks_do_not_leak_into_each_other(self):
        """Two requests served on one event loop must not see each other's
        principal — this is why it is a ContextVar and not an attribute."""
        seen: dict[str, str | None] = {}

        async def request(name: str, delay: float) -> None:
            with bind_principal(name):
                await asyncio.sleep(delay)
                seen[name] = get_principal()

        await asyncio.gather(request("alice", 0.02), request("bob", 0.01))
        assert seen == {"alice": "alice", "bob": "bob"}

    def test_the_telemetry_user_id_is_not_a_principal(self):
        """THE TRAP. ``core.context._user_id`` is set by logging/tracing from a
        caller-supplied argument, so an attacker calling
        ``run(session_id=<victim's>, user_id="victim")`` controls it. It must
        never satisfy an access check."""
        from continuum.core.context import _user_id

        token = _user_id.set("victim")
        try:
            assert get_principal() is None  # telemetry did NOT grant identity
        finally:
            _user_id.reset(token)


# ── 3. the decision, as a pure function ───────────────────────────────────────


class TestOwnershipDecision:
    def test_owner_matches_the_principal(self):
        assert evaluate_ownership(stored_user_id="alice", principal="alice").allowed

    def test_owner_differs_from_the_principal(self):
        check = evaluate_ownership(stored_user_id="alice", principal="mallory", mode="enforce")
        assert not check.allowed
        assert check.problem

    def test_a_session_with_no_owner_is_open(self):
        """Anonymous / single-user deployments never bound an owner, so there is
        nobody to exclude."""
        check = evaluate_ownership(stored_user_id=None, principal=None, mode="enforce")
        assert check.allowed
        assert check.problem is None

    def test_an_unowned_session_is_open_even_to_a_bound_principal(self):
        assert evaluate_ownership(stored_user_id=None, principal="anyone", mode="enforce").allowed

    def test_no_principal_is_capability_access_not_a_refusal(self):
        """Deliberate: with hashed ids (Fix A) an unguessable id in someone's
        hand is evidence they were given it. This is what keeps the change
        non-breaking for callers that never adopted principals."""
        check = evaluate_ownership(stored_user_id="alice", principal=None, mode="enforce")
        assert check.allowed
        assert check.problem is None

    def test_require_principal_closes_that_door(self):
        """For deployments that are ready, the omission path can be shut without
        waiting for the next major."""
        check = evaluate_ownership(
            stored_user_id="alice", principal=None, mode="enforce", require_principal=True
        )
        assert not check.allowed

    @pytest.mark.parametrize("mode", ["open", "audit"])
    def test_non_enforcing_modes_allow_but_report(self, mode):
        """The ramp: a mismatch is visible long before it is fatal, so a team can
        measure breakage before flipping the switch."""
        check = evaluate_ownership(stored_user_id="alice", principal="mallory", mode=mode)
        assert check.allowed
        assert check.problem  # still reported for logs/metrics

    def test_enforce_refuses_the_same_case(self):
        check = evaluate_ownership(stored_user_id="alice", principal="mallory", mode="enforce")
        assert not check.allowed

    def test_unverifiable_ownership_fails_closed_under_enforce(self):
        """Redis down means 'cannot tell', which is not the same as 'unowned'.
        Refuse rather than skip — matching the fail-closed precedent already set
        for the weak-Redis-password path."""
        check = evaluate_ownership(
            stored_user_id=None, principal="alice", mode="enforce", unverifiable=True
        )
        assert not check.allowed

    def test_unverifiable_ownership_is_tolerated_below_enforce(self):
        check = evaluate_ownership(
            stored_user_id=None, principal="alice", mode="audit", unverifiable=True
        )
        assert check.allowed
        assert check.problem


# ── 1. the seam: SessionClient gates every data-bearing operation ─────────────


def _client(mode: str = "enforce", **kw) -> SessionClient:
    cfg = SessionConfig(enabled=True, provider="memory", session_ownership=mode, **kw)
    client = SessionClient(session_config=cfg, memory_client=None, auto_initialize=False)
    client.set_provider(MemorySessionProvider(cfg))
    client._initialized = True
    return client


async def _seed_owned_session(client: SessionClient, owner: str = "alice") -> str:
    with bind_principal(owner):
        return await client.get_or_create_session(user_id=owner, conversation_id="conv-1")


@pytest.mark.asyncio
class TestSessionClientGate:
    async def test_owner_can_read_their_history(self):
        client = _client()
        sid = await _seed_owned_session(client)
        with bind_principal("alice"):
            await client.add_message(
                sid, ChatMessage(role="user", content="hi"), store_in_memory=False
            )
            assert len(await client.get_conversation_history(sid)) == 1

    async def test_a_stranger_cannot_read_history(self):
        client = _client()
        sid = await _seed_owned_session(client)
        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError):
                await client.get_conversation_history(sid)

    async def test_a_stranger_cannot_write_into_the_session(self):
        """Writes matter as much as reads: an unchecked write puts the
        attacker's turns into the victim's history."""
        client = _client()
        sid = await _seed_owned_session(client)
        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError):
                await client.add_message(
                    sid, ChatMessage(role="user", content="hi"), store_in_memory=False
                )

    async def test_a_stranger_cannot_read_metadata(self):
        """Metadata carries the owner's identifiers — leaking it is its own
        disclosure."""
        client = _client()
        sid = await _seed_owned_session(client)
        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError):
                await client.get_session_metadata(sid)

    async def test_a_stranger_cannot_clear_the_session(self):
        client = _client()
        sid = await _seed_owned_session(client)
        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError):
                await client.clear_session(sid)

    async def test_a_stranger_cannot_delete_the_session(self):
        client = _client()
        sid = await _seed_owned_session(client)
        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError):
                await client.delete_session(sid)

    async def test_the_error_names_the_session_but_not_the_owner(self):
        """The refusal must not become an identity-disclosure oracle.

        Checked with hashed ids, because under the plaintext scheme the session
        id *is* the owner's user id — see the test below.
        """
        cfg = SessionConfig(
            enabled=True,
            provider="memory",
            session_ownership="enforce",
            hash_session_ids=True,
            session_id_secret="deployment-secret-value",
        )
        client = SessionClient(session_config=cfg, memory_client=None, auto_initialize=False)
        client.set_provider(MemorySessionProvider(cfg))
        client._initialized = True

        sid = await _seed_owned_session(client, owner="alice")
        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError) as exc:
                await client.get_conversation_history(sid)
        assert "alice" not in str(exc.value)
        assert exc.value.session_id == sid

    async def test_a_plaintext_id_discloses_its_owner_by_construction(self):
        """Why Fix B alone is not enough. Under the plaintext scheme the owner's
        user id is embedded in the session id, so it appears in the refusal, in
        every log line, and in any trace that records the id. The refusal itself
        adds nothing the caller did not already hold — but the scheme leaks
        regardless, which is what Fix A removes.
        """
        client = _client()
        sid = await _seed_owned_session(client, owner="alice")
        assert "alice" in sid  # the id itself is the disclosure

        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError) as exc:
                await client.get_conversation_history(sid)
        # Only ever the id the caller supplied — no owner named separately.
        assert str(exc.value).count("alice") == sid.count("alice")

    async def test_unowned_sessions_stay_reachable(self):
        """Anonymous deployments must be unaffected."""
        client = _client()
        sid = await client.get_or_create_session()
        await client.add_message(sid, ChatMessage(role="user", content="hi"), store_in_memory=False)
        assert len(await client.get_conversation_history(sid)) == 1

    async def test_no_principal_bound_still_works(self):
        """Backward compatibility: callers that never adopted principals keep
        working, because Fix A removed the guessability that made this unsafe."""
        client = _client()
        sid = await _seed_owned_session(client)
        assert await client.get_conversation_history(sid) == []

    async def test_require_principal_closes_that_path(self):
        client = _client(require_principal=True)
        sid = await _seed_owned_session(client)
        with pytest.raises(SessionOwnershipError):
            await client.get_conversation_history(sid)

    @pytest.mark.parametrize("mode", ["open", "audit"])
    async def test_non_enforcing_modes_do_not_raise(self, mode):
        client = _client(mode)
        sid = await _seed_owned_session(client)
        with bind_principal("mallory"):
            await client.get_conversation_history(sid)  # allowed, reported

    async def test_open_mode_is_todays_behaviour(self):
        """The default must not change under anyone's feet on upgrade."""
        assert SessionConfig().session_ownership == "open"


@pytest.mark.asyncio
class TestFailClosedWhenDegraded:
    async def test_enforce_refuses_when_ownership_cannot_be_read(self):
        """Degraded persistence makes every session look unowned. Under enforce
        that must refuse, not wave everything through."""
        client = _client()
        sid = await _seed_owned_session(client)
        client._degraded = True
        await client.provider.delete_session(sid)  # the in-memory fallback knows nothing

        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError):
                await client.get_conversation_history(sid)

    async def test_audit_keeps_serving_when_degraded(self):
        """Below enforce the same unverifiable state is reported, not refused:
        the call reaches the store and fails (or succeeds) on its own terms."""
        from continuum.session.exceptions import SessionNotFoundError

        client = _client("audit")
        sid = await _seed_owned_session(client)
        client._degraded = True
        await client.provider.delete_session(sid)

        with bind_principal("mallory"):
            with pytest.raises(SessionNotFoundError):
                await client.get_conversation_history(sid)


# ── the door PR #98's placement misses ────────────────────────────────────────


@pytest.mark.asyncio
class TestLLMClientDoor:
    async def test_llm_client_history_load_is_gated(self):
        """``LLMClient.achat(session_id=...)`` reads history directly and takes
        no ``user_id``. A gate in ``AgentRunner._prepare_run`` cannot see this
        call at all; a gate in ``SessionClient`` covers it for free."""
        client = _client()
        sid = await _seed_owned_session(client)

        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError):
                # exactly what llm/client.py:335 does
                await client.get_conversation_history(session_id=sid)

    async def test_llm_client_history_save_is_gated(self):
        client = _client()
        sid = await _seed_owned_session(client)

        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError):
                # exactly what llm/client.py:480 does
                await client.add_message(
                    session_id=sid,
                    message=ChatMessage(role="assistant", content="leak"),
                    store_in_memory=False,
                )


# ── the two fixes together ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFixesCompose:
    async def test_a_guessed_id_no_longer_reaches_a_session(self):
        """End to end: Fix A stops Mallory constructing the id, Fix B stops him
        using one he obtained anyway."""
        cfg = SessionConfig(
            enabled=True,
            provider="memory",
            session_ownership="enforce",
            hash_session_ids=True,
            session_id_secret="deployment-secret-value",
        )
        client = SessionClient(session_config=cfg, memory_client=None, auto_initialize=False)
        client.set_provider(MemorySessionProvider(cfg))
        client._initialized = True

        with bind_principal("alice"):
            real = await client.get_or_create_session(user_id="alice", conversation_id="conv-1")
            await client.add_message(
                real, ChatMessage(role="user", content="private"), store_in_memory=False
            )

        # Fix A: the id Mallory can construct from alice's user id is not it.
        assert real != "c:conv-1:u:alice"

        # Fix B: and the real id, however he got it, does not work either.
        with bind_principal("mallory"):
            with pytest.raises(SessionOwnershipError):
                await client.get_conversation_history(real)
