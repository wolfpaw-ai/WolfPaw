"""Web channel: a single `/channels/web/chat` SSE endpoint.

Authenticated POST → server-sent events. Slash commands route through the
shared dispatcher and stream back a `command` event. Plain messages are
handed to the Quick Agent (step 10); the agent runs its tool loop and
returns final text, which streams back as a `delta` event.

Stream shape:
    event: thread  | command | tool | delta | done | error
    data: <text>             # one `data:` line per newline in the text

`WebChannel.send()` is intentionally unimplemented — proactive web push
needs websockets and isn't on the v1 roadmap.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from wolfpaw.agents.quick import QuickAgent, get_quick_agent
from wolfpaw.auth.deps import require_user_id
from wolfpaw.channels import Channel, InboundMessage
from wolfpaw.channels.commands import get_dispatcher
from wolfpaw.memory import conversational as conv
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()
router = APIRouter(prefix="/channels/web", tags=["channels"])


class ChatRequest(BaseModel):
    content: str
    thread_id: UUID | None = None


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


_web_channel = WebChannel()


def _sse_event(event: str, data: str) -> bytes:
    """Render a single SSE event. Each newline in `data` becomes its own
    `data:` line, per the EventSource spec."""
    lines = data.split("\n") if data else [""]
    payload = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event}\n{payload}\n\n".encode("utf-8")


async def _run_agent_into_queue(
    *,
    agent: QuickAgent,
    user_id: UUID,
    thread_id: UUID,
    content: str,
    queue: asyncio.Queue,
) -> None:
    """Run the agent, push its final text + any tool events through the
    queue, and signal end-of-stream with a None sentinel."""
    try:
        async def emit(event: str, data: str) -> None:
            await queue.put((event, data))

        ctx = ToolContext(user_id=user_id)
        text = await agent.handle(
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
            yield _sse_event("command", cmd_result.text)
            yield _sse_event("done", "")
            return

        # 2. Resolve or create the thread. Emit the id so the client can
        # reuse it on the next turn.
        async with acquire() as conn:
            thread_id = await conv.get_or_create_thread(
                conn,
                user_id=user_id,
                channel=_web_channel.name,
                thread_id=payload.thread_id,
            )
        yield _sse_event("thread", str(thread_id))

        # 3. Run the agent in a background task so tool-event emissions
        # interleave with the agent's progress.
        queue: asyncio.Queue = asyncio.Queue()
        agent = get_quick_agent()
        task = asyncio.create_task(
            _run_agent_into_queue(
                agent=agent,
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
