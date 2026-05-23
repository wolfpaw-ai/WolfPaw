"""SubprocessSandbox — long-lived Python REPL in a child process.

Dev/test default. Not a real security boundary (no namespace isolation),
but enforces CPU/memory limits via POSIX rlimit and keeps user code's
filesystem activity inside a temp directory.

State persists across `run_python` calls because a single child process
handles every command. Pip installs land in `<sandbox>/site-packages/`
which the REPL has on `sys.path`, so packages become importable without
a restart (with `invalidate_caches` to refresh the import system).

For production self-host use DockerSandbox; for hosted use E2BSandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import shutil
import subprocess
import sys
from pathlib import Path
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

# We launch the REPL via `python -m wolfpaw.sandbox._repl_server` rather
# than `python <path>/_repl_server.py`. The script-path form sets
# sys.path[0] to the script's directory; since this package contains a
# `subprocess.py`, that would shadow stdlib `subprocess` inside the child
# and break anything that imports asyncio (e.g. matplotlib pulls it in).
# `-m` sets sys.path[0] to "" (cwd) instead — the sandbox workdir has no
# .py files, so stdlib wins.
_REPL_MODULE = "wolfpaw.sandbox._repl_server"


def _set_limits(cpu_seconds: int, memory_mb: int):
    """Pre-exec hook that applies rlimits to the child process.

    RLIMIT_CPU is wall-time-ish on most platforms (a hard ceiling beyond
    which the OS sends SIGKILL). RLIMIT_AS bounds total virtual memory.
    Both apply to the child for its lifetime — per-call timeout is
    enforced separately at the asyncio layer.
    """

    def _apply() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        except (ValueError, OSError):
            pass  # best-effort: not all platforms allow this
        try:
            resource.setrlimit(
                resource.RLIMIT_AS,
                (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024),
            )
        except (ValueError, OSError):
            pass

    return _apply


class SubprocessSandbox(Sandbox):
    name = "subprocess"

    def __init__(
        self,
        *,
        root: Path,
        user_id: UUID,
        task_id: UUID | None,
        default_timeout: int,
        max_cpu_seconds: int,
        default_memory_mb: int,
    ) -> None:
        self.id = uuid4()
        self.user_id = user_id
        self.task_id = task_id
        self._dir = root / user_id.hex / self.id.hex
        self._workdir = self._dir / "workdir"
        self._site_packages = self._dir / "site-packages"
        for p in (self._workdir, self._site_packages):
            p.mkdir(parents=True, exist_ok=True)
        self._default_timeout = default_timeout
        self._max_cpu = max_cpu_seconds
        self._memory_mb = default_memory_mb
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._elapsed = 0.0

    @property
    def elapsed_compute_seconds(self) -> float:
        return self._elapsed

    @property
    def workdir(self) -> Path:
        return self._workdir

    # --- lifecycle -----------------------------------------------------------

    async def _ensure_proc(self) -> asyncio.subprocess.Process:
        if self._proc and self._proc.returncode is None:
            return self._proc
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = (
            str(self._site_packages)
            + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        )
        env["HOME"] = str(self._dir)
        log.info(
            "sandbox.subprocess.start",
            sandbox_id=str(self.id),
            user_id=str(self.user_id),
        )
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", _REPL_MODULE,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self._workdir),
            env=env,
            preexec_fn=_set_limits(self._max_cpu, self._memory_mb),
        )
        return self._proc

    async def close(self) -> None:
        async with self._lock:
            if self._proc and self._proc.returncode is None:
                try:
                    self._proc.terminate()
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    self._proc.kill()
                    await self._proc.wait()
            self._proc = None
        if self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
        log.info("sandbox.subprocess.close", sandbox_id=str(self.id))

    # --- ipc -----------------------------------------------------------------

    async def _send(self, cmd: dict, *, timeout: int) -> dict:
        proc = await self._ensure_proc()
        assert proc.stdin and proc.stdout
        line = (json.dumps(cmd) + "\n").encode("utf-8")
        try:
            proc.stdin.write(line)
            await proc.stdin.drain()
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            # Kill the child — its state is unknowable. Next call will spawn
            # a fresh REPL (losing globals/locals; the caller is informed by
            # the timed_out result so they can decide).
            try:
                proc.kill()
                await proc.wait()
            finally:
                self._proc = None
            raise
        if not raw:
            self._proc = None
            raise SandboxError("sandbox REPL exited unexpectedly")
        return json.loads(raw.decode("utf-8"))

    # --- public API ----------------------------------------------------------

    async def run_python(
        self, code: str, *, timeout_seconds: int | None = None
    ) -> CodeResult:
        timeout = min(timeout_seconds or self._default_timeout, self._max_cpu)
        async with self._lock:
            try:
                resp = await self._send({"op": "exec", "code": code}, timeout=timeout)
            except asyncio.TimeoutError:
                return CodeResult(
                    stdout="", stderr=f"timed out after {timeout}s",
                    exit_code=124, elapsed_seconds=float(timeout),
                    timed_out=True,
                )
        self._elapsed += float(resp.get("elapsed", 0.0))
        return CodeResult(
            stdout=str(resp.get("stdout", "")),
            stderr=str(resp.get("stderr", "")),
            exit_code=int(resp.get("exit_code", 0)),
            elapsed_seconds=float(resp.get("elapsed", 0.0)),
        )

    async def install_package(self, package: str) -> InstallResult:
        """`pip install --target=<sandbox>/site-packages <package>`. The
        REPL already has that dir on `sys.path`, so `invalidate_caches` +
        `import` picks it up without restart."""
        cmd = [
            sys.executable, "-m", "pip", "install",
            "--target", str(self._site_packages),
            "--no-warn-script-location",
            package,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        log_text = (stdout_b + stderr_b).decode("utf-8", errors="replace")
        ok = proc.returncode == 0
        if ok:
            async with self._lock:
                try:
                    await self._send({"op": "invalidate_caches"}, timeout=10)
                except Exception:  # noqa: BLE001 — best effort
                    pass
        return InstallResult(package=package, ok=ok, log=log_text)

    async def read_file(self, path: str) -> bytes:
        p = self._resolve(path)
        return await asyncio.to_thread(p.read_bytes)

    async def write_file(self, path: str, data: bytes) -> None:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, data)

    def _resolve(self, path: str) -> Path:
        """Resolve a user-supplied path against the sandbox workdir, rejecting
        anything that would escape the sandbox dir."""
        candidate = (self._workdir / path).resolve()
        try:
            candidate.relative_to(self._workdir.resolve())
        except ValueError as e:
            raise SandboxError(
                f"path {path!r} escapes the sandbox workdir"
            ) from e
        return candidate
