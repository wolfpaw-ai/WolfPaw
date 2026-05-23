"""Planner Agent — Sonnet 4.6 (Opus 4.7 for ambitious work) with retrieval.

Flow on every planning request:
    1. Embed the user's query (Voyage)
    2. Search procedural memory for similar past plans (cosine on embedding)
    3. Search skills memory for matching seeded skills
    4. (Step 12.5) Per-thread vector recall via conv.search_relevant
    5. Call Sonnet with the retrieved context + the conversation history,
       forcing a single `generate_plan` tool_use so the output is a
       structured Plan (steps + summary + is_task flag).
    6. Persist the generated plan into procedural memory (success/score=NULL
       until the Post-Evaluator scores it in step 14).
    7. Return the Plan to the caller (today the Router renders it as a
       text preview; the Executor in step 13 will execute it).

Model tier:
    - simple    → Sonnet
    - moderate  → Sonnet
    - ambitious → Opus (every dev user is currently tier='dev' which
      allows Opus; step 24 wires real tier-based gating).

The Planner does NOT persist anything to `messages` — that's the
downstream handler's responsibility, same posture as Triage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.embeddings import EmbeddingClient, get_embedder
from wolfpaw.memory import conversational as conv
from wolfpaw.memory import procedural, skills as skills_mem
from wolfpaw.memory.db import acquire
from wolfpaw.metering.model_client import ModelClient, get_model_client
from wolfpaw.metering.prompt_versions import bump_prompt_version
from wolfpaw.metering.recorder import record_usage
from wolfpaw.metering.types import TokenCounts
from wolfpaw.schemas import Plan, Step
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()


@dataclass(frozen=True)
class PlanContext:
    """What the Planner pulled from memory before calling the model.
    Returned alongside the Plan for transparency / debugging."""

    past_plans: list[procedural.StoredPlan]
    relevant_skills: list[skills_mem.Skill]


_GENERATE_PLAN_TOOL = {
    "name": "generate_plan",
    "description": "Record the structured plan to address the user's request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One-paragraph prose summary of the plan.",
            },
            "is_task": {
                "type": "boolean",
                "description": (
                    "True iff this should be tracked as a long-running"
                    " Task (multi-day, scheduled, or needs external"
                    " waits). False for single-session plans."
                ),
            },
            "steps": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["functional", "reasoning", "evaluation"],
                        },
                        "description": {"type": "string"},
                        "tool": {"type": "string"},
                        "inputs": {"type": "object"},
                        "parallel_group": {"type": "integer"},
                    },
                    "required": ["id", "kind", "description"],
                },
            },
            "adapted_from_past_plan_id": {
                "type": "string",
                "description": "If you reused a past plan, its UUID.",
            },
            "applied_skill_name": {
                "type": "string",
                "description": "If you adapted a seeded skill, its name.",
            },
        },
        "required": ["summary", "is_task", "steps"],
    },
}


_SYSTEM_PROMPT = """You are Wolfpaw's Planning Agent. Your job is to design a structured plan for a non-trivial user request — the Triage Agent has already decided this needs more than a one-shot answer.

ALWAYS consult retrieved context first:
  - **Past plans**: similar plans the user has run before. If one is a strong match and scored well, *adapt* it (cite its id). If you adapt one, lead the plan with that fact in your summary.
  - **Relevant skills**: the v1 starter skill set has hand-written exemplars (vendor comparisons, research one-pagers, newsletter digests, etc). If one fits, adapt its step skeleton; cite the skill name.

Step kinds:
  - "functional" — invoke a specific tool with structured inputs (set `tool` + `inputs`).
  - "reasoning"  — a model call you'll handle inline (no tool); describe what to think through.
  - "evaluation" — a model-graded check; describe what to validate.

Use `parallel_group: <int>` on steps that may run concurrently (e.g. fetching N URLs at once). Omit `parallel_group` for sequential steps. Keep parallelism conservative — only when steps are genuinely independent.

Set `is_task=true` ONLY if the work is long-running (hours/days), needs scheduling, or requires external waits. Most "research X and write Y" requests are single-session — leave `is_task=false`.

Tread lightly: prefer fewer, broader steps over many tiny ones. Don't over-engineer.

You MUST call the `generate_plan` tool exactly once. Do not respond with prose."""


class PlannerAgent:
    AGENT_KIND = "planner"
    VERSION_LABEL = "v1"

    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
        embedder: EmbeddingClient | None = None,
    ) -> None:
        self._model_client = model_client
        self._embedder = embedder
        self._prompt_version_id: UUID | None = None
        self._prompt_seeded = False

    @property
    def model_client(self) -> ModelClient:
        return self._model_client or get_model_client()

    @property
    def embedder(self) -> EmbeddingClient:
        return self._embedder or get_embedder()

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
                        "system": _SYSTEM_PROMPT,
                        "tool": _GENERATE_PLAN_TOOL,
                    },
                )
                self._prompt_version_id = row.id
        except Exception:  # noqa: BLE001 — degraded mode
            log.warning(
                "agents.planner.prompt_version_seed_failed", exc_info=True
            )

    def _pick_model(self, complexity_hint: str) -> str:
        settings = get_settings()
        if complexity_hint == "ambitious":
            return settings.model_planner_opus
        return settings.model_planner

    async def plan(
        self,
        *,
        ctx: ToolContext,
        thread_id: UUID,
        content: str,
        complexity_hint: str = "moderate",
    ) -> tuple[Plan, PlanContext]:
        await self._ensure_prompt_version()

        # 1. Embed query — cost recorded as a token_usage row.
        embed_result = await self.embedder.embed_one(content)
        query_embedding = embed_result.vectors[0] if embed_result.vectors else None
        if embed_result.input_tokens > 0:
            try:
                await record_usage(
                    user_id=ctx.user_id,
                    agent=self.AGENT_KIND,
                    model=embed_result.model,
                    usage=TokenCounts(input_tokens=embed_result.input_tokens),
                    cost_cents=_price_voyage(embed_result.input_tokens),
                    prompt_version_id=self._prompt_version_id,
                    task_id=ctx.task_id,
                )
            except Exception:  # noqa: BLE001 — metering must not break planning
                log.warning("agents.planner.embed_record_failed", exc_info=True)

        # 2-3. Retrieval.
        async with acquire() as conn:
            past = await conv.fetch_recent(conn, thread_id=thread_id, n=20)
            past_plans = (
                await procedural.search_similar(
                    conn, user_id=ctx.user_id,
                    query_embedding=query_embedding, k=3,
                )
                if query_embedding is not None else []
            )
            relevant_skills = (
                await skills_mem.search_by_task(
                    conn, user_id=ctx.user_id,
                    query_embedding=query_embedding, k=3,
                )
                if query_embedding is not None else []
            )

        plan_ctx = PlanContext(past_plans=past_plans, relevant_skills=relevant_skills)

        # 4. Build messages with retrieved context inlined into a system extension.
        context_block = _format_context_block(past_plans, relevant_skills)
        system = _SYSTEM_PROMPT + ("\n\n" + context_block if context_block else "")

        messages = [{"role": m.role, "content": m.content} for m in past]
        messages.append({"role": "user", "content": content})

        # 5. Sonnet call with forced tool_use.
        model = self._pick_model(complexity_hint)
        result = await self.model_client.call(
            user_id=ctx.user_id,
            agent=self.AGENT_KIND,
            model=model,
            messages=messages,
            system=system,
            prompt_version_id=self._prompt_version_id,
            task_id=ctx.task_id,
            tools=[_GENERATE_PLAN_TOOL],
            tool_choice={"type": "tool", "name": "generate_plan"},
        )

        plan = _parse_plan_from_response(result.raw, query=content, model=model)

        # 6. Persist the generated plan.
        try:
            async with acquire() as conn:
                plan_id = await procedural.store(
                    conn,
                    user_id=ctx.user_id,
                    thread_id=thread_id,
                    task_id=ctx.task_id,
                    query=content,
                    query_embedding=query_embedding,
                    steps=plan.to_steps_jsonb(),
                    final_answer=None,
                    success=None,
                    score=None,
                    trace_id=None,
                )
            plan = Plan(
                query=plan.query, summary=plan.summary, steps=plan.steps,
                is_task=plan.is_task, model_used=plan.model_used,
                adapted_from_past_plan_id=plan.adapted_from_past_plan_id,
                applied_skill_name=plan.applied_skill_name,
                id=plan_id,
            )
        except Exception:  # noqa: BLE001
            log.warning("agents.planner.persist_failed", exc_info=True)

        return plan, plan_ctx


# --- helpers ---------------------------------------------------------------


_VOYAGE_MICROCENTS_PER_MTOK = 6  # matches the seeded model_prices row


def _price_voyage(input_tokens: int) -> int:
    """Rough estimate so embeddings show up in /usage. Real per-call
    pricing flows through the model_prices row already (and could be
    computed via pricing.compute_cost_cents if we routed embeddings
    through ModelClient instead). One-cent ceiling minimum keeps the
    /usage line readable for small queries."""
    from math import ceil

    cents = (input_tokens * _VOYAGE_MICROCENTS_PER_MTOK) / 1_000_000
    return max(0, ceil(cents))


def _format_context_block(
    past_plans: list[procedural.StoredPlan],
    relevant_skills: list[skills_mem.Skill],
) -> str:
    parts: list[str] = []
    if past_plans:
        lines = ["## Past plans you've run for this user (most-similar first)"]
        for p in past_plans:
            score_part = f"score={p.score}" if p.score is not None else "score=?"
            lines.append(
                f"- id={p.id} similarity={p.similarity:.2f} {score_part}"
                f"\n  query: {p.query}"
                f"\n  steps: {json.dumps(p.steps)}"
            )
        parts.append("\n".join(lines))
    if relevant_skills:
        lines = ["## Relevant seeded skills"]
        for s in relevant_skills:
            lines.append(
                f"- name={s.name} similarity={s.similarity:.2f}"
                f"\n  description: {s.description}"
                f"\n  steps skeleton: {json.dumps(s.steps)}"
            )
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _parse_plan_from_response(raw: Any, *, query: str, model: str) -> Plan:
    for block in getattr(raw, "content", []) or []:
        if _block_type(block) == "tool_use" and _block_name(block) == "generate_plan":
            payload = _block_input(block) or {}
            steps_raw = payload.get("steps") or []
            steps = [Step.from_dict(s) for s in steps_raw]
            adapted_from = payload.get("adapted_from_past_plan_id")
            adapted_uuid: UUID | None = None
            if adapted_from:
                try:
                    adapted_uuid = UUID(adapted_from)
                except (TypeError, ValueError):
                    adapted_uuid = None
            return Plan(
                query=query,
                summary=str(payload.get("summary", "")),
                steps=steps,
                is_task=bool(payload.get("is_task", False)),
                model_used=model,
                adapted_from_past_plan_id=adapted_uuid,
                applied_skill_name=payload.get("applied_skill_name"),
            )
    log.warning("agents.planner.no_tool_use_block")
    return Plan(
        query=query,
        summary="(planner returned no structured plan — defaulting to a single reasoning step)",
        steps=[
            Step(
                id="reason",
                kind="reasoning",
                description="Answer the user's request directly.",
            )
        ],
        is_task=False,
        model_used=model,
    )


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


_agent: PlannerAgent | None = None


def get_planner_agent() -> PlannerAgent:
    global _agent
    if _agent is None:
        _agent = PlannerAgent()
    return _agent


def reset_planner_agent() -> None:
    global _agent
    _agent = None
