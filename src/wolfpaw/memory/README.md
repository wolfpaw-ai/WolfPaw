# memory/

Postgres access + the agent-facing memory subsystems. Step 10 added the verbatim recent window over `messages`; the rest (summaries, procedural, skills) lands in steps 12–12.5.

## Files

- **`db.py`** — process-wide asyncpg pool. `get_pool()` lazily creates it from `WOLFPAW_DATABASE_URL`; `acquire()` is the standard `async with acquire() as conn` context manager every DAO uses. Registers pgvector types on each connection so callers can pass/receive `numpy`-shaped vectors. Also provides `migrations_dir()` + `apply_sql_file(conn, path)` for tests and dev bootstrap.
- **`conversational.py`** — `threads` + `messages` access for the agent loop. `Message` dataclass + `MessageRole` / `ChannelName` literal types. `get_or_create_thread(conn, *, user_id, channel, thread_id=None)` validates ownership before reuse and silently mints a fresh thread if the supplied id belongs to another user (don't leak existence). `append(conn, *, thread_id, role, content, metadata=None)` writes one row. `fetch_recent(conn, *, thread_id, n=20)` returns the most recent messages in chronological order (oldest first). Persistence policy: agents store visible user turns + final assistant text only; intermediate tool calls/results live in-process.

## How it fits together

Every module that touches Postgres imports `acquire` from `db.py`. The pool is closed on FastAPI shutdown via the lifespan hook in `api.py`.

The Quick Agent (step 10) uses `conversational` like this on every turn:
```
async with acquire() as conn:
    past = await conv.fetch_recent(conn, thread_id=tid, n=20)
    await conv.append(conn, thread_id=tid, role="user", content=text)
# ... agent loop ...
async with acquire() as conn:
    await conv.append(conn, thread_id=tid, role="assistant", content=final_text)
```

Tests that need a real DB drop and re-apply migrations into a scratch schema, then close the pool between cases (see the `_fresh_db` fixture pattern in `test_auth_flow.py` / `test_memory_conversational.py`). Tests that *don't* want a DB monkeypatch `wolfpaw.memory.db.acquire` to a no-op context manager and patch the bound names in importing modules.

## Extending

- **Tiered summaries** (step 12.5) land here as `fetch_summaries(conn, *, thread_id)` and a writer driven by `workers/jobs/compact_thread.py`. The `thread_summaries` table is already provisioned in migration `001_init.sql`.
- **Per-thread vector recall** (step 12.5) lands as `search_relevant(conn, *, thread_id, query_embedding, k)` against `message_embeddings`. The Planner uses this; the Quick + Triage agents stay verbatim-only.
- **Procedural memory** (step 12) lands as `procedural.py`: query-embedding lookup into `plans` for the planner's "have we solved this before?" path.
- **Skills memory** (step 12) lands as `skills.py`: retrieval against the seeded starter set in v1; auto-emission is v2.

## Importing-module gotcha

`acquire` is typically imported with `from wolfpaw.memory.db import acquire`. That binds the name into the importing module's namespace, so monkeypatching `wolfpaw.memory.db.acquire` in tests doesn't hit the bound name. Patch the importing module too:

```python
monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
monkeypatch.setattr("wolfpaw.agents.quick.acquire", _fake_acquire)
monkeypatch.setattr("wolfpaw.metering.model_client.acquire", _fake_acquire)
```

Same applies to other DB helpers like `get_active_price` that some modules pull in by name.
