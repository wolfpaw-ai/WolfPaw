"""Dropbox OAuth + integration management routes (step 29).

Three endpoints:

  * ``GET /integrations/dropbox/install-url`` — authenticated. Mints a
    state token bound to the Wolfpaw user, returns the Dropbox
    authorize URL the React app redirects to.
  * ``GET /integrations/dropbox/oauth/callback`` — Dropbox redirects
    here after the user grants access. Validates the state token,
    exchanges the code for tokens, persists, then renders a tiny HTML
    success page with a "Return to Wolfpaw" link.
  * ``DELETE /integrations/dropbox`` — authenticated. Removes the
    user's stored tokens (disconnect).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.responses import HTMLResponse, RedirectResponse

from wolfpaw.auth.deps import require_user_id
from wolfpaw.config import get_settings
from wolfpaw.integrations.dropbox import client as dbx_client
from wolfpaw.integrations.oauth_state import (
    StateTokenError, consume, issue,
)
from wolfpaw.memory import dropbox_links as links_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()
router = APIRouter(prefix="/integrations/dropbox", tags=["integrations"])

_PROVIDER = "dropbox"


def _redirect_uri() -> str:
    settings = get_settings()
    return f"{settings.web_base_url.rstrip('/')}/integrations/dropbox/oauth/callback"


@router.get("/install-url")
async def install_url(user_id=Depends(require_user_id)) -> dict[str, str]:
    """Return the URL the React app redirects the user to in order to
    grant Wolfpaw Dropbox access. Mints a fresh state token bound to
    the authenticated user."""
    settings = get_settings()
    if not (settings.dropbox_client_id and settings.dropbox_client_secret):
        raise HTTPException(
            status_code=503,
            detail="Dropbox integration not configured on this deployment",
        )
    async with acquire() as conn:
        state = await issue(
            conn,
            user_id=user_id,
            provider=_PROVIDER,
            ttl_minutes=settings.integration_state_ttl_minutes,
        )
    url = dbx_client.build_authorize_url(
        client_id=settings.dropbox_client_id,
        redirect_uri=_redirect_uri(),
        state=state,
    )
    return {"url": url}


@router.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> HTMLResponse:
    """Dropbox redirects here after the user grants (or denies) access.
    Validates the state token, exchanges the code, persists tokens.

    Failure paths render a small HTML page rather than a JSON error
    because the redirect lands the user in their browser — they need
    to see something rendered, not a JSON blob."""
    settings = get_settings()
    async with acquire() as conn:
        try:
            user_id = await consume(
                conn, plain=state, provider=_PROVIDER,
            )
        except StateTokenError as e:
            log.warning("integrations.dropbox.bad_state", reason=str(e))
            return _failure_page(
                "That Dropbox connect link expired or has already been used."
                " Try connecting again from the web app."
            )

    try:
        result = await dbx_client.exchange_code(
            code=code,
            redirect_uri=_redirect_uri(),
            client_id=settings.dropbox_client_id,
            client_secret=settings.dropbox_client_secret,
        )
    except dbx_client.DropboxApiError as e:
        log.warning(
            "integrations.dropbox.exchange_failed",
            status=e.status_code, body=e.body,
        )
        return _failure_page(
            "Dropbox refused to issue tokens. The Wolfpaw operator may"
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
                account_id=result.account_id,
                scope=result.scope,
            )
    except Exception:  # noqa: BLE001
        log.exception(
            "integrations.dropbox.persist_failed",
            user_id=str(user_id),
        )
        return _failure_page(
            "We couldn't save your Dropbox tokens. Try connecting again."
        )

    log.info(
        "integrations.dropbox.connected",
        user_id=str(user_id),
        account_id=result.account_id,
    )
    return _success_page()


@router.delete("", status_code=204)
async def disconnect(user_id=Depends(require_user_id)) -> None:
    """Remove the user's Dropbox tokens. Doesn't revoke the OAuth
    grant on Dropbox's side (that's the user's job in their Dropbox
    settings) — just deletes our local record so subsequent tool
    calls surface "not connected"."""
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
        <h2>Dropbox connected</h2>
        <p>Wolfpaw can now read and write inside your
        <code>/Apps/Wolfpaw/</code> folder.</p>
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
        <h2>Couldn't connect Dropbox</h2>
        <p>{message}</p>
        <p><a href="/">Return to Wolfpaw</a></p>
        </body></html>
        """,
        status_code=400,
    )
