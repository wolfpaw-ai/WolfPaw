"""Planner Agent — Sonnet 4.6 (Opus 4.7 for ambitious work) with retrieval.

Flow on every planning request:
    1. Embed the user's query (Voyage)
    2. Search procedural memory for similar past plans (cosine on embedding)
    3. Search skills memory for matching seeded skills
    4. Per-thread vector recall via :func:`conv.search_relevant` — older
       messages from this same thread that semantically match the new
       query (surfaces context past the verbatim window).
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
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.embeddings import EmbeddingClient, get_embedder
from wolfpaw.memory import conversational as conv
from wolfpaw.memory import (
    dropbox_links as dropbox_links_dao,
    microsoft_links as microsoft_links_dao,
    notion_links as notion_links_dao,
    procedural,
    skills as skills_mem,
    tools as user_tools_dao,
)
from wolfpaw.memory.db import acquire
from wolfpaw.metering.model_client import ModelClient, get_model_client
from wolfpaw.metering.prompt_versions import bump_prompt_version
from wolfpaw.metering.recorder import record_usage
from wolfpaw.metering.types import TokenCounts
from wolfpaw.persona.builder import build_for_agent
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
    summaries: list[conv.ThreadSummary]
    vector_recall: list[conv.Message]
    user_tools: list[user_tools_dao.UserTool] = field(default_factory=list)
    connected_integrations: list[str] = field(default_factory=list)


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
                    "Routes the plan. True wraps execution in a Task"
                    " lifecycle (persistent, resumable, push when"
                    " complete) — pick for work that outlives the"
                    " current request. False executes inline — pick"
                    " when the user is waiting now."
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
                            "enum": [
                                "functional", "reasoning",
                                "evaluation", "subagent",
                                "tool_creator",
                            ],
                        },
                        "description": {"type": "string"},
                        "tool": {
                            "type": "string",
                            "description": (
                                "REQUIRED when kind='functional'."
                                " Must match a tool from the catalog."
                                " If no catalog tool fits, use"
                                " kind='tool_creator' instead — NEVER"
                                " leave this blank."
                            ),
                        },
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


_AGENT_ROLE = """You are the **Planning Agent**. Your job is to design a structured plan for a non-trivial user request — the Triage Agent has already decided this needs more than a one-shot answer.

ALWAYS consult retrieved context first:
  - **Past plans**: similar plans the user has run before. If one is a strong match and scored well, *adapt* it (cite its id). If you adapt one, lead the plan with that fact in your summary.
  - **Relevant skills**: the v1 starter skill set has hand-written exemplars (vendor comparisons, research one-pagers, newsletter digests, etc). If one fits, adapt its step skeleton; cite the skill name.

Step kinds:
  - "functional" — invoke a tool from the catalog. Set `tool` to the name and `inputs` to every required argument the tool's signature lists.
  - "reasoning"  — a model call you'll handle inline (no tool); describe what to think through.
  - "evaluation" — a model-graded check; describe what to validate.
  - "tool_creator" — propose a NEW tool for capability gaps (no catalog tool fits). Sets `inputs: {intent: "one-paragraph spec", required_inputs?: [...]}`. The user approves via ask_user before the tool is registered.
  - "subagent"   — delegate a chunk of work to a child task that runs its own full
                   Planner→Executor→Post-Evaluator pipeline. Use this when:
                     * the work splits into independent investigations that benefit
                       from their own context window (e.g. "research these 5 vendors"
                       → 5 subagent steps in `parallel_group: 1`)
                     * the parent's job is to coordinate + synthesize, not execute
                   `inputs` carries `{query, title?, budget_cents?, complexity_hint?, on_failure?}`.
                   `query` is what the child agent is asked to do (be specific —
                   the child has no parent context).
                   `on_failure` controls what happens if the subagent fails:
                     * "fail" (default) — the parent step fails; the whole plan stops.
                       Use when the subagent's output is load-bearing.
                     * "drop"            — the parent step succeeds with a stub
                       output (`{"dropped": true, "error": "..."}`) so the
                       synthesis step can work around the missing data. Use for
                       "research these 5 vendors" style fanouts where 4-of-5
                       results is still useful.
                     * "retry"           — re-spawn the subagent once with the
                       failure context appended to the query. If the retry also
                       fails, the parent step fails (no further retries). Use
                       when the failure is likely transient.
                   Depth is capped at 3 levels. Don't nest subagents unless really
                   needed; prefer flattening.
                   End the parent plan with a reasoning step that synthesizes the
                   subagent outputs into a coherent answer.

Use `parallel_group: <int>` on steps that may run concurrently (e.g. fetching N URLs at once, or N subagent investigations). Omit `parallel_group` for sequential steps. Keep parallelism conservative — only when steps are genuinely independent.

Set `is_task=true` for plans that should outlive the current chat turn: long-running work, external waits, monitoring/recurring jobs, plans containing a `tool_creator` step (it needs `ask_user`, which needs a task lifecycle). Default `false` for plans the user is waiting on now.

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
                        "agent_role": _AGENT_ROLE,
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
        thread_id: UUID | None,
        content: str,
        complexity_hint: str = "moderate",
        revision_diagnosis: str | None = None,
    ) -> tuple[Plan, PlanContext]:
        """Generate a Plan. `thread_id=None` means no conversational history
        to load — used by subagent tasks (step 16) which run with a fresh
        context derived only from the subagent step's `inputs.query`.

        `revision_diagnosis` is set by the Pre-Evaluator's retry path
        (step 24): when a first-pass plan is rejected, the diagnosis
        gets folded into the system prompt as concrete feedback so the
        second pass can address it directly rather than re-deriving."""
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

        # 2-4. Retrieval. Conversational memory is per-thread, so we
        # skip those queries when running subagent (thread_id=None).
        settings = get_settings()
        async with acquire() as conn:
            past = (
                await conv.fetch_recent(
                    conn,
                    thread_id=thread_id,
                    n=settings.recent_window_size,
                )
                if thread_id is not None else []
            )
            summaries = (
                await conv.fetch_summaries(conn, thread_id=thread_id)
                if thread_id is not None else []
            )
            vector_recall = (
                await conv.search_relevant(
                    conn,
                    thread_id=thread_id,
                    query_embedding=query_embedding,
                    k=settings.vector_recall_k,
                    exclude_recent_n=settings.recent_window_size,
                )
                if thread_id is not None and query_embedding is not None
                else []
            )
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
            # Every approved user-tool for this user — small list in
            # practice, well under the model's budget. Inlined into the
            # prompt so the Planner can pick them alongside builtins
            # (and avoid proposing a duplicate via `tool_creator`).
            user_tools = await user_tools_dao.list_approved_for_user(
                conn, user_id=ctx.user_id,
            )
            # Which OAuth integrations this user has connected.
            # Surfaced as plain provider names so the prompt can tell
            # the model "you may use the dropbox_* tools because the
            # user has connected Dropbox" / "don't propose Notion
            # steps, they haven't connected it."
            connected_integrations = await _resolve_connected_integrations(
                conn, user_id=ctx.user_id,
            )

        plan_ctx = PlanContext(
            past_plans=past_plans,
            relevant_skills=relevant_skills,
            summaries=summaries,
            vector_recall=vector_recall,
            user_tools=user_tools,
            connected_integrations=connected_integrations,
        )

        # 5. Build messages. The system prompt is Soul + User File + the
        # planner's role + the retrieved context block (past plans +
        # skills + thread summaries + vector recall). build_for_agent
        # assembles the first three; we append the dynamic context
        # block after. When the Pre-Evaluator rejected a first-pass
        # draft, the diagnosis goes in front so the retry attends to
        # it before reading the rest.
        context_block = _format_context_block(
            past_plans=past_plans,
            relevant_skills=relevant_skills,
            summaries=summaries,
            vector_recall=vector_recall,
            user_tools=user_tools,
            connected_integrations=connected_integrations,
        )
        role_with_context = _AGENT_ROLE
        if revision_diagnosis:
            role_with_context = (
                _format_revision_block(revision_diagnosis) + "\n\n"
                + role_with_context
            )
        if context_block:
            role_with_context = role_with_context + "\n\n" + context_block
        system = await build_for_agent(
            user_id=ctx.user_id, agent_role=role_with_context,
        )

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


def _format_revision_block(diagnosis: str) -> str:
    """Pre-Evaluator retry preamble — sits at the front of the system
    prompt so the model treats the diagnosis as a top-line directive
    rather than another piece of context."""
    return (
        "## You are revising a rejected draft\n"
        "Your previous plan for this same user request was rejected by"
        " the Pre-Evaluator with this diagnosis:\n\n"
        f"> {diagnosis.strip()}\n\n"
        "Address the diagnosis concretely. Don't repeat the rejected"
        " approach. If the diagnosis says a step is redundant, drop it."
        " If it says a step doesn't achieve the objective, replace it."
        " If it says a past plan would do better, adapt that past plan."
    )


async def _resolve_connected_integrations(
    conn, *, user_id: UUID,
) -> list[str]:
    """Return the list of provider names the user has connected OAuth
    tokens for. Currently just Dropbox; Notion + Microsoft Calendar
    extend this in steps 30 / 32. Best-effort per provider — a DAO
    error for one doesn't block the others."""
    connected: list[str] = []
    try:
        if await dropbox_links_dao.get(conn, user_id=user_id) is not None:
            connected.append("dropbox")
    except Exception:  # noqa: BLE001
        log.warning(
            "agents.planner.dropbox_link_lookup_failed", exc_info=True,
        )
    try:
        if await notion_links_dao.get(conn, user_id=user_id) is not None:
            connected.append("notion")
    except Exception:  # noqa: BLE001
        log.warning(
            "agents.planner.notion_link_lookup_failed", exc_info=True,
        )
    try:
        if await microsoft_links_dao.get(conn, user_id=user_id) is not None:
            connected.append("microsoft")
    except Exception:  # noqa: BLE001
        log.warning(
            "agents.planner.microsoft_link_lookup_failed", exc_info=True,
        )
    return connected


def _format_context_block(
    *,
    past_plans: list[procedural.StoredPlan],
    relevant_skills: list[skills_mem.Skill],
    summaries: list[conv.ThreadSummary],
    vector_recall: list[conv.Message],
    user_tools: list[user_tools_dao.UserTool],
    connected_integrations: list[str],
) -> str:
    parts: list[str] = []
    # Tool catalog FIRST — the Planner needs to know which tools
    # exist + what inputs they require before deciding step kinds.
    # Without this block the model would emit `{tool: "read_doc",
    # inputs: {}}` because it has no schema visibility (the bug found
    # in the recipes.md → DB write attempt).
    from wolfpaw.toolbox.registry import get_registry

    parts.append(get_registry().catalog_block())

    summary_block = conv.format_summaries_block(summaries)
    if summary_block:
        parts.append(summary_block)
    recall_block = conv.format_vector_recall_block(vector_recall)
    if recall_block:
        parts.append(recall_block)
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
    if user_tools:
        lines = [
            "## User tools (approved by this user, available like builtins)",
        ]
        for t in user_tools:
            lines.append(
                f"- `{t.name}`: {t.description}"
                f"\n  input_schema: {json.dumps(t.signature)}"
            )
        parts.append("\n".join(lines))
    # OAuth integrations: tell the model which provider tools are
    # actually usable. Dropbox (step 29), Notion (30), Microsoft
    # Calendar (32) tool names all share a `<provider>_*` prefix; the
    # model is expected to read this list and ONLY pick tools whose
    # prefix is in the connected set.
    integration_lines = [
        "## OAuth integrations (provider tools the user can use)",
    ]
    if connected_integrations:
        for provider in connected_integrations:
            integration_lines.append(
                f"- `{provider}` connected — `{provider}_*` tools are usable."
            )
    else:
        integration_lines.append(
            "- None connected. Do NOT pick `dropbox_*`, `notion_*`, or"
            " `outlook_*` tools — they'll surface 'not connected'"
            " errors. If a plan needs one, end the plan with a"
            " reasoning step telling the user how to connect."
        )
    parts.append("\n".join(integration_lines))
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
