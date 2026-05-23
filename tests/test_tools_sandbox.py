"""End-to-end tests for the 4 sandbox tools against the real SubprocessSandbox.

These confirm the tool registrations, input validation, and the
end-to-end flow through SandboxManager → SubprocessSandbox → REPL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from wolfpaw.sandbox import get_manager, reset_manager
from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry


@asynccontextmanager
async def _fake_acquire():
    yield None


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Stub out DB writes so SandboxManager teardown doesn't need Postgres."""
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.sandbox.metering.acquire", _fake_acquire)
    reset_manager()
    yield
    reset_manager()


@pytest.fixture
def sandbox_root(tmp_path, monkeypatch):
    monkeypatch.setenv("WOLFPAW_LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("WOLFPAW_SANDBOX_BACKEND", "subprocess")
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield tmp_path
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_tools_registered():
    names = get_registry().names()
    assert {"run_python", "install_package",
            "sandbox_read_file", "sandbox_write_file"}.issubset(set(names))


async def test_run_python_round_trip(sandbox_root):
    tool = get_registry().get("run_python")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    r = await tool.run(ctx, code="print(2 ** 10)")
    assert r["exit_code"] == 0
    assert r["stdout"].strip() == "1024"
    await get_manager().shutdown_all()


async def test_run_python_preserves_state(sandbox_root):
    tool = get_registry().get("run_python")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    await tool.run(ctx, code="total = 0")
    await tool.run(ctx, code="total += 7")
    r = await tool.run(ctx, code="print(total)")
    assert r["stdout"].strip() == "7"
    await get_manager().shutdown_all()


async def test_sandbox_write_then_read(sandbox_root):
    write = get_registry().get("sandbox_write_file")
    read = get_registry().get("sandbox_read_file")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    await write.run(ctx, path="hello.txt", content="hi there")
    r = await read.run(ctx, path="hello.txt")
    assert r["text"] == "hi there"
    await get_manager().shutdown_all()


async def test_sandbox_read_missing_returns_tool_error(sandbox_root):
    read = get_registry().get("sandbox_read_file")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    with pytest.raises(ToolError):
        await read.run(ctx, path="never_written.txt")
    await get_manager().shutdown_all()


async def test_install_package_rejects_shell_injection(sandbox_root):
    install = get_registry().get("install_package")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    for bad in ["numpy; rm -rf /", "pkg && evil", "$(whoami)", "../escape"]:
        with pytest.raises(ToolError):
            await install.run(ctx, package=bad)


async def test_run_python_empty_code_rejected(sandbox_root):
    tool = get_registry().get("run_python")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    with pytest.raises(ToolError):
        await tool.run(ctx, code="   ")
