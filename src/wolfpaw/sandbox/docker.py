"""DockerSandbox — locked-down Python container per task.

Self-host production option. Boots a long-lived container from
`settings.docker_image` (default `python:3.12-slim`) with network
disabled, memory + CPU caps, and a tmpfs `/sandbox` workdir. `run_python`
and `install_package` use `docker exec`; file I/O uses
`get_archive`/`put_archive`.

The `docker` package is loaded lazily so importing this module without
the `[docker]` extra installed doesn't crash.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
from typing import Any
from uuid import UUID, uuid4

from wolfpaw.sandbox.base import (
    CodeResult,
    InstallResult,
    Sandbox,
    SandboxError,
    SandboxFatalError,
)
from wolfpaw.tracing import get_logger

log = get_logger()


def _ensure_docker():
    try:
        import docker  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "docker is required for DockerSandbox. Install with"
            " `pip install wolfpaw[docker]`."
        ) from e
    return docker


class DockerSandbox(Sandbox):
    name = "docker"

    def __init__(
        self,
        *,
        image: str,
        user_id: UUID,
        task_id: UUID | None,
        default_timeout: int,
        max_cpu_seconds: int,
        default_memory_mb: int,
        client: Any | None = None,
    ) -> None:
        docker = _ensure_docker()
        self.id = uuid4()
        self.user_id = user_id
        self.task_id = task_id
        self._image = image
        self._default_timeout = default_timeout
        self._max_cpu = max_cpu_seconds
        self._memory_mb = default_memory_mb
        self._client = client or docker.from_env()
        self._container: Any | None = None
        self._lock = asyncio.Lock()
        self._elapsed = 0.0

    @property
    def elapsed_compute_seconds(self) -> float:
        return self._elapsed

    # --- lifecycle -----------------------------------------------------------

    async def _ensure(self) -> Any:
        if self._container is not None:
            return self._container

        def _start() -> Any:
            return self._client.containers.run(
                self._image,
                command=["sleep", "infinity"],
                detach=True,
                network_disabled=True,
                mem_limit=f"{self._memory_mb}m",
                pids_limit=64,
                tmpfs={"/sandbox": "size=1g,mode=1777"},
                working_dir="/sandbox",
                labels={
                    "wolfpaw.sandbox": "true",
                    "wolfpaw.user_id": self.user_id.hex,
                    "wolfpaw.sandbox_id": self.id.hex,
                },
            )

        self._container = await asyncio.to_thread(_start)
        log.info(
            "sandbox.docker.start",
            sandbox_id=str(self.id),
            container_id=self._container.id,
        )
        return self._container

    async def close(self) -> None:
        if self._container is None:
            return
        c = self._container

        def _stop() -> None:
            try:
                c.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                c.remove(force=True)
            except Exception:  # noqa: BLE001
                pass

        await asyncio.to_thread(_stop)
        self._container = None
        log.info("sandbox.docker.close", sandbox_id=str(self.id))

    # --- exec helpers --------------------------------------------------------

    async def _exec(
        self, cmd: list[str], *, timeout: int
    ) -> tuple[int, bytes, bytes]:
        container = await self._ensure()

        def _run() -> tuple[int, bytes, bytes]:
            res = container.exec_run(
                cmd, demux=True, stdout=True, stderr=True,
            )
            out, err = res.output
            return res.exit_code, (out or b""), (err or b"")

        try:
            return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
        except asyncio.TimeoutError:
            raise SandboxError(f"docker exec timed out after {timeout}s")

    # --- public API ----------------------------------------------------------

    async def run_python(
        self, code: str, *, timeout_seconds: int | None = None
    ) -> CodeResult:
        timeout = min(timeout_seconds or self._default_timeout, self._max_cpu)
        start = time.monotonic()
        try:
            exit_code, out, err = await self._exec(
                ["python", "-c", code], timeout=timeout
            )
        except SandboxError:
            elapsed = time.monotonic() - start
            return CodeResult(
                stdout="", stderr=f"timed out after {timeout}s",
                exit_code=124, elapsed_seconds=elapsed, timed_out=True,
            )
        elapsed = time.monotonic() - start
        self._elapsed += elapsed
        return CodeResult(
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            exit_code=int(exit_code or 0),
            elapsed_seconds=elapsed,
        )

    async def install_package(self, package: str) -> InstallResult:
        # Container has no network by default; install_package only makes
        # sense for adapters that allow egress. Surface that cleanly.
        return InstallResult(
            package=package, ok=False,
            log=(
                "DockerSandbox has network_disabled=True; per-task egress"
                " allowlists are a step-13 (executor) concern. Until then,"
                " install_package is unsupported here."
            ),
        )

    async def read_file(self, path: str) -> bytes:
        container = await self._ensure()

        def _read() -> bytes:
            stream, _ = container.get_archive(path)
            buf = io.BytesIO(b"".join(stream))
            with tarfile.open(fileobj=buf) as tf:
                member = tf.next()
                if member is None:
                    raise FileNotFoundError(path)
                f = tf.extractfile(member)
                if f is None:
                    raise FileNotFoundError(path)
                return f.read()

        return await asyncio.to_thread(_read)

    async def write_file(self, path: str, data: bytes) -> None:
        container = await self._ensure()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=path.lstrip("/").split("/")[-1])
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        target_dir = "/" + "/".join(path.strip("/").split("/")[:-1] or [""])
        if target_dir == "/":
            target_dir = "/sandbox"

        def _put() -> None:
            container.put_archive(target_dir, buf.getvalue())

        await asyncio.to_thread(_put)
