"""SandboxManager — one live sandbox per (user_id, task_id|None).

The agent's run_python / install_package / sandbox_read_file /
sandbox_write_file tools all flow through here so:
  - State persists across tool calls within the same task
  - The Manager (not each tool) owns construction + teardown
  - Compute-seconds are tallied at close time and routed to the
    metering helper

When tasks lifecycle lands in step 15 the executor will call
`close_for_task` on task complete / pause; until then the test suite
calls `shutdown_all` between cases.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.sandbox.base import Sandbox
from wolfpaw.sandbox.metering import record_compute
from wolfpaw.tracing import get_logger

log = get_logger()


class SandboxManager:
    def __init__(self) -> None:
        self._sandboxes: dict[tuple[UUID, UUID | None], Sandbox] = {}
        self._lock = asyncio.Lock()

    async def get(self, user_id: UUID, task_id: UUID | None = None) -> Sandbox:
        key = (user_id, task_id)
        async with self._lock:
            sb = self._sandboxes.get(key)
            if sb is None:
                sb = await self._build(user_id, task_id)
                self._sandboxes[key] = sb
                log.info(
                    "sandbox.manager.create",
                    sandbox_id=str(getattr(sb, "id", "?")),
                    user_id=str(user_id),
                    task_id=str(task_id) if task_id else None,
                    provider=sb.name,
                )
            return sb

    async def close_for_task(
        self, user_id: UUID, task_id: UUID | None = None
    ) -> None:
        key = (user_id, task_id)
        async with self._lock:
            sb = self._sandboxes.pop(key, None)
        if sb is not None:
            await self._tear_down(sb, user_id, task_id)

    async def shutdown_all(self) -> None:
        async with self._lock:
            items = list(self._sandboxes.items())
            self._sandboxes.clear()
        for (user_id, task_id), sb in items:
            await self._tear_down(sb, user_id, task_id)

    async def _tear_down(
        self, sb: Sandbox, user_id: UUID, task_id: UUID | None
    ) -> None:
        seconds = sb.elapsed_compute_seconds
        try:
            await sb.close()
        finally:
            if seconds > 0:
                try:
                    await record_compute(
                        user_id=user_id,
                        task_id=task_id,
                        sandbox_id=getattr(sb, "id", None),
                        provider=sb.name,
                        compute_seconds=seconds,
                    )
                except Exception:  # noqa: BLE001 — metering must not break teardown
                    log.exception(
                        "sandbox.metering.failed",
                        sandbox_id=str(getattr(sb, "id", "?")),
                    )

    async def _build(self, user_id: UUID, task_id: UUID | None) -> Sandbox:
        settings = get_settings()
        backend = settings.sandbox_backend.lower()
        if backend == "subprocess":
            from wolfpaw.sandbox.subprocess import SubprocessSandbox

            return SubprocessSandbox(
                root=Path(settings.local_storage_root) / "_sandboxes",
                user_id=user_id,
                task_id=task_id,
                default_timeout=settings.sandbox_default_cpu_seconds,
                max_cpu_seconds=settings.sandbox_max_cpu_seconds,
                default_memory_mb=settings.sandbox_default_memory_mb,
            )
        if backend == "docker":
            from wolfpaw.sandbox.docker import DockerSandbox

            return DockerSandbox(
                image=settings.docker_image,
                user_id=user_id,
                task_id=task_id,
                default_timeout=settings.sandbox_default_cpu_seconds,
                max_cpu_seconds=settings.sandbox_max_cpu_seconds,
                default_memory_mb=settings.sandbox_default_memory_mb,
            )
        if backend == "e2b":
            from wolfpaw.sandbox.e2b import E2BSandbox

            return E2BSandbox(
                api_key=settings.e2b_api_key,
                template=settings.e2b_template,
                user_id=user_id,
                task_id=task_id,
                default_timeout=settings.sandbox_default_cpu_seconds,
                max_cpu_seconds=settings.sandbox_max_cpu_seconds,
            )
        raise ValueError(
            f"Unknown WOLFPAW_SANDBOX_BACKEND={backend!r}; expected"
            " 'subprocess', 'docker', or 'e2b'"
        )


_manager: SandboxManager | None = None


def get_manager() -> SandboxManager:
    global _manager
    if _manager is None:
        _manager = SandboxManager()
    return _manager


def reset_manager() -> None:
    """Test hook — drop the cached manager (does NOT shut down sandboxes)."""
    global _manager
    _manager = None
