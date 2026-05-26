"""Notion OAuth + integration management routes (step 30).

Same shape as the Dropbox routes — install-url, oauth/callback,
disconnect. Notion tokens don't expire so there's no refresh path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.responses import HTMLResponse

from wolfpaw.auth.deps import require_user_id
from wolfpaw.config import get_settings
from wolfpaw.integrations.notion import client as notion_client
from wolfpaw.integrations.oauth_state import (
    StateTokenError, consume, issue,
)
from wolfpaw.memory import notion_links as links_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()
router = APIRouter(prefix="/integrations/notion", tags=["integrations"])

_PROVIDER = "notion"


def _redirect_uri() -> str:
    settings = get_settings()
    return f"{settings.web_base_url.rstrip('/')}/integrations/notion/oauth/callback"


@router.get("/install-url")
async def install_url(user_id=Depends(require_user_id)) -> dict[str, str]:
    settings = get_settings()
    if not (settings.notion_client_id and settings.notion_client_secret):
        raise HTTPException(
            status_code=503,
            detail="Notion integration not configured on this deployment",
        )
    async with acquire() as conn:
        state = await issue(
            conn,
            user_id=user_id,
            provider=_PROVIDER,
            ttl_minutes=settings.integration_state_ttl_minutes,
        )
    url = notion_client.build_authorize_url(
        client_id=settings.notion_client_id,
        redirect_uri=_redirect_uri(),
        state=state,
    )
    return {"url": url}


@router.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> HTMLResponse:
    settings = get_settings()
    async with acquire() as conn:
        try:
            user_id = await consume(
                conn, plain=state, provider=_PROVIDER,
            )
        except StateTokenError as e:
            log.warning("integrations.notion.bad_state", reason=str(e))
            return _failure_page(
                "That Notion connect link expired or has already been used."
                " Try connecting again from the web app."
            )

    try:
        result = await notion_client.exchange_code(
            code=code,
            redirect_uri=_redirect_uri(),
            client_id=settings.notion_client_id,
            client_secret=settings.notion_client_secret,
        )
    except notion_client.NotionApiError as e:
        log.warning(
            "integrations.notion.exchange_failed",
            status=e.status_code, body=e.body,
        )
        return _failure_page(
            "Notion refused to issue a token. The Wolfpaw operator may"
            " need to fix the OAuth app configuration."
        )

    try:
        async with acquire() as conn:
            await links_dao.upsert(
                conn,
                user_id=user_id,
                access_token=result.access_token,
                workspace_id=result.workspace_id,
                workspace_name=result.workspace_name,
                workspace_icon=result.workspace_icon,
                bot_id=result.bot_id,
                owner=result.owner,
            )
    except Exception:  # noqa: BLE001
        log.exception(
            "integrations.notion.persist_failed", user_id=str(user_id),
        )
        return _failure_page(
            "We couldn't save your Notion token. Try connecting again."
        )

    log.info(
        "integrations.notion.connected",
        user_id=str(user_id),
        workspace_id=result.workspace_id,
        workspace_name=result.workspace_name,
    )
    return _success_page(result.workspace_name or "your Notion workspace")


@router.delete("", status_code=204)
async def disconnect(user_id=Depends(require_user_id)) -> None:
    async with acquire() as conn:
        await links_dao.delete(conn, user_id=user_id)


# --- HTML responses ------------------------------------------------------


def _success_page(workspace_name: str) -> HTMLResponse:
    safe = workspace_name.replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(
        f"""
        <!doctype html>
        <html><head><title>Connected — Wolfpaw</title>
        <style>body{{font-family:system-ui;max-width:480px;margin:64px auto;
        padding:24px;text-align:center;}}</style></head>
        <body>
        <h2>Notion connected</h2>
        <p>Wolfpaw can now search + read + create pages in
        <strong>{safe}</strong> (pages shared with the Wolfpaw
        integration).</p>
        <p><a href="/">Return to Wolfpaw</a></p>
        </body></html>
        """,
    )


def _failure_page(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"""
        <!doctype html>
        <html><head><title>Couldn't connect — Wolfpaw</title>
        <style>body{{font-family:system-ui;max-width:480px;margin:64px auto;
        padding:24px;text-align:center;}}</style></head>
        <body>
        <h2>Couldn't connect Notion</h2>
        <p>{message}</p>
        <p><a href="/">Return to Wolfpaw</a></p>
        </body></html>
        """,
        status_code=400,
    )
