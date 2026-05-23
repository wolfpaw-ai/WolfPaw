"""Skills memory — generalized reusable procedures the Planner can adapt.

A Skill is a description + ingredients + steps. Unlike Plans (which are
specific past attempts), Skills are deliberately general — they encode
*how to approach a class of task* rather than what was done one time.

v1 ships:
  - Retrieval (`search_by_task`) — vector lookup over `skills.embedding`,
    scoped to `user_id = $current OR user_id IS NULL` so the user's own
    skills (none yet) and the seeded starter set both surface.
  - A hand-written **seeded starter set** loaded once at boot via
    `seed_starter_skills(conn, embedder)`.

v2 (deferred):
  - Auto-emission by the Post-Evaluator when a plan scores highly + looks
    reusable.
  - Per-user write-back from the agent ("save this approach as a skill").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from wolfpaw.embeddings.base import EmbeddingClient


@dataclass(frozen=True)
class Skill:
    id: UUID
    user_id: UUID | None         # NULL = seeded / shared
    name: str
    description: str
    ingredients: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    source_plan_id: UUID | None = None
    score: int | None = None
    created_at: datetime | None = None
    similarity: float | None = None  # set by search_by_task


def _row_to_skill(row: asyncpg.Record, similarity: float | None = None) -> Skill:
    ingredients = row["ingredients"]
    if isinstance(ingredients, str):
        ingredients = json.loads(ingredients)
    steps = row["steps"]
    if isinstance(steps, str):
        steps = json.loads(steps)
    return Skill(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        description=row["description"],
        ingredients=dict(ingredients or {}),
        steps=list(steps or []),
        source_plan_id=row["source_plan_id"],
        score=row["score"],
        created_at=row["created_at"],
        similarity=similarity,
    )


async def search_by_task(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    query_embedding: list[float],
    k: int = 5,
) -> list[Skill]:
    """Top-k skills for this user OR seeded (user_id IS NULL), ranked by
    cosine similarity. Rows without embeddings (haven't been embedded yet)
    are excluded."""
    rows = await conn.fetch(
        """
        SELECT id, user_id, name, description, ingredients, steps,
               source_plan_id, score, created_at,
               1 - (embedding <=> $2) AS similarity
          FROM skills
         WHERE (user_id = $1 OR user_id IS NULL)
           AND embedding IS NOT NULL
         ORDER BY embedding <=> $2
         LIMIT $3
        """,
        user_id, query_embedding, k,
    )
    return [_row_to_skill(r, similarity=float(r["similarity"])) for r in rows]


# --- seeding ---------------------------------------------------------------


# Hand-written exemplars from implementation_plan.md §"Seed skills".
# Each has a name, a description (the embedding is computed from this),
# and a rough steps skeleton the Planner can adapt.
STARTER_SKILLS: list[dict[str, Any]] = [
    {
        "name": "vendor_comparison_spreadsheet",
        "description": (
            "Research N vendors against user-supplied criteria, normalize"
            " attributes, emit a .xlsx with one row per vendor + columns"
            " per criterion + a recommendation."
        ),
        "ingredients": {
            "tools": ["web_search", "http_get", "create_spreadsheet"],
        },
        "steps": [
            {"id": "discover",     "kind": "functional", "tool": "web_search",
             "description": "Find each vendor's homepage + product page."},
            {"id": "fetch",        "kind": "functional", "tool": "http_get",
             "description": "Fetch the relevant pages per vendor.",
             "parallel_group": 1},
            {"id": "normalize",    "kind": "reasoning",
             "description": "Extract the criterion values from the fetched text."},
            {"id": "spreadsheet",  "kind": "functional", "tool": "create_spreadsheet",
             "description": "Emit the comparison + a recommendation row."},
        ],
    },
    {
        "name": "research_one_pager",
        "description": (
            "Read a paper or report (URL or upload), produce a single-page"
            " .pdf with key findings, methodology, and limitations."
        ),
        "ingredients": {
            "tools": ["read_doc", "http_get", "create_pdf"],
        },
        "steps": [
            {"id": "load",     "kind": "functional", "tool": "http_get",
             "description": "Fetch / load the source document."},
            {"id": "extract", "kind": "reasoning",
             "description": "Pull out findings, methodology, limitations."},
            {"id": "render",  "kind": "functional", "tool": "create_pdf",
             "description": "Render the one-pager as HTML → PDF."},
        ],
    },
    {
        "name": "newsletter_digest",
        "description": (
            "Ingest forwarded newsletters over a date range, group by"
            " theme, produce a Markdown digest with section headings + a"
            " links appendix."
        ),
        "ingredients": {
            "tools": ["sql_query", "write_doc"],
        },
        "steps": [
            {"id": "load",   "kind": "functional", "tool": "sql_query",
             "description": "SELECT the newsletter rows in the date range."},
            {"id": "group",  "kind": "reasoning",
             "description": "Cluster items by theme."},
            {"id": "write",  "kind": "functional", "tool": "write_doc",
             "description": "Emit Markdown digest with sections."},
        ],
    },
    {
        "name": "receipt_to_ledger",
        "description": (
            "Extract date, vendor, amount, and tax from a receipt"
            " (image or PDF text), append a row to a sandboxed SQL"
            " table the user maintains for expenses."
        ),
        "ingredients": {
            "tools": ["read_doc", "sql_query", "create_table"],
        },
        "steps": [
            {"id": "read",        "kind": "functional", "tool": "read_doc",
             "description": "Load the receipt file."},
            {"id": "extract",     "kind": "reasoning",
             "description": "Pull date/vendor/amount/tax."},
            {"id": "ensure",      "kind": "functional", "tool": "create_table",
             "description": "Create the receipts table if it doesn't exist."},
            {"id": "insert",      "kind": "reasoning",
             "description": "Append the row (via the Quick path's SQL access)."},
        ],
    },
    {
        "name": "inventory_snapshot",
        "description": (
            "Read a user-supplied list of items, look up current data per"
            " item (price / stock / metadata), emit an .xlsx snapshot"
            " including deltas vs the prior snapshot."
        ),
        "ingredients": {
            "tools": ["read_doc", "web_search", "http_get", "create_spreadsheet"],
        },
        "steps": [
            {"id": "load_list",  "kind": "functional", "tool": "read_doc",
             "description": "Read the item list from workspace."},
            {"id": "lookup",     "kind": "functional", "tool": "http_get",
             "description": "Fetch current data per item.",
             "parallel_group": 1},
            {"id": "compare",    "kind": "reasoning",
             "description": "Diff vs the prior snapshot (if any)."},
            {"id": "snapshot",   "kind": "functional", "tool": "create_spreadsheet",
             "description": "Emit the new snapshot xlsx."},
        ],
    },
]


async def seed_starter_skills(
    conn: asyncpg.Connection,
    embedder: EmbeddingClient,
) -> int:
    """Idempotently insert + embed the v1 starter set.

    Existing seeded rows (user_id IS NULL) keyed by `name` are reused.
    Returns the number of rows inserted on this call (0 once seeded)."""
    # Names of skills already seeded.
    seeded = {
        r["name"]
        for r in await conn.fetch(
            "SELECT name FROM skills WHERE user_id IS NULL"
        )
    }
    to_insert = [s for s in STARTER_SKILLS if s["name"] not in seeded]
    if not to_insert:
        return 0
    vectors = await embedder.embed([s["description"] for s in to_insert])
    for skill, embedding in zip(to_insert, vectors.vectors):
        await conn.execute(
            """
            INSERT INTO skills (user_id, name, description, embedding,
                                ingredients, steps, score)
            VALUES (NULL, $1, $2, $3, $4::jsonb, $5::jsonb, NULL)
            """,
            skill["name"],
            skill["description"],
            embedding,
            json.dumps(skill["ingredients"]),
            json.dumps(skill["steps"]),
        )
    return len(to_insert)
