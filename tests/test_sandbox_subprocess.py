"""End-to-end tests for SubprocessSandbox — real child process, no mocks.

These exercise the dev/test default sandbox provider: stateful REPL,
stdout/stderr capture, file I/O, path traversal rejection, timeout
handling. They're real subprocesses, so each test pays REPL spin-up
cost (~50ms) — kept small to stay quick.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from wolfpaw.sandbox.base import SandboxError
from wolfpaw.sandbox.subprocess import SubprocessSandbox


def _make(tmp_path: Path) -> SubprocessSandbox:
    return SubprocessSandbox(
        root=tmp_path,
        user_id=uuid4(),
        task_id=None,
        default_timeout=10,
        max_cpu_seconds=30,
        default_memory_mb=512,
    )


@pytest.fixture
async def sandbox(tmp_path):
    sb = _make(tmp_path)
    yield sb
    await sb.close()


async def test_run_python_returns_stdout(sandbox):
    r = await sandbox.run_python("print('hello sandbox')")
    assert r.exit_code == 0
    assert r.stdout.strip() == "hello sandbox"
    assert r.stderr == ""
    assert r.timed_out is False


async def test_state_persists_across_calls(sandbox):
    await sandbox.run_python("counter = 0")
    for _ in range(3):
        await sandbox.run_python("counter += 1")
    r = await sandbox.run_python("print(counter)")
    assert r.stdout.strip() == "3"


async def test_exception_caught_with_traceback(sandbox):
    r = await sandbox.run_python("raise ValueError('boom')")
    assert r.exit_code == 1
    assert "ValueError" in r.stderr
    assert "boom" in r.stderr


async def test_subsequent_calls_after_exception_still_work(sandbox):
    """The REPL must not die when user code raises."""
    await sandbox.run_python("raise RuntimeError('first')")
    r = await sandbox.run_python("print('still alive')")
    assert r.stdout.strip() == "still alive"


async def test_elapsed_compute_seconds_accumulates(sandbox):
    assert sandbox.elapsed_compute_seconds == 0
    await sandbox.run_python("x = sum(range(1000))")
    assert sandbox.elapsed_compute_seconds > 0


async def test_run_python_timeout(sandbox):
    """Long-running code must hit the per-call timeout and kill the REPL."""
    r = await sandbox.run_python("import time; time.sleep(5)", timeout_seconds=1)
    assert r.timed_out is True
    assert r.exit_code == 124


async def test_run_python_after_timeout_recovers(sandbox):
    """After a timeout-kill, the manager should spin up a fresh REPL."""
    await sandbox.run_python("import time; time.sleep(5)", timeout_seconds=1)
    r = await sandbox.run_python("print('fresh')")
    assert r.stdout.strip() == "fresh"


async def test_write_then_read_file(sandbox):
    await sandbox.write_file("notes.txt", b"hello from test")
    data = await sandbox.read_file("notes.txt")
    assert data == b"hello from test"


async def test_path_traversal_rejected(sandbox):
    with pytest.raises(SandboxError):
        await sandbox.write_file("../escape.txt", b"x")


async def test_workdir_is_cwd(sandbox):
    """run_python's cwd should be the sandbox workdir, so relative paths work."""
    r = await sandbox.run_python(
        "import os; print(os.getcwd())"
    )
    assert sandbox.workdir.name in r.stdout
