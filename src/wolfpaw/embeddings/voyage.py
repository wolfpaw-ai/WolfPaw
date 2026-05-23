"""VoyageEmbedder — POST to Voyage AI's `/v1/embeddings` endpoint.

Voyage's response format:
    {
      "object": "list",
      "data": [{"object": "embedding", "embedding": [...], "index": 0}, ...],
      "model": "voyage-3",
      "usage": {"total_tokens": N}
    }

We use httpx (already a base dep) rather than the official Voyage SDK
to keep the dependency footprint small — it's a single HTTP call.
"""

from __future__ import annotations

import httpx

from wolfpaw.embeddings.base import EmbeddingClient, EmbeddingResult

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


class VoyageEmbedder(EmbeddingClient):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "voyage-3",
        dimensions: int = 1024,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self.name = model
        self.dimensions = dimensions
        self._timeout = timeout_seconds

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], input_tokens=0, model=self.name)
        if not self._api_key:
            raise RuntimeError(
                "VoyageEmbedder requires WOLFPAW_VOYAGE_API_KEY (or"
                " set WOLFPAW_EMBEDDING_BACKEND=stub for dev / tests)."
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                VOYAGE_URL,
                headers={
                    "authorization": f"Bearer {self._api_key}",
                    "content-type": "application/json",
                },
                json={
                    "input": texts,
                    "model": self.name,
                    "input_type": "query",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        vectors = [item["embedding"] for item in data.get("data", [])]
        tokens = int((data.get("usage") or {}).get("total_tokens", 0))
        return EmbeddingResult(
            vectors=vectors,
            input_tokens=tokens,
            model=data.get("model", self.name),
        )
