# memory/

Postgres access and (eventually) the agent-facing memory subsystems. Today it's just the asyncpg pool; the conversational / procedural / skills memories land in steps 11–12.5.

## Files

- **`db.py`** — process-wide asyncpg pool. `get_pool()` lazily creates it from `WOLFPAW_DATABASE_URL`; `acquire()` is the standard `async with acquire() as conn` context manager every DAO uses. Registers pgvector types on each connection so callers can pass/receive `numpy`-shaped vectors. Also provides `migrations_dir()` + `apply_sql_file(conn, path)` for tests and dev bootstrap.

## How it fits together

Every module that touches Postgres imports `acquire` from here. The pool is closed on FastAPI shutdown via the lifespan hook in `api.py`. Tests that need a real DB drop and re-apply the migrations into a scratch schema, then close the pool between cases (see the `_fresh_db` fixture pattern in `test_auth_flow.py` and `test_metering_integration.py`).

## Extending

- **Per-thread conversational memory** (step 11+) lands as `conversational.py`: `fetch_recent(thread_id, n)`, `fetch_summaries(thread_id)`, `search_relevant(thread_id, query_embedding, k)`, `append(message)`.
- **Procedural memory** (step 12) lands as `procedural.py`: query-embedding lookup into `plans` for the planner's "have we solved this before?" path.
- **Skills memory** (step 12) lands as `skills.py`: retrieval against the seeded starter set in v1; auto-emission is v2.
