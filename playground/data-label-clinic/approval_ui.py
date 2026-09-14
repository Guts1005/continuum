"""The `ask` mode: an approval prompt that reaches the browser (finding F7).

The SDK's gate calls an async handler and waits for a decision. Getting that
prompt in front of a person, over HTTP, is the part an application has to solve,
and this is the smallest honest version of it.

HOW IT WORKS, AND WHY IT LOOKS LIKE THIS

``POST /chat`` is already in flight and blocked inside the tool executor when the
handler runs, so the decision cannot come back on that connection. The browser
opens a SECOND request: it polls ``/approval/pending`` while the chat request is
still open, renders whatever it finds, and posts the answer to
``/approval/decide``. The handler is parked on an ``asyncio.Future`` that the
decide endpoint resolves.

That is the shape of every blocking-approval UI over HTTP, and it is worth
seeing plainly because it is also the shape's limitation: the chat request stays
open the whole time. The SDK's ``approval_timeout`` (30s by default) is what
keeps that inside ordinary browser and proxy limits, and when it expires the
gate fails closed. A reviewer who needs longer needs Temporal or a
refuse-and-resume flow instead -- not a bigger timeout.

The pending map is process-local and deliberately not persisted. A restart loses
in-flight prompts, which is correct for a demo and exactly what a durable
implementation would have to fix.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from continuum.agent.approval import ToolApprovalDecision, ToolApprovalRequest

# request_id -> {"request": ..., "future": ...}
_PENDING: dict[str, dict[str, Any]] = {}


async def ui_approval_handler(request: ToolApprovalRequest) -> ToolApprovalDecision:
    """Park the call until the browser answers, or the SDK's timeout fires.

    No timeout of its own: ``request_approval`` already bounds this with
    ``approval_timeout`` and denies when it expires. A second timeout here would
    be a second answer to the same question, and the two would drift.
    """

    request_id = uuid.uuid4().hex[:8]
    future: asyncio.Future[ToolApprovalDecision] = asyncio.get_running_loop().create_future()
    _PENDING[request_id] = {"request": request, "future": future}
    try:
        return await future
    finally:
        # Also runs when the SDK cancels this on timeout, so an unanswered
        # prompt does not sit in the panel forever claiming to be live.
        _PENDING.pop(request_id, None)


def pending_approvals() -> list[dict[str, Any]]:
    """What the browser should show. Arguments included on purpose: a reviewer
    shown only a tool name is approving the name, not the action."""
    return [
        {
            "request_id": rid,
            "tool_name": entry["request"].tool_name,
            "arguments": entry["request"].arguments,
            "agent_name": entry["request"].agent_name,
            "data_labels": sorted(entry["request"].data_labels),
        }
        for rid, entry in _PENDING.items()
        if not entry["future"].done()
    ]


def submit_decision(request_id: str, approved: bool, reviewer: str = "ui") -> bool:
    """Resolve a waiting handler. False when there is nothing to resolve --
    already answered, or already timed out and cleaned up."""
    entry = _PENDING.get(request_id)
    if entry is None or entry["future"].done():
        return False

    from continuum.agent.approval import ToolApprovalDecision

    entry["future"].set_result(
        ToolApprovalDecision(
            approved=approved,
            reviewer=reviewer,
            reason=None if approved else "A reviewer declined this action.",
        )
    )
    return True
