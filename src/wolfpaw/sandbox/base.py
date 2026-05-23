"""Sandbox interface — every provider (subprocess, Docker, E2B) implements
the same shape so the agent loop and tool layer don't have to know which
backend they're talking to.

A sandbox is a stateful Python execution environment:
    - `run_python(code)` executes code and returns stdout/stderr/exit_code;
      state (globals, imports, files) persists across calls.
    - `install_package(name)` makes a package importable in subsequent runs.
    - `read_file(path)` / `write_file(path, data)` move bytes between the
      caller and the sandbox's filesystem.
    - `close()` tears the sandbox down and releases resources.

The Subprocess provider is the dev/test default — easy to spin up, no
external deps. Docker and E2B are production providers; their
implementations gate the heavyweight client libraries behind optional
extras (`[docker]`, `[e2b]`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class CodeResult:
    """Outcome of one `run_python` call."""

    stdout: str
    stderr: str
    exit_code: int
    elapsed_seconds: float
    timed_out: bool = False


@dataclass(frozen=True)
class InstallResult:
    """Outcome of one `install_package` call."""

    package: str
    ok: bool
    log: str


class SandboxError(Exception):
    """Recoverable sandbox failure (timeout, OOM, install failure)."""


class SandboxFatalError(Exception):
    """Unrecoverable sandbox failure — caller should tear it down."""


class Sandbox(ABC):
    name: str

    @abstractmethod
    async def run_python(
        self, code: str, *, timeout_seconds: int | None = None
    ) -> CodeResult: ...

    @abstractmethod
    async def install_package(self, package: str) -> InstallResult: ...

    @abstractmethod
    async def read_file(self, path: str) -> bytes: ...

    @abstractmethod
    async def write_file(self, path: str, data: bytes) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @property
    @abstractmethod
    def elapsed_compute_seconds(self) -> float:
        """Cumulative compute-seconds attributable to this sandbox.

        Used by the metering helper to write `compute_usage` rows.
        Providers track this however makes sense for their backend
        (subprocess: sum of run_python elapsed; Docker: same; E2B:
        whatever the provider reports)."""
