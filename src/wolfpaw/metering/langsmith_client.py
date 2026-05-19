"""LangSmith trace forwarder — gated on `WOLFPAW_LANGSMITH_ENABLED`.

The `langsmith` package is an optional dependency (`pip install
"wolfpaw[langsmith]"`). When disabled or absent, `trace()` is a no-op
context manager so call sites stay uniform.

For now this is the abstraction only — the actual LangSmith wire-up runs
when the user enables LangSmith and installs the package. Hooking the real
trace API is a follow-up inside this step that doesn't change call sites.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.tracing import get_logger

log = get_logger()


class LangSmithClient:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    @asynccontextmanager
    async def trace(
        self,
        *,
        agent: str,
        model: str,
        trace_id: str | None,
        prompt_version_id: UUID | None = None,
    ) -> AsyncIterator[None]:
        if not self.enabled:
            yield
            return
        # Real implementation: wrap the block in a langsmith @traceable run.
        # Lazy-import to keep langsmith out of the base dep tree.
        try:
            import langsmith  # noqa: F401
        except ImportError:
            log.warning("langsmith.import_failed", agent=agent)
            yield
            return
        log.info(
            "langsmith.trace.begin",
            agent=agent,
            model=model,
            trace_id=trace_id,
            prompt_version_id=str(prompt_version_id) if prompt_version_id else None,
        )
        try:
            yield
        finally:
            log.info("langsmith.trace.end", agent=agent, model=model)


def get_langsmith_client() -> LangSmithClient:
    return LangSmithClient(enabled=get_settings().langsmith_enabled)
