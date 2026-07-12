"""Quick Agent — Haiku 4.5 + the non-sandbox tools.

Used for one-shot answers and simple lookups that don't need a plan. The
Triage Agent (step 11) routes here for low-complexity requests; until
Triage lands, the web channel pipes every non-slash message through
this agent directly.

Loop shape: load recent thread history → append new user turn →
call ModelClient (which records tokens + traces in LangSmith) → if the
response contains tool_use blocks, run them, append tool_result blocks,
loop. Cap iterations so a misbehaving agent can't burn budget forever.

Persistence policy: store the user message and the final assistant text
in `messages`. Intermediate tool calls / results live in-process only —
see `memory/conversational.py`.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.memory import conversational as conv
from wolfpaw.memory.db import acquire
from wolfpaw.metering.model_client import ModelClient, get_model_client
from wolfpaw.metering.prompt_versions import bump_prompt_version
from wolfpaw.persona.builder import build_for_agent
from wolfpaw.toolbox.registry import Registry, ToolContext, ToolError, get_registry
from wolfpaw.tracing import get_logger

log = get_logger()

EmitFn = Callable[[str, str], Awaitable[None] | None]

# Agent-role prompt (the per-agent part). Wrapped by `build_for_agent` at
# call time with the Soul + the user's profile to form the full `system`
# string sent to Anthropic. Stored verbatim in `prompt_versions` so the
# version hash tracks intentional role-prompt edits — Soul / Profile
# variation is per-user and doesn't bump this hash.
_AGENT_ROLE = """You are the **Quick Agent**: Wolfpaw's path for one-shot answers that don't need a plan.

Be brief and direct; expand when the topic warrants it. Use tools when they
help and skip them when they don't. If you're uncertain, say so. If a tool
fails, surface the error plainly rather than dressing it up.

Available tools handle web search, fetching pages, arithmetic, durable
per-user SQL tables, reading/writing markdown files in the user's
workspace, sending the user a Telegram message (`send_telegram_message`)
when they ask you to message or notify them, and scheduling work for later
(`schedule_task` / `list_schedules` / `cancel_schedule`) when they ask you
to do something at a time, on an interval, or repeatedly. Code execution and
artifact production live in the sandbox tools (used by the Executor on larger
plans, not by you).

**Memory.** The recent messages you can see are only a small window of a
possibly long-running relationship — NOT the whole history. For any question
about what you've discussed before — "have we talked about X?", "did I ever
mention Y?", "what did we say about Z?", "remember when we…" — call
`recall_memory` to search the user's entire past conversation before you
answer. Never claim you haven't discussed something based only on what's
currently visible in this window; the topic may simply have scrolled out of
view. Search first, then answer from what you find.

When you schedule something, split the request: the cadence ("every 10
minutes", "at 8pm") becomes the recurrence; `instruction` is what to do on
ONE run with the cadence removed. Bake any condition and "otherwise do
nothing" into `instruction`, since scheduled runs are silent unless they
reach out. Don't compute timestamps yourself: for "in N minutes" pass
`delay_seconds`; for a clock time use a `cron` expression.

You are speaking with one person at a time — the one described above in the
User File. Stay in their context."""


class QuickAgent:
    AGENT_KIND = "quick"
    VERSION_LABEL = "v1"
    MAX_ITERATIONS = 10
    ALLOWED_TOOL_NAMES = frozenset(
        {
            "calculator",
            "http_get",
            "web_search",
            "sql_query",
            "sql_insert",
            "sql_update",
            "sql_delete",
            "create_table",
            "list_tables",
            "describe_table",
            "read_doc",
            "write_doc",
            "list_docs",
            "search_docs",
            "recall_memory",
            "send_telegram_message",
            "schedule_task",
            "list_schedules",
            "cancel_schedule",
        }
    )

    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
        registry: Registry | None = None,
        max_iterations: int | None = None,
    ) -> None:
        self._model_client = model_client
        self._registry = registry
        self._prompt_version_id: UUID | None = None
        self._prompt_seeded = False
        self._max_iterations = max_iterations or self.MAX_ITERATIONS

    @property
    def model_client(self) -> ModelClient:
        return self._model_client or get_model_client()

    @property
    def registry(self) -> Registry:
        return self._registry or get_registry()

    @property
    def allowed_tools(self) -> list:
        return [self.registry.get(n) for n in sorted(self.ALLOWED_TOOL_NAMES)]

    async def _ensure_prompt_version(self) -> None:
        """Lazy-seed (and cache) the `quick:v1` prompt version row. Tolerant
        of DB unavailability — falls back to a NULL prompt_version_id on
        token_usage rows so the agent can still run in degraded mode."""
        if self._prompt_seeded:
            return
        self._prompt_seeded = True
        try:
            async with acquire() as conn:
                row = await bump_prompt_version(
                    conn,
                    agent=self.AGENT_KIND,
                    version_label=self.VERSION_LABEL,
                    content_template={"agent_role": _AGENT_ROLE},
                )
                self._prompt_version_id = row.id
        except Exception:  # noqa: BLE001 — degraded mode is acceptable
            log.warning(
                "agents.quick.prompt_version_seed_failed",
                exc_info=True,
            )

    async def handle(
        self,
        *,
        ctx: ToolContext,
        thread_id: UUID,
        content: str,
        emit: EmitFn | None = None,
    ) -> str:
        """Run one user turn end-to-end. Returns the assistant's final text.

        `emit(event, data)` is an optional callback the channel uses to
        surface progress (tool invocations) as it happens. The final
        response text is returned, not emitted — the channel decides how
        to render it.
        """
        await self._ensure_prompt_version()
        settings = get_settings()

        async with acquire() as conn:
            past = await conv.fetch_recent(
                conn, thread_id=thread_id, n=settings.recent_window_size,
            )
            summaries = await conv.fetch_summaries(conn, thread_id=thread_id)
            await conv.append(
                conn, thread_id=thread_id, role="user", content=content,
                metadata={"channel": ctx.channel} if ctx.channel else None,
            )

        messages: list[dict[str, Any]] = []
        for m in past:
            messages.append({"role": m.role, "content": m.content})
        messages.append({"role": "user", "content": content})

        tool_specs = [t.to_anthropic_schema() for t in self.allowed_tools]
        final_text = await self._run_loop(
            ctx=ctx,
            model=settings.model_quick,
            messages=messages,
            tool_specs=tool_specs,
            summaries=summaries,
            emit=emit,
        )

        async with acquire() as conn:
            await conv.append(
                conn, thread_id=thread_id, role="assistant", content=final_text,
                metadata={"channel": ctx.channel} if ctx.channel else None,
            )
        return final_text

    async def run_headless(
        self,
        *,
        ctx: ToolContext,
        content: str,
        emit: EmitFn | None = None,
    ) -> str:
        """Run one turn through the tool loop with no thread or history —
        used by scheduled tasks, which have no conversation to read from or
        write back to. Same engine as `handle`, so the model calls tools
        iteratively with their real outputs in context (e.g. web_search →
        compose → send_telegram_message). Returns the final text; any
        user-facing delivery happens via the tools the loop calls."""
        await self._ensure_prompt_version()
        settings = get_settings()
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        tool_specs = [t.to_anthropic_schema() for t in self.allowed_tools]
        return await self._run_loop(
            ctx=ctx,
            model=settings.model_quick,
            messages=messages,
            tool_specs=tool_specs,
            summaries=[],
            emit=emit,
        )

    async def _run_loop(
        self,
        *,
        ctx: ToolContext,
        model: str,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]],
        summaries: list[conv.ThreadSummary],
        emit: EmitFn | None,
    ) -> str:
        # Build the full system prompt once per turn (Soul + Profile +
        # agent role + earlier-thread summaries). Stable across the
        # tool loop, so we don't re-fetch mid-loop even though it could
        # in principle change.
        role = _AGENT_ROLE
        summary_block = conv.format_summaries_block(summaries)
        if summary_block:
            role = role + "\n\n" + summary_block
        system_prompt = await build_for_agent(
            user_id=ctx.user_id, agent_role=role,
        )
        for iteration in range(self._max_iterations):
            result = await self.model_client.call(
                user_id=ctx.user_id,
                agent=self.AGENT_KIND,
                model=model,
                messages=messages,
                system=system_prompt,
                prompt_version_id=self._prompt_version_id,
                task_id=ctx.task_id,
                tools=tool_specs,
            )

            raw = result.raw
            assistant_blocks = list(getattr(raw, "content", []) or [])
            messages.append({"role": "assistant", "content": assistant_blocks})

            stop = getattr(raw, "stop_reason", None)
            if stop != "tool_use":
                return result.text or ""

            tool_uses = [
                b for b in assistant_blocks if _block_type(b) == "tool_use"
            ]
            tool_results: list[dict[str, Any]] = []
            for block in tool_uses:
                name = getattr(block, "name", None)
                tool_input = getattr(block, "input", None) or {}
                use_id = getattr(block, "id", None)
                if emit is not None:
                    await _maybe_await(
                        emit("tool", f"{name}({_summarize_input(tool_input)})")
                    )
                tool_results.append(
                    await self._run_tool(ctx, name, tool_input, use_id)
                )
            messages.append({"role": "user", "content": tool_results})

        log.warning(
            "agents.quick.iteration_cap_hit",
            iterations=self._max_iterations,
            user_id=str(ctx.user_id),
        )
        return (
            "I ran out of steps trying to answer that. Try asking a more"
            " specific question, or break the request into smaller pieces."
        )

    async def _run_tool(
        self,
        ctx: ToolContext,
        name: str | None,
        tool_input: dict[str, Any],
        use_id: str | None,
    ) -> dict[str, Any]:
        if not name or name not in self.ALLOWED_TOOL_NAMES:
            return {
                "type": "tool_result",
                "tool_use_id": use_id,
                "content": f"Tool {name!r} is not available to this agent.",
                "is_error": True,
            }
        try:
            tool = self.registry.get(name)
        except KeyError:
            return {
                "type": "tool_result",
                "tool_use_id": use_id,
                "content": f"Tool {name!r} not found in the registry.",
                "is_error": True,
            }
        try:
            output = await tool.run(ctx, **tool_input)
            return {
                "type": "tool_result",
                "tool_use_id": use_id,
                "content": json.dumps(output, default=str),
            }
        except ToolError as e:
            return {
                "type": "tool_result",
                "tool_use_id": use_id,
                "content": str(e),
                "is_error": True,
            }
        except Exception as e:  # noqa: BLE001
            log.exception(
                "agents.quick.tool_unexpected_failure", tool=name
            )
            return {
                "type": "tool_result",
                "tool_use_id": use_id,
                "content": f"Unexpected tool error: {e}",
                "is_error": True,
            }


# --- helpers ---------------------------------------------------------------


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _summarize_input(d: Any) -> str:
    if not isinstance(d, dict):
        s = repr(d)
        return s if len(s) <= 60 else s[:57] + "..."
    parts = []
    for k, v in list(d.items())[:3]:
        s = repr(v)
        if len(s) > 30:
            s = s[:27] + "..."
        parts.append(f"{k}={s}")
    if len(d) > 3:
        parts.append("…")
    return ", ".join(parts)


async def _maybe_await(value: Any) -> None:
    if value is None:
        return
    if hasattr(value, "__await__"):
        await value


# Singleton accessor mirroring the get_*() pattern elsewhere.
_agent: QuickAgent | None = None


def get_quick_agent() -> QuickAgent:
    global _agent
    if _agent is None:
        _agent = QuickAgent()
    return _agent


def reset_quick_agent() -> None:
    """Test/dev hook to drop the cached agent."""
    global _agent
    _agent = None
