# memory/

Postgres access + the agent-facing memory subsystems. Step 10 added the verbatim recent window over `messages`; step 12 added procedural + skills retrieval via pgvector. Tiered summaries + per-thread vector recall land in step 12.5.

## Files

- **`db.py`** — process-wide asyncpg pool. `get_pool()` lazily creates it from `WOLFPAW_DATABASE_URL`; `acquire()` is the standard `async with acquire() as conn` context manager every DAO uses. Registers pgvector types on each connection so callers can pass/receive `numpy`-shaped vectors. Also provides `migrations_dir()` + `apply_sql_file(conn, path)` for tests and dev bootstrap.
- **`conversational.py`** — `threads` + `messages` access for the agent loop. `Message` dataclass + `MessageRole` / `ChannelName` literal types. `get_or_create_thread(conn, *, user_id, channel, thread_id=None)` validates ownership before reuse and silently mints a fresh thread if the supplied id belongs to another user (don't leak existence). New threads are stamped with the active `soul_version` (12-char SHA-256 prefix of `soul.md`) + the user's `user_profile.version` so plan/procedural-memory retrieval can later filter to "same persona snapshot" (step 17). `append(conn, *, thread_id, role, content, metadata=None)` writes one row. `fetch_recent(conn, *, thread_id, n=20)` returns the most recent messages in chronological order (oldest first). Persistence policy: agents store visible user turns + final assistant text only; intermediate tool calls/results live in-process.
- **`procedural.py`** — the "recipe-box" over `plans`. `StoredPlan` dataclass. `search_similar(conn, *, user_id, query_embedding, k=5, min_score=None)` does cosine pgvector lookup; rows without embeddings are skipped. `store(...)` inserts a generated plan (success/score=None initially). `update_outcome(...)` is partial-write — pass only the columns you want to set, untouched fields aren't overwritten. The Executor (step 13) writes `final_answer` + `success` + `error`; the Post-Evaluator (step 14) follows up with `score`. Scoped per `user_id`.
- **`skills.py`** — generalized reusable procedures over `skills`. `Skill` dataclass; `search_by_task(conn, *, user_id, query_embedding, k=5)` returns user-owned + seeded (`user_id IS NULL`) matches ordered by cosine distance. `STARTER_SKILLS` is the v1 hand-written exemplar set (vendor comparison, research one-pager, newsletter digest, receipt-to-ledger, inventory snapshot). `seed_starter_skills(conn, embedder)` is idempotent — call once at boot (or via a one-off task) to insert + embed the seed set; subsequent calls are no-ops.
- **`task_events.py`** — append-only DAO over `task_events`. `append_event(conn, *, task_id, event_type, content)` writes one row; `fetch_for_task(conn, *, task_id, limit)` reads chronologically (oldest first). Migration `005_post_evaluator.sql` made `task_id` nullable so the Post-Evaluator can emit `plan_scored` events for plans that aren't wrapped in a Task; the originating plan id is carried in `content`.
- **`tasks.py`** — DAO for `tasks` with the full state machine: `create` (accepts `parent_task_id` + `budget_cents` for subagent tasks), `get_by_id` (user-scoped), `list_for_user` (newest first, optional status filter), `mark_started` / `mark_awaiting_user` / `mark_blocked` / `mark_completed` / `mark_failed` / `cancel`, `attach_plan`, `get_depth` (walks `parent_task_id` chain — step 16's subagent depth cap relies on this), `get_root` (walks to the top of the chain — the executor scopes its per-root subagent concurrency semaphore on this), `rollup_spent_cents` (sums `token_usage.cost_cents + compute_usage.cost_cents` for the task and writes back to `tasks.spent_cents` — called by TaskService on terminal transitions + by the cancel paths). Terminal status (completed/failed/cancelled) is sticky — subsequent transition attempts no-op rather than corrupting state. `cancel` and `get_by_id` are user-scoped so one user can't kill or inspect another's task.
- **`channel_links.py`** — DAO for `channel_links` (per-user mapping from Wolfpaw user_id to a channel-side identity, e.g. Telegram user id). `create`, `find_user(channel, external_id)` (the webhook's hot path), `list_for_user`, `delete`. Unique on `(channel, external_id)` so the same Telegram account can't link to two Wolfpaw users.

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

- **Tiered summaries** (step 12.5) land here as `fetch_summaries(conn, *, thread_id)` and a writer driven by `workers/jobs/compact_thread.py`. The `thread_summaries` table is already provisioned in `001_init.sql`.
- **Per-thread vector recall** (step 12.5) lands as `search_relevant(conn, *, thread_id, query_embedding, k)` against `message_embeddings`. The Planner already passes the query embedding through; this just adds the call. The Quick + Triage agents stay verbatim-only.
- **Skills auto-emission** (v2): the Post-Evaluator emits new rows into `skills` keyed on the originating plan when a plan scores highly and looks reusable.
- **New seeded skill**: append a dict to `STARTER_SKILLS` in `skills.py` with a `name` (unique), `description` (this is what gets embedded), `ingredients` (which tools it uses), and a `steps` skeleton. `seed_starter_skills` is name-keyed so it'll add the new one without re-inserting the existing seeds.

## Importing-module gotcha

`acquire` is typically imported with `from wolfpaw.memory.db import acquire`. That binds the name into the importing module's namespace, so monkeypatching `wolfpaw.memory.db.acquire` in tests doesn't hit the bound name. Patch the importing module too:

```python
monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
monkeypatch.setattr("wolfpaw.agents.quick.acquire", _fake_acquire)
monkeypatch.setattr("wolfpaw.metering.model_client.acquire", _fake_acquire)
```

Same applies to other DB helpers like `get_active_price` that some modules pull in by name.
