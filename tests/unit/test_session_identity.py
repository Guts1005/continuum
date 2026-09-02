"""
Fix A — session ids must not be derivable from the identifiers they scope.

The deployed scheme builds the storage key in plaintext::

    user_id="user-123", conversation_id="conv-456"  ->  "c:conv-456:u:user-123"

which means anyone who knows a user id can *construct* that user's session id.
No leak is required. These tests pin the replacement: an HMAC of the same
derived key under a deployment secret, which keeps determinism (a returning
user still resolves to the same session) while removing derivability.

A plain (unkeyed) hash would not do: SHA-256 is public, so an attacker with the
same guessable input reproduces the same digest. The secret is the whole fix,
which is why configuring hashing without one is a startup error rather than a
silent fallback to plaintext.

Legacy plaintext keys are still readable through a dual-read migration so an
upgrade does not orphan live conversations.
"""

from __future__ import annotations

import hashlib

import pytest

from continuum.session.config import SessionConfig
from continuum.session.exceptions import SessionConfigurationError
from continuum.session.identity import compute_session_id, legacy_session_id

SECRET = "deployment-secret-value"
OTHER_SECRET = "a-different-deployment-secret"


# ── the scheme itself ─────────────────────────────────────────────────────────


class TestDerivation:
    def test_without_a_secret_the_legacy_plaintext_scheme_is_unchanged(self):
        """Opt-in: an un-migrated deployment keeps byte-identical keys."""
        assert compute_session_id(None, "user-123", "conv-456") == "c:conv-456:u:user-123"
        assert compute_session_id(None, "user-123", None) == "u:user-123"

    def test_with_a_secret_the_id_is_opaque(self):
        sid = compute_session_id(None, "user-123", "conv-456", secret=SECRET)
        assert sid.startswith("s_")
        assert "user-123" not in sid
        assert "conv-456" not in sid

    def test_derivation_stays_deterministic(self):
        """Determinism is the property that makes the key useful at all: the
        same user coming back must land on the same session without a lookup."""
        a = compute_session_id(None, "user-123", "conv-456", secret=SECRET)
        b = compute_session_id(None, "user-123", "conv-456", secret=SECRET)
        assert a == b

    def test_an_unkeyed_hash_would_not_have_helped(self):
        """The attack this closes: SHA-256 is public, so hashing a guessable
        input yields a guessable output. The id must not equal anything an
        attacker can compute from the user id alone."""
        guessable = hashlib.sha256(b"c:conv-456:u:user-123").hexdigest()
        sid = compute_session_id(None, "user-123", "conv-456", secret=SECRET)
        assert sid != guessable
        assert guessable[:32] not in sid

    def test_the_secret_is_what_makes_it_unguessable(self):
        """Same identifiers, different secret -> different id. An attacker who
        knows the user id but not the secret cannot reach the session."""
        mine = compute_session_id(None, "user-123", "conv-456", secret=SECRET)
        theirs = compute_session_id(None, "user-123", "conv-456", secret=OTHER_SECRET)
        assert mine != theirs

    def test_distinct_identifiers_stay_distinct(self):
        a = compute_session_id(None, "user-a", "conv-1", secret=SECRET)
        b = compute_session_id(None, "user-b", "conv-1", secret=SECRET)
        c = compute_session_id(None, "user-a", "conv-2", secret=SECRET)
        assert len({a, b, c}) == 3

    def test_the_namespace_separation_survives_hashing(self):
        """The 'c:'/'u:' prefixes separate a bare user id from a
        conversation+user pair. Identifier validation already rejects ':' so the
        two shapes cannot be spelled into each other, but the prefixes still
        have to keep them apart after hashing."""
        from continuum.utils.sanitization import InvalidIdentifierError

        with pytest.raises(InvalidIdentifierError):
            compute_session_id(None, "foo:bar", None, secret=SECRET)

        bare_user = compute_session_id(None, "bar", None, secret=SECRET)
        user_in_conv = compute_session_id(None, "bar", "foo", secret=SECRET)
        assert bare_user != user_in_conv

    def test_an_explicit_session_id_is_passed_through_untouched(self):
        """The internal handoff path supplies framework-generated ids; they are
        already unguessable and must not be re-derived."""
        assert compute_session_id("run-abc-42", None, None, secret=SECRET) == "run-abc-42"

    def test_anonymous_sessions_get_a_random_id(self):
        a = compute_session_id(None, None, None, secret=SECRET)
        b = compute_session_id(None, None, None, secret=SECRET)
        assert a != b  # random, not derived

    def test_user_identifiers_never_appear_in_the_key(self):
        """Secondary benefit: a raw user id in a Redis key also lands in logs,
        traces and slow-query dumps. Hashing removes that exposure."""
        sid = compute_session_id(None, "tom@shyftlabs.io", None, secret=SECRET)
        assert "tom" not in sid
        assert "@" not in sid


# ── migration support ─────────────────────────────────────────────────────────


class TestLegacyKey:
    def test_legacy_key_is_the_plaintext_form(self):
        assert legacy_session_id(None, "user-123", "conv-456") == "c:conv-456:u:user-123"
        assert legacy_session_id(None, "user-123", None) == "u:user-123"

    def test_no_legacy_form_for_an_explicit_id(self):
        """Explicit ids were never derived, so there is nothing to migrate."""
        assert legacy_session_id("run-abc-42", None, None) is None

    def test_no_legacy_form_for_an_anonymous_session(self):
        """A random UUID has no plaintext predecessor to look up."""
        assert legacy_session_id(None, None, None) is None


# ── configuration: fail closed, never silently plaintext ──────────────────────


class TestConfigValidation:
    def test_hashing_without_a_secret_is_a_startup_error(self):
        """The failure mode worth engineering against is a security control that
        reports 'enabled' while doing nothing. Refuse to start instead."""
        with pytest.raises(SessionConfigurationError, match="session_id_secret"):
            SessionConfig(hash_session_ids=True, session_id_secret=None)

    def test_hashing_with_a_secret_is_accepted(self):
        cfg = SessionConfig(hash_session_ids=True, session_id_secret=SECRET)
        assert cfg.hash_session_ids is True
        assert cfg.active_session_id_secret == SECRET

    def test_hashing_off_is_the_default(self):
        """Opt-in: enabling it changes every key, so it is a deliberate act."""
        cfg = SessionConfig()
        assert cfg.hash_session_ids is False
        assert cfg.active_session_id_secret is None

    def test_a_blank_secret_does_not_count_as_configured(self):
        with pytest.raises(SessionConfigurationError, match="session_id_secret"):
            SessionConfig(hash_session_ids=True, session_id_secret="   ")


# ── the providers use it ──────────────────────────────────────────────────────


class TestProvidersHonourTheConfig:
    def test_memory_provider_hashes_when_configured(self):
        from continuum.session.providers.memory import MemorySessionProvider

        cfg = SessionConfig(hash_session_ids=True, session_id_secret=SECRET)
        p = MemorySessionProvider(cfg)
        sid = p._compute_session_id(None, "user-123", "conv-456")
        assert sid.startswith("s_")
        assert "user-123" not in sid

    def test_memory_provider_is_plaintext_by_default(self):
        from continuum.session.providers.memory import MemorySessionProvider

        p = MemorySessionProvider(SessionConfig())
        assert p._compute_session_id(None, "user-123", "conv-456") == "c:conv-456:u:user-123"

    @pytest.mark.asyncio
    async def test_memory_provider_round_trips_a_hashed_session(self):
        from continuum.session.providers.memory import MemorySessionProvider
        from continuum.session.types import ChatMessage

        cfg = SessionConfig(hash_session_ids=True, session_id_secret=SECRET)
        p = MemorySessionProvider(cfg)

        sid = await p.get_or_create_session(user_id="user-123", conversation_id="conv-456")
        await p.add_message(sid, ChatMessage(role="user", content="hello"))

        # Same identifiers must resolve back to the same session.
        again = await p.get_or_create_session(user_id="user-123", conversation_id="conv-456")
        assert again == sid
        assert len(await p.get_messages(again)) == 1

    @pytest.mark.asyncio
    async def test_memory_provider_migrates_a_legacy_plaintext_session(self):
        """Upgrading must not orphan live conversations: the plaintext key is
        read once, moved under the hashed key, and the old key removed."""
        from continuum.session.providers.memory import MemorySessionProvider
        from continuum.session.types import ChatMessage

        # Written before the upgrade, under the plaintext scheme.
        legacy = MemorySessionProvider(SessionConfig())
        old_sid = await legacy.get_or_create_session(user_id="user-123", conversation_id="conv-456")
        assert old_sid == "c:conv-456:u:user-123"
        await legacy.add_message(old_sid, ChatMessage(role="user", content="from before"))

        # Same store, now with hashing switched on.
        upgraded = MemorySessionProvider(
            SessionConfig(hash_session_ids=True, session_id_secret=SECRET)
        )
        upgraded._store = legacy._store

        new_sid = await upgraded.get_or_create_session(
            user_id="user-123", conversation_id="conv-456"
        )
        assert new_sid.startswith("s_")

        messages = await upgraded.get_messages(new_sid)
        assert [m.content for m in messages] == ["from before"]
        assert old_sid not in upgraded._store  # migrated, not copied


class TestRedisProviderMigration:
    @pytest.mark.asyncio
    async def test_redis_provider_migrates_a_legacy_plaintext_session(self):
        import fakeredis.aioredis

        from continuum.session.providers.redis import RedisSessionProvider
        from continuum.session.types import ChatMessage

        shared = fakeredis.aioredis.FakeRedis(decode_responses=True)

        def _provider(cfg: SessionConfig) -> RedisSessionProvider:
            p = RedisSessionProvider(cfg, auto_initialize=False)
            p._redis = shared
            p._initialized = True
            return p

        base = dict(enabled=True, redis_host="localhost")

        legacy = _provider(SessionConfig(**base))
        old_sid = await legacy.get_or_create_session(user_id="user-123", conversation_id="conv-456")
        assert old_sid == "c:conv-456:u:user-123"
        await legacy.add_message(old_sid, ChatMessage(role="user", content="from before"))

        upgraded = _provider(SessionConfig(**base, hash_session_ids=True, session_id_secret=SECRET))
        new_sid = await upgraded.get_or_create_session(
            user_id="user-123", conversation_id="conv-456"
        )
        assert new_sid.startswith("s_")

        messages = await upgraded.get_messages(new_sid)
        assert [m.content for m in messages] == ["from before"]

        # The plaintext keys are gone, so the guessable id no longer resolves.
        assert await shared.exists(legacy._get_metadata_key(old_sid)) == 0
        assert await shared.exists(legacy._get_session_key(old_sid)) == 0

    @pytest.mark.asyncio
    async def test_migration_is_idempotent_under_concurrency(self):
        """Two workers can race on the same first-touch migration; neither may
        lose the conversation."""
        import asyncio

        import fakeredis.aioredis

        from continuum.session.providers.redis import RedisSessionProvider
        from continuum.session.types import ChatMessage

        shared = fakeredis.aioredis.FakeRedis(decode_responses=True)

        def _provider(cfg: SessionConfig) -> RedisSessionProvider:
            p = RedisSessionProvider(cfg, auto_initialize=False)
            p._redis = shared
            p._initialized = True
            return p

        base = dict(enabled=True, redis_host="localhost")
        legacy = _provider(SessionConfig(**base))
        old_sid = await legacy.get_or_create_session(user_id="user-123")
        await legacy.add_message(old_sid, ChatMessage(role="user", content="from before"))

        hashed_cfg = SessionConfig(**base, hash_session_ids=True, session_id_secret=SECRET)
        workers = [_provider(hashed_cfg) for _ in range(4)]
        results = await asyncio.gather(
            *(w.get_or_create_session(user_id="user-123") for w in workers)
        )

        assert len(set(results)) == 1  # all agree on one id
        messages = await workers[0].get_messages(results[0])
        assert [m.content for m in messages] == ["from before"]
