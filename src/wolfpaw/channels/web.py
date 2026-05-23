"""Web channel: a single `/channels/web/chat` SSE endpoint.

Authenticated POST → server-sent events. Slash commands route through the
shared dispatcher and stream back a `command` event. Plain messages fall
through to the agent pipeline — which doesn't exist yet in step 5, so we
emit a placeholder `delta` until the Quick Agent lands in step 10.

Stream shape:
    event: command | delta | done | error
    data: <text>             # one `data:` line per newline in the text

`WebChannel.send()` is intentionally unimplemented — proactive web push
needs websockets and isn't on the v1 roadmap.
"""

from __future__ import annotations

from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from wolfpaw.auth.deps import require_user_id
from wolfpaw.channels import Channel, InboundMessage
from wolfpaw.channels.commands import get_dispatcher
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
        result = await get_dispatcher().dispatch(inbound)
        if result is not None:
            yield _sse_event("command", result.text)
        else:
            yield _sse_event(
                "delta",
                "Agent pipeline is not online yet (lands in step 10).",
            )
        yield _sse_event("done", "")

    return StreamingResponse(stream(), media_type="text/event-stream")
