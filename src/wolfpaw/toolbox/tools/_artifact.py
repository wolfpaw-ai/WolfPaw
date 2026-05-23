"""Shared `emit_artifact` helper for the four artifact production tools.

Every artifact tool follows the same pipeline:

    1. structured inputs validated + serialized to JSON
    2. JSON written into the sandbox at `inputs.json`
    3. a small Python script (tool-specific) reads `inputs.json`, builds
       the artifact with its library, and writes bytes to a known output
       path inside the sandbox
    4. sandbox executes the script; on success the bytes are read back out
    5. bytes flow to Storage + a `workspace_files` row is inserted with
       `source = 'agent_output'`

Collisions raise `WorkspaceCollision` exactly like `write_doc` does — the
executor's overwrite-with-confirmation flow (step 15) catches and pings
the user. Passing `overwrite=True` bypasses the check and version-bumps.

The script template is tool-defined and static (no user input is ever
interpolated into Python source). Inputs flow in only through the JSON
file, decoded inside the sandbox via `json.load`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from wolfpaw.memory.db import acquire
from wolfpaw.sandbox import get_manager
from wolfpaw.storage import get_storage
from wolfpaw.toolbox.registry import ToolContext, ToolError
from wolfpaw.workspace import files as files_dao
from wolfpaw.workspace.files import WorkspaceCollision

INPUTS_BASENAME = "inputs.json"


@dataclass(frozen=True)
class ArtifactSpec:
    filename: str          # workspace filename (e.g. "report.xlsx")
    mime_type: str
    sandbox_inputs: dict   # JSON-serializable, written to inputs.json
    sandbox_script: str    # reads inputs.json, writes to `output_basename`
    output_basename: str   # e.g. "output.xlsx" inside the sandbox workdir
    overwrite: bool = False


async def emit_artifact(ctx: ToolContext, spec: ArtifactSpec) -> dict[str, Any]:
    """Run the spec's script in the task's sandbox, capture the output
    bytes, and register them as an `agent_output` workspace file."""

    # 1. Pre-collision check.
    async with acquire() as conn:
        existing = await files_dao.get_latest_by_filename(
            conn, ctx.user_id, spec.filename
        )
    if existing is not None and not spec.overwrite:
        raise WorkspaceCollision(existing)

    # 2. Stage inputs + script in the sandbox.
    sandbox = await get_manager().get(ctx.user_id, ctx.task_id)
    try:
        inputs_bytes = json.dumps(spec.sandbox_inputs).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise ToolError(f"inputs are not JSON-serializable: {e}") from e
    await sandbox.write_file(INPUTS_BASENAME, inputs_bytes)

    # 3. Execute.
    result = await sandbox.run_python(spec.sandbox_script)
    if result.timed_out:
        raise ToolError(
            f"artifact generation timed out after {result.elapsed_seconds:.1f}s"
        )
    if result.exit_code != 0:
        raise ToolError(
            f"artifact script failed (exit_code={result.exit_code}):"
            f" {result.stderr.strip() or result.stdout.strip() or 'no output'}"
        )

    # 4. Read bytes back.
    try:
        data = await sandbox.read_file(spec.output_basename)
    except FileNotFoundError as e:
        raise ToolError(
            f"artifact script completed but produced no {spec.output_basename}"
        ) from e

    # 5. Persist to workspace.
    storage = get_storage()
    obj = await storage.put(ctx.user_id, spec.filename, data)
    async with acquire() as conn:
        version = (existing.version + 1) if existing else 1
        supersedes_id = existing.id if existing else None
        f = await files_dao.register(
            conn,
            user_id=ctx.user_id,
            source="agent_output",
            filename=spec.filename,
            storage_url=obj.storage_url,
            size_bytes=obj.size_bytes,
            mime_type=spec.mime_type,
            sha256=obj.sha256,
            task_id=ctx.task_id,
            version=version,
            supersedes_id=supersedes_id,
        )

    return {
        "file_id": str(f.id),
        "filename": f.filename,
        "version": f.version,
        "size_bytes": f.size_bytes,
        "mime_type": f.mime_type,
        "sha256": f.sha256,
    }
