"""`recall_memory` — deliberate, deep recall of older conversation.

The automatic recall the Planner does on every turn (recency-weighted,
small k) is tuned for "what's relevant right now". This tool is the
opposite: an *explicit, age-blind* search of the user's whole past
conversation, for when they reach back — "remember when we were talking
about ...". Higher k, recency turned off (they're asking about the old
stuff, so age shouldn't penalize it), returned with neighbor windows for
coherence.

Re-anchoring is automatic and needs no special machinery: the tool's
contract is that the agent restates what it found in its reply. That
reply is persisted as a normal assistant turn, so the recalled material
re-enters the recent window and gets a fresh-timestamped embedding —
naturally staying in working memory until it organically compacts and
fades again. Saying it out loud is what makes it stick.
"""

from __future__ import annotations

from typing import Any

from wolfpaw.embeddings import get_embedder
from wolfpaw.memory import conversational as conv
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)

_DEFAULT_K = 25
_MAX_K = 60
# Loose relevance floor (cosine distance, 0=identical … 2=opposite) so a
# deep recall doesn't surface unrelated noise; still permissive since the
# agent restates only the genuinely-relevant hits.
_MAX_DISTANCE = 0.75


@register_tool
class RecallMemoryTool(Tool):
    name = "recall_memory"
    description = (
        "Deliberately search the user's *entire* past conversation for older"
        " messages about a topic — use when they reach back, e.g. \"remember"
        " when we talked about ...\". Deeper and age-blind vs. the automatic"
        " recall. Returns matching past messages with surrounding context;"
        " you MUST restate what you find in your reply so it re-enters the"
        " conversation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The topic or thing to recall — a phrase, question, or"
                    " description of what was discussed."
                ),
            },
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_K,
                "default": _DEFAULT_K,
                "description": "Max number of older messages to surface.",
            },
        },
        "required": ["query"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        query = inputs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("`query` must be a non-empty string")
        k = max(1, min(_MAX_K, int(inputs.get("k") or _DEFAULT_K)))

        embedder = get_embedder()
        result = await embedder.embed_one(query)
        if not result.vectors:
            return {"query": query, "result_count": 0, "results": []}
        query_vec = result.vectors[0]

        async with acquire() as conn:
            msgs = await conv.search_user_messages(
                conn,
                user_id=ctx.user_id,
                query_embedding=query_vec,
                k=k,
                recency_weight=0.0,  # deliberate recall — don't penalize age
                window=2,
                max_distance=_MAX_DISTANCE,
            )

        return {
            "query": query,
            "result_count": len(msgs),
            "results": [
                {
                    "role": m.role,
                    "timestamp": m.created_at.isoformat(),
                    "content": m.content,
                }
                for m in msgs
            ],
        }
