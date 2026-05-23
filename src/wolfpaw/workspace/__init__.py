"""Per-user workspace: file registry + REST API.

`files.py` owns the `workspace_files` table — listing, lookup, version
chains, registration of completed uploads.
`routes.py` wires those operations and the storage signing into FastAPI.
"""
