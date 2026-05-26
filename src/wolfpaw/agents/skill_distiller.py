"""Skill Distiller — auto-emit reusable Skills from high-scoring plans.

The Post-Evaluator scores plans 0-100 (step 14). When a plan scores at
or above ``WOLFPAW_SKILL_EMIT_MIN_SCORE`` (default 90) AND the plan
looks reusable (multi-step, uses tools, not a one-shot answer), the
Skill Distiller generalizes it into a new named Skill the Planner
can adapt on future similar requests.

Flow (the public helper :func:`maybe_distill_skill`):

  1. Threshold + reusability heuristic — early return None if either fails.
  2. Embed the plan's user-facing query.
  3. Cosine-search existing skills (user-owned + seeded) against that
     embedding. Drop on hit above
     ``WOLFPAW_SKILL_DEDUP_SIMILARITY_THRESHOLD`` (default 0.85) so we
     don't proliferate near-duplicates as the user runs more plans.
  4. Call Sonnet (via :class:`SkillDistillerAgent`) with the plan +
     execution + verdict — forced ``emit_skill`` tool returns
     ``name``, ``description``, ``ingredients``, ``generalized_steps``.
  5. Embed the new description; persist to ``skills`` via
     :func:`memory.skills.store_emitted` with ``source_plan_id``
     pointing back at the originating plan row.

Failure posture: every step is best-effort. Embedding failures, model
call failures, persistence failures all log and return None. Skill
emission is purely about the agent getting smarter over time — it must
never block returning the user's answer.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Awaitable, Callable
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.embeddings import EmbeddingClient, get_embedder
from wolfpaw.memory import skills as skills_mem
from wolfpaw.memory.db import acquire
from wolfpaw.metering.model_client import ModelClient, get_model_client
from wolfpaw.metering.prompt_versions import bump_prompt_version
from wolfpaw.metering.recorder import record_usage
from wolfpaw.metering.types import TokenCounts
from wolfpaw.persona.builder import build_for_agent
from wolfpaw.schemas import ExecutionPlan, Plan, PostEvalVerdict, StepStatus
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()

EmitFn = Callable[[str, str], Awaitable[None] | None]


_EMIT_SKILL_TOOL = {
    "name": "emit_skill",
    "description": (
        "Record a generalized Skill distilled from a high-scoring plan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Short snake_case identifier. Verb_object style:"
                    " 'vendor_comparison_spreadsheet',"
                    " 'research_one_pager'. No user-specific names or"
                    " timestamps."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "One-paragraph description of the class of task this"
                    " skill handles. Written in the abstract — 'research"
                    " N vendors on user-supplied criteria…' rather than"
                    " 'the user asked for X, so we did Y'. This is the"
                    " text that gets embedded for retrieval, so write"
                    " it to embed similarly to future user queries that"
                    " should match this skill."
                ),
            },
            "ingredients": {
                "type": "object",
                "description": (
                    "Structured hints about what the skill needs."
                    " Conventionally `{\"tools\": [\"web_search\","
                    " \"http_get\", \"create_spreadsheet\"]}`."
                    " Inferred from the plan that worked."
                ),
                "properties": {
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "generalized_steps": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Skeleton of the procedure with placeholders for"
                    " user-specific values. Same shape as a Plan's steps"
                    " (id, kind, description, optional tool / inputs /"
                    " parallel_group) but written abstractly. The"
                    " Planner adapts this skeleton to future requests."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "functional", "reasoning",
                                "evaluation", "subagent",
                            ],
                        },
                        "description": {"type": "string"},
                        "tool": {"type": "string"},
                        "inputs": {"type": "object"},
                        "parallel_group": {"type": "integer"},
                    },
                    "required": ["id", "kind", "description"],
                },
            },
        },
        "required": ["name", "description", "ingredients", "generalized_steps"],
    },
}


_AGENT_ROLE = """You are the **Skill Distiller**. You're handed a specific plan that scored highly (≥ 90/100 by the Post-Evaluator). Your job is to generalize it into a reusable Skill the Planner can adapt on future similar requests.

Generalize ruthlessly:
- Strip out user-specific details (names, dates, URLs, file paths, exact criteria). Replace with placeholders or general descriptions.
- Keep the **shape**: which tools, in what order, with what kind of step.
- Don't keep the literal user query. The description should read like a recipe title + summary, not a re-statement of one user's request.

Naming guidance:
- Use snake_case, verb_object style. Examples: `vendor_comparison_spreadsheet`, `research_one_pager`, `inventory_snapshot`. Avoid timestamps, user names, or one-off keywords.
- Look at the seeded starter set in the user's library if you're unsure of style — names like `receipt_to_ledger`, `newsletter_digest` are the right register.

Description guidance:
- One paragraph. Lead with the class of task ("Research N vendors against user-supplied criteria…"). Mention the inputs the skill expects and the output format it produces.
- This description gets embedded for retrieval, so write it to embed similarly to how a future user would phrase the same request. "Research five vendors" embeds well; "this skill was generated from plan 0a1b…" does not.

Step skeleton guidance:
- One generalized step per step in the original plan, in the same order. Drop steps that were one-offs (e.g. a debugging reasoning step that wouldn't recur). Keep `parallel_group` where the plan parallelized.
- `inputs` should use abstract placeholders ("the document URL"), not the original concrete values.

You MUST call the `emit_skill` tool exactly once. Do not respond with prose."""


class SkillDistillerAgent:
    AGENT_KIND = "skill_distiller"
    VERSION_LABEL = "v1"

    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
    ) -> None:
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
                        "tool": _EMIT_SKILL_TOOL,
                    },
                )
                self._prompt_version_id = row.id
        except Exception:  # noqa: BLE001 — degraded mode is fine
            log.warning(
                "agents.skill_distiller.prompt_version_seed_failed",
                exc_info=True,
            )

    async def distill(
        self,
        *,
        ctx: ToolContext,
        plan: Plan,
        execution: ExecutionPlan,
        verdict: PostEvalVerdict,
    ) -> _RawSkill | None:
        """Single Sonnet call. Returns the raw distilled-skill dict, or
        ``None`` if the model fails to call the forced tool. The caller
        embeds the description + dedups + persists separately."""
        await self._ensure_prompt_version()
        settings = get_settings()
        prompt = _build_distill_prompt(plan, execution, verdict)
        system = await build_for_agent(
            user_id=ctx.user_id, agent_role=_AGENT_ROLE,
        )
        result = await self.model_client.call(
            user_id=ctx.user_id,
            agent=self.AGENT_KIND,
            model=settings.model_planner,  # Sonnet
            messages=[{"role": "user", "content": prompt}],
            system=system,
            prompt_version_id=self._prompt_version_id,
            task_id=ctx.task_id,
            tools=[_EMIT_SKILL_TOOL],
            tool_choice={"type": "tool", "name": "emit_skill"},
        )
        for block in getattr(result.raw, "content", []) or []:
            if (
                _block_type(block) == "tool_use"
                and _block_name(block) == "emit_skill"
            ):
                payload = _block_input(block) or {}
                return _normalize_distilled(payload)
        log.warning(
            "agents.skill_distiller.no_tool_use_block",
            user_id=str(ctx.user_id),
        )
        return None


# --- public helper ---------------------------------------------------------


async def maybe_distill_skill(
    *,
    ctx: ToolContext,
    plan: Plan,
    execution: ExecutionPlan,
    verdict: PostEvalVerdict,
    distiller: SkillDistillerAgent | None = None,
    embedder: EmbeddingClient | None = None,
    emit: EmitFn | None = None,
) -> skills_mem.Skill | None:
    """The full emission flow: heuristic → dedup → distill → persist.

    Returns the new :class:`Skill` row on success, ``None`` when any
    gate skipped emission (below score threshold, not reusable, dedup
    hit, distiller failure, embed failure, persist failure).
    """
    settings = get_settings()

    if verdict.score < settings.skill_emit_min_score:
        log.debug(
            "agents.skill_distiller.below_threshold",
            score=verdict.score,
            threshold=settings.skill_emit_min_score,
        )
        return None

    if not _is_reusable(plan):
        log.debug(
            "agents.skill_distiller.not_reusable",
            step_count=len(plan.steps),
        )
        return None

    if plan.id is None:
        # No persisted plan row → no source_plan_id to point at. Skip.
        log.debug("agents.skill_distiller.no_plan_id")
        return None

    embedder = embedder or get_embedder()

    # Dedup pass 1: by the user's original query.
    query_embedding: list[float] | None = None
    try:
        embed_result = await embedder.embed_one(plan.query)
        query_embedding = embed_result.vectors[0] if embed_result.vectors else None
        if embed_result.input_tokens > 0:
            await _record_voyage(ctx, embed_result.model, embed_result.input_tokens)
    except Exception:  # noqa: BLE001
        log.warning(
            "agents.skill_distiller.query_embed_failed",
            exc_info=True,
        )
        # Without an embedding we can't dedup; bail rather than risk
        # spawning a duplicate of an existing skill.
        return None

    if query_embedding is None:
        return None

    if await _has_near_duplicate(
        ctx.user_id, query_embedding,
        settings.skill_dedup_similarity_threshold,
    ):
        log.info(
            "agents.skill_distiller.dedup_hit",
            user_id=str(ctx.user_id), plan_id=str(plan.id),
        )
        return None

    # Distill.
    agent = distiller or get_skill_distiller_agent()
    try:
        distilled = await agent.distill(
            ctx=ctx, plan=plan, execution=execution, verdict=verdict,
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "agents.skill_distiller.distill_failed",
            user_id=str(ctx.user_id), plan_id=str(plan.id), exc_info=True,
        )
        return None
    if distilled is None:
        return None

    # Re-embed the distilled description — that's the canonical
    # retrieval text. (The query-side dedup was a coarse filter; the
    # description embedding is what `search_by_task` actually matches
    # against on future Planner runs.)
    try:
        desc_result = await embedder.embed_one(distilled.description)
        if not desc_result.vectors:
            log.warning(
                "agents.skill_distiller.description_embed_empty",
                user_id=str(ctx.user_id),
            )
            return None
        description_embedding = desc_result.vectors[0]
        if desc_result.input_tokens > 0:
            await _record_voyage(ctx, desc_result.model, desc_result.input_tokens)
    except Exception:  # noqa: BLE001
        log.warning(
            "agents.skill_distiller.description_embed_failed",
            user_id=str(ctx.user_id), exc_info=True,
        )
        return None

    # Persist.
    try:
        async with acquire() as conn:
            skill_id = await skills_mem.store_emitted(
                conn,
                user_id=ctx.user_id,
                name=distilled.name,
                description=distilled.description,
                embedding=description_embedding,
                ingredients=distilled.ingredients,
                steps=distilled.generalized_steps,
                source_plan_id=plan.id,
                score=verdict.score,
            )
    except Exception:  # noqa: BLE001
        log.warning(
            "agents.skill_distiller.persist_failed",
            user_id=str(ctx.user_id), exc_info=True,
        )
        return None

    log.info(
        "agents.skill_distiller.emitted",
        user_id=str(ctx.user_id),
        skill_id=str(skill_id),
        name=distilled.name,
        source_plan_id=str(plan.id),
    )
    await _maybe_emit(emit, "skill_emitted", distilled.name)
    return skills_mem.Skill(
        id=skill_id,
        user_id=ctx.user_id,
        name=distilled.name,
        description=distilled.description,
        ingredients=distilled.ingredients,
        steps=distilled.generalized_steps,
        source_plan_id=plan.id,
        score=verdict.score,
    )


# --- heuristic + dedup -----------------------------------------------------


def _is_reusable(plan: Plan) -> bool:
    """Reusability heuristic: multi-step AND at least one functional /
    subagent step that uses a tool. Single-step plans, all-reasoning
    plans, and trivial conversational replies don't generalize."""
    if len(plan.steps) < 2:
        return False
    for step in plan.steps:
        if step.kind in ("functional", "subagent"):
            return True
    return False


async def _has_near_duplicate(
    user_id: UUID, query_embedding: list[float], threshold: float,
) -> bool:
    """Return True if any existing user-owned or seeded skill matches
    the embedding above ``threshold``."""
    try:
        async with acquire() as conn:
            matches = await skills_mem.search_by_task(
                conn, user_id=user_id,
                query_embedding=query_embedding, k=1,
            )
    except Exception:  # noqa: BLE001
        log.warning(
            "agents.skill_distiller.dedup_search_failed", exc_info=True,
        )
        return False  # fail-open: better one extra skill than a crashed emit
    if not matches:
        return False
    top = matches[0]
    return (top.similarity or 0.0) >= threshold


# --- helpers ---------------------------------------------------------------


class _RawSkill:
    """Plain Python container for the distilled fields. Defined here
    (not as a dataclass) so we can shape the JSON tool output without
    leaking conversion details to callers."""

    __slots__ = ("name", "description", "ingredients", "generalized_steps")

    def __init__(
        self, *,
        name: str, description: str,
        ingredients: dict[str, Any],
        generalized_steps: list[dict[str, Any]],
    ) -> None:
        self.name = name
        self.description = description
        self.ingredients = ingredients
        self.generalized_steps = generalized_steps


def _normalize_distilled(payload: dict[str, Any]) -> _RawSkill | None:
    """Coerce the Anthropic tool_use payload into a clean ``_RawSkill``.
    Returns None if any required field is missing or malformed."""
    name = (payload.get("name") or "").strip()
    description = (payload.get("description") or "").strip()
    ingredients = payload.get("ingredients")
    generalized_steps = payload.get("generalized_steps")
    if not name or not description:
        return None
    if not isinstance(ingredients, dict):
        ingredients = {}
    if not isinstance(generalized_steps, list) or not generalized_steps:
        return None
    cleaned_steps: list[dict[str, Any]] = []
    for s in generalized_steps:
        if not isinstance(s, dict):
            continue
        cleaned_steps.append(copy.deepcopy(s))
    if not cleaned_steps:
        return None
    return _RawSkill(
        name=name,
        description=description,
        ingredients=copy.deepcopy(ingredients),
        generalized_steps=cleaned_steps,
    )


def _build_distill_prompt(
    plan: Plan, execution: ExecutionPlan, verdict: PostEvalVerdict,
) -> str:
    lines: list[str] = [
        "# User request that produced the high-scoring plan",
        plan.query,
        "",
        "# Plan (the specific instance to generalize)",
        f"summary: {plan.summary}",
        "",
        "## Steps the plan took",
    ]
    for s in plan.steps:
        tool_part = f" — `{s.tool}`" if s.tool else ""
        para_part = (
            f" (parallel group {s.parallel_group})"
            if s.parallel_group is not None else ""
        )
        lines.append(f"- {s.id} [{s.kind}]{tool_part}: {s.description}{para_part}")
        if s.inputs:
            lines.append(f"  inputs: {json.dumps(s.inputs, default=str)}")
    lines.extend([
        "",
        "## How it went",
        f"score: {verdict.score}/100",
        f"summary: {verdict.summary}",
    ])
    if verdict.what_went_well:
        lines.append(f"what_went_well: {verdict.what_went_well}")
    if verdict.improvements:
        lines.append(f"improvements (next time): {verdict.improvements}")
    lines.extend([
        "",
        "## Step outcomes (truncated)",
    ])
    for r in execution.results:
        if r.status == StepStatus.COMPLETED:
            out_repr = json.dumps(r.output, default=str)
            if len(out_repr) > 400:
                out_repr = out_repr[:400] + " …(truncated)"
            lines.append(f"- {r.step_id}: COMPLETED — {out_repr}")
        elif r.status == StepStatus.FAILED:
            lines.append(f"- {r.step_id}: FAILED — {r.error or ''}")
        else:
            lines.append(f"- {r.step_id}: {r.status.value}")
    lines.extend([
        "",
        "Generalize this into a reusable Skill via the `emit_skill` tool.",
    ])
    return "\n".join(lines)


_VOYAGE_MICROCENTS_PER_MTOK = 6  # matches the seeded model_prices row


async def _record_voyage(ctx: ToolContext, model: str, input_tokens: int) -> None:
    """Best-effort cost attribution for the dedup + description embeddings."""
    from math import ceil

    cents = max(0, ceil((input_tokens * _VOYAGE_MICROCENTS_PER_MTOK) / 1_000_000))
    try:
        await record_usage(
            user_id=ctx.user_id,
            agent="skill_distiller",
            model=model,
            usage=TokenCounts(input_tokens=input_tokens),
            cost_cents=cents,
            task_id=ctx.task_id,
        )
    except Exception:  # noqa: BLE001
        log.warning("agents.skill_distiller.voyage_record_failed", exc_info=True)


async def _maybe_emit(emit: EmitFn | None, event: str, data: str) -> None:
    if emit is None:
        return
    result = emit(event, data)
    if hasattr(result, "__await__"):
        await result


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


_agent: SkillDistillerAgent | None = None


def get_skill_distiller_agent() -> SkillDistillerAgent:
    global _agent
    if _agent is None:
        _agent = SkillDistillerAgent()
    return _agent


def reset_skill_distiller_agent() -> None:
    global _agent
    _agent = None
