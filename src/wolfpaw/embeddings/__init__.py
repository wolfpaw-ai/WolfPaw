"""Embedding subsystem — pluggable provider behind a uniform interface.

The Planner (step 12) is the first caller: embed the user's query, look
up similar past plans + matching skills via cosine distance in pgvector.
Future callers (search_relevant in step 12.5, message embedding on
append) plug in the same way.

Provider selection: `WOLFPAW_EMBEDDING_BACKEND` = `voyage` (default) or
`stub` (deterministic fake — useful in tests and CI without an API key).
Both produce `voyage_dimensions`-wide vectors so the DB IVFFlat indexes
fit regardless of backend.
"""

from __future__ import annotations

from wolfpaw.config import get_settings
from wolfpaw.embeddings.base import EmbeddingClient, EmbeddingResult

_embedder: EmbeddingClient | None = None


def get_embedder() -> EmbeddingClient:
    global _embedder
    if _embedder is not None:
        return _embedder
    settings = get_settings()
    backend = settings.embedding_backend.lower()
    if backend == "voyage":
        from wolfpaw.embeddings.voyage import VoyageEmbedder

        _embedder = VoyageEmbedder(
            api_key=settings.voyage_api_key,
            model=settings.voyage_model,
            dimensions=settings.voyage_dimensions,
        )
    elif backend == "stub":
        from wolfpaw.embeddings.stub import StubEmbedder

        _embedder = StubEmbedder(dimensions=settings.voyage_dimensions)
    else:
        raise ValueError(
            f"Unknown WOLFPAW_EMBEDDING_BACKEND={backend!r};"
            " expected 'voyage' or 'stub'"
        )
    return _embedder


def reset_embedder() -> None:
    """Test/dev hook to drop the cached embedder."""
    global _embedder
    _embedder = None


__all__ = [
    "EmbeddingClient",
    "EmbeddingResult",
    "get_embedder",
    "reset_embedder",
]
