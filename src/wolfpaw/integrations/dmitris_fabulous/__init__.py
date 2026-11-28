"""Document Organizer (Dmitri's Fabulous) integration.

Gives the agent read access to the user's documents through the public
``/v1`` API: search, read a document, get a file link, and run reports.
Wolfpaw never re-implements any of it — the natural-language request goes
straight to the Document Organizer's own agent.

Off unless ``WOLFPAW_DF_BASE_URL`` is set. Users connect by pasting a
personal API key from Profile; the key is validated against ``GET /v1/me``
and must carry the scopes the tools need.

Importing this package registers the integration and its tools.
"""

from __future__ import annotations

from wolfpaw.config import get_settings
from wolfpaw.integrations.base import (
    ConnectError, Credentials, Integration, register_integration,
)
from wolfpaw.integrations.dmitris_fabulous import client as df_client

# Scopes the tools need. A key missing any of them is rejected at connect
# time with a clear message rather than failing mid-conversation.
REQUIRED_SCOPES = ("documents:read", "documents:content", "search", "reports")


class DocumentOrganizerIntegration(Integration):
    name = df_client.PROVIDER
    display_name = "Document Organizer"
    description = (
        "Search and read your documents, and build reports from them, with"
        " your Dmitri's Fabulous Document Organizer."
    )
    auth_kind = "api_key"
    tool_prefix = "df"
    api_key_label = "Document Organizer API key (df_live_… or df_test_…)"

    def is_configured(self) -> bool:
        return bool(get_settings().df_base_url.strip())

    async def connect_api_key(self, api_key: str) -> Credentials:
        if not api_key.startswith("df_"):
            raise ConnectError("That doesn't look like a Document Organizer API key (it should start with df_).")
        client = df_client.DocumentOrganizerClient(api_key=api_key)
        try:
            me = await client.me()
        except df_client.DocumentOrganizerError as e:
            if e.code == "invalid_token":
                raise ConnectError("The Document Organizer rejected that key. Check it, or ask for a new one.") from e
            raise ConnectError(f"Couldn't verify the key with the Document Organizer ({e.code}).") from e
        token = me.get("token") or {}
        scopes = list(token.get("scopes") or [])
        missing = [s for s in REQUIRED_SCOPES if s not in scopes]
        if missing:
            raise ConnectError(
                "That key is missing scopes Wolfpaw needs: " + ", ".join(missing) + "."
            )
        return Credentials(
            credentials={"api_key": api_key},
            scopes=scopes,
            display={
                "key_prefix": token.get("prefix"),
                "key_name": token.get("name"),
                "environment": me.get("environment"),
            },
        )

    def planner_hint(self) -> str | None:
        return (
            "`df_*` tools search and read the user's own documents (receipts,"
            " bills, recipes, contracts, medical records…) and build reports."
            " Prefer `df_search` for any question about their documents."
        )


register_integration(DocumentOrganizerIntegration())

# Import side effect: registers the df_* tools.
from wolfpaw.integrations.dmitris_fabulous import tools  # noqa: E402,F401
