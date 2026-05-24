"""Apply every `NNN_*.sql` file in `migrations/` in order, exactly once.

Used as a one-shot container in docker-compose (depends_on: db) and as a
manual `python -m scripts.migrate` for self-host operators.

Idempotency: tracks applied filenames in `schema_migrations(filename)`.
On each run, computes the set difference between files on disk and rows
in the table, and applies the missing ones in lexical order. Re-running
is safe — already-applied files are skipped.

Failure: stops at the first migration that errors. Already-applied
migrations stay marked; the failing one does NOT get marked, so a fix +
retry runs only it.

Reads `WOLFPAW_DATABASE_URL` from the env (the same setting the app
uses). Reads `WOLFPAW_MIGRATIONS_DIR` if set, otherwise uses the
in-source `migrations/` (works for dev).
"""

from __future__ import annotations

import asyncio
import sys

import asyncpg

from wolfpaw.config import get_settings
from wolfpaw.memory.db import apply_sql_file, migrations_dir


_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def _run() -> int:
    settings = get_settings()
    mdir = migrations_dir()
    if not mdir.is_dir():
        print(f"[migrate] migrations dir not found: {mdir}", file=sys.stderr)
        return 2

    files = sorted(p for p in mdir.glob("*.sql") if p.is_file())
    if not files:
        print(f"[migrate] no .sql files in {mdir}")
        return 0

    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        await conn.execute(_TRACKING_TABLE)
        applied = {
            row["filename"]
            for row in await conn.fetch("SELECT filename FROM schema_migrations")
        }
        pending = [p for p in files if p.name not in applied]
        if not pending:
            print(f"[migrate] up to date ({len(files)} migrations applied)")
            return 0
        print(f"[migrate] applying {len(pending)} migration(s)...")
        for path in pending:
            print(f"[migrate]   {path.name}")
            await apply_sql_file(conn, path)
            await conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES ($1)",
                path.name,
            )
        print(f"[migrate] done ({len(pending)} applied)")
        return 0
    finally:
        await conn.close()


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
