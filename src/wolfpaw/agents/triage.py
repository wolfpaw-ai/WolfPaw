"""Triage Agent — first hop on every non-slash inbound message.

Classifies the user's message into one of two routes:
    - "quick" : one-shot answer / lookup, no plan needed
    - "plan"  : anything that needs the Planning Agent

Uses Haiku with a *forced* `classify` tool_use so the output is structured
JSON, not parsed text. The model can't reply with prose — it must call the
tool exactly once with route + complexity + reasoning.

Triage is read-only — it never writes to `messages`. Persistence is the
downstream handler's responsibility. Triage doesn't decide whether work
becomes a long-running Task; that judgment lives on the Planner, which
has the full tool catalog + retrieved context to make the call. Triage
just routes between "quick reply" and "needs the Planner."

Triage reads the verbatim recent window (:func:`conv.fetch_recent`) and
the tiered summaries (:func:`conv.fetch_summaries`) so a long-running
thread's older context informs routing decisions — e.g. recognizing
that a "follow up on yesterday's analysis" turn belongs in the plan
route even when yesterday's messages have already compacted out of the
verbatim window. Vector recall is the Planner's job, not Triage's
(Haiku runs on every turn — keep its context light).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.memory import conversational as conv
from wolfpaw.memory.db import acquire
from wolfpaw.metering.model_client import ModelClient, get_model_client
from wolfpaw.metering.prompt_versions import bump_prompt_version
from wolfpaw.persona.builder import build_for_agent
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()

Route = Literal["quick", "plan"]
Complexity = Literal["simple", "moderate", "ambitious"]


@dataclass(frozen=True)
class TriageVerdict:
    route: Route
    complexity: Complexity
    reasoning: str


_CLASSIFY_TOOL = {
    "name": "classify",
    "description": "Record how to handle the user's most recent message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "route": {
                "type": "string",
                "enum": ["quick", "plan"],
                "description": (
                    "Routing target. 'quick' = single-response answer."
                    " 'plan' = anything that needs the Planning Agent"
                    " (multi-step work, deliverables, ongoing"
                    " monitoring, etc.)."
                ),
            },
            "complexity": {
                "type": "string",
                "enum": ["simple", "moderate", "ambitious"],
                "description": (
                    "Informational hint for downstream agents picking a"
                    " model tier (Haiku/Sonnet/Opus)."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "One-sentence rationale.",
            },
        },
        "required": ["route", "complexity", "reasoning"],
    },
}


_AGENT_ROLE = """You are the **Triage Agent**. Classify each user message into one of two routes:

- "quick": a one-shot answer — lookup, definition, casual reply, anything resolvable in a single response with no deliverable. The default.

- "plan": anything that needs the Planning Agent — multi-step work, real deliverables (spreadsheets, PDFs, code), monitoring/recurring jobs.

Default to "quick" when uncertain. Lean toward "plan" if the user profile (above) signals a preference for detailed deliverables.

Also classify complexity (simple / moderate / ambitious) — informational hint for downstream model tier choice, not a routing input.

You MUST call the `classify` tool exactly once. Do not respond with prose."""


class TriageAgent:
    AGENT_KIND = "triage"
    VERSION_LABEL = "v1"

    def __init__(self, *, model_client: ModelClient | None = None) -> None:
        self._model_client = model_client
        self._prompt_version_id: UUID | None = None
        self._prompt_seeded = False

    @property
    def model_client(self) -> ModelClient:
        return self._model_client or get_model_client()

    async def _ensure_prompt_version(self) -> None:
        if self._prompt_seeded:
            return
        self._prompt_seeded = True
        try:
            async with acquire() as conn:
                row = await bump_prompt_version(
                    conn,
                    agent=self.AGENT_KIND,
                    version_label=self.VERSION_LABEL,
                    content_template={
                        "agent_role": _AGENT_ROLE,
                        "tool": _CLASSIFY_TOOL,
                    },
                )
                self._prompt_version_id = row.id
        except Exception:  # noqa: BLE001 — degraded mode
            log.warning("agents.triage.prompt_version_seed_failed", exc_info=True)

    async def classify(
        self, *, ctx: ToolContext, thread_id: UUID, content: str
    ) -> TriageVerdict:
        await self._ensure_prompt_version()
        settings = get_settings()

        async with acquire() as conn:
            past = await conv.fetch_recent(
                conn, thread_id=thread_id, n=settings.recent_window_size,
            )
            summaries = await conv.fetch_summaries(conn, thread_id=thread_id)

        messages: list[dict] = [
            {"role": m.role, "content": m.content} for m in past
        ]
        messages.append({"role": "user", "content": content})

        role = _AGENT_ROLE
        summary_block = conv.format_summaries_block(summaries)
        if summary_block:
            role = role + "\n\n" + summary_block
        system_prompt = await build_for_agent(
            user_id=ctx.user_id, agent_role=role,
        )
        result = await self.model_client.call(
            user_id=ctx.user_id,
            agent=self.AGENT_KIND,
            model=settings.model_triage,
            messages=messages,
            system=system_prompt,
            prompt_version_id=self._prompt_version_id,
            task_id=ctx.task_id,
            tools=[_CLASSIFY_TOOL],
            tool_choice={"type": "tool", "name": "classify"},
        )

        for block in getattr(result.raw, "content", []) or []:
            if (
                _block_type(block) == "tool_use"
                and _block_name(block) == "classify"
            ):
                payload = _block_input(block) or {}
                route = payload.get("route", "quick")
                complexity = payload.get("complexity", "simple")
                reasoning = payload.get("reasoning", "")
                if route not in ("quick", "plan"):
                    log.warning("agents.triage.unknown_route", route=route)
                    route = "quick"
                if complexity not in ("simple", "moderate", "ambitious"):
                    complexity = "simple"
                return TriageVerdict(
                    route=route, complexity=complexity, reasoning=reasoning,
                )

        # Forced tool_choice should have prevented this, but degrade safely.
        log.warning("agents.triage.no_tool_use_block", user_id=str(ctx.user_id))
        return TriageVerdict(
            route="quick",
            complexity="simple",
            reasoning="defaulted: model did not call classify",
        )


# --- helpers ---------------------------------------------------------------


def _block_type(block):
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _block_name(block):
    if isinstance(block, dict):
        return block.get("name")
    return getattr(block, "name", None)


def _block_input(block):
    if isinstance(block, dict):
        return block.get("input")
    return getattr(block, "input", None)


_agent: TriageAgent | None = None


def get_triage_agent() -> TriageAgent:
    global _agent
    if _agent is None:
        _agent = TriageAgent()
    return _agent


def reset_triage_agent() -> None:
    global _agent
    _agent = None
