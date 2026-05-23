"""DockerSandbox tests with a mocked docker client — no daemon required.

Confirms wiring: container creation params, exec command shape, tar
encoding/decoding of file I/O.
"""

from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

# Skip the suite cleanly if docker isn't installed.
pytest.importorskip("docker", reason="install with `pip install wolfpaw[docker]`")

from wolfpaw.sandbox.docker import DockerSandbox  # noqa: E402


def _mock_client_with_container(exit_code=0, stdout=b"", stderr=b""):
    container = MagicMock()
    container.id = "fake-container-id"
    container.exec_run.return_value = SimpleNamespace(
        exit_code=exit_code, output=(stdout, stderr)
    )
    client = MagicMock()
    client.containers.run.return_value = container
    return client, container


def _make(client, *, task_id=None) -> DockerSandbox:
    return DockerSandbox(
        image="python:3.12-slim",
        user_id=uuid4(),
        task_id=task_id,
        default_timeout=30,
        max_cpu_seconds=60,
        default_memory_mb=512,
        client=client,
    )


async def test_run_python_invokes_exec_with_python_dash_c():
    client, container = _mock_client_with_container(stdout=b"42\n")
    sb = _make(client)
    try:
        r = await sb.run_python("print(40 + 2)")
    finally:
        await sb.close()
    container.exec_run.assert_called_once()
    args, kwargs = container.exec_run.call_args
    assert args[0] == ["python", "-c", "print(40 + 2)"]
    assert r.stdout == "42\n"
    assert r.exit_code == 0


async def test_container_started_with_locked_down_options():
    client, container = _mock_client_with_container()
    sb = _make(client)
    try:
        await sb.run_python("pass")
    finally:
        await sb.close()
    _, kwargs = client.containers.run.call_args
    assert kwargs["network_disabled"] is True
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["working_dir"] == "/sandbox"
    assert "wolfpaw.sandbox" in kwargs["labels"]


async def test_install_package_disabled_with_message():
    client, _ = _mock_client_with_container()
    sb = _make(client)
    try:
        result = await sb.install_package("numpy")
    finally:
        await sb.close()
    assert result.ok is False
    assert "network_disabled" in result.log


async def test_write_then_read_file_round_trips_via_archive():
    client, container = _mock_client_with_container()

    archived: dict = {}

    def fake_put(path, data):
        archived["path"] = path
        archived["data"] = data

    def fake_get(path):
        # Echo back what was put — wrap "hello" in a tar archive.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name="note.txt")
            info.size = 5
            tf.addfile(info, io.BytesIO(b"hello"))
        return (iter([buf.getvalue()]), {})

    container.put_archive.side_effect = fake_put
    container.get_archive.side_effect = fake_get

    sb = _make(client)
    try:
        await sb.write_file("/sandbox/note.txt", b"hello")
        data = await sb.read_file("/sandbox/note.txt")
    finally:
        await sb.close()
    assert archived["path"] == "/sandbox"
    assert data == b"hello"


async def test_close_kills_and_removes_container():
    client, container = _mock_client_with_container()
    sb = _make(client)
    await sb.run_python("pass")  # force container creation
    await sb.close()
    container.kill.assert_called_once()
    container.remove.assert_called_once_with(force=True)
