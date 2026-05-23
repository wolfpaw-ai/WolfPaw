"""Unit tests for the embedding subsystem — stub determinism + Voyage HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from wolfpaw.embeddings.stub import StubEmbedder
from wolfpaw.embeddings.voyage import VOYAGE_URL, VoyageEmbedder


# --- stub ------------------------------------------------------------------


async def test_stub_returns_unit_vector():
    e = StubEmbedder(dimensions=32)
    out = await e.embed(["hello"])
    assert len(out.vectors) == 1
    v = out.vectors[0]
    assert len(v) == 32
    norm = sum(x * x for x in v) ** 0.5
    assert abs(norm - 1.0) < 1e-6


async def test_stub_deterministic_same_text_same_vector():
    e = StubEmbedder(dimensions=16)
    a = (await e.embed(["query"])).vectors[0]
    b = (await e.embed(["query"])).vectors[0]
    assert a == b


async def test_stub_different_texts_different_vectors():
    e = StubEmbedder(dimensions=16)
    a = (await e.embed(["one"])).vectors[0]
    b = (await e.embed(["two"])).vectors[0]
    assert a != b


async def test_stub_handles_empty_input():
    e = StubEmbedder(dimensions=16)
    out = await e.embed([])
    assert out.vectors == []
    assert out.input_tokens == 0


# --- voyage ----------------------------------------------------------------


@respx.mock
async def test_voyage_round_trips_payload():
    respx.post(VOYAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"object": "embedding", "embedding": [0.1, 0.2, 0.3], "index": 0},
                    {"object": "embedding", "embedding": [0.4, 0.5, 0.6], "index": 1},
                ],
                "model": "voyage-3",
                "usage": {"total_tokens": 12},
            },
        )
    )
    e = VoyageEmbedder(api_key="test", model="voyage-3", dimensions=3)
    out = await e.embed(["a", "b"])
    assert out.vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert out.input_tokens == 12
    assert out.model == "voyage-3"


async def test_voyage_requires_api_key():
    e = VoyageEmbedder(api_key="", model="voyage-3", dimensions=1024)
    with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
        await e.embed(["x"])


async def test_voyage_empty_input_short_circuits():
    e = VoyageEmbedder(api_key="", model="voyage-3", dimensions=1024)
    out = await e.embed([])  # would error on no key, but empty input is free
    assert out.vectors == []
