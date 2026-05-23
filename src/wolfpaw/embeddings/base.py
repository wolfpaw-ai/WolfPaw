"""Embedding interface — any provider implements `embed(texts)` and gets
a vector per text back, plus a token-usage tally for cost recording.

The caller is responsible for routing the cost to `token_usage` via the
existing recorder; the embedder just reports tokens consumed (Voyage's
API returns this; the stub fakes it). Keeping cost recording out of the
embedder means tests don't need to fake a recorder."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    input_tokens: int
    model: str


class EmbeddingClient(ABC):
    name: str
    dimensions: int

    @abstractmethod
    async def embed(self, texts: list[str]) -> EmbeddingResult: ...

    async def embed_one(self, text: str) -> EmbeddingResult:
        """Convenience for the common single-string case."""
        return await self.embed([text])
