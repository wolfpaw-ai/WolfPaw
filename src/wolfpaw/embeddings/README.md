# embeddings/

Pluggable embedding subsystem. v1 ships two providers behind one
interface so dev / tests can run without a Voyage API key while
production embeds through Voyage's `voyage-3` model.

## Files

- **`base.py`** — `EmbeddingClient` ABC + `EmbeddingResult` dataclass (`vectors`, `input_tokens`, `model`). Implementations expose `embed(texts) -> EmbeddingResult` and the convenience `embed_one(text)`.
- **`voyage.py`** — `VoyageEmbedder`: POSTs to `https://api.voyageai.com/v1/embeddings` via httpx (already a base dep). Reads the API key passed to its constructor; raises a clear error if missing rather than crashing in the middle of a planner call. Returns Voyage's reported `total_tokens` for cost recording.
- **`stub.py`** — `StubEmbedder`: deterministic SHA-256-derived L2-normalized vectors. Same input always embeds the same way; different inputs get different vectors. NOT semantically meaningful (similarity between two stub vectors is essentially random) — use it for wiring / round-trip tests, not for retrieval-quality experiments.
- **`__init__.py`** — `get_embedder()` factory honoring `WOLFPAW_EMBEDDING_BACKEND` (`voyage` | `stub`). Cached; `reset_embedder()` is the test/dev hook.

## How it fits together

The Planner (step 12) is the first caller: embed the user's query → use the vector for `procedural.search_similar` and `skills.search_by_task`. Future callers:
- `seed_starter_skills` (step 12) — embeds the seed skill descriptions on first boot.
- `message_embeddings` writes on append (step 12.5) — every incoming message gets embedded so the Planner can do per-thread vector recall.

Cost metering: the embedder returns `input_tokens`; the caller routes that through `record_usage` with `model=<embedder.name>` so embeddings show up in `/usage` as their own line (the seeded `model_prices` row already prices `voyage-3` at $0.06/Mtok).

## Extending

- **New provider** (OpenAI / Cohere / local): subclass `EmbeddingClient`, implement `embed(texts)`, add a branch in the `get_embedder()` factory keyed on a new `WOLFPAW_EMBEDDING_BACKEND` value. Keep the dimension the same (1024) so the DB IVFFlat indexes don't need to change; if you must change dimension, add a migration that drops + recreates the `vector(N)` columns and the IVFFlat indexes on `plans.query_embedding`, `skills.embedding`, `tools.embedding`, `message_embeddings.embedding`.
- **Cost recording at provider level**: today the caller does the `record_usage` write. If a new provider has cost data shaped differently (per-call instead of per-token), route through a small helper here rather than the per-caller pattern.
