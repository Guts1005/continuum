"""The approval gate has to be reachable from the ordinary run path (F7).

``agent/approval.py`` holds the primitive and ``tools/executor.py`` enforces it,
but until the runner builds ``ToolApprovalSettings`` from the agent's config and
passes it down, the gate is inert by construction: declared tools run
unapproved and nothing says so.

That is the failure mode this whole finding is about -- a control that exists,
reads as configured, and is wired to nothing -- so the wiring is asserted rather
than assumed. There are TWO call sites in ``ToolService``; one covered and one
missed is indistinguishable from working for whichever path a given deployment
happens not to take.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _agent(*, tools=None, handler=None, timeout=30.0, name="clinic"):
    from continuum.agent.config import AgentConfig

    cfg = AgentConfig()
    cfg.tool_approval = set(tools or ())
    cfg.approval_handler = handler
    cfg.approval_timeout = timeout
    return SimpleNamespace(
        name=name,
        config=cfg,
        policy_store=None,
        tool_executor=None,
        on_tool_call=None,
        on_tool_result=None,
    )


class TestSettingsAreBuiltFromConfig:
    def test_a_configured_agent_produces_settings(self):
        from continuum.agent.approval import build_approval_settings

        async def handler(_req): ...

        settings = build_approval_settings(
            _agent(tools={"send_referral_email"}, handler=handler, timeout=12.0)
        )
        assert settings is not None
        assert settings.tools == frozenset({"send_referral_email"})
        assert settings.handler is handler
        assert settings.timeout == 12.0
        assert settings.agent_name == "clinic"

    def test_an_agent_with_nothing_declared_produces_none(self):
        """Passing settings that gate nothing would mean every call pays a
        lookup for a feature nobody enabled."""
        from continuum.agent.approval import build_approval_settings

        assert build_approval_settings(_agent()) is None

    def test_an_agent_without_a_config_produces_none(self):
        from continuum.agent.approval import build_approval_settings

        assert build_approval_settings(SimpleNamespace(name="bare", config=None)) is None

    def test_declared_without_a_handler_still_produces_settings(self):
        """It must reach the gate precisely BECAUSE it is misconfigured: the gate
        refuses, loudly. Returning None here would turn 'declared but unwired'
        into 'silently ungated', which is the worse of the two."""
        from continuum.agent.approval import build_approval_settings

        settings = build_approval_settings(_agent(tools={"send_referral_email"}))
        assert settings is not None
        assert settings.handler is None


class TestTheRunnerPassesThemDown:
    async def test_the_run_path_passes_approval(self):
        from continuum.agent.services.tool_service import ToolService

        captured = await _run_tool_service(ToolService, streaming=False)
        assert captured.get("approval") is not None, (
            "the non-streaming path does not pass approval settings — declared "
            "tools would run unapproved"
        )
        assert captured["approval"].tools == frozenset({"send_referral_email"})

    async def test_both_call_sites_pass_approval(self):
        """Two sites call execute_tool_calls. One wired and one not is
        indistinguishable from working, for whichever path a deployment does not
        exercise."""
        import inspect

        from continuum.agent.services import tool_service

        src = inspect.getsource(tool_service)
        calls = src.count("execute_tool_calls(")
        passes = src.count("approval=")
        assert passes >= calls, (
            f"{calls} call sites but only {passes} pass approval — "
            "a gate missed at one site is a gate that does not exist there"
        )


async def _run_tool_service(service_cls, *, streaming: bool):
    """Drive ToolService far enough to capture what reaches execute_tool_calls."""
    captured: dict = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return []

    async def handler(_req): ...

    agent = _agent(tools={"send_referral_email"}, handler=handler)
    executor = MagicMock()
    executor.execute_tool_calls = fake_execute
    executor.tool_registry = {}
    agent.tool_executor = executor

    svc = service_cls.__new__(service_cls)
    svc._tool_executor = executor
    svc._message_to_dict = lambda m: {"content": ""}

    context = SimpleNamespace(
        trace_id="t1", data_labels=set(), metadata={}, run_id="r1", taint=lambda *a: None
    )
    tool_call = {"id": "c1", "function": {"name": "send_referral_email", "arguments": "{}"}}
    await svc.execute_tool_call(agent, tool_call, context)
    return captured


class TestTheTemporalRouteIsNotAvailableYet:
    """Wiring the existing HITL primitive as a handler does not work today, and
    the reason is structural rather than missing glue.

    ``HumanInLoopManager`` is the decision-SUBMISSION side -- approve, reject,
    submit_decision -- the API a reviewer's UI calls. The waiting lives inside
    the workflow (``_run_approval_step``), which appends to ``_pending_approvals``
    and blocks on a signal. Nothing outside the workflow can register a request:
    that list is only ever appended to by workflow code.

    And an approval handler runs wherever the tool call runs, which under
    Temporal is inside an ACTIVITY. Workflow APIs -- signals, wait_condition,
    workflow.info() -- are unavailable there by design.

    So the adapter needs workflow-side support that does not exist: a signal to
    register an ad-hoc approval, and a query to read its decision. Asserted here
    so the gap is a tested fact rather than a note someone may not read.
    """

    def test_the_manager_has_no_ask_and_wait_api(self):
        from continuum.temporal.human_in_loop import HumanInLoopManager

        assert not hasattr(HumanInLoopManager, "request_and_wait"), (
            "an ask-and-wait API appeared — the adapter may now be buildable; "
            "see the module docstring before assuming it is"
        )

    def test_pending_approvals_are_only_created_inside_the_workflow(self):
        import inspect

        from continuum.temporal.workflows import agent_workflow

        src = inspect.getsource(agent_workflow)
        appends = [ln for ln in src.splitlines() if "_pending_approvals.append" in ln]
        assert len(appends) == 1, (
            "more than one place creates pending approvals — check whether one of "
            "them is reachable from outside the workflow"
        )
        assert "_run_approval_step" in src


def _request(tool: str = "send_referral_email", **kwargs):
    from continuum.agent.approval import ToolApprovalRequest

    return ToolApprovalRequest(
        tool_name=tool,
        arguments=kwargs or {"to": "dr@example.com"},
        agent_name="clinic",
        run_id="r1",
        data_labels=frozenset(),
    )
