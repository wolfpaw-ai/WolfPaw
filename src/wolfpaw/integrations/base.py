"""Integration framework — one small contract for outbound integrations.

An *integration* connects Wolfpaw to an outside service the agent can use
as tools (Document Organizer today; Slack-as-a-tool, Microsoft Office,
etc. later). It is **not** a channel: channels (web, Telegram, Slack,
voice) are two-way messaging with inbound webhooks and account linking;
integrations are outbound tool use. They overlap only on outbound sends.

To add an integration:

  1. Subclass :class:`Integration` in ``integrations/<name>/__init__.py``
     and call :func:`register_integration` on an instance.
  2. Put its tools in ``integrations/<name>/tools.py`` with
     ``@register_tool`` (tool names share one prefix: ``tool_prefix``).
  3. Import the package from ``api.py`` so it registers (built-ins), or
     ship it as a separate package exposing a ``wolfpaw.integrations``
     entry point (see :func:`load_entry_points`).

The framework supplies the standard routes (:func:`build_router`):
``GET /integrations``, ``GET /integrations/{name}``,
``POST /integrations/{name}/connect`` and ``DELETE /integrations/{name}``,
plus per-user storage (``memory.integration_links``) and the "which
providers are connected" answer the Planner uses.

Dropbox, Notion and Microsoft predate the framework and keep their own
routes and link tables; they are not registered here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from wolfpaw.auth.deps import require_user_id
from wolfpaw.memory import integration_links as links_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()

AuthKind = Literal["api_key", "oauth"]


class ConnectError(Exception):
    """Raised by :meth:`Integration.connect_api_key` when the supplied
    credentials are rejected. The message is shown to the user, so it must
    not echo the credential."""


@dataclass(frozen=True)
class Credentials:
    """What a successful connect produces, ready to store."""

    credentials: dict[str, Any]
    scopes: list[str] = field(default_factory=list)
    display: dict[str, Any] = field(default_factory=dict)


class Integration:
    """Base class. Override the class attributes and the hooks you need."""

    # Stable identifier: storage key and URL segment.
    name: str = ""
    display_name: str = ""
    description: str = ""
    auth_kind: AuthKind = "api_key"
    # Prefix shared by this integration's tool names (``df_*``). The Planner
    # is told "``<prefix>_*`` tools are usable" when the user is connected.
    tool_prefix: str = ""
    # What the connect form asks for, shown on Profile.
    api_key_label: str = "API key"

    def is_configured(self) -> bool:
        """Whether this deployment has what the integration needs (e.g. a
        base URL). Unconfigured integrations are hidden from users."""
        return True

    async def connect_api_key(self, api_key: str) -> Credentials:
        """Validate a pasted key against the service and return what to
        store. Raise :class:`ConnectError` with a user-safe message on
        failure. Only called for ``auth_kind == "api_key"``."""
        raise NotImplementedError

    def planner_hint(self) -> str | None:
        """Optional extra line for the Planner when connected."""
        return None


# --- registry ------------------------------------------------------------

_integrations: dict[str, Integration] = {}


def register_integration(integration: Integration) -> Integration:
    if not integration.name:
        raise ValueError("integration must have a non-empty name")
    if not integration.tool_prefix:
        raise ValueError(f"integration {integration.name!r} needs a tool_prefix")
    _integrations[integration.name] = integration
    return integration


def get_integration(name: str) -> Integration | None:
    return _integrations.get(name)


def all_integrations() -> list[Integration]:
    return sorted(_integrations.values(), key=lambda i: i.name)


def configured_integrations() -> list[Integration]:
    return [i for i in all_integrations() if i.is_configured()]


def load_entry_points() -> list[str]:
    """Import externally-packaged integrations.

    A separate package can plug in without forking Wolfpaw by declaring::

        [project.entry-points."wolfpaw.integrations"]
        my_integration = "my_package.wolfpaw_integration"

    Importing the target module must call :func:`register_integration`.
    Returns the names that loaded; a broken plugin is logged and skipped
    so it can't take the app down.
    """
    from importlib.metadata import entry_points

    loaded: list[str] = []
    for ep in entry_points(group="wolfpaw.integrations"):
        try:
            ep.load()
            loaded.append(ep.name)
        except Exception:  # noqa: BLE001
            log.warning("integrations.entry_point_failed", entry_point=ep.name, exc_info=True)
    return loaded


async def connected_framework_integrations(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> list[str]:
    """Names of framework integrations this user has connected. Best
    effort per provider, like the legacy per-provider checks."""
    connected: list[str] = []
    for integration in configured_integrations():
        try:
            link = await links_dao.get(
                conn, user_id=user_id, provider=integration.name,
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "integrations.link_lookup_failed",
                provider=integration.name, exc_info=True,
            )
            continue
        if link is not None:
            connected.append(integration.name)
    return connected


# --- standard routes -----------------------------------------------------


class _ConnectBody(BaseModel):
    api_key: str


def _card(integration: Integration, link: links_dao.IntegrationLink | None) -> dict[str, Any]:
    return {
        "name": integration.name,
        "display_name": integration.display_name or integration.name,
        "description": integration.description,
        "auth_kind": integration.auth_kind,
        "api_key_label": integration.api_key_label,
        "connected": link is not None,
        "scopes": list(link.scopes) if link else [],
        "display": dict(link.display) if link else {},
        "connected_at": link.created_at.isoformat() if link and link.created_at else None,
    }


def _require(name: str) -> Integration:
    integration = get_integration(name)
    if integration is None or not integration.is_configured():
        raise HTTPException(status_code=404, detail="unknown integration")
    return integration


def build_router() -> APIRouter:
    """The standard routes for every registered integration. Mount it
    *after* the legacy per-provider routers so their more specific paths
    (``/integrations/notion/...``) win."""
    router = APIRouter(prefix="/integrations", tags=["integrations"])

    @router.get("")
    async def list_integrations(user_id=Depends(require_user_id)) -> dict[str, Any]:
        cards = []
        async with acquire() as conn:
            for integration in configured_integrations():
                link = await links_dao.get(
                    conn, user_id=user_id, provider=integration.name,
                )
                cards.append(_card(integration, link))
        return {"integrations": cards}

    @router.get("/{name}")
    async def get_status(name: str, user_id=Depends(require_user_id)) -> dict[str, Any]:
        integration = _require(name)
        async with acquire() as conn:
            link = await links_dao.get(conn, user_id=user_id, provider=integration.name)
        return _card(integration, link)

    @router.post("/{name}/connect")
    async def connect(
        name: str, body: _ConnectBody, user_id=Depends(require_user_id),
    ) -> dict[str, Any]:
        integration = _require(name)
        if integration.auth_kind != "api_key":
            raise HTTPException(
                status_code=400,
                detail=f"{integration.display_name} connects with OAuth, not a pasted key",
            )
        api_key = body.api_key.strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="api_key is required")
        try:
            creds = await integration.connect_api_key(api_key)
        except ConnectError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        async with acquire() as conn:
            await links_dao.upsert(
                conn,
                user_id=user_id,
                provider=integration.name,
                auth_kind=integration.auth_kind,
                credentials=creds.credentials,
                scopes=creds.scopes,
                display=creds.display,
            )
            link = await links_dao.get(conn, user_id=user_id, provider=integration.name)
        log.info("integrations.connected", provider=integration.name)
        return _card(integration, link)

    @router.delete("/{name}")
    async def disconnect(name: str, user_id=Depends(require_user_id)) -> dict[str, bool]:
        integration = _require(name)
        async with acquire() as conn:
            removed = await links_dao.delete(
                conn, user_id=user_id, provider=integration.name,
            )
        log.info("integrations.disconnected", provider=integration.name)
        return {"disconnected": removed}

    return router
