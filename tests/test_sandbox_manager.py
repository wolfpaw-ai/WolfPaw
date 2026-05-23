"""SandboxManager tests — sandbox reuse, teardown, factory dispatch.

Uses SubprocessSandbox throughout; metering writes are stubbed so the
suite doesn't need Postgres.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from wolfpaw.sandbox.manager import SandboxManager, reset_manager


@asynccontextmanager
async def _fake_acquire():
    yield None


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Disable the DB-touching parts of compute metering."""
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.sandbox.metering.acquire", _fake_acquire)
    reset_manager()
    yield
    reset_manager()


@pytest.fixture
def subprocess_storage_root(tmp_path, monkeypatch):
    monkeypatch.setenv("WOLFPAW_LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("WOLFPAW_SANDBOX_BACKEND", "subprocess")
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield tmp_path
    get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_same_user_same_task_reuses_sandbox(subprocess_storage_root):
    m = SandboxManager()
    uid, tid = uuid4(), uuid4()
    a = await m.get(uid, tid)
    b = await m.get(uid, tid)
    assert a is b
    await m.shutdown_all()


async def test_different_tasks_get_separate_sandboxes(subprocess_storage_root):
    m = SandboxManager()
    uid = uuid4()
    a = await m.get(uid, uuid4())
    b = await m.get(uid, uuid4())
    assert a is not b
    await m.shutdown_all()


async def test_different_users_get_separate_sandboxes(subprocess_storage_root):
    m = SandboxManager()
    a = await m.get(uuid4(), None)
    b = await m.get(uuid4(), None)
    assert a is not b
    await m.shutdown_all()


async def test_close_for_task_drops_sandbox(subprocess_storage_root):
    m = SandboxManager()
    uid, tid = uuid4(), uuid4()
    first = await m.get(uid, tid)
    await m.close_for_task(uid, tid)
    second = await m.get(uid, tid)
    assert first is not second
    await m.shutdown_all()


async def test_unknown_backend_raises(monkeypatch, subprocess_storage_root):
    monkeypatch.setenv("WOLFPAW_SANDBOX_BACKEND", "imaginary")
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    m = SandboxManager()
    with pytest.raises(ValueError, match="Unknown"):
        await m.get(uuid4(), None)
