"""`delete_memories` — permanent, confirmation-gated deletion of the
user's stored memories about a subject.

Deliberately built as ONE task-context tool rather than a find-then-delete
pair, because tool results aren't persisted between turns: holding the
numbered candidate list in memory across the `ask_user` await is what makes
the numbering stable between what the user sees and what actually gets
deleted. The flow:

  1. Embed the subject and search the user's whole conversation (age-blind,
     no recent-window exclusion) for matching messages.
  2. Cluster the hits into time-proximate *episodes* and present them as a
     stably-numbered list via `ask_user`.
  3. Parse the reply into a selection (numbers / "all" / "all except N" /
     "none"); re-ask once if it's ambiguous.
  4. Show exactly what will be deleted and ask for a final yes/no — the
     destructive step never fires on an unconfirmed or mis-parsed reply.
  5. Delete the chosen messages (embeddings cascade). If the deletion
     touched already-summarized history, clear the thread's tiered
     summaries and enqueue a rebuild so the deleted content can't survive
     in compressed form.

Trigger is deletion *intent* ("delete the memory about X", "delete all
conversations about X"), never a casual "forget about that" — the model
decides that from the description; the confirmation gate is the real safety
net regardless.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.embeddings import get_embedder
from wolfpaw.memory import conversational as conv
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    get_registry,
    register_tool,
)
from wolfpaw.tracing import get_logger

log = get_logger()

_SEARCH_K = 40
_EPISODE_GAP = timedelta(hours=2)
_SNIPPET_CHARS = 80
# Relevance floor (cosine distance, 0=identical … 2=opposite). Deletion is
# destructive, so only surface messages genuinely on-subject — never dredge
# up loosely-related turns the user didn't mean.
_MAX_DISTANCE = 0.6


@register_tool
class DeleteMemoriesTool(Tool):
    name = "delete_memories"
    requires_task_context = True
    description = (
        "PERMANENTLY delete the user's stored memories about a subject, after"
        " showing them a numbered list and confirming. Use ONLY on an explicit"
        " delete request (\"delete the memory about X\", \"delete all"
        " conversations about X\") — NEVER for a casual \"forget about that\""
        " (that just means move on). Deletion cannot be undone."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": (
                    "What to delete memories about — a topic, person, event,"
                    " or thing the user named."
                ),
            },
        },
        "required": ["subject"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        if ctx.task_id is None:
            raise ToolError(
                "delete_memories needs a Task context for the confirmation"
                " step — the request should have routed to the Task path."
            )
        subject = inputs.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            raise ToolError("`subject` must be a non-empty string")
        subject = subject.strip()
        settings = get_settings()

        embedder = get_embedder()
        emb = await embedder.embed_one(subject)
        if not emb.vectors:
            raise ToolError("Could not embed the subject to search memories.")
        subject_vec = emb.vectors[0]

        async with acquire() as conn:
            thread_id = await conv.get_most_recent_thread(
                conn, user_id=ctx.user_id,
            )
            if thread_id is None:
                return {
                    "subject": subject, "found": 0, "deleted": 0,
                    "message": "There's no conversation history to search.",
                }
            hits = await conv.search_relevant(
                conn,
                thread_id=thread_id,
                query_embedding=subject_vec,
                k=_SEARCH_K,
                exclude_recent_n=0,   # deletion can target recent stuff too
                recency_weight=0.0,   # age-blind
                window=0,             # just the matches, not their neighbors
                max_distance=_MAX_DISTANCE,
            )

        if not hits:
            return {
                "subject": subject, "found": 0, "deleted": 0,
                "message": f'I couldn\'t find any memories about "{subject}".',
            }

        episodes = _cluster(hits, _EPISODE_GAP)
        ask = get_registry().get("ask_user")

        # 1. Present the numbered list, ask which to delete.
        question = (
            f'I found these memories related to "{subject}". Reply with the'
            ' numbers to delete (e.g. "1,3", or "all", or "all except 2"),'
            ' or "none" to cancel:\n\n' + _format_episodes(episodes)
        )
        payload = await ask.run(ctx, question=question, urgency="normal")
        chosen = _parse_selection(str(payload.get("answer", "")), len(episodes))
        if chosen is None:
            # Ambiguous — re-ask once, explicitly.
            payload = await ask.run(ctx, question=(
                "Sorry, I didn't catch that. Reply with just the numbers to"
                f" delete (1–{len(episodes)}), \"all\", or \"none\"."
            ), urgency="normal")
            chosen = _parse_selection(
                str(payload.get("answer", "")), len(episodes),
            )
        if not chosen:
            return {
                "subject": subject, "found": len(episodes), "deleted": 0,
                "cancelled": True, "message": "Okay — I won't delete anything.",
            }

        chosen_eps = [episodes[n - 1] for n in chosen]

        # 2. Final confirmation showing exactly what will be deleted, so a
        #    mis-parse is caught before anything is destroyed.
        confirm_q = (
            "I'll PERMANENTLY delete these — this can't be undone:\n\n"
            + "\n".join(
                f"• {_episode_label(e)}" for e in chosen_eps
            )
            + '\n\nConfirm? (yes / no)'
        )
        payload = await ask.run(ctx, question=confirm_q,
                                options=["yes", "no"], urgency="high")
        if not _is_yes(str(payload.get("answer", ""))):
            return {
                "subject": subject, "found": len(episodes), "deleted": 0,
                "cancelled": True,
                "message": "Okay — I've left everything as it was.",
            }

        # 3. Delete, and scrub summaries if we touched summarized history.
        to_delete: list[UUID] = []
        for e in chosen_eps:
            to_delete.extend(e["message_ids"])

        async with acquire() as conn:
            deleted = await conv.delete_messages(
                conn, thread_id=thread_id, message_ids=to_delete,
            )
            scrubbed = False
            if deleted and await conv.thread_has_summaries(
                conn, thread_id=thread_id,
            ):
                cutoff = await conv.recent_window_cutoff(
                    conn, thread_id=thread_id,
                    recent_window=settings.recent_window_size,
                )
                if cutoff is not None and any(
                    ts < cutoff for _id, ts in deleted
                ):
                    await conv.clear_summaries(conn, thread_id=thread_id)
                    scrubbed = True

        if scrubbed:
            try:
                from wolfpaw.workers.queue import enqueue_compact_thread

                await enqueue_compact_thread(thread_id)
            except Exception:  # noqa: BLE001 — best-effort rebuild
                log.warning(
                    "delete_memories.recompact_enqueue_failed",
                    thread_id=str(thread_id), exc_info=True,
                )

        return {
            "subject": subject,
            "found": len(episodes),
            "deleted": len(deleted),
            "episodes_deleted": [_episode_label(e) for e in chosen_eps],
            "summaries_rebuilding": scrubbed,
            "message": (
                f"Permanently deleted {len(deleted)} message(s) across"
                f" {len(chosen_eps)} episode(s) about \"{subject}\"."
                + (" I'm rebuilding the older-conversation summaries so the"
                   " deleted content is gone from those too." if scrubbed else "")
            ),
        }


# --- helpers ---------------------------------------------------------------


def _cluster(hits: list[conv.Message], gap: timedelta) -> list[dict]:
    """Group chronologically-ordered hits into episodes: a new episode
    starts whenever the gap to the previous hit exceeds ``gap``."""
    episodes: list[dict] = []
    cur: dict | None = None
    for m in hits:
        if cur is None or (m.created_at - cur["end"]) > gap:
            cur = {
                "start": m.created_at, "end": m.created_at,
                "message_ids": [m.id], "messages": [m],
            }
            episodes.append(cur)
        else:
            cur["end"] = m.created_at
            cur["message_ids"].append(m.id)
            cur["messages"].append(m)
    return episodes


def _episode_label(e: dict) -> str:
    n = len(e["messages"])
    start: datetime = e["start"]
    end: datetime = e["end"]
    date = start.strftime("%b %d, %Y")
    span = "" if start.date() == end.date() else f"–{end.strftime('%b %d')}"
    snippet = " ".join(e["messages"][0].content.split())
    if len(snippet) > _SNIPPET_CHARS:
        snippet = snippet[: _SNIPPET_CHARS - 1].rstrip() + "…"
    plural = "message" if n == 1 else "messages"
    return f'{date}{span} · {n} {plural} · "{snippet}"'


def _format_episodes(episodes: list[dict]) -> str:
    return "\n".join(
        f"{i}. {_episode_label(e)}" for i, e in enumerate(episodes, 1)
    )


def _parse_selection(reply: str, count: int) -> list[int] | None:
    """Resolve a free-form reply into a list of 1-based episode numbers.

    Returns ``[]`` for an explicit cancel, a non-empty list for a clear
    selection, or ``None`` when the reply is ambiguous (caller re-asks).
    Deliberately conservative: "keep/only/just" phrasings and anything it
    can't confidently read return ``None`` rather than risk deleting the
    wrong thing."""
    r = reply.strip().lower()
    if not r:
        return None
    nums = sorted({int(x) for x in re.findall(r"\d+", r) if 1 <= int(x) <= count})
    is_all = "all" in r or "everything" in r
    is_except = "except" in r or "but not" in r or "but " in r or "not " in r
    is_keep = "keep" in r or "only" in r or "just" in r

    # Explicit cancel (no digits, cancel-ish words).
    if not nums and any(w in r for w in (
        "none", "cancel", "nothing", "stop", "no thanks", "leave",
    )):
        return []

    if is_all and is_except and nums:
        return sorted(set(range(1, count + 1)) - set(nums))
    if is_keep:
        return None  # ambiguous keep-vs-delete semantics — re-ask
    if is_all and not nums:
        return list(range(1, count + 1))
    if nums and not is_all:
        return nums
    if is_all:
        return list(range(1, count + 1))
    return None


def _is_yes(reply: str) -> bool:
    r = reply.strip().lower()
    return r in {"yes", "y", "yeah", "yep", "confirm", "confirmed", "do it", "ok", "okay"}
