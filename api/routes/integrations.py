import base64
import os
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from middleware.auth import get_current_user
from models.connected_account import (
    delete_connected_account,
    get_connected_account,
    list_connected_accounts,
    update_auto_join,
    upsert_connected_account,
)
from models.user import User

router = APIRouter(prefix="/integrations", tags=["integrations"])

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ZOOM_CLIENT_ID       = os.environ.get("ZOOM_OAUTH_CLIENT_ID", "")
ZOOM_CLIENT_SECRET   = os.environ.get("ZOOM_OAUTH_CLIENT_SECRET", "")
BACKEND_URL          = os.environ.get("BACKEND_URL", "http://localhost:8000")
FRONTEND_URL         = os.environ.get("APP_URL", "http://localhost:5173")
JWT_SECRET           = os.environ.get("JWT_SECRET", "dev_secret")

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_SCOPES    = "openid email https://www.googleapis.com/auth/calendar.readonly"

_ZOOM_AUTH_URL  = "https://zoom.us/oauth/authorize"
_ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"


def _make_state(user_id: str, origin: str = "") -> str:
    from jose import jwt
    return jwt.encode({"sub": user_id, "ori": origin, "exp": time.time() + 600}, JWT_SECRET, algorithm="HS256")


def _decode_state(state: str) -> Optional[dict]:
    from jose import jwt, JWTError
    try:
        payload = jwt.decode(state, JWT_SECRET, algorithms=["HS256"])
        return {"user_id": payload["sub"], "origin": payload.get("ori", "")}
    except JWTError:
        return None


class UpdateIntegrationBody(BaseModel):
    auto_join: Optional[bool] = None


@router.get("")
def get_integrations(user: User = Depends(get_current_user)):
    accounts = list_connected_accounts(user.id)
    result = {a.provider: {"connected": True, "email": a.email, "auto_join": a.auto_join} for a in accounts}
    for provider in ("google", "zoom"):
        result.setdefault(provider, {"connected": False, "email": None, "auto_join": True})
    return result


# ── Google ────────────────────────────────────────────────────────────────────

@router.get("/connect/google")
def connect_google(origin: str = Query(default=""), user: User = Depends(get_current_user)):
    state = _make_state(user.id, origin)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/google",
        "response_type": "code",
        "scope": _GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return {"url": f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"}


@router.get("/callback/google")
def callback_google(code: str = None, state: str = None, error: str = None):
    decoded = _decode_state(state) if state else None
    base = (decoded or {}).get("origin") or FRONTEND_URL

    if error or not code or not state:
        return RedirectResponse(f"{base}/settings?tab=integrations&error=google_denied")

    if not decoded:
        return RedirectResponse(f"{base}/settings?tab=integrations&error=invalid_state")

    user_id = decoded["user_id"]

    token_res = requests.post(_GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/google",
        "grant_type": "authorization_code",
    })
    if not token_res.ok:
        return RedirectResponse(f"{base}/settings?tab=integrations&error=google_token")

    tokens = token_res.json()
    access_token  = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    expiry        = datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600))

    email = None
    info_res = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if info_res.ok:
        email = info_res.json().get("email")

    upsert_connected_account(
        user_id=user_id, provider="google",
        access_token=access_token, refresh_token=refresh_token,
        token_expiry=expiry, email=email,
    )
    return RedirectResponse(f"{base}/settings?tab=integrations&connected=google")


# ── Zoom ──────────────────────────────────────────────────────────────────────

@router.get("/connect/zoom")
def connect_zoom(origin: str = Query(default=""), user: User = Depends(get_current_user)):
    state = _make_state(user.id, origin)
    params = {
        "client_id": ZOOM_CLIENT_ID,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/zoom",
        "response_type": "code",
        "state": state,
    }
    return {"url": f"{_ZOOM_AUTH_URL}?{urllib.parse.urlencode(params)}"}


@router.get("/callback/zoom")
def callback_zoom(code: str = None, state: str = None, error: str = None):
    decoded = _decode_state(state) if state else None
    base = (decoded or {}).get("origin") or FRONTEND_URL

    if error or not code or not state:
        return RedirectResponse(f"{base}/settings?tab=integrations&error=zoom_denied")

    if not decoded:
        return RedirectResponse(f"{base}/settings?tab=integrations&error=invalid_state")

    user_id = decoded["user_id"]

    credentials = base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode()
    token_res = requests.post(
        _ZOOM_TOKEN_URL,
        headers={"Authorization": f"Basic {credentials}"},
        data={
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": f"{BACKEND_URL}/integrations/callback/zoom",
        },
    )
    if not token_res.ok:
        return RedirectResponse(f"{base}/settings?tab=integrations&error=zoom_token")

    tokens = token_res.json()
    access_token  = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    expiry        = datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600))

    email = None
    me_res = requests.get(
        "https://api.zoom.us/v2/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if me_res.ok:
        email = me_res.json().get("email")

    upsert_connected_account(
        user_id=user_id, provider="zoom",
        access_token=access_token, refresh_token=refresh_token,
        token_expiry=expiry, email=email,
    )
    return RedirectResponse(f"{base}/settings?tab=integrations&connected=zoom")


# ── Manage ────────────────────────────────────────────────────────────────────

@router.delete("/{provider}")
def disconnect_integration(provider: str, user: User = Depends(get_current_user)):
    if provider not in ("google", "zoom"):
        raise HTTPException(status_code=400, detail="Unknown provider")
    delete_connected_account(user.id, provider)
    return {"ok": True}


@router.patch("/{provider}")
def update_integration(provider: str, body: UpdateIntegrationBody, user: User = Depends(get_current_user)):
    if provider not in ("google", "zoom"):
        raise HTTPException(status_code=400, detail="Unknown provider")
    if body.auto_join is not None:
        account = update_auto_join(user.id, provider, body.auto_join)
        if not account:
            raise HTTPException(status_code=404, detail="Integration not connected")
    account = get_connected_account(user.id, provider)
    if not account:
        raise HTTPException(status_code=404, detail="Integration not connected")
    return {"connected": True, "email": account.email, "auto_join": account.auto_join}
