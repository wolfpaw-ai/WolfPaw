"""REPL server for SubprocessSandbox — runs inside the child Python process.

Reads JSON-line commands from stdin, writes JSON-line results to stdout.
Globals persist across exec() calls so the sandbox keeps state. Stdout
and stderr are captured per call via contextlib.redirect_* so the
agent's prints come back in the result rather than getting tangled with
the IPC framing.

Commands:
    {"op": "exec", "code": "..."}            -> {"stdout","stderr","exit_code","elapsed"}
    {"op": "invalidate_caches"}              -> {"ok": true}
    {"op": "ping"}                           -> {"pong": true}

This file is meant to be invoked as `python -u repl_server.py` — never
imported.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
import traceback


def main() -> None:
    globals_dict: dict = {"__name__": "__sandbox__"}
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as e:
            _write({"error": f"bad command json: {e}"})
            continue

        op = cmd.get("op")
        if op == "ping":
            _write({"pong": True})
        elif op == "invalidate_caches":
            import importlib

            importlib.invalidate_caches()
            _write({"ok": True})
        elif op == "exec":
            _write(_exec(cmd.get("code", ""), globals_dict))
        else:
            _write({"error": f"unknown op: {op!r}"})


def _exec(code: str, globals_dict: dict) -> dict:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    start = time.monotonic()
    exit_code = 0
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            compiled = compile(code, "<sandbox>", "exec")
            exec(compiled, globals_dict)  # noqa: S102 — sandbox is the point
        except SystemExit as e:
            exit_code = int(e.code or 0)
        except BaseException:
            exit_code = 1
            err_buf.write(traceback.format_exc())
    elapsed = time.monotonic() - start
    return {
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
        "exit_code": exit_code,
        "elapsed": elapsed,
    }


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
