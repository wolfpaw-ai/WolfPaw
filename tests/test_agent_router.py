"""Router tests — dispatch based on TriageVerdict + emit `triage` event.

Triage and Quick are both faked so we test the routing logic in
isolation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable
from uuid import uuid4

import pytest

from wolfpaw.agents.router import Router
from wolfpaw.agents.triage import TriageVerdict
from wolfpaw.toolbox.registry import ToolContext


@dataclass
class FakeTriage:
    verdict: TriageVerdict

    async def classify(self, *, ctx, thread_id, content):
        return self.verdict


class FakeQuick:
    def __init__(self, reply: str = "quick-reply"):
        self.reply = reply
        self.calls: list[dict] = []

    async def handle(self, *, ctx, thread_id, content, emit=None):
        self.calls.append({"content": content, "thread_id": thread_id})
        return self.reply


def _ctx():
    return ToolContext(user_id=uuid4())


async def test_quick_verdict_dispatches_to_quick_unchanged():
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="quick", complexity="simple", reasoning="r"),
        ),
        quick=FakeQuick(reply="2+2 is 4"),
    )
    text = await router.handle(ctx=_ctx(), thread_id=uuid4(), content="hi")
    assert text == "2+2 is 4"


# Note: the "plan" verdict now invokes the Planner and renders a plan
# preview (rather than falling back to Quick). That path is covered by
# test_agent_router_plan.py — see the FakePlanner there.


async def test_task_verdict_falls_back_to_quick_with_preamble():
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="task", complexity="ambitious", reasoning="r"),
        ),
        quick=FakeQuick(reply="(answer)"),
    )
    text = await router.handle(ctx=_ctx(), thread_id=uuid4(), content="hi")
    assert text.startswith("(Triage suggested I track this")
    assert text.endswith("(answer)")


async def test_emits_triage_event_before_dispatch():
    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    quick = FakeQuick()
    router = Router(
        triage=FakeTriage(
            TriageVerdict(
                route="quick", complexity="simple",
                reasoning="just a lookup",
            ),
        ),
        quick=quick,
    )
    await router.handle(
        ctx=_ctx(), thread_id=uuid4(), content="hello", emit=emit,
    )
    triage_events = [e for e in emitted if e[0] == "triage"]
    assert len(triage_events) == 1
    assert "quick" in triage_events[0][1]
    assert "just a lookup" in triage_events[0][1]


async def test_quick_receives_emit_callback():
    """The emit callback should pass through to the downstream Quick agent."""
    received_emit: list[Callable | None] = []

    class CapturingQuick:
        async def handle(self, *, ctx, thread_id, content, emit=None):
            received_emit.append(emit)
            return "ok"

    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="quick", complexity="simple", reasoning="r"),
        ),
        quick=CapturingQuick(),
    )

    async def emit(event, data):
        pass

    await router.handle(
        ctx=_ctx(), thread_id=uuid4(), content="x", emit=emit,
    )
    assert received_emit == [emit]
