"""E2BSandbox tests with a mocked e2b client — no real E2B account required."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from wolfpaw.sandbox.e2b import E2BSandbox


def _mock_client(stdout=None, stderr=None, error=None):
    client = MagicMock()
    client.run_code.return_value = SimpleNamespace(
        logs=SimpleNamespace(stdout=stdout or [], stderr=stderr or []),
        error=error,
    )
    return client


def _make(client) -> E2BSandbox:
    return E2BSandbox(
        api_key="",  # ignored when client is injected
        template="base",
        user_id=uuid4(),
        task_id=None,
        default_timeout=10,
        max_cpu_seconds=60,
        client=client,
    )


async def test_run_python_collects_stdout():
    client = _mock_client(stdout=["hello", "world"])
    sb = _make(client)
    try:
        r = await sb.run_python("print('hello')\nprint('world')")
    finally:
        await sb.close()
    assert "hello" in r.stdout
    assert "world" in r.stdout
    assert r.exit_code == 0


async def test_run_python_surfaces_error_as_nonzero_exit():
    client = _mock_client(stderr=["NameError"], error="x is not defined")
    sb = _make(client)
    try:
        r = await sb.run_python("print(x)")
    finally:
        await sb.close()
    assert r.exit_code == 1
    assert "not defined" in r.stderr


async def test_install_package_invokes_pip_shell_command():
    client = _mock_client(stdout=["Successfully installed numpy"])
    sb = _make(client)
    try:
        result = await sb.install_package("numpy")
    finally:
        await sb.close()
    client.run_code.assert_called_with("!pip install numpy")
    assert result.ok is True


async def test_read_and_write_file_delegate_to_files_api():
    client = MagicMock()
    client.files.read.return_value = b"contents"
    sb = _make(client)
    try:
        await sb.write_file("/sandbox/x.txt", b"contents")
        data = await sb.read_file("/sandbox/x.txt")
    finally:
        await sb.close()
    client.files.write.assert_called_once_with("/sandbox/x.txt", b"contents")
    assert data == b"contents"


def test_init_requires_api_key_or_client():
    with pytest.raises(ValueError):
        E2BSandbox(
            api_key="", template="base", user_id=uuid4(),
            task_id=None, default_timeout=10, max_cpu_seconds=60,
        )
