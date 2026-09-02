"""
Session configuration.

Provides configuration classes for session management settings.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from continuum.config import settings
from continuum.session.exceptions import SessionConfigurationError

# Safe minimum pool size — a configured value below this is raised to it so the
# pool can never be accidentally under-provisioned.
_MIN_REDIS_CONNECTIONS = 10


class SessionConfig(BaseModel):
    """Configuration for session management.

    Supports multiple providers via the `provider` field. Each provider
    may use different configuration fields. Redis-specific fields are
    kept for backward compatibility when using the Redis provider.

    Example:
        ```python
        from continuum.session import SessionConfig

        # Using defaults from environment (Redis)
        config = SessionConfig()

        # Explicit configuration with Redis provider
        config = SessionConfig(
            provider="redis",
            enabled=True,
            redis_host="localhost",
            redis_port=6379,
        )

        # Future: Use different provider
        config = SessionConfig(
            provider="dynamodb",
            enabled=True,
            # DynamoDB-specific config...
        )
        ```
    """

    # Provider Selection
    provider: str = Field(
        default="redis",
        description="Session provider to use: 'redis', 'dynamodb', etc.",
    )

    # Session Enable/Disable
    enabled: bool = Field(
        default_factory=lambda: settings.session_enabled,
        description="Enable/disable session management",
    )

    # Redis Configuration (for Redis provider)
    redis_host: str = Field(
        default_factory=lambda: settings.session_redis_host,
        description="Redis host for session storage",
    )
    redis_port: int = Field(
        default_factory=lambda: settings.session_redis_port,
        description="Redis port for session storage",
    )
    redis_password: str | None = Field(
        default_factory=lambda: settings.session_redis_password,
        description="Redis password for authentication",
    )
    redis_db: int = Field(
        default_factory=lambda: settings.session_redis_db,
        description="Redis database number",
    )
    redis_ssl: bool = Field(
        default_factory=lambda: settings.session_redis_ssl,
        description="Enable SSL/TLS for Redis",
    )
    redis_ssl_cert_reqs: str | None = Field(
        default_factory=lambda: settings.session_redis_ssl_cert_reqs,
        description=(
            "TLS cert verification policy passed to redis-py's SSLConnection "
            "only when set. None => redis-py's verifying default ('required'). "
            "'none' disables verification (opt-in, for self-signed test endpoints)."
        ),
    )
    redis_ssl_ca_certs: str | None = Field(
        default_factory=lambda: settings.session_redis_ssl_ca_certs,
        description=(
            "Path to a CA bundle for verifying the Redis server cert. "
            "None => system CA store. Set for private-CA endpoints."
        ),
    )

    # Connection Pool Configuration
    redis_max_connections: int = Field(
        default_factory=lambda: settings.session_redis_max_connections,
        description="Maximum Redis connections in pool (floored at the safe minimum)",
    )

    @field_validator("redis_max_connections")
    @classmethod
    def _enforce_connection_floor(cls, v: int) -> int:
        # A value below the safe minimum is raised to it; higher values are honored.
        return max(v, _MIN_REDIS_CONNECTIONS)

    # Session Behavior
    ttl_seconds: int = Field(
        default_factory=lambda: settings.session_ttl_seconds,
        description="Session TTL in seconds (default: 7 days)",
    )
    max_messages: int = Field(
        default_factory=lambda: settings.session_max_messages,
        description="Maximum messages per session (for scalability)",
    )
    key_prefix: str = Field(
        default_factory=lambda: settings.session_key_prefix,
        description="Redis key prefix for sessions",
    )

    # Message Limit Behavior
    message_limit_strategy: Literal["error", "sliding_window"] = Field(
        default="sliding_window",
        description=(
            "Strategy when message limit is reached. "
            "'error' raises SessionMessageLimitError, "
            "'sliding_window' removes oldest messages to make room for new ones."
        ),
    )
    sliding_window_trim_count: int = Field(
        default=100,
        description=(
            "Number of oldest messages to remove when sliding window is triggered. "
            "Higher values reduce trim frequency but remove more history at once."
        ),
    )

    # Behavior when Redis persistence is unavailable: 'degrade' (in-memory
    # fallback, keep serving) or 'fail' (raise instead of silently degrading).
    fallback_mode: Literal["degrade", "fail"] = Field(
        default_factory=lambda: settings.session_fallback_mode,
        description=(
            "What to do when Redis persistence is unavailable. 'degrade' falls "
            "back to a non-durable in-memory store and keeps serving (the client "
            "reports persistence_degraded=True for monitoring); 'fail' raises "
            "SessionConnectionError instead of silently degrading."
        ),
    )

    # Long-term Memory Write Behavior
    memory_write_mode: Literal["sync", "background"] = Field(
        default_factory=lambda: settings.session_memory_write_mode,
        description=(
            "When to perform the long-term memory (mem0) write relative to the "
            "request. 'background' (default) schedules the memory write as a "
            "fire-and-forget task and returns immediately — faster responses, at "
            "the cost of eventual consistency (a just-stored fact may not be "
            "searchable for a brief moment). 'sync' awaits the memory write before "
            "returning — strong read-after-write consistency, but the mem0 "
            "fact-extraction (an LLM call) adds latency to the response. The "
            "short-term Redis session write is always synchronous regardless of "
            "this setting. NOTE: writes executing inside a Temporal activity are "
            "automatically forced to 'sync' (detected per-call), so the write "
            "stays within the durable, retriable activity boundary and cannot be "
            "lost on worker recycle — no manual configuration needed for Temporal."
        ),
    )

    # Guardrail: how to react when a session_id is passed to runner.run() but
    # no such session exists in the store (the caller forgot to create it).
    strict_sessions: bool = Field(
        default=False,
        description=(
            "When True, runner.run()/run_stream() raise SessionNotCreatedError if "
            "a session_id is passed but the session does not exist (it was never "
            "created via get_or_create_session). When False (default), the runner "
            "logs a loud warning and continues without history/persistence for "
            "that run — non-breaking, but the caller is told what went wrong. Has "
            "no effect on stateless runs (session_id=None), which never trigger "
            "the check. A per-call require_session= argument overrides this."
        ),
    )

    # -------------------------------------------------------------------------
    # Session ownership — a session id is a name, not an authorization
    # -------------------------------------------------------------------------
    session_ownership: Literal["open", "audit", "enforce"] = Field(
        default_factory=lambda: settings.session_ownership,
        description=(
            "How to react when a caller touches a session owned by a different "
            "principal (bound via continuum.session.bind_principal). 'open' "
            "(default) reports the problem and allows the call — today's "
            "behaviour, so upgrading changes nothing for a running deployment. "
            "'audit' reports it loudly with a metric and still allows it, which "
            "is how you measure impact before switching on enforcement. "
            "'enforce' raises SessionOwnershipError. Sessions with no stored "
            "owner (anonymous / single-user deployments) are never affected."
        ),
    )
    require_principal: bool = Field(
        default_factory=lambda: settings.session_require_principal,
        description=(
            "Treat 'no principal bound' as an ownership problem. Off by default: "
            "with hashed session ids an unguessable id in a caller's hand is "
            "reasonable evidence they were given it, so capability-style access "
            "stays available and existing callers keep working. Turn this on "
            "once your auth boundary binds a principal on every request."
        ),
    )
    hash_session_ids: bool = Field(
        default_factory=lambda: settings.session_hash_ids,
        description=(
            "Derive session ids as an HMAC of the identifiers instead of storing "
            "them in plaintext. Without this, 'u:{user_id}' can be constructed "
            "by anyone who knows a user id, so no leak is needed to reach "
            "another user's session. Determinism is preserved (a returning user "
            "still resolves to the same session) and legacy plaintext keys are "
            "migrated on first touch. Requires session_id_secret."
        ),
    )
    session_id_secret: str | None = Field(
        default_factory=lambda: settings.session_id_secret,
        description=(
            "HMAC key for hash_session_ids. Must be identical in every process "
            "and stable across restarts — it is a derivation parameter, not a "
            "per-process random. A secret generated at boot would give each "
            "worker its own key space (a user's history appearing and "
            "disappearing depending on which worker answered) and would orphan "
            "every stored session on redeploy. Rotating it changes every id and "
            "is the same kind of event as the plaintext migration."
        ),
    )

    @model_validator(mode="after")
    def _validate_session_id_secret(self) -> "SessionConfig":
        """Refuse to start with hashing enabled but no secret.

        The alternative — warn and fall back to plaintext derivation — would
        leave the configuration claiming a protection that is not in force. A
        security control reporting 'enabled' while doing nothing is the exact
        failure this whole change exists to remove, so it fails closed instead.
        """
        if self.hash_session_ids and not (self.session_id_secret or "").strip():
            raise SessionConfigurationError(
                "hash_session_ids=True requires session_id_secret (SESSION_ID_SECRET). "
                "A plain hash of a guessable input is still guessable, so the secret "
                "is what makes the id unguessable — without it, hashing would only "
                "change how keys look. Set the same value in every process and keep "
                "it stable across restarts; changing it re-derives every session id."
            )
        return self

    @property
    def active_session_id_secret(self) -> str | None:
        """The secret in force, or None when ids are derived in plaintext.

        Providers read this rather than the raw field so that "hashing is off"
        has exactly one meaning at the call site.
        """
        if not self.hash_session_ids:
            return None
        secret = (self.session_id_secret or "").strip()
        return secret or None

    def is_configured(self) -> bool:
        """Check if session is properly configured."""
        if not self.enabled:
            return False

        # Provider-specific configuration checks
        if self.provider == "redis":
            return bool(self.redis_host)
        # Add checks for other providers as they're added
        # elif self.provider == "dynamodb":
        #     return bool(self.dynamodb_table_name)

        # Unknown provider — fail explicitly rather than silently proceeding
        # and crashing later with a confusing error.
        raise ValueError(
            f"Unknown session provider: '{self.provider}'. "
            f"Supported providers: 'redis'. "
            f"Check the SESSION_PROVIDER environment variable."
        )

    def get_redis_url(self) -> str:
        """Get Redis connection URL."""
        auth = f":{self.redis_password}@" if self.redis_password else ""
        protocol = "rediss" if self.redis_ssl else "redis"
        return f"{protocol}://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"
