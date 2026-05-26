"""Dropbox integration (v2 step 29).

App-folder scope only — every read/write is rooted at
``/Apps/Wolfpaw/`` inside the user's Dropbox. The agent can't reach
files outside that folder regardless of behaviour.

Three tools, registered on package import:
  * ``dropbox_list_folder(path?)`` — list files
  * ``dropbox_read_file(path)`` — download text contents
  * ``dropbox_write_file(path, content, overwrite?)`` — upload text contents

Tokens flow through :mod:`wolfpaw.memory.dropbox_links`. Refresh is
automatic via :class:`DropboxClient` — tools call it transparently.
"""

# Tool registration side-effect (registers @register_tool decorated classes).
from wolfpaw.integrations.dropbox import tools  # noqa: F401
