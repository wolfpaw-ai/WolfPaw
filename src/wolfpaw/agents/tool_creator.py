"""Tool Creator — propose, get user-approval, persist a new user-tool.

Invoked by the Executor when a plan contains a ``tool_creator`` step.
The Planner emits these steps when it spots a gap in its toolkit
(no existing builtin or user-tool covers what's needed). Step inputs:
``{intent, required_inputs?}``.

Flow:

  1. Sonnet drafts a structured spec via a forced ``propose_tool``
     tool_use: ``name`` (snake_case), ``description``,
     ``input_schema`` (JSON-schema object), ``implementation``
     (Python source that reads from a local ``inputs`` dict and sets
     a local ``result`` dict).
  2. Embed the description; dedup against the user's existing approved
     tools by cosine similarity. Hit above
     ``WOLFPAW_TOOL_DEDUP_SIMILARITY_THRESHOLD`` → skip without
     prompting (return the existing tool's id).
  3. Persist as ``status='proposed'`` so we have an audit trail even
     if the user later rejects.
  4. Call ``ask_user`` with the full spec (name + description +
     input_schema + implementation) embedded in the question text.
     The user replies yes / no.
  5. yes → ``mark_approved``; the tool becomes dispatchable.
     no → ``mark_rejected``; row kept for audit, not dispatchable.

Returns a dict the Executor turns into the step's output:
``{"tool_id": str, "name": str, "status": "approved"|"rejected"|"duplicate", "message": str}``.

Failure posture:
  * Sonnet can't produce a structured spec → ToolError, step fails.
  * Dedup error → log + fail-open (proceed to propose).
  * ``ask_user`` timeout → propagates per the tool's contract
    (the task transitions to ``blocked`` and the step fails).
  * Persistence failure on the approval transition → log + return
    with status reflecting the underlying outcome; the row stays in
    ``proposed`` for follow-up.

Feature gate: ``WOLFPAW_TOOL_CREATOR_ENABLED`` (default true). When
false, the Executor short-circuits and returns a step failure
explaining that user-tool creation is disabled.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.embeddings import EmbeddingClient, get_embedder
from wolfpaw.memory import tools as tools_dao
from wolfpaw.memory.db import acquire
from wolfpaw.metering.model_client import ModelClient, get_model_client
from wolfpaw.metering.prompt_versions import bump_prompt_version
from wolfpaw.metering.recorder import record_usage
from wolfpaw.metering.types import TokenCounts
from wolfpaw.persona.builder import build_for_agent
from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry
from wolfpaw.tracing import get_logger

log = get_logger()

EmitFn = Callable[[str, str], Awaitable[None] | None]


_PROPOSE_TOOL_TOOL = {
    "name": "propose_tool",
    "description": (
        "Record a structured proposal for a new user-tool. The user"
        " will review + approve before the tool becomes callable."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Short snake_case identifier, verb_object style:"
                    " 'scrape_hn_frontpage', 'fetch_arxiv_abstract',"
                    " 'compute_rolling_mean'. Must NOT collide with"
                    " an existing builtin or this user's existing"
                    " approved tools (you've been shown those above)."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "One-paragraph description of what the tool does,"
                    " written abstractly so the Planner can recognize"
                    " when to use it on future requests. Mention the"
                    " inputs it expects and the output shape."
                ),
            },
            "input_schema": {
                "type": "object",
                "description": (
                    "JSON-schema for the tool's inputs. Must be"
                    " ``{\"type\": \"object\", \"properties\": {...},"
                    " \"required\": [...]}`` — same shape Anthropic"
                    " tool_use expects, since that's what the Planner"
                    " emits and the Executor passes through."
                ),
            },
            "implementation": {
                "type": "string",
                "description": (
                    "Python source that runs in the sandbox. Contract:"
                    " a global ``inputs`` dict carries the keyword"
                    " arguments the Executor invoked the tool with"
                    " (already JSON-decoded from the inputs.json file"
                    " the sandbox harness wrote). Your code MUST set a"
                    " global ``result`` dict before completing; the"
                    " harness serializes it to output.json and the"
                    " Executor returns it as the tool's output. Keep"
                    " the code self-contained: only stdlib + packages"
                    " you `pip install` via install_package at run"
                    " time. No network egress unless explicitly"
                    " allowlisted (it isn't by default). No hardcoded"
                    " secrets — the sandbox's credential proxy fetches"
                    " them per call when needed."
                ),
            },
        },
        "required": ["name", "description", "input_schema", "implementation"],
    },
}


_AGENT_ROLE = """You are the **Tool Creator**. The Planner spotted a gap — no existing tool covers what it needs — and asked you to design a new user-tool.

Your output is a structured proposal that the user will review + approve. Three rules:

1. **Don't duplicate.** You've been shown the user's existing approved tools above. If a sensible variation of one of them would do the job, do NOT propose a new tool — tell the user via the description that the existing tool can be adapted.

2. **Make the implementation runnable in the sandbox.** Your `implementation` field is raw Python that runs in an isolated sandbox. The contract:
   - A global dict named `inputs` is already in scope with the keyword arguments the tool was called with.
   - Your code MUST set a global `result` dict by the time it finishes.
   - You can `pip install` packages, but stdlib-only is preferred for portability.
   - You CAN'T reach the network without explicit allowlisting (defaults to disabled). Plan around this — fetching arbitrary URLs is a Bad Tool unless you have an obvious egress need.
   - No hardcoded secrets — the sandbox's credential proxy is what tools use when they need credentials. For a v1 user-tool, prefer designs that don't need credentials.

3. **Match the JSON-schema convention.** `input_schema` must be a `{"type": "object", "properties": {...}, "required": [...]}` shape — the Planner emits inputs against this schema and the Executor passes them through to your `inputs` dict.

Naming: snake_case, verb_object style. Examples that read well: `parse_csv_to_records`, `summarize_text_chunks`, `format_phone_numbers`. Avoid generic names like `helper` or `process`.

You MUST call the `propose_tool` tool exactly once. Do not respond with prose."""


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


@dataclass(frozen=True)
class ToolCreationOutcome:
    """What :meth:`ToolCreatorAgent.create_tool` returns to the
    Executor. ``status`` is the user's verdict (or "duplicate" when
    we short-circuited)."""

    status: str  # "approved" | "rejected" | "duplicate"
    tool_id: UUID | None
    name: str
    message: str

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tool_id": str(self.tool_id) if self.tool_id else None,
            "name": self.name,
            "message": self.message,
        }


class ToolCreatorAgent:
    AGENT_KIND = "tool_creator"
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
                        "tool": _PROPOSE_TOOL_TOOL,
                    },
                )
                self._prompt_version_id = row.id
        except Exception:  # noqa: BLE001
            log.warning(
                "agents.tool_creator.prompt_version_seed_failed",
                exc_info=True,
            )

    async def create_tool(
        self,
        *,
        ctx: ToolContext,
        intent: str,
        required_inputs: list[str] | None = None,
        plan_id: UUID | None = None,
    ) -> ToolCreationOutcome:
        """Run the propose → dedup → ask_user → persist pipeline.

        Requires ``ctx.task_id`` because the ``ask_user`` step pauses
        the task and dispatches via the originating channel. The
        Executor enforces this via the ``tool_creator`` step kind
        check; we re-validate here too."""
        if ctx.task_id is None:
            raise ToolError(
                "tool_creator requires a Task context — the Triage Agent"
                " should have routed this through the task path"
            )

        settings = get_settings()
        if not settings.tool_creator_enabled:
            raise ToolError(
                "tool_creator is disabled"
                " (WOLFPAW_TOOL_CREATOR_ENABLED=false)"
            )

        await self._ensure_prompt_version()

        # 1. Sonnet drafts the spec.
        spec = await self._propose(ctx=ctx, intent=intent,
                                    required_inputs=required_inputs)
        if spec is None:
            raise ToolError(
                "Tool Creator didn't return a structured proposal"
                " (no tool_use block)"
            )

        # 2. Dedup against existing approved tools by cosine on the
        #    description embedding.
        description_embedding = await self._embed_description(
            ctx, spec.description,
        )
        if description_embedding is not None:
            duplicate = await self._find_duplicate(
                user_id=ctx.user_id,
                embedding=description_embedding,
                threshold=settings.tool_dedup_similarity_threshold,
            )
            if duplicate is not None:
                log.info(
                    "agents.tool_creator.duplicate_skipped",
                    proposed_name=spec.name,
                    existing_name=duplicate.name,
                )
                return ToolCreationOutcome(
                    status="duplicate",
                    tool_id=duplicate.id,
                    name=duplicate.name,
                    message=(
                        f"An existing approved tool `{duplicate.name}`"
                        f" already covers this — use it instead of"
                        f" creating a new one."
                    ),
                )

        # 3. Persist as proposed before asking the user — keeps the
        #    audit trail intact if they reject.
        try:
            async with acquire() as conn:
                tool_id = await tools_dao.store_proposed(
                    conn,
                    user_id=ctx.user_id,
                    name=spec.name,
                    description=spec.description,
                    signature=spec.input_schema,
                    implementation=spec.implementation,
                    embedding=description_embedding,
                    source_plan_id=plan_id,
                    source_task_id=ctx.task_id,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "agents.tool_creator.persist_proposed_failed",
                exc_info=True,
            )
            raise ToolError(
                f"failed to persist proposed tool: {e}"
            ) from e

        # 4. Ask the user to approve.
        question = _format_approval_question(spec)
        registry = get_registry()
        try:
            ask = registry.get("ask_user")
        except KeyError as e:
            raise ToolError(
                "ask_user tool not registered — Tool Creator can't"
                " surface the proposal for approval"
            ) from e

        try:
            answer_payload = await ask.run(
                ctx,
                question=question,
                options=["approve", "reject"],
                urgency="normal",
            )
        except Exception:  # noqa: BLE001
            # ask_user may raise on timeout / cross-user reject. Leave
            # the row in 'proposed' so the user (or a /tasks resume)
            # can still pick it up.
            log.warning(
                "agents.tool_creator.ask_user_failed",
                tool_id=str(tool_id), exc_info=True,
            )
            raise

        # 5. Mark approved or rejected based on the answer.
        answer = str(answer_payload.get("answer", "")).strip().lower()
        approved = answer in {"approve", "yes", "y", "ok", "confirm"}
        try:
            async with acquire() as conn:
                if approved:
                    await tools_dao.mark_approved(conn, tool_id=tool_id)
                else:
                    await tools_dao.mark_rejected(conn, tool_id=tool_id)
        except Exception:  # noqa: BLE001
            log.warning(
                "agents.tool_creator.persist_decision_failed",
                tool_id=str(tool_id), exc_info=True,
            )

        if approved:
            log.info(
                "agents.tool_creator.approved",
                user_id=str(ctx.user_id),
                tool_id=str(tool_id),
                name=spec.name,
            )
            return ToolCreationOutcome(
                status="approved",
                tool_id=tool_id,
                name=spec.name,
                message=(
                    f"User approved the new tool `{spec.name}` —"
                    f" subsequent steps can call it like any builtin."
                ),
            )
        log.info(
            "agents.tool_creator.rejected",
            user_id=str(ctx.user_id),
            tool_id=str(tool_id),
            name=spec.name,
        )
        return ToolCreationOutcome(
            status="rejected",
            tool_id=tool_id,
            name=spec.name,
            message=f"User rejected the proposed tool `{spec.name}`.",
        )

    # --- internals --------------------------------------------------------

    async def _propose(
        self,
        *,
        ctx: ToolContext,
        intent: str,
        required_inputs: list[str] | None,
    ) -> _ProposedSpec | None:
        settings = get_settings()

        # Pre-load existing tools so the model can avoid duplicates.
        builtin_names = sorted(get_registry().names())
        async with acquire() as conn:
            existing_user_tools = await tools_dao.list_approved_for_user(
                conn, user_id=ctx.user_id,
            )

        prompt = _build_propose_prompt(
            intent=intent,
            required_inputs=required_inputs,
            builtin_names=builtin_names,
            existing_user_tools=existing_user_tools,
        )
        system = await build_for_agent(
            user_id=ctx.user_id, agent_role=_AGENT_ROLE,
        )
        result = await self.model_client.call(
            user_id=ctx.user_id,
            agent=self.AGENT_KIND,
            model=settings.model_planner,  # Sonnet — code generation
            messages=[{"role": "user", "content": prompt}],
            system=system,
            prompt_version_id=self._prompt_version_id,
            task_id=ctx.task_id,
            tools=[_PROPOSE_TOOL_TOOL],
            tool_choice={"type": "tool", "name": "propose_tool"},
            max_tokens=2048,
        )
        for block in getattr(result.raw, "content", []) or []:
            if (
                _block_type(block) == "tool_use"
                and _block_name(block) == "propose_tool"
            ):
                payload = _block_input(block) or {}
                return _normalize_spec(payload)
        return None

    async def _embed_description(
        self, ctx: ToolContext, description: str,
    ) -> list[float] | None:
        try:
            embed_result = await self.embedder.embed_one(description)
        except Exception:  # noqa: BLE001
            log.warning(
                "agents.tool_creator.embed_failed", exc_info=True,
            )
            return None
        if not embed_result.vectors:
            return None
        if embed_result.input_tokens > 0:
            try:
                await record_usage(
                    user_id=ctx.user_id,
                    agent=self.AGENT_KIND,
                    model=embed_result.model,
                    usage=TokenCounts(input_tokens=embed_result.input_tokens),
                    cost_cents=_voyage_cents(embed_result.input_tokens),
                    task_id=ctx.task_id,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "agents.tool_creator.embed_record_failed",
                    exc_info=True,
                )
        return embed_result.vectors[0]

    async def _find_duplicate(
        self,
        *,
        user_id: UUID,
        embedding: list[float],
        threshold: float,
    ) -> tools_dao.UserTool | None:
        try:
            async with acquire() as conn:
                matches = await tools_dao.search_by_task(
                    conn, user_id=user_id,
                    query_embedding=embedding, k=1,
                )
        except Exception:  # noqa: BLE001
            log.warning(
                "agents.tool_creator.dedup_search_failed", exc_info=True,
            )
            return None
        if not matches:
            return None
        top = matches[0]
        if (top.similarity or 0.0) >= threshold:
            return top
        return None


# --- spec normalization + prompt building ---------------------------------


@dataclass(frozen=True)
class _ProposedSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    implementation: str


def _normalize_spec(payload: dict[str, Any]) -> _ProposedSpec | None:
    """Validate the propose_tool payload. Returns None on any
    structural problem so the caller can surface a uniform error."""
    name = (payload.get("name") or "").strip()
    description = (payload.get("description") or "").strip()
    schema = payload.get("input_schema")
    implementation = (payload.get("implementation") or "").strip()
    if not name or not description or not implementation:
        return None
    if not _NAME_RE.match(name):
        log.warning(
            "agents.tool_creator.bad_name", name=name,
        )
        return None
    if not isinstance(schema, dict):
        return None
    # JSON-schema sanity: must be an object schema with properties.
    if schema.get("type") != "object":
        return None
    if not isinstance(schema.get("properties"), dict):
        return None
    return _ProposedSpec(
        name=name,
        description=description,
        input_schema=dict(schema),
        implementation=implementation,
    )


def _build_propose_prompt(
    *,
    intent: str,
    required_inputs: list[str] | None,
    builtin_names: list[str],
    existing_user_tools: list[tools_dao.UserTool],
) -> str:
    lines = [
        "# Intent",
        intent,
        "",
    ]
    if required_inputs:
        lines.append("# Required inputs (hints from the Planner)")
        for hint in required_inputs:
            lines.append(f"- {hint}")
        lines.append("")
    lines.append("# Existing builtin tools (don't duplicate)")
    for n in builtin_names:
        lines.append(f"- `{n}`")
    lines.append("")
    if existing_user_tools:
        lines.append("# This user's approved tools (also don't duplicate)")
        for t in existing_user_tools:
            lines.append(f"- `{t.name}`: {t.description}")
        lines.append("")
    lines.append(
        "Design a new tool that fills the intent and doesn't overlap"
        " with what's listed. Call `propose_tool` now."
    )
    return "\n".join(lines)


def _format_approval_question(spec: _ProposedSpec) -> str:
    """Render the full proposal as a single text question for
    ``ask_user``. The Telegram + Slack channels render this verbatim;
    the web client renders Markdown. The user picks ``approve`` or
    ``reject`` from the options list."""
    return (
        f"**Proposed new tool — please review:**\n\n"
        f"- **name**: `{spec.name}`\n"
        f"- **description**: {spec.description}\n\n"
        f"**input_schema:**\n```json\n"
        f"{json.dumps(spec.input_schema, indent=2)}\n```\n\n"
        f"**implementation (runs in your sandbox):**\n```python\n"
        f"{spec.implementation}\n```\n\n"
        f"Approve to add this tool to your toolkit; reject to skip."
    )


# --- helpers ---------------------------------------------------------------


_VOYAGE_MICROCENTS_PER_MTOK = 6  # matches the seeded model_prices row


def _voyage_cents(input_tokens: int) -> int:
    from math import ceil

    return max(0, ceil((input_tokens * _VOYAGE_MICROCENTS_PER_MTOK) / 1_000_000))


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


_agent: ToolCreatorAgent | None = None


def get_tool_creator_agent() -> ToolCreatorAgent:
    global _agent
    if _agent is None:
        _agent = ToolCreatorAgent()
    return _agent


def reset_tool_creator_agent() -> None:
    global _agent
    _agent = None
