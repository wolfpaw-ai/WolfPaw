"""Procedural memory — pgvector lookup over `plans` + plan persistence.

The "recipe-box" the Planner consults before designing a new plan: "have
I solved something like this before?" Each row carries the user's query,
its embedding, the executed steps, and (once the Post-Evaluator scores
it in step 14) a success flag + score.

Retrieval ranks by cosine distance on `query_embedding`. Rows without
embeddings (failed embed call, legacy data) are skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class StoredPlan:
    id: UUID
    user_id: UUID
    thread_id: UUID | None
    task_id: UUID | None
    query: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str | None = None
    success: bool | None = None
    score: int | None = None
    error: str | None = None
    trace_id: str | None = None
    created_at: datetime | None = None
    similarity: float | None = None  # set by search_similar; 1.0 = identical


def _row_to_stored(row: asyncpg.Record, similarity: float | None = None) -> StoredPlan:
    steps_raw = row["steps"]
    if isinstance(steps_raw, str):
        steps_raw = json.loads(steps_raw)
    return StoredPlan(
        id=row["id"],
        user_id=row["user_id"],
        thread_id=row["thread_id"],
        task_id=row["task_id"],
        query=row["query"],
        steps=list(steps_raw or []),
        final_answer=row["final_answer"],
        success=row["success"],
        score=row["score"],
        error=row["error"],
        trace_id=row["trace_id"],
        created_at=row["created_at"],
        similarity=similarity,
    )


async def search_similar(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    query_embedding: list[float],
    k: int = 5,
    min_score: int | None = None,
) -> list[StoredPlan]:
    """Top-k past plans for this user, ranked by cosine similarity to the
    query embedding. `min_score` filters out poorly-scored attempts (use
    e.g. 60 once the Post-Evaluator is online; leave as None during early
    development when no plans have scores yet)."""
    score_clause = ""
    args: list[Any] = [user_id, query_embedding, k]
    if min_score is not None:
        score_clause = " AND (score IS NULL OR score >= $4)"
        args = [user_id, query_embedding, k, min_score]
    rows = await conn.fetch(
        f"""
        SELECT id, user_id, thread_id, task_id, query, steps, final_answer,
               success, score, error, trace_id, created_at,
               1 - (query_embedding <=> $2) AS similarity
          FROM plans
         WHERE user_id = $1
           AND query_embedding IS NOT NULL
           {score_clause}
         ORDER BY query_embedding <=> $2
         LIMIT $3
        """,
        *args,
    )
    return [_row_to_stored(r, similarity=float(r["similarity"])) for r in rows]


async def store(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    thread_id: UUID | None,
    query: str,
    query_embedding: list[float] | None,
    steps: list[dict[str, Any]],
    task_id: UUID | None = None,
    final_answer: str | None = None,
    success: bool | None = None,
    score: int | None = None,
    error: str | None = None,
    trace_id: str | None = None,
) -> UUID:
    """Insert one plan row. The Planner calls this right after generation
    (with success/score=None); the Post-Evaluator updates the same row
    after execution scoring (step 14)."""
    return await conn.fetchval(
        """
        INSERT INTO plans
            (user_id, thread_id, task_id, query, query_embedding, steps,
             final_answer, success, score, error, trace_id)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        user_id, thread_id, task_id, query, query_embedding,
        json.dumps(steps), final_answer, success, score, error, trace_id,
    )


async def update_outcome(
    conn: asyncpg.Connection,
    *,
    plan_id: UUID,
    final_answer: str | None = None,
    success: bool | None = None,
    score: int | None = None,
    error: str | None = None,
) -> None:
    """Two callers update outcomes on a plan row:

    - The Executor (step 13) writes `final_answer` + `success` + `error`
      as soon as the plan finishes. `score` stays NULL.
    - The Post-Evaluator (step 14) follows up with `score` once it grades
      the outcome.

    Only non-None fields get written, so calling this twice (executor
    then evaluator) doesn't clobber the other's columns.
    """
    sets: list[str] = []
    params: list[Any] = [plan_id]
    if final_answer is not None:
        sets.append(f"final_answer = ${len(params) + 1}")
        params.append(final_answer)
    if success is not None:
        sets.append(f"success = ${len(params) + 1}")
        params.append(success)
    if score is not None:
        sets.append(f"score = ${len(params) + 1}")
        params.append(score)
    if error is not None:
        sets.append(f"error = ${len(params) + 1}")
        params.append(error)
    if not sets:
        return
    await conn.execute(
        f"UPDATE plans SET {', '.join(sets)} WHERE id = $1",
        *params,
    )
