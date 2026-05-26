"""Notion integration (v2 step 30).

OAuth tokens don't expire — the schema + client are simpler than
Dropbox's. Three tools: ``notion_search``, ``notion_read_page``,
``notion_create_page``. Rate-limited at the client layer to 3 req/s
(Notion's documented cap).

Tool registration fires on package import.
"""

from wolfpaw.integrations.notion import tools  # noqa: F401
