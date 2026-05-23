"""StubEmbedder — deterministic fake vectors keyed on text content.

Lets dev + the test suite exercise the procedural / skills retrieval
paths end-to-end without a Voyage API key. Vectors are derived from a
SHA-256 of the input so the same text always embeds the same way, but
two different texts get different vectors.

NOT suitable for actually doing semantic search — the "similarity"
between two stub vectors is essentially random. Tests that depend on
real cosine-distance ordering should pre-seed specific vectors.
"""

from __future__ import annotations

import hashlib
import struct

from wolfpaw.embeddings.base import EmbeddingClient, EmbeddingResult


class StubEmbedder(EmbeddingClient):
    name = "stub"

    def __init__(self, *, dimensions: int = 1024) -> None:
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        vectors = [self._vec(t) for t in texts]
        tokens = sum(max(1, len(t) // 4) for t in texts)  # rough char-quarter heuristic
        return EmbeddingResult(
            vectors=vectors, input_tokens=tokens, model=self.name,
        )

    def _vec(self, text: str) -> list[float]:
        # Expand SHA-256 into the requested dimension by hashing
        # (counter || text) repeatedly. Deterministic + cheap.
        out: list[float] = []
        counter = 0
        while len(out) < self.dimensions:
            digest = hashlib.sha256(
                f"{counter}:{text}".encode("utf-8")
            ).digest()
            # 8 floats per digest (8 × 4-byte floats from 32 bytes).
            for i in range(0, 32, 4):
                (val,) = struct.unpack("<I", digest[i : i + 4])
                # Map uint32 → [-1, 1).
                out.append((val / 2_147_483_648.0) - 1.0)
                if len(out) >= self.dimensions:
                    break
            counter += 1
        # L2-normalize so cosine distance behaves like with real embeddings.
        norm = sum(v * v for v in out) ** 0.5 or 1.0
        return [v / norm for v in out]
