# toolbox/

Agent-callable tools and the registry that holds them. Importing the package registers every tool with the global registry as a side effect.

## Files

- **`registry.py`** — `Tool` ABC, `Registry`, `@register_tool` decorator, `ToolContext` (carries `user_id`, `task_id`, `trace_id` into every call), and the two error types (`ToolError` recoverable, `ToolFatalError` not). `Tool.to_anthropic_schema()` emits the shape Anthropic's `messages.create(tools=...)` expects.
- **`user_data.py`** — per-user `user_data_<uuid_hex>` schema management for the SQL tools. `ensure_schema(conn, user_id)` creates the schema on demand; `validate_identifier` + `quote_ident` are the safe-naming primitives for any DDL the tools build.
- **`__init__.py`** — imports every tool module to trigger side-effect registration. Order doesn't matter; the registry deduplicates by name.

## Tools (in `tools/`)

Step 7 (information & data, docs):
- **`calculator`** — AST-whitelisted arithmetic eval. No names, no calls, no attribute access. Returns one number.
- **`http_get`** — `httpx` GET → readability extraction for HTML, verbatim for non-HTML. Size-capped.
- **`web_search`** — POST to Tavily, returns ranked results. Reads `WOLFPAW_TAVILY_API_KEY` at call time so missing key surfaces as an actionable `ToolError`.
- **`sql_query`** — read-only SELECT against the user's `user_data_*` schema. Two layers: SELECT-only prefix check + READ ONLY transaction.
- **`create_table`** — DDL into `user_data_*`. Column types from a tight whitelist; identifiers validated.
- **`read_doc`** — fetch the latest version of a workspace file by filename via `Storage` + `workspace_files` DAO.
- **`write_doc`** — write text to a workspace file. Collision → `WorkspaceCollision` unless `overwrite=True` (then version-bump).

Step 8 (sandbox):
- **`run_python`** — execute Python in the task's sandbox; state persists across calls. Returns stdout/stderr/exit_code/elapsed/timed_out.
- **`install_package`** — `pip install` into the sandbox. Package spec is regex-validated against shell injection.
- **`sandbox_read_file`** / **`sandbox_write_file`** — UTF-8 text I/O against paths inside the sandbox.

## How it fits together

The Quick Agent (step 10) and Executor (step 13) look up tools via `get_registry().get(name)` and invoke `await tool.run(ctx, **inputs)`. The sandbox tools flow through `wolfpaw.sandbox.get_manager()` so one sandbox per `(user_id, task_id)` is reused across calls. Workspace tools talk to `wolfpaw.storage.get_storage()` and the `workspace_files` DAO; SQL tools talk to Postgres via `memory.db.acquire`.

## Extending

- **New tool:** subclass `Tool`, set `name` / `description` / `input_schema` (JSON Schema), implement `async def run(self, ctx, **inputs) -> dict`, decorate the class with `@register_tool`, and add the module import to `tools/__init__.py`. Raise `ToolError` for recoverable issues so the agent gets the message and can retry.
- **New tool dimension** (e.g. `awaiting_user` for HITL): add a new exception type alongside `ToolError`; the executor (step 13) catches and routes.
