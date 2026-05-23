# workspace/

Per-user file registry — what the user uploads and what the agent produces, all in one listing. Backed by `workspace_files` rows + the `Storage` adapter from `storage/`.

## Files

- **`files.py`** — `workspace_files` DAO. `list_latest(user_id)`, `get_by_id`, `get_latest_by_filename`, `register(...)`. Append-only: overwrites insert a new row with `version = prior + 1` and `supersedes_id = prior.id` so the history chain stays. Also defines `WorkspaceCollision` — raised by `write_doc` so the executor (step 15) can route to `awaiting_user`.
- **`routes.py`** — HTTP API. `GET /workspace/files` (latest version per filename, per the user), `POST /workspace/upload-url` (mint a short-lived URL), `POST /workspace/files` (register a completed upload), `GET /workspace/files/{id}/download-url`. Plus the LocalStorage-only blob transit endpoints `PUT /workspace/blob/upload?token=...` and `GET /workspace/blob/download?token=...`.

## How it fits together

Client upload flow: `POST /workspace/upload-url` → API mints a `SignedURL` from `storage.get_storage().issue_upload_url(user_id, filename)` → client PUTs bytes to that URL → client `POST /workspace/files` registers a row in `workspace_files`. On LocalStorage the URL points back at `/workspace/blob/upload`; on S3 it's a real AWS URL and bytes never touch the API.

Download is the mirror: `GET .../download-url` → client GETs.

Agent writes (via `write_doc`) follow the same registration shape but with `source = 'agent_output'` and `task_id` set; collisions raise `WorkspaceCollision`.

## Extending

- **Folders / nested namespace:** v1 is intentionally flat in the UI. The S3 keys are already hierarchical (`<user_id_hex>/<filename>`) so adding folders is a frontend + DAO listing change, not a data migration.
- **New file source** (e.g. email attachment intake from step 23): use `register(...)` with the appropriate `source` enum value; the listing endpoint surfaces it the same way.
- **Quotas / retention:** wire into `register` (check `sum(size_bytes)` against `tier_limits` before insert). v2 concern; the schema is ready.
