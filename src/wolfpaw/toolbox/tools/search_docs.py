"""`search_docs` — semantic search over the user's workspace docs.

Embeds the query, then runs an ANN search against `workspace_files`
(latest version per filename only). Returns filenames + similarity
scores; the agent calls `read_doc` to actually fetch content for the
hits it wants to use.

Docs that haven't been embedded yet (workers still queued, or
pre-existing rows before this column landed) are simply absent from
results — never an error, just a quieter recall.
"""

from __future__ import annotations

from typing import Any

from wolfpaw.embeddings import get_embedder
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)
from wolfpaw.workspace import files as files_dao

_DEFAULT_K = 5
_MAX_K = 25


@register_tool
class SearchDocsTool(Tool):
    name = "search_docs"
    description = (
        "Find workspace docs by *content* (semantic search). Returns"
        " filenames + similarity scores ordered by relevance — call"
        " `read_doc` afterward on the hits you want to use. Prefer this"
        " over reading every doc when you're looking for information"
        " whose filename you don't already know."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What you're looking for — a phrase, question, or"
                    " topic. Will be embedded and compared against"
                    " every doc's embedding."
                ),
            },
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_K,
                "default": _DEFAULT_K,
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
            hits = await files_dao.search_latest_by_embedding(
                conn, user_id=ctx.user_id,
                query_embedding=query_vec, k=k,
            )

        return {
            "query": query,
            "result_count": len(hits),
            "results": [
                {
                    "filename": f.filename,
                    "version": f.version,
                    "size_bytes": f.size_bytes,
                    "similarity": round(score, 4),
                }
                for f, score in hits
            ],
        }
