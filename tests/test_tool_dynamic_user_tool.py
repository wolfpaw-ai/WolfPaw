"""Tests for the DynamicUserTool sandbox-execution wrapper.

The wrapper script is what we mostly verify: inputs flow through the
inputs.json file, the user implementation runs in the sandbox, the
result comes back through output.json.

Uses a hand-rolled fake Sandbox that records writes + runs the script
in-process via exec() so tests don't need a real subprocess sandbox.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from wolfpaw.memory.tools import UserTool
from wolfpaw.toolbox.registry import ToolContext, ToolError
from wolfpaw.toolbox.tools._dynamic_user_tool import DynamicUserTool


@dataclass
class FakeSandboxRunResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    elapsed_seconds: float = 0.0


@dataclass
class FakeSandbox:
    """In-process sandbox stand-in. Stores writes in `files`,
    interprets `run_python` by exec()'ing the script in a fresh
    globals dict (with `open()` redirected at our `files` dict)."""

    files: dict[str, bytes] = field(default_factory=dict)

    async def write_file(self, name: str, data: bytes) -> None:
        self.files[name] = data

    async def read_file(self, name: str) -> bytes:
        if name not in self.files:
            raise FileNotFoundError(name)
        return self.files[name]

    async def run_python(self, script: str) -> FakeSandboxRunResult:
        # Provide a wrapped `open` that reads/writes against
        # self.files instead of the real FS so the wrapper's
        # `with open("inputs.json")` works without touching disk.
        files = self.files

        class _FakeFile:
            def __init__(self, name: str, mode: str):
                self._name = name
                self._mode = mode
                self._buf = b""

            def __enter__(self):
                if "r" in self._mode:
                    self._buf = files[self._name]
                return self

            def __exit__(self, *exc):
                if "w" in self._mode:
                    files[self._name] = self._buf
                return False

            def read(self) -> str:
                return self._buf.decode("utf-8")

            def write(self, data: str) -> None:
                self._buf += data.encode("utf-8")

        def fake_open(name, mode="r", *args, **kwargs):
            return _FakeFile(name, mode)

        ns = {"__builtins__": __builtins__, "open": fake_open}
        try:
            exec(script, ns)
        except SystemExit as e:
            # SystemExit.code may be int (normal exits) or a string
            # message (the wrapper uses this for "result was never
            # set"). Strings → non-zero, stderr = the message.
            if isinstance(e.code, int):
                code = e.code
            elif e.code is None:
                code = 0
            else:
                code = 1
            return FakeSandboxRunResult(exit_code=code, stderr=str(e.code or ""))
        except Exception as e:  # noqa: BLE001
            return FakeSandboxRunResult(
                exit_code=1, stderr=f"{type(e).__name__}: {e}",
            )
        return FakeSandboxRunResult(exit_code=0)


@pytest.fixture
def with_fake_sandbox(monkeypatch):
    sandbox = FakeSandbox()

    class _Manager:
        async def get(self, user_id, task_id):
            return sandbox

    monkeypatch.setattr(
        "wolfpaw.toolbox.tools._dynamic_user_tool.get_manager",
        lambda: _Manager(),
    )
    return sandbox


def _user_tool(*, implementation: str, name: str = "scrape_demo") -> UserTool:
    return UserTool(
        id=uuid4(), user_id=uuid4(), name=name,
        description="demo", signature={"type": "object", "properties": {}},
        implementation=implementation, status="approved",
    )


def _ctx() -> ToolContext:
    return ToolContext(user_id=uuid4(), task_id=uuid4())


# --- happy path ----------------------------------------------------------


async def test_dynamic_user_tool_runs_user_code_and_returns_result(
    with_fake_sandbox,
):
    """Inputs land in inputs.json; the user code reads them, sets
    `result`; the wrapper writes output.json; the tool returns it."""
    tool = DynamicUserTool(_user_tool(
        implementation="result = {'doubled': inputs['x'] * 2}",
    ))
    out = await tool.run(_ctx(), x=21)
    assert out == {"doubled": 42}
    # inputs.json was staged.
    assert json.loads(with_fake_sandbox.files["inputs.json"]) == {"x": 21}


async def test_dynamic_user_tool_handles_complex_input_shapes(
    with_fake_sandbox,
):
    tool = DynamicUserTool(_user_tool(
        implementation=(
            "result = {"
            "  'reversed': list(reversed(inputs['items'])),"
            "  'count': len(inputs['items'])"
            "}"
        ),
    ))
    out = await tool.run(_ctx(), items=["a", "b", "c"])
    assert out == {"reversed": ["c", "b", "a"], "count": 3}


async def test_dynamic_user_tool_preserves_indentation_inside_implementation(
    with_fake_sandbox,
):
    """User code with `if/for` blocks must keep its own indentation
    intact. textwrap.dedent in the wrapper trims a common leading
    indent — verify that doesn't break syntax for code that's already
    flush-left."""
    tool = DynamicUserTool(_user_tool(
        implementation=(
            "total = 0\n"
            "for v in inputs['values']:\n"
            "    total += v\n"
            "result = {'total': total}"
        ),
    ))
    out = await tool.run(_ctx(), values=[1, 2, 3, 4])
    assert out == {"total": 10}


# --- failure modes ------------------------------------------------------


async def test_dynamic_user_tool_raises_on_missing_task_context():
    tool = DynamicUserTool(_user_tool(implementation="result = {}"))
    with pytest.raises(ToolError, match="Task context"):
        await tool.run(ToolContext(user_id=uuid4(), task_id=None))


async def test_dynamic_user_tool_raises_when_inputs_not_json_serializable(
    with_fake_sandbox,
):
    tool = DynamicUserTool(_user_tool(implementation="result = {}"))
    not_serializable = object()
    with pytest.raises(ToolError, match="JSON-serializable"):
        await tool.run(_ctx(), bad=not_serializable)


async def test_dynamic_user_tool_raises_when_user_code_doesnt_set_result(
    with_fake_sandbox,
):
    """The wrapper enforces ``result`` being set via SystemExit if
    it's still None at the end."""
    tool = DynamicUserTool(_user_tool(
        implementation="x = inputs.get('x', 0) * 2  # forgot to set `result`",
    ))
    with pytest.raises(ToolError, match="crashed"):
        await tool.run(_ctx(), x=5)


async def test_dynamic_user_tool_raises_on_sandbox_crash(monkeypatch):
    """Non-zero exit_code from the sandbox surfaces as a ToolError
    with the stderr message."""
    crash_sb = FakeSandbox()

    async def crashing_run_python(_self, _script):
        return FakeSandboxRunResult(
            exit_code=1, stderr="NameError: undefined",
        )

    monkeypatch.setattr(FakeSandbox, "run_python", crashing_run_python)

    class _Manager:
        async def get(self, user_id, task_id):
            return crash_sb

    monkeypatch.setattr(
        "wolfpaw.toolbox.tools._dynamic_user_tool.get_manager",
        lambda: _Manager(),
    )

    tool = DynamicUserTool(_user_tool(implementation="result = {}"))
    with pytest.raises(ToolError, match="crashed"):
        await tool.run(_ctx())


async def test_dynamic_user_tool_raises_on_timeout(monkeypatch):
    timeout_sb = FakeSandbox()

    async def timing_out_run_python(_self, _script):
        return FakeSandboxRunResult(
            exit_code=0, timed_out=True, elapsed_seconds=60.0,
        )

    monkeypatch.setattr(FakeSandbox, "run_python", timing_out_run_python)

    class _Manager:
        async def get(self, user_id, task_id):
            return timeout_sb

    monkeypatch.setattr(
        "wolfpaw.toolbox.tools._dynamic_user_tool.get_manager",
        lambda: _Manager(),
    )

    tool = DynamicUserTool(_user_tool(implementation="result = {}"))
    with pytest.raises(ToolError, match="timed out"):
        await tool.run(_ctx())
