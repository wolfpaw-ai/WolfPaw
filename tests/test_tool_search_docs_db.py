"""DB-backed tests for `search_docs` + the workspace embedding job.

Exercises the full path: write_doc → enqueue_embed_workspace_file →
embed_and_store_doc (inline, since WORKERS off) → search_docs returns
the embedded file via pgvector.

The stub embedder's vectors are deterministic per text but otherwise
random-directional. We still get a meaningful round-trip test: search
with the *same* text as one of the docs and assert that doc appears in
the top hits.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir
from wolfpaw.toolbox.registry import ToolContext, get_registry

pytestmark = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch, tmp_path):
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    monkeypatch.setenv("WOLFPAW_DATABASE_URL", dsn)
    monkeypatch.setenv("WOLFPAW_LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("WOLFPAW_WEB_BASE_URL", "http://testserver")
    monkeypatch.setenv("WOLFPAW_EMBEDDING_BACKEND", "stub")
    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "false")
    from wolfpaw.config import get_settings
    from wolfpaw.embeddings import reset_embedder
    from wolfpaw.storage import reset_storage

    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_embedder()
    reset_storage()
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for f in ("001_init.sql", "002_auth.sql", "015_workspace_embeddings.sql"):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()
    reset_embedder()
    reset_storage()


async def _seed_user() -> UUID:
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"docs+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


async def test_write_doc_then_search_docs_round_trip():
    """The stub embedder is deterministic on text → vector. Writing
    docA and then searching with the same text as docA should put docA
    at the top of the result list."""
    uid = await _seed_user()
    ctx = ToolContext(user_id=uid)
    write = get_registry().get("write_doc")
    search = get_registry().get("search_docs")

    await write.run(ctx, filename="alpha.md", content="meatloaf recipe — onion, ground beef, ketchup")
    await write.run(ctx, filename="beta.md", content="quarterly planning notes for the Acme account")
    await write.run(ctx, filename="gamma.md", content="grocery list: eggs, milk, bread")

    out = await search.run(
        ctx, query="meatloaf recipe — onion, ground beef, ketchup", k=3,
    )
    assert out["result_count"] >= 1
    assert out["results"][0]["filename"] == "alpha.md"
    # Same-text similarity is 1.0 for stub (and real) embedders.
    assert out["results"][0]["similarity"] == pytest.approx(1.0, abs=1e-3)


async def test_search_docs_returns_only_embedded_docs():
    """A row with no embedding column populated must be skipped (not
    raise). Insert a workspace_files row directly without an embedding
    and confirm search_docs excludes it."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user()
    ctx = ToolContext(user_id=uid)
    search = get_registry().get("search_docs")

    # Raw insert — no embedding, no storage put. Just a catalog row.
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            "INSERT INTO workspace_files"
            " (user_id, source, filename, storage_url, size_bytes)"
            " VALUES ($1, 'user_upload'::file_source, $2, $3, $4)",
            uid, "orphan.md", "file://nowhere", 10,
        )
    finally:
        await conn.close()

    out = await search.run(ctx, query="anything", k=5)
    assert all(r["filename"] != "orphan.md" for r in out["results"])


async def test_search_docs_isolated_per_user():
    a = await _seed_user()
    b = await _seed_user()
    write = get_registry().get("write_doc")
    search = get_registry().get("search_docs")

    await write.run(
        ToolContext(user_id=a),
        filename="alice_secret.md",
        content="only alice's content",
    )
    bs_view = await search.run(
        ToolContext(user_id=b), query="only alice's content", k=5,
    )
    assert all(
        r["filename"] != "alice_secret.md" for r in bs_view["results"]
    )


async def test_search_docs_returns_latest_version_only():
    """When alpha.md is overwritten, the new version's embedding wins —
    older versions stay in `workspace_files` but should NOT appear as
    separate hits in search results."""
    uid = await _seed_user()
    ctx = ToolContext(user_id=uid)
    write = get_registry().get("write_doc")
    search = get_registry().get("search_docs")

    await write.run(ctx, filename="alpha.md", content="version one text")
    await write.run(
        ctx, filename="alpha.md", content="version two text", overwrite=True,
    )

    out = await search.run(ctx, query="version two text", k=5)
    versions_for_alpha = [
        r["version"] for r in out["results"] if r["filename"] == "alpha.md"
    ]
    assert versions_for_alpha == [2]
