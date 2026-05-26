"""Microsoft Calendar OAuth routes (step 32). Same shape as Dropbox +
Notion routes — install-url, oauth/callback, disconnect."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.responses import HTMLResponse

from wolfpaw.auth.deps import require_user_id
from wolfpaw.config import get_settings
from wolfpaw.integrations.microsoft import client as ms_client
from wolfpaw.integrations.oauth_state import (
    StateTokenError, consume, issue,
)
from wolfpaw.memory import microsoft_links as links_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()
router = APIRouter(prefix="/integrations/microsoft", tags=["integrations"])

_PROVIDER = "microsoft"


def _redirect_uri() -> str:
    settings = get_settings()
    return (
        f"{settings.web_base_url.rstrip('/')}"
        "/integrations/microsoft/oauth/callback"
    )


@router.get("/install-url")
async def install_url(user_id=Depends(require_user_id)) -> dict[str, str]:
    settings = get_settings()
    if not (settings.microsoft_client_id and settings.microsoft_client_secret):
        raise HTTPException(
            status_code=503,
            detail="Microsoft integration not configured on this deployment",
        )
    async with acquire() as conn:
        state = await issue(
            conn,
            user_id=user_id,
            provider=_PROVIDER,
            ttl_minutes=settings.integration_state_ttl_minutes,
        )
    url = ms_client.build_authorize_url(
        tenant=settings.microsoft_tenant,
        client_id=settings.microsoft_client_id,
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
            log.warning("integrations.microsoft.bad_state", reason=str(e))
            return _failure_page(
                "That Microsoft connect link expired or has already been used."
                " Try connecting again from the web app."
            )

    try:
        result = await ms_client.exchange_code(
            code=code,
            redirect_uri=_redirect_uri(),
            tenant=settings.microsoft_tenant,
            client_id=settings.microsoft_client_id,
            client_secret=settings.microsoft_client_secret,
        )
    except ms_client.MicrosoftApiError as e:
        log.warning(
            "integrations.microsoft.exchange_failed",
            status=e.status_code, body=e.body,
        )
        return _failure_page(
            "Microsoft refused to issue tokens. The Wolfpaw operator may"
            " need to fix the OAuth app configuration."
        )

    try:
        async with acquire() as conn:
            await links_dao.upsert(
                conn,
                user_id=user_id,
                access_token=result.access_token,
                refresh_token=result.refresh_token,
                expires_at=result.expires_at,
                tenant_id=None,
                account_id=None,
                scope=result.scope,
            )
    except Exception:  # noqa: BLE001
        log.exception(
            "integrations.microsoft.persist_failed",
            user_id=str(user_id),
        )
        return _failure_page(
            "We couldn't save your Microsoft tokens. Try connecting again."
        )

    log.info(
        "integrations.microsoft.connected",
        user_id=str(user_id),
    )
    return _success_page()


@router.delete("", status_code=204)
async def disconnect(user_id=Depends(require_user_id)) -> None:
    async with acquire() as conn:
        await links_dao.delete(conn, user_id=user_id)


# --- HTML responses ------------------------------------------------------


def _success_page() -> HTMLResponse:
    return HTMLResponse(
        """
        <!doctype html>
        <html><head><title>Connected — Wolfpaw</title>
        <style>body{font-family:system-ui;max-width:480px;margin:64px auto;
        padding:24px;text-align:center;}</style></head>
        <body>
        <h2>Microsoft Calendar connected</h2>
        <p>Wolfpaw can now read + create events in your Outlook
        calendar.</p>
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
        <h2>Couldn't connect Microsoft</h2>
        <p>{message}</p>
        <p><a href="/">Return to Wolfpaw</a></p>
        </body></html>
        """,
        status_code=400,
    )
