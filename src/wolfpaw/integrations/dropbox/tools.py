"""Three Dropbox tools the agent uses to read + write inside the
user's ``/Apps/Wolfpaw/`` folder (step 29).

All three share the same shape: resolve the user's
:class:`DropboxClient`, call the matching API method, return a
JSON-safe dict. Tools surface a friendly "you haven't connected
Dropbox yet" error when called without a token rather than crashing
— the Planner sees the message and can fall back to a different
approach.
"""

from __future__ import annotations

from typing import Any

from wolfpaw.integrations.dropbox.client import (
    DropboxApiError,
    DropboxClient,
    DropboxNotConnectedError,
)
from wolfpaw.toolbox.registry import (
    Tool, ToolContext, ToolError, register_tool,
)


_NOT_CONNECTED_MSG = (
    "Dropbox isn't connected for this user. Have them visit"
    " /integrations to connect their Dropbox account, then retry."
)


@register_tool
class DropboxListFolderTool(Tool):
    name = "dropbox_list_folder"
    description = (
        "List entries in the user's Dropbox App folder"
        " (`/Apps/Wolfpaw/...`). Pass `path` to list a subfolder; empty"
        " string lists the App folder root. Returns each entry's"
        " name, full path, is_folder flag, and size."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Relative path inside the App folder. Empty string"
                    " or '/' means the root. Subfolders look like"
                    " '/subdir'."
                ),
                "default": "",
            },
        },
        "required": [],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        path = inputs.get("path", "") or ""
        if path == "/":
            path = ""
        try:
            client = await DropboxClient.for_user(ctx.user_id)
        except DropboxNotConnectedError as e:
            raise ToolError(_NOT_CONNECTED_MSG) from e
        try:
            entries = await client.list_folder(path)
        except DropboxApiError as e:
            raise ToolError(
                f"Dropbox list_folder failed: {e}"
            ) from e
        return {"entries": entries, "path": path or "/"}


@register_tool
class DropboxReadFileTool(Tool):
    name = "dropbox_read_file"
    description = (
        "Read a text file from the user's Dropbox App folder. Returns"
        " the file's content decoded as UTF-8. Binary files surface as"
        " an error — use a different tool for those."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "App-folder-relative path. Always starts with '/'."
                    " Example: '/Q3-plan.md'."
                ),
            },
        },
        "required": ["path"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        path = inputs.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ToolError("`path` must be a non-empty string")
        try:
            client = await DropboxClient.for_user(ctx.user_id)
        except DropboxNotConnectedError as e:
            raise ToolError(_NOT_CONNECTED_MSG) from e
        try:
            data = await client.read_file(path)
        except DropboxApiError as e:
            raise ToolError(f"Dropbox read_file failed: {e}") from e
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ToolError(
                f"file at {path!r} isn't valid UTF-8 ({e}); use a"
                " binary-aware tool to handle it"
            ) from e
        return {"path": path, "content": text, "size_bytes": len(data)}


@register_tool
class DropboxWriteFileTool(Tool):
    name = "dropbox_write_file"
    description = (
        "Write a text file into the user's Dropbox App folder. By"
        " default refuses to overwrite an existing file (the user gets"
        " an error and can be asked via `ask_user` whether to retry"
        " with `overwrite=true`)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "App-folder-relative target path, starting with"
                    " '/'. Parent folders are auto-created."
                ),
            },
            "content": {
                "type": "string",
                "description": "UTF-8 text to write.",
            },
            "overwrite": {
                "type": "boolean",
                "description": (
                    "False (default) refuses to overwrite an existing"
                    " file. Set true only after explicit user approval."
                ),
                "default": False,
            },
        },
        "required": ["path", "content"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        path = inputs.get("path")
        content = inputs.get("content")
        overwrite = bool(inputs.get("overwrite", False))
        if not isinstance(path, str) or not path.strip():
            raise ToolError("`path` must be a non-empty string")
        if not isinstance(content, str):
            raise ToolError("`content` must be a string")
        try:
            client = await DropboxClient.for_user(ctx.user_id)
        except DropboxNotConnectedError as e:
            raise ToolError(_NOT_CONNECTED_MSG) from e
        try:
            result = await client.write_file(
                path, content.encode("utf-8"), overwrite=overwrite,
            )
        except DropboxApiError as e:
            raise ToolError(f"Dropbox write_file failed: {e}") from e
        return {
            "path": result.get("path_display", path),
            "size_bytes": result.get("size"),
            "rev": result.get("rev"),
            "server_modified": result.get("server_modified"),
        }
