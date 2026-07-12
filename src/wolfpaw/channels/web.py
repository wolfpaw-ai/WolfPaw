"""Web channel: `/channels/web/chat` SSE endpoint + `/channels/web/answer`
for resolving `ask_user` pauses.

Stream shape:
    event: thread | command | triage | plan | tool | task | ask_user
         | step.start | step.end | step.error | score | delta | done | error
    data: <text>             # one `data:` line per newline in the text

`/channels/web/answer` (POST): the user supplies an answer to a pending
ask_user question. Resolves the in-process registry's future so the tool
returns the answer to the executor and the task resumes.

`WebChannel.send()` is intentionally unimplemented — proactive web push
needs websockets and isn't on the v1 roadmap.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from wolfpaw.agents.router import Router, get_router
from wolfpaw.auth.deps import require_user_id
from wolfpaw.channels import Channel, InboundMessage, register_channel
from wolfpaw.channels.commands import get_dispatcher
from wolfpaw.memory import conversational as conv, pending_questions as pq_dao
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()
router = APIRouter(prefix="/channels/web", tags=["channels"])


class ChatRequest(BaseModel):
    content: str
    thread_id: UUID | None = None


class AnswerRequest(BaseModel):
    question_id: UUID
    answer: str


class ChatMessage(BaseModel):
    id: UUID
    role: str
    content: str
    created_at: datetime


class MessagesResponse(BaseModel):
    # None thread_id + empty messages means the user has no thread yet
    # (brand-new account or right after a /reset that never got a message).
    thread_id: UUID | None
    messages: list[ChatMessage]
    has_more: bool


class WebChannel(Channel):
    name = "web"

    async def receive(self, payload: dict) -> InboundMessage:
        return InboundMessage(
            user_id=payload["user_id"],
            content=payload["content"],
            channel_name=self.name,
            thread_id=payload.get("thread_id"),
            metadata=payload.get("metadata", {}),
        )

    async def send(self, user_id: UUID, content: str, **kwargs: Any) -> None:
        raise NotImplementedError(
            "WebChannel.send is not implemented — web replies stream"
            " through the /chat SSE endpoint instead."
        )

    def supports_streaming(self) -> bool:
        return True


_web_channel = register_channel(WebChannel())


def _sse_event(event: str, data: str) -> bytes:
    """Render a single SSE event. Each newline in `data` becomes its own
    `data:` line, per the EventSource spec."""
    lines = data.split("\n") if data else [""]
    payload = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event}\n{payload}\n\n".encode("utf-8")


async def _run_router_into_queue(
    *,
    router: Router,
    user_id: UUID,
    thread_id: UUID,
    content: str,
    queue: asyncio.Queue,
) -> None:
    """Run the router, push its final text + any tool/triage events
    through the queue, and signal end-of-stream with a None sentinel."""
    try:
        async def emit(event: str, data: str) -> None:
            await queue.put((event, data))

        ctx = ToolContext(user_id=user_id, channel=_web_channel.name)
        text = await router.handle(
            ctx=ctx, thread_id=thread_id, content=content, emit=emit,
        )
        await queue.put(("delta", text))
    except Exception as e:  # noqa: BLE001 — channel the error to the client
        log.exception("channel.web.agent_failed", user_id=str(user_id))
        await queue.put(("error", f"agent failed: {e}"))
    finally:
        await queue.put(None)


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    user_id: UUID = Depends(require_user_id),
) -> StreamingResponse:
    inbound = await _web_channel.receive(
        {
            "user_id": user_id,
            "content": payload.content,
            "thread_id": payload.thread_id,
        }
    )
    log.info(
        "channel.chat.received",
        channel=_web_channel.name,
        user_id=str(user_id),
        content_length=len(inbound.content),
    )

    async def stream() -> AsyncIterator[bytes]:
        # 1. Slash commands short-circuit before any agent invocation so
        # they don't burn model tokens or create a thread.
        cmd_result = await get_dispatcher().dispatch(inbound)
        if cmd_result is not None:
            if cmd_result.clear_thread:
                yield _sse_event("reset", "")
            yield _sse_event("command", cmd_result.text)
            yield _sse_event("done", "")
            return

        # 2. Resolve or create the thread.
        # If the client supplied a thread_id, honor it (validated against
        # the user's threads inside get_or_create_thread). If not — a
        # new device, fresh browser, or a tab that lost its in-memory
        # threadId — continue the user's most-recent thread (any channel)
        # so the conversation follows them across devices and channels.
        # `/reset`
        # mints a new thread server-side, which becomes the "most
        # recent" pickup point for the next message.
        async with acquire() as conn:
            if payload.thread_id is not None:
                thread_id = await conv.get_or_create_thread(
                    conn,
                    user_id=user_id,
                    channel=_web_channel.name,
                    thread_id=payload.thread_id,
                )
            else:
                existing = await conv.get_most_recent_thread(
                    conn,
                    user_id=user_id,
                )
                if existing is not None:
                    thread_id = existing
                else:
                    thread_id = await conv.get_or_create_thread(
                        conn,
                        user_id=user_id,
                        channel=_web_channel.name,
                    )
        yield _sse_event("thread", str(thread_id))

        # 3. Run the router in a background task so triage + tool event
        # emissions interleave with the agent's progress.
        queue: asyncio.Queue = asyncio.Queue()
        router = get_router()
        task = asyncio.create_task(
            _run_router_into_queue(
                router=router,
                user_id=user_id,
                thread_id=thread_id,
                content=inbound.content,
                queue=queue,
            )
        )
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                event, data = item
                yield _sse_event(event, data)
        finally:
            if not task.done():
                task.cancel()
        yield _sse_event("done", "")

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/messages", response_model=MessagesResponse)
async def messages(
    user_id: UUID = Depends(require_user_id),
    thread_id: UUID | None = None,
    before: datetime | None = None,
    before_id: UUID | None = None,
    limit: int = 30,
) -> MessagesResponse:
    """Paginated raw chat history for the web UI's infinite scroll.

    With no `thread_id`, resolves the user's most-recent thread (the same
    thread `/chat` continues), so a page refresh shows the ongoing
    conversation. `before` + `before_id` form a keyset cursor: pass the
    oldest message currently on screen to fetch the page just before it.
    Messages come back oldest-first; `has_more` says whether older
    messages remain to scroll back to.
    """
    limit = max(1, min(limit, 100))
    async with acquire() as conn:
        if thread_id is not None:
            owned = await conn.fetchrow(
                "SELECT id FROM threads WHERE id = $1 AND user_id = $2",
                thread_id, user_id,
            )
            if owned is None:
                raise HTTPException(404, "no such thread")
            resolved: UUID | None = thread_id
        else:
            resolved = await conv.get_most_recent_thread(conn, user_id=user_id)
        if resolved is None:
            return MessagesResponse(thread_id=None, messages=[], has_more=False)
        # Over-fetch by one to detect whether an older page exists.
        rows = await conv.fetch_page(
            conn,
            thread_id=resolved,
            before_created_at=before,
            before_id=before_id,
            limit=limit + 1,
            roles=("user", "assistant"),
        )
    has_more = len(rows) > limit
    if has_more:
        rows = rows[-limit:]  # drop the oldest extra; list is chronological
    return MessagesResponse(
        thread_id=resolved,
        messages=[
            ChatMessage(
                id=m.id,
                role=m.role,
                content=m.content,
                created_at=m.created_at,
            )
            for m in rows
        ],
        has_more=has_more,
    )


@router.post("/answer", status_code=204)
async def answer(
    payload: AnswerRequest,
    user_id: UUID = Depends(require_user_id),
) -> None:
    """Resolve a pending `ask_user` question. The web client POSTs here
    when the user types their answer to a question that was emitted as
    an `ask_user` SSE event during a task. Writes the answer to
    `pending_questions`, which wakes the waiting task (possibly in another
    process) via NOTIFY."""
    async with acquire() as conn:
        outcome = await pq_dao.mark_answered(
            conn,
            question_id=payload.question_id,
            user_id=user_id,
            answer=payload.answer,
        )
    if outcome is pq_dao.AnswerOutcome.UNKNOWN:
        raise HTTPException(404, "no such pending question")
    if outcome is pq_dao.AnswerOutcome.ALREADY:
        raise HTTPException(409, "question already answered")
