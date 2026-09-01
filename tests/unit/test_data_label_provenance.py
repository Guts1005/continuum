"""
Tests for data-label PROVENANCE tainting (Phase 0 + 1).

Background
----------
`RunContext.data_labels` is a taint set (e.g. {"pii", "phi"}) that rides along
with a run. Historically nothing *set* it automatically — labels were manual-only
and, in practice, always empty — so the one consumer that reads them (the tool
gate) never fired.

This adds the PRODUCER half, with no PII detector in the SDK. Instead the
integrator declares *provenance* — which sources carry which labels — and the
runtime taints the run when data crosses those boundaries. Three declaration
sites:

  1. Tool      — AgentConfig.tool_data_labels[tool_name] -> labels;
                 a tool's result taints the run.
  2. Memory    — AgentMemoryConfig.scope_data_labels[scope] -> labels;
                 reading from that scope taints the run ("read = taint").
  3. Run-level — create_run_context(data_labels=...) seeds the run at start.

Plus the Phase-0 plumbing the producer needs: RunContext.taint().

No detector is shipped: tests declare provenance explicitly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from continuum.agent.utils.context_utils import create_run_context

# ---------------------------------------------------------------------------
# Phase 0 — core plumbing (pure; no runtime harness)
# ---------------------------------------------------------------------------


class TestRunContextTaint:
    def test_taint_adds_a_label(self):
        from continuum.agent.types import RunContext

        ctx = RunContext(run_id="r1")
        ctx.taint("pii")
        assert "pii" in ctx.data_labels

    def test_taint_multiple_and_is_set_semantics(self):
        from continuum.agent.types import RunContext

        ctx = RunContext(run_id="r1")
        ctx.taint("pii", "phi")
        ctx.taint("pii")  # duplicate is a no-op (set)
        assert ctx.data_labels == {"pii", "phi"}

    def test_taint_no_args_is_noop(self):
        from continuum.agent.types import RunContext

        ctx = RunContext(run_id="r1")
        ctx.taint()
        assert ctx.data_labels == set()


# ---------------------------------------------------------------------------
# Phase 1 — site 3: run-level provenance (seed at start)
# ---------------------------------------------------------------------------


class TestRunLevelProvenance:
    def test_create_run_context_seeds_data_labels(self):
        ctx = create_run_context(data_labels={"pii"})
        assert ctx.data_labels == {"pii"}

    def test_create_run_context_defaults_empty(self):
        ctx = create_run_context()
        assert ctx.data_labels == set()


# ---------------------------------------------------------------------------
# Phase 1 — site 1: tool provenance (a declared tool's result taints the run)
# ---------------------------------------------------------------------------


def _tool_call(name: str):
    from continuum.llm.types import FunctionCall, ToolCall

    return ToolCall(id="tc-1", type="function", function=FunctionCall(name=name, arguments="{}"))


def _agent_with_tool_labels(tool_labels: dict[str, set[str]]):
    """A BaseAgent whose tool_executor returns a canned tool result."""
    from continuum.agent.base import BaseAgent
    from continuum.agent.config import AgentConfig

    agent = BaseAgent(
        name="prov-agent",
        instructions="test",
        config=AgentConfig(tool_data_labels=tool_labels),
    )
    # Fake executor: registry hit + a successful tool-result message.
    executor = MagicMock()
    executor.tool_registry = {name: (MagicMock(name="server"), object()) for name in tool_labels}
    executor.tool_registry.setdefault("plain_tool", (MagicMock(name="server"), object()))
    executor.execute_tool_calls = AsyncMock(
        return_value=[{"role": "tool", "tool_call_id": "tc-1", "content": "done"}]
    )
    agent.tool_executor = executor
    agent.on_tool_call = None
    return agent


def _tool_service():
    from continuum.agent.services.tool_service import ToolService

    return ToolService(tool_executor=None)


class TestToolProvenance:
    def test_config_field_defaults_empty(self):
        from continuum.agent.config import AgentConfig

        assert AgentConfig().tool_data_labels == {}

    async def test_declared_tool_result_taints_run(self):
        agent = _agent_with_tool_labels({"fetch_record": {"phi"}})
        svc = _tool_service()
        ctx = create_run_context()

        await svc.execute_tool_call(agent, _tool_call("fetch_record"), ctx)

        assert "phi" in ctx.data_labels

    async def test_undeclared_tool_does_not_taint(self):
        agent = _agent_with_tool_labels({"fetch_record": {"phi"}})
        svc = _tool_service()
        ctx = create_run_context()

        await svc.execute_tool_call(agent, _tool_call("plain_tool"), ctx)

        assert ctx.data_labels == set()


# ---------------------------------------------------------------------------
# Phase 1 — site 1, same-turn batch: a declared tool's taint must gate its
# SIBLINGS in the same batch, not just later turns.
#
# Regression: provenance taint was applied only AFTER a tool returned, while a
# batch of tool calls is gated up front (and, by default, executed in parallel).
# So [lookup_patient (declares "phi"), send_referral_email] would gate the
# exfiltration tool against the still-clean labels and let it through. The fix
# pre-taints the batch from the DECLARED labels of every tool before gating or
# executing any of them — order- and concurrency-independent.
# ---------------------------------------------------------------------------


def _tc(name: str, tid: str):
    from continuum.llm.types import FunctionCall, ToolCall

    return ToolCall(id=tid, type="function", function=FunctionCall(name=name, arguments="{}"))


def _content(msg):
    return msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")


def _gate_simulating_agent(tool_labels: dict[str, set[str]]):
    """Agent whose fake executor records the data_labels it was handed per tool
    and simulates the exfil gate: it denies ``send_referral_email`` whenever the
    labels it receives contain ``phi`` (i.e. the run is already tainted)."""
    from continuum.agent.base import BaseAgent
    from continuum.agent.config import AgentConfig

    agent = BaseAgent(
        name="batch-agent", instructions="test", config=AgentConfig(tool_data_labels=tool_labels)
    )
    agent.policy_store = MagicMock()  # truthy → tool_service threads data_labels through
    agent.on_tool_call = None
    seen: dict[str, set[str]] = {}

    async def fake_exec(
        tool_calls, trace_id=None, policy_store=None, subject=None, data_labels=None
    ):
        tc = tool_calls[0]
        name = tc.function.name
        seen[name] = set(data_labels or ())  # snapshot at the moment this tool is gated
        if name == "send_referral_email" and "phi" in (data_labels or set()):
            return [
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "POLICY DENIED: PHI exfiltration",
                }
            ]
        return [{"role": "tool", "tool_call_id": tc.id, "content": "ok"}]

    executor = MagicMock()
    executor.tool_registry = {
        n: (MagicMock(name="server"), object()) for n in ("lookup_patient", "send_referral_email")
    }
    executor.execute_tool_calls = AsyncMock(side_effect=fake_exec)
    agent.tool_executor = executor
    return agent, seen


class TestBatchSiblingTaint:
    async def test_exfil_tool_listed_first_is_still_gated_against_sibling_taint(self):
        # Adversarial ordering: the exfil tool is listed BEFORE the producer.
        # Without the pre-taint fix, sequential execution runs it on a clean
        # context and it slips through. With the fix it sees "phi" and is denied.
        agent, seen = _gate_simulating_agent({"lookup_patient": {"phi"}})
        svc = _tool_service()
        ctx = create_run_context()

        results = await svc.execute_tools_batch(
            agent,
            [_tc("send_referral_email", "tc-1"), _tc("lookup_patient", "tc-2")],
            ctx,
        )

        assert "phi" in seen["send_referral_email"]  # sibling saw the producer's declared taint
        assert ctx.data_labels == {"phi"}
        assert any("POLICY DENIED" in str(_content(r)) for r in results)

    async def test_parallel_batch_also_gated(self):
        from continuum.agent.config import RunnerConfig

        agent, seen = _gate_simulating_agent({"lookup_patient": {"phi"}})
        # parallel execution is the default-on path that originally leaked.
        from continuum.agent.services.tool_service import ToolService

        svc = ToolService(tool_executor=None, config=RunnerConfig(parallel_tool_calls=True))
        ctx = create_run_context()

        results = await svc.execute_tools_batch(
            agent,
            [_tc("lookup_patient", "tc-1"), _tc("send_referral_email", "tc-2")],
            ctx,
        )

        assert "phi" in seen["send_referral_email"]
        assert any("POLICY DENIED" in str(_content(r)) for r in results)


# ---------------------------------------------------------------------------
# Phase 1 — site 2: memory-scope provenance (reading a labeled scope taints)
# ---------------------------------------------------------------------------


def _memory_result(n=1):
    res = MagicMock()
    items = []
    for i in range(n):
        m = MagicMock()
        m.to_dict.return_value = {"memory": f"fact-{i}"}
        m.metadata = {}
        m.user_id = "u1"
        m.score = 0.9
        m.memory = f"fact-{i}"
        items.append(m)
    res.results = items
    res.total_results = len(items)
    return res


def _memory_client(isolation="user", result=None):
    mc = MagicMock()
    mc.is_enabled = True
    mc.config = MagicMock()
    mc.config.memory_isolation = isolation
    mc.search = AsyncMock(return_value=result if result is not None else _memory_result())
    return mc


def _agent_with_scope_labels(scope_labels: dict[str, set[str]]):
    from continuum.agent.base import BaseAgent
    from continuum.agent.config import AgentConfig, AgentMemoryConfig

    return BaseAgent(
        name="mem-prov-agent",
        instructions="test",
        config=AgentConfig(),
        memory_config=AgentMemoryConfig(scope_data_labels=scope_labels),
    )


def _memory_service(mc):
    from continuum.agent.services.memory_service import MemoryService

    return MemoryService(memory_client=mc, session_client=None)


class TestMemoryScopeProvenance:
    def test_config_field_defaults_empty(self):
        from continuum.agent.config import AgentMemoryConfig

        assert AgentMemoryConfig().scope_data_labels == {}

    async def test_read_from_labeled_scope_taints_run(self):
        svc = _memory_service(_memory_client(isolation="user"))
        agent = _agent_with_scope_labels({"user": {"pii"}})
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)

        assert "pii" in ctx.data_labels

    async def test_no_results_does_not_taint(self):
        # No data actually flowed out of the scope → no taint.
        svc = _memory_service(_memory_client(isolation="user", result=_memory_result(0)))
        agent = _agent_with_scope_labels({"user": {"pii"}})
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)

        assert ctx.data_labels == set()

    async def test_undeclared_scope_does_not_taint(self):
        svc = _memory_service(_memory_client(isolation="user"))
        agent = _agent_with_scope_labels({"agent": {"pii"}})  # labels a different scope
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)

        assert ctx.data_labels == set()


# ---------------------------------------------------------------------------
# Row-level memory provenance (security finding F6)
#
# ``scope_data_labels`` above taints by SCOPE -- the integrator declares "the
# user scope is sensitive" and any read from it taints. That is coarse: it cannot
# distinguish a preference the user really stated from a sentence an attacker
# planted in a web page that the model then echoed into its reply, because both
# end up as rows in the same scope.
#
# The distinguishing fact is provenance, and it is destroyed at write time: the
# extracted fact is stored and nothing records how tainted the run that produced
# it was. So a poisoned row comes back indistinguishable from a genuine one, and
# every downstream defence has to guess.
#
# Two halves close that:
#   write -- stamp the run's live labels onto the row's metadata
#   read  -- re-taint the reading run from the row's own labels
#
# The point is not to label memory for its own sake. It is that the tool gate
# (executor.py, ``subjects = [subject, *sorted(data_labels)]``) then denies the
# action regardless of what the model was persuaded to believe -- which is the
# only defence measured to hold on models that ignore instruction hierarchy.
# ---------------------------------------------------------------------------

PROVENANCE_KEY = "_data_labels"


def _mem_client_with_provider():
    """A real MemoryClient over a mock provider, with memory forced enabled."""
    from unittest.mock import AsyncMock, MagicMock

    from continuum.memory.client import MemoryClient

    provider = MagicMock()
    provider.add = AsyncMock(return_value=MagicMock(results=[]))
    client = MemoryClient(provider=provider, auto_initialize=False)
    client._initialized = True
    type(client)  # keep the class handy for monkeypatching is_enabled below
    return client, provider


class TestMemoryWriteStampsProvenance:
    """The write path records how tainted the producing run was."""

    def _client(self, monkeypatch):
        client, provider = _mem_client_with_provider()
        monkeypatch.setattr(type(client), "is_enabled", property(lambda self: True))
        return client, provider

    async def test_explicit_labels_are_stamped_on_the_row(self, monkeypatch):
        client, provider = self._client(monkeypatch)

        await client.add("a fact", user_id="u1", data_labels={"external"})

        meta = provider.add.await_args.kwargs["metadata"]
        assert meta[PROVENANCE_KEY] == ["external"]

    async def test_labels_are_stored_sorted_and_json_serialisable(self, monkeypatch):
        """A set is not JSON-serialisable and its order is not stable; the vector
        store round-trips metadata as JSON, so persist a sorted list."""
        import json

        client, provider = self._client(monkeypatch)

        await client.add("a fact", user_id="u1", data_labels={"phi", "external", "pii"})

        stamped = provider.add.await_args.kwargs["metadata"][PROVENANCE_KEY]
        assert stamped == ["external", "phi", "pii"]
        json.dumps(stamped)  # must not raise

    async def test_untainted_run_adds_no_provenance_key(self, monkeypatch):
        """Absence of labels must not write an empty marker -- a clean row stays
        clean, so the read side can tell "no labels" from "labelled with none"."""
        client, provider = self._client(monkeypatch)

        await client.add("a fact", user_id="u1")

        meta = provider.add.await_args.kwargs["metadata"] or {}
        assert PROVENANCE_KEY not in meta

    async def test_caller_metadata_is_preserved(self, monkeypatch):
        client, provider = self._client(monkeypatch)

        await client.add(
            "a fact", user_id="u1", metadata={"session_id": "s1"}, data_labels={"external"}
        )

        meta = provider.add.await_args.kwargs["metadata"]
        assert meta["session_id"] == "s1"
        assert meta[PROVENANCE_KEY] == ["external"]

    async def test_caller_metadata_dict_is_not_mutated(self, monkeypatch):
        """The caller's dict may be reused across messages in a save loop."""
        client, provider = self._client(monkeypatch)
        caller_meta = {"session_id": "s1"}

        await client.add("a fact", user_id="u1", metadata=caller_meta, data_labels={"external"})

        assert caller_meta == {"session_id": "s1"}

    async def test_ambient_run_labels_are_stamped_without_explicit_argument(self, monkeypatch):
        """The session-save write path does not thread RunContext, so the labels
        have to come from the ambient policy the runner publishes."""
        from continuum.agent.types import RunContext
        from continuum.security.policy import PolicyStore
        from continuum.security.policy_context import use_active_policy

        client, provider = self._client(monkeypatch)
        ctx = RunContext(run_id="r1")
        ctx.taint("external")

        with use_active_policy(PolicyStore(), "agent-x", ctx):
            await client.add("a fact", user_id="u1")

        meta = provider.add.await_args.kwargs["metadata"]
        assert meta[PROVENANCE_KEY] == ["external"]

    async def test_stamped_even_when_no_policy_store_is_configured(self, monkeypatch):
        """Provenance is bookkeeping, not enforcement. The runner publishes the
        ambient context with ``policy_store=None`` when the agent has none, so
        rows must still be stamped -- otherwise enabling a policy later would
        find every existing row unlabelled and silently ungated."""
        from continuum.agent.types import RunContext
        from continuum.security.policy_context import use_active_policy

        client, provider = self._client(monkeypatch)
        ctx = RunContext(run_id="r1")
        ctx.taint("external")

        with use_active_policy(None, "agent-x", ctx):
            await client.add("a fact", user_id="u1")

        assert provider.add.await_args.kwargs["metadata"][PROVENANCE_KEY] == ["external"]

    async def test_taint_added_mid_run_is_reflected(self, monkeypatch):
        """ActivePolicy reads labels live, so a tool result that taints the run
        after the ambient publish must still reach the row."""
        from continuum.agent.types import RunContext
        from continuum.security.policy import PolicyStore
        from continuum.security.policy_context import use_active_policy

        client, provider = self._client(monkeypatch)
        ctx = RunContext(run_id="r1")

        with use_active_policy(PolicyStore(), "agent-x", ctx):
            ctx.taint("external")  # after the publish
            await client.add("a fact", user_id="u1")

        assert provider.add.await_args.kwargs["metadata"][PROVENANCE_KEY] == ["external"]


def _memory_result_with_labels(*label_sets):
    """A search result whose rows carry the given provenance labels."""
    from unittest.mock import MagicMock

    res = MagicMock()
    items = []
    for i, labels in enumerate(label_sets):
        m = MagicMock()
        meta = {} if labels is None else {PROVENANCE_KEY: labels}
        m.to_dict.return_value = {"memory": f"fact-{i}", "metadata": meta}
        m.metadata = meta
        m.user_id = "u1"
        m.score = 0.9
        m.memory = f"fact-{i}"
        items.append(m)
    res.results = items
    res.total_results = len(items)
    return res


class TestMemoryReadRetaintsFromRowProvenance:
    """Reading a row written by a tainted run re-taints the reading run."""

    async def test_row_label_taints_the_reading_run(self):
        svc = _memory_service(_memory_client(result=_memory_result_with_labels(["external"])))
        agent = _agent_with_scope_labels({})  # no scope labels — row provenance alone
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)

        assert "external" in ctx.data_labels

    async def test_labels_from_several_rows_are_unioned(self):
        svc = _memory_service(
            _memory_client(result=_memory_result_with_labels(["external"], ["pii"]))
        )
        agent = _agent_with_scope_labels({})
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)

        assert ctx.data_labels == {"external", "pii"}

    async def test_unlabelled_rows_do_not_taint(self):
        svc = _memory_service(_memory_client(result=_memory_result_with_labels(None, None)))
        agent = _agent_with_scope_labels({})
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)

        assert ctx.data_labels == set()

    async def test_row_provenance_composes_with_scope_labels(self):
        """Both producers are additive: neither replaces the other."""
        svc = _memory_service(_memory_client(result=_memory_result_with_labels(["external"])))
        agent = _agent_with_scope_labels({"user": {"pii"}})
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)

        assert ctx.data_labels == {"external", "pii"}

    async def test_malformed_provenance_is_ignored_not_fatal(self):
        """Metadata is third-party data round-tripped through a vector store; a
        row whose labels arrive as the wrong type must not take the run down.
        Failing closed here would break every read on one bad row."""
        svc = _memory_service(
            _memory_client(result=_memory_result_with_labels("external", 42, {"a": 1}))
        )
        agent = _agent_with_scope_labels({})
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)  # must not raise

        assert ctx.data_labels == set()

    async def test_non_string_members_are_skipped(self):
        svc = _memory_service(_memory_client(result=_memory_result_with_labels(["external", 7])))
        agent = _agent_with_scope_labels({})
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)

        assert ctx.data_labels == {"external"}

    async def test_empty_results_do_not_taint(self):
        svc = _memory_service(_memory_client(result=_memory_result_with_labels()))
        agent = _agent_with_scope_labels({})
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)

        assert ctx.data_labels == set()


class TestPoisonedMemoryIsDeniedAtTheAction:
    """The whole point: the gate holds without the model's cooperation.

    Measured across four models, no prompt-level framing of retrieved memory
    reliably stops a planted instruction -- gpt-4o-mini obeyed one under every
    envelope and every wording tried. This path does not ask the model anything.
    """

    async def test_tool_denied_after_reading_a_poisoned_row(self):
        from continuum.security.policy import AccessPolicy, PolicyStore
        from continuum.tools.executor import ToolExecutor

        # A row written during a run that had read external content.
        svc = _memory_service(_memory_client(result=_memory_result_with_labels(["external"])))
        agent = _agent_with_scope_labels({})
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)
        assert "external" in ctx.data_labels, "read must taint for the gate to fire"

        store = PolicyStore()
        store.add_policy(
            AccessPolicy(
                name="no-refunds-on-external-data",
                subjects=["external"],
                resources=["tool:issue_refund"],
                effect="deny",
            )
        )
        decision = store.check(["refund-agent", *sorted(ctx.data_labels)], "tool:issue_refund")
        assert decision.allowed is False
        assert decision.policy_name == "no-refunds-on-external-data"

        # And the same subjects list the executor builds is what was checked.
        assert hasattr(ToolExecutor, "execute_tool_call")

    async def test_clean_memory_leaves_the_tool_allowed(self):
        """No false positives: an untainted row must not gate anything."""
        from continuum.security.policy import AccessPolicy, PolicyStore

        svc = _memory_service(_memory_client(result=_memory_result_with_labels(None)))
        agent = _agent_with_scope_labels({})
        ctx = create_run_context(user_id="u1")

        await svc.retrieve_memories(agent, "query", ctx)

        store = PolicyStore()
        store.add_policy(
            AccessPolicy(
                name="no-refunds-on-external-data",
                subjects=["external"],
                resources=["tool:issue_refund"],
                effect="deny",
            )
        )
        decision = store.check(["refund-agent", *sorted(ctx.data_labels)], "tool:issue_refund")
        assert decision.allowed is True
