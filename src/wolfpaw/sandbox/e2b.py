"""E2BSandbox — hosted code execution via E2B (https://e2b.dev).

The `e2b-code-interpreter` SDK is loaded lazily so importing this module
without the `[e2b]` extra installed doesn't crash. The hosted product
runs against this provider; self-host typically uses Docker or
Subprocess.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID, uuid4

from wolfpaw.sandbox.base import (
    CodeResult,
    InstallResult,
    Sandbox,
    SandboxError,
)
from wolfpaw.tracing import get_logger

log = get_logger()


def _ensure_e2b():
    try:
        from e2b_code_interpreter import (  # type: ignore[import-not-found]
            Sandbox as E2BClient,
        )
    except ImportError as e:
        raise ImportError(
            "e2b-code-interpreter is required for E2BSandbox. Install with"
            " `pip install wolfpaw[e2b]`."
        ) from e
    return E2BClient


class E2BSandbox(Sandbox):
    name = "e2b"

    def __init__(
        self,
        *,
        api_key: str,
        template: str,
        user_id: UUID,
        task_id: UUID | None,
        default_timeout: int,
        max_cpu_seconds: int,
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            raise ValueError(
                "E2BSandbox requires WOLFPAW_E2B_API_KEY (or an injected client for tests)."
            )
        self.id = uuid4()
        self.user_id = user_id
        self.task_id = task_id
        self._api_key = api_key
        self._template = template
        self._default_timeout = default_timeout
        self._max_cpu = max_cpu_seconds
        self._client = client
        self._lock = asyncio.Lock()
        self._elapsed = 0.0

    @property
    def elapsed_compute_seconds(self) -> float:
        return self._elapsed

    # --- lifecycle -----------------------------------------------------------

    async def _ensure(self) -> Any:
        if self._client is not None:
            return self._client
        E2BClient = _ensure_e2b()

        def _create() -> Any:
            return E2BClient(template=self._template, api_key=self._api_key)

        self._client = await asyncio.to_thread(_create)
        log.info("sandbox.e2b.start", sandbox_id=str(self.id))
        return self._client

    async def close(self) -> None:
        if self._client is None:
            return
        client = self._client

        def _kill() -> None:
            try:
                client.kill()
            except Exception:  # noqa: BLE001
                pass

        await asyncio.to_thread(_kill)
        self._client = None
        log.info("sandbox.e2b.close", sandbox_id=str(self.id))

    # --- public API ----------------------------------------------------------

    async def run_python(
        self, code: str, *, timeout_seconds: int | None = None
    ) -> CodeResult:
        timeout = min(timeout_seconds or self._default_timeout, self._max_cpu)
        client = await self._ensure()
        start = time.monotonic()

        def _run() -> Any:
            return client.run_code(code, timeout=timeout)

        try:
            execution = await asyncio.wait_for(
                asyncio.to_thread(_run), timeout=timeout + 5,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            return CodeResult(
                stdout="", stderr=f"timed out after {timeout}s",
                exit_code=124, elapsed_seconds=elapsed, timed_out=True,
            )
        elapsed = time.monotonic() - start
        self._elapsed += elapsed
        logs = getattr(execution, "logs", None)
        stdout = "\n".join(getattr(logs, "stdout", []) or []) if logs else ""
        stderr = "\n".join(getattr(logs, "stderr", []) or []) if logs else ""
        err = getattr(execution, "error", None)
        if err is not None:
            stderr = (stderr + "\n" + str(err)).strip()
            exit_code = 1
        else:
            exit_code = 0
        return CodeResult(
            stdout=stdout, stderr=stderr, exit_code=exit_code,
            elapsed_seconds=elapsed,
        )

    async def install_package(self, package: str) -> InstallResult:
        client = await self._ensure()

        def _install() -> Any:
            return client.run_code(f"!pip install {package}")

        try:
            result = await asyncio.to_thread(_install)
        except Exception as e:  # noqa: BLE001 — surface as recoverable
            return InstallResult(package=package, ok=False, log=str(e))
        logs = getattr(result, "logs", None)
        log_text = "\n".join(getattr(logs, "stdout", []) or []) if logs else ""
        return InstallResult(
            package=package,
            ok=getattr(result, "error", None) is None,
            log=log_text,
        )

    async def read_file(self, path: str) -> bytes:
        client = await self._ensure()

        def _read() -> bytes:
            return client.files.read(path, format="bytes")

        return await asyncio.to_thread(_read)

    async def write_file(self, path: str, data: bytes) -> None:
        client = await self._ensure()

        def _write() -> None:
            client.files.write(path, data)

        await asyncio.to_thread(_write)
