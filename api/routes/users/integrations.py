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
    update_integration_settings,
    upsert_connected_account,
)
from models.user import User

router = APIRouter(prefix="/integrations", tags=["integrations"])

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ZOOM_CLIENT_ID       = os.environ.get("ZOOM_OAUTH_CLIENT_ID", "")
ZOOM_CLIENT_SECRET   = os.environ.get("ZOOM_OAUTH_CLIENT_SECRET", "")
SLACK_CLIENT_ID      = os.environ.get("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET  = os.environ.get("SLACK_CLIENT_SECRET", "")
NOTION_CLIENT_ID     = os.environ.get("NOTION_CLIENT_ID", "")
NOTION_CLIENT_SECRET = os.environ.get("NOTION_CLIENT_SECRET", "")
HUBSPOT_CLIENT_ID    = os.environ.get("HUBSPOT_CLIENT_ID", "")
HUBSPOT_CLIENT_SECRET = os.environ.get("HUBSPOT_CLIENT_SECRET", "")
LINEAR_CLIENT_ID     = os.environ.get("LINEAR_CLIENT_ID", "")
LINEAR_CLIENT_SECRET = os.environ.get("LINEAR_CLIENT_SECRET", "")
MICROSOFT_CLIENT_ID  = os.environ.get("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
BACKEND_URL          = os.environ.get("BACKEND_URL", "http://localhost:8000")
FRONTEND_URL         = os.environ.get("APP_URL", "http://localhost:5173")
JWT_SECRET           = os.environ.get("JWT_SECRET", "dev_secret")

_GOOGLE_AUTH_URL      = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL     = "https://oauth2.googleapis.com/token"
_GOOGLE_CALENDAR_URL  = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_GOOGLE_SCOPES        = "openid email https://www.googleapis.com/auth/calendar.readonly"
_GOOGLE_DRIVE_SCOPES  = "openid email https://www.googleapis.com/auth/drive.file"

_ZOOM_AUTH_URL  = "https://zoom.us/oauth/authorize"
_ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"

_SLACK_AUTH_URL  = "https://slack.com/oauth/v2/authorize"
_SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"

_NOTION_AUTH_URL  = "https://api.notion.com/v1/oauth/authorize"
_NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"

_HUBSPOT_AUTH_URL  = "https://app.hubspot.com/oauth/authorize"
_HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"

_LINEAR_AUTH_URL  = "https://linear.app/oauth/authorize"
_LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"

_MICROSOFT_AUTH_URL  = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


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


class IntegrationSettingsIn(BaseModel):
    database_id: Optional[str] = None       # Notion
    folder_id: Optional[str] = None         # Google Drive
    team_id: Optional[str] = None           # Linear
    channel_id: Optional[str] = None        # Slack
    url: Optional[str] = None               # Webhook
    bot_join_rule: Optional[str] = None     # 'all' | 'owned' | 'team_only' | 'manual'
    auto_post: Optional[bool] = None        # auto-post to messaging integrations
    post_categories: Optional[list[str]] = None  # which meeting categories to post
    post_sections: Optional[list[str]] = None    # which sections to include in post


class RoutingDestination(BaseModel):
    id: str
    name: str
    webhook_url: str
    categories: list[str] = []
    keywords: list[str] = []
    is_default: bool = False


class RoutingBody(BaseModel):
    destinations: list[RoutingDestination]


class WebhookConnectBody(BaseModel):
    url: str


@router.get("")
def get_integrations(user: User = Depends(get_current_user)):
    accounts = list_connected_accounts(user.id)
    result = {
        a.provider: {
            "connected": True,
            "email": a.email,
            "auto_join": a.auto_join,
            "settings": a.settings,
        }
        for a in accounts
    }
    for provider in ("google", "zoom"):
        result.setdefault(provider, {"connected": False, "email": None, "auto_join": True, "settings": {}})
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
        return RedirectResponse(f"{base}/integrations?error=google_denied")

    if not decoded:
        return RedirectResponse(f"{base}/integrations?error=invalid_state")

    user_id = decoded["user_id"]

    token_res = requests.post(_GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/google",
        "grant_type": "authorization_code",
    })
    if not token_res.ok:
        return RedirectResponse(f"{base}/integrations?error=google_token")

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
    return RedirectResponse(f"{base}/integrations?connected=google")


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
        return RedirectResponse(f"{base}/integrations?error=zoom_denied")

    if not decoded:
        return RedirectResponse(f"{base}/integrations?error=invalid_state")

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
        return RedirectResponse(f"{base}/integrations?error=zoom_token")

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
        settings={"scope_v2": True},
    )
    return RedirectResponse(f"{base}/integrations?connected=zoom")


# ── Slack ─────────────────────────────────────────────────────────────────────

@router.get("/connect/slack")
def connect_slack(origin: str = Query(default=""), user: User = Depends(get_current_user)):
    state = _make_state(user.id, origin)
    params = {
        "client_id": SLACK_CLIENT_ID,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/slack",
        "scope": "incoming-webhook",
        "state": state,
    }
    return {"url": f"{_SLACK_AUTH_URL}?{urllib.parse.urlencode(params)}"}


@router.get("/callback/slack")
def callback_slack(code: str = None, state: str = None, error: str = None):
    decoded = _decode_state(state) if state else None
    base = (decoded or {}).get("origin") or FRONTEND_URL

    if error or not code or not state:
        return RedirectResponse(f"{base}/integrations?error=slack_denied")

    if not decoded:
        return RedirectResponse(f"{base}/integrations?error=invalid_state")

    user_id = decoded["user_id"]

    credentials = base64.b64encode(f"{SLACK_CLIENT_ID}:{SLACK_CLIENT_SECRET}".encode()).decode()
    token_res = requests.post(
        _SLACK_TOKEN_URL,
        headers={"Authorization": f"Basic {credentials}"},
        data={
            "code": code,
            "redirect_uri": f"{BACKEND_URL}/integrations/callback/slack",
            "grant_type": "authorization_code",
        },
    )
    if not token_res.ok:
        return RedirectResponse(f"{base}/integrations?error=slack_token")

    tokens = token_res.json()
    if not tokens.get("ok"):
        return RedirectResponse(f"{base}/integrations?error=slack_token")

    access_token = tokens["access_token"]
    webhook = tokens.get("incoming_webhook", {})
    settings = {
        "webhook_url": webhook.get("url", ""),
        "channel": webhook.get("channel", ""),
        "channel_id": webhook.get("channel_id", ""),
    }
    email = tokens.get("team", {}).get("name")  # team name used as display label

    upsert_connected_account(
        user_id=user_id, provider="slack",
        access_token=access_token, refresh_token=None,
        token_expiry=None, email=email,
        settings=settings,
    )
    return RedirectResponse(f"{base}/integrations?connected=slack")


# ── Notion ────────────────────────────────────────────────────────────────────

@router.get("/connect/notion")
def connect_notion(origin: str = Query(default=""), user: User = Depends(get_current_user)):
    state = _make_state(user.id, origin)
    params = {
        "client_id": NOTION_CLIENT_ID,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/notion",
        "response_type": "code",
        "state": state,
    }
    return {"url": f"{_NOTION_AUTH_URL}?{urllib.parse.urlencode(params)}"}


@router.get("/callback/notion")
def callback_notion(code: str = None, state: str = None, error: str = None):
    decoded = _decode_state(state) if state else None
    base = (decoded or {}).get("origin") or FRONTEND_URL

    if error or not code or not state:
        return RedirectResponse(f"{base}/integrations?error=notion_denied")

    if not decoded:
        return RedirectResponse(f"{base}/integrations?error=invalid_state")

    user_id = decoded["user_id"]

    credentials = base64.b64encode(f"{NOTION_CLIENT_ID}:{NOTION_CLIENT_SECRET}".encode()).decode()
    token_res = requests.post(
        _NOTION_TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        },
        json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{BACKEND_URL}/integrations/callback/notion",
        },
    )
    if not token_res.ok:
        return RedirectResponse(f"{base}/integrations?error=notion_token")

    tokens = token_res.json()
    access_token = tokens["access_token"]

    owner = tokens.get("owner", {})
    owner_user = owner.get("user", {})
    email = owner_user.get("person", {}).get("email") or owner_user.get("name")
    if not email:
        email = tokens.get("workspace_name")

    upsert_connected_account(
        user_id=user_id, provider="notion",
        access_token=access_token, refresh_token=None,
        token_expiry=None, email=email,
    )
    return RedirectResponse(f"{base}/integrations?connected=notion")


# ── HubSpot ───────────────────────────────────────────────────────────────────

@router.get("/connect/hubspot")
def connect_hubspot(origin: str = Query(default=""), user: User = Depends(get_current_user)):
    state = _make_state(user.id, origin)
    params = {
        "client_id": HUBSPOT_CLIENT_ID,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/hubspot",
        "scope": "crm.objects.notes.write",
        "state": state,
    }
    return {"url": f"{_HUBSPOT_AUTH_URL}?{urllib.parse.urlencode(params)}"}


@router.get("/callback/hubspot")
def callback_hubspot(code: str = None, state: str = None, error: str = None):
    decoded = _decode_state(state) if state else None
    base = (decoded or {}).get("origin") or FRONTEND_URL

    if error or not code or not state:
        return RedirectResponse(f"{base}/integrations?error=hubspot_denied")

    if not decoded:
        return RedirectResponse(f"{base}/integrations?error=invalid_state")

    user_id = decoded["user_id"]

    token_res = requests.post(
        _HUBSPOT_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": HUBSPOT_CLIENT_ID,
            "client_secret": HUBSPOT_CLIENT_SECRET,
            "redirect_uri": f"{BACKEND_URL}/integrations/callback/hubspot",
            "code": code,
        },
    )
    if not token_res.ok:
        return RedirectResponse(f"{base}/integrations?error=hubspot_token")

    tokens = token_res.json()
    access_token  = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    expiry        = datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600))

    email = None
    info_res = requests.get(f"https://api.hubapi.com/oauth/v1/access-tokens/{access_token}")
    if info_res.ok:
        email = info_res.json().get("user")

    upsert_connected_account(
        user_id=user_id, provider="hubspot",
        access_token=access_token, refresh_token=refresh_token,
        token_expiry=expiry, email=email,
    )
    return RedirectResponse(f"{base}/integrations?connected=hubspot")


# ── Linear ────────────────────────────────────────────────────────────────────

@router.get("/connect/linear")
def connect_linear(origin: str = Query(default=""), user: User = Depends(get_current_user)):
    state = _make_state(user.id, origin)
    params = {
        "client_id": LINEAR_CLIENT_ID,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/linear",
        "response_type": "code",
        "scope": "read,write",
        "state": state,
    }
    return {"url": f"{_LINEAR_AUTH_URL}?{urllib.parse.urlencode(params)}"}


@router.get("/callback/linear")
def callback_linear(code: str = None, state: str = None, error: str = None):
    decoded = _decode_state(state) if state else None
    base = (decoded or {}).get("origin") or FRONTEND_URL

    if error or not code or not state:
        return RedirectResponse(f"{base}/integrations?error=linear_denied")

    if not decoded:
        return RedirectResponse(f"{base}/integrations?error=invalid_state")

    user_id = decoded["user_id"]

    token_res = requests.post(
        _LINEAR_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": LINEAR_CLIENT_ID,
            "client_secret": LINEAR_CLIENT_SECRET,
            "redirect_uri": f"{BACKEND_URL}/integrations/callback/linear",
            "code": code,
        },
    )
    if not token_res.ok:
        return RedirectResponse(f"{base}/integrations?error=linear_token")

    tokens = token_res.json()
    access_token  = tokens["access_token"]
    expiry_secs   = tokens.get("expires_in")
    expiry        = datetime.utcnow() + timedelta(seconds=expiry_secs) if expiry_secs else None

    email = None
    viewer_res = requests.post(
        "https://api.linear.app/graphql",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"query": "{ viewer { id name email } }"},
        timeout=10,
    )
    if viewer_res.ok:
        viewer = viewer_res.json().get("data", {}).get("viewer", {})
        email = viewer.get("email") or viewer.get("name")

    # Fetch first team and store in settings
    settings = {}
    try:
        from services.integrations.linear import list_teams
        teams = list_teams(access_token)
        if teams:
            settings = {"team_id": teams[0]["id"], "team_name": teams[0]["name"]}
    except Exception:
        pass

    upsert_connected_account(
        user_id=user_id, provider="linear",
        access_token=access_token, refresh_token=None,
        token_expiry=expiry, email=email,
        settings=settings,
    )
    return RedirectResponse(f"{base}/integrations?connected=linear")


# ── Outlook (Microsoft) ───────────────────────────────────────────────────────

@router.get("/connect/outlook")
def connect_outlook(origin: str = Query(default=""), user: User = Depends(get_current_user)):
    state = _make_state(user.id, origin)
    params = {
        "client_id": MICROSOFT_CLIENT_ID,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/outlook",
        "response_type": "code",
        "scope": "User.Read Calendars.Read offline_access",
        "state": state,
    }
    return {"url": f"{_MICROSOFT_AUTH_URL}?{urllib.parse.urlencode(params)}"}


@router.get("/callback/outlook")
def callback_outlook(code: str = None, state: str = None, error: str = None):
    decoded = _decode_state(state) if state else None
    base = (decoded or {}).get("origin") or FRONTEND_URL

    if error or not code or not state:
        return RedirectResponse(f"{base}/integrations?error=outlook_denied")

    if not decoded:
        return RedirectResponse(f"{base}/integrations?error=invalid_state")

    user_id = decoded["user_id"]

    token_res = requests.post(
        _MICROSOFT_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": MICROSOFT_CLIENT_ID,
            "client_secret": MICROSOFT_CLIENT_SECRET,
            "redirect_uri": f"{BACKEND_URL}/integrations/callback/outlook",
            "code": code,
            "scope": "User.Read Calendars.Read offline_access",
        },
    )
    if not token_res.ok:
        return RedirectResponse(f"{base}/integrations?error=outlook_token")

    tokens = token_res.json()
    access_token  = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    expiry        = datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600))

    email = None
    me_res = requests.get(
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if me_res.ok:
        me = me_res.json()
        email = me.get("mail") or me.get("userPrincipalName")

    upsert_connected_account(
        user_id=user_id, provider="outlook",
        access_token=access_token, refresh_token=refresh_token,
        token_expiry=expiry, email=email,
    )
    return RedirectResponse(f"{base}/integrations?connected=outlook")


# ── Google Drive ──────────────────────────────────────────────────────────────

@router.get("/connect/google_drive")
def connect_google_drive(origin: str = Query(default=""), user: User = Depends(get_current_user)):
    state = _make_state(user.id, origin)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/google_drive",
        "response_type": "code",
        "scope": _GOOGLE_DRIVE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return {"url": f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"}


@router.get("/callback/google_drive")
def callback_google_drive(code: str = None, state: str = None, error: str = None):
    decoded = _decode_state(state) if state else None
    base = (decoded or {}).get("origin") or FRONTEND_URL

    if error or not code or not state:
        return RedirectResponse(f"{base}/integrations?error=google_drive_denied")

    if not decoded:
        return RedirectResponse(f"{base}/integrations?error=invalid_state")

    user_id = decoded["user_id"]

    token_res = requests.post(_GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": f"{BACKEND_URL}/integrations/callback/google_drive",
        "grant_type": "authorization_code",
    })
    if not token_res.ok:
        return RedirectResponse(f"{base}/integrations?error=google_drive_token")

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
        user_id=user_id, provider="google_drive",
        access_token=access_token, refresh_token=refresh_token,
        token_expiry=expiry, email=email,
    )
    return RedirectResponse(f"{base}/integrations?connected=google_drive")


# ── Webhook (no OAuth) ────────────────────────────────────────────────────────

@router.post("/webhook/connect")
def connect_webhook(body: WebhookConnectBody, user: User = Depends(get_current_user)):
    upsert_connected_account(
        user_id=user.id, provider="webhook",
        access_token="", refresh_token=None,
        token_expiry=None, email=None,
        settings={"url": body.url},
    )
    return {"ok": True}


# ── Microsoft Teams (incoming webhook — no OAuth) ─────────────────────────────

class TeamsConnectBody(BaseModel):
    webhook_url: str


@router.post("/teams/connect")
def connect_teams(body: TeamsConnectBody, user: User = Depends(get_current_user)):
    """Store a Teams incoming webhook URL for the user."""
    if not body.webhook_url.strip():
        raise HTTPException(400, "webhook_url is required")
    upsert_connected_account(
        user_id=user.id, provider="teams",
        access_token="", refresh_token=None,
        token_expiry=None, email=None,
        settings={"webhook_url": body.webhook_url.strip()},
    )
    return {"ok": True}


@router.delete("/teams/disconnect")
def disconnect_teams(user: User = Depends(get_current_user)):
    delete_connected_account(user.id, "teams")
    return {"ok": True}


# ── Settings ──────────────────────────────────────────────────────────────────

@router.patch("/{provider}/settings")
def update_settings(provider: str, body: IntegrationSettingsIn, user: User = Depends(get_current_user)):
    updates = {k: v for k, v in body.dict().items() if v is not None}
    # also allow explicit False / empty list overrides
    for k, v in body.dict().items():
        if v is False or v == []:
            updates[k] = v
    if not updates:
        raise HTTPException(400, "No settings provided")
    account = update_integration_settings(user.id, provider, updates)
    if not account:
        raise HTTPException(404, "Integration not connected")
    return {"ok": True, "settings": account.settings}


@router.put("/{provider}/routing")
def update_routing(provider: str, body: RoutingBody, user: User = Depends(get_current_user)):
    """Save multi-destination routing rules for an integration."""
    account = get_connected_account(user.id, provider)
    if not account:
        raise HTTPException(404, "Integration not connected")
    destinations = [d.dict() for d in body.destinations]
    account = update_integration_settings(user.id, provider, {"destinations": destinations})
    return {"ok": True, "destinations": destinations}


@router.get("/{provider}/resources")
def get_resources(provider: str, user: User = Depends(get_current_user)):
    """Return available resources for config (channels, databases, teams)."""
    account = get_connected_account(user.id, provider)
    if not account:
        raise HTTPException(404, "Not connected")
    if provider == "notion":
        import requests as _req
        resp = _req.post(
            "https://api.notion.com/v1/search",
            headers={"Authorization": f"Bearer {account.access_token}", "Notion-Version": "2022-06-28"},
            json={"filter": {"value": "database", "property": "object"}},
            timeout=10,
        )
        dbs = [
            {"id": d["id"], "name": d.get("title", [{}])[0].get("plain_text", "Untitled")}
            for d in resp.json().get("results", [])
            if resp.ok
        ]
        return {"resources": dbs}
    elif provider == "linear":
        from services.integrations.linear import list_teams
        return {"resources": list_teams(account.access_token)}
    elif provider == "google_drive":
        from services.integrations.google_drive import list_folders
        return {"resources": list_folders(account.access_token)}
    return {"resources": []}


# ── Zoom upcoming meetings ────────────────────────────────────────────────────

@router.get("/zoom/upcoming")
def get_upcoming_zoom_meetings(user: User = Depends(get_current_user)):
    account = get_connected_account(user.id, "zoom")
    if not account:
        return {"meetings": [], "connected": False}

    access_token = account.access_token

    # Refresh if within 5 minutes of expiry
    if account.token_expiry and (account.token_expiry - datetime.utcnow()).total_seconds() < 300:
        if account.refresh_token:
            credentials = base64.b64encode(
                f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()
            ).decode()
            r = requests.post(
                _ZOOM_TOKEN_URL,
                headers={"Authorization": f"Basic {credentials}"},
                data={"grant_type": "refresh_token", "refresh_token": account.refresh_token},
            )
            if r.ok:
                td = r.json()
                access_token = td["access_token"]
                expiry = datetime.utcnow() + timedelta(seconds=td.get("expires_in", 3600))
                upsert_connected_account(
                    user_id=user.id, provider="zoom",
                    access_token=access_token,
                    refresh_token=td.get("refresh_token", account.refresh_token),
                    token_expiry=expiry, email=account.email,
                )

    res = requests.get(
        "https://api.zoom.us/v2/users/me/meetings",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"type": "upcoming", "page_size": 20},
    )
    if not res.ok:
        return {"meetings": [], "connected": True}

    meetings = []
    for m in res.json().get("meetings", []):
        meetings.append({
            "id": str(m["id"]),
            "title": m.get("topic", "Untitled Meeting"),
            "start": m.get("start_time", ""),
            "join_url": m.get("join_url", ""),
            "duration": m.get("duration"),
        })

    return {"meetings": meetings, "connected": True}


# ── Calendar ──────────────────────────────────────────────────────────────────

@router.get("/calendar/upcoming")
def get_upcoming_calendar_events(
    days: int = Query(default=7, ge=1, le=14),
    user: User = Depends(get_current_user),
):
    from models.meeting_bot import get_session_by_user_event

    account = get_connected_account(user.id, "google")
    if not account:
        return {"events": [], "connected": False, "auto_join": True}

    access_token = account.access_token

    # Refresh if token is within 5 minutes of expiry
    if account.token_expiry and (account.token_expiry - datetime.utcnow()).total_seconds() < 300:
        if account.refresh_token:
            r = requests.post(_GOOGLE_TOKEN_URL, data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": account.refresh_token,
                "grant_type": "refresh_token",
            })
            if r.ok:
                td = r.json()
                access_token = td["access_token"]
                expiry = datetime.utcnow() + timedelta(seconds=td.get("expires_in", 3600))
                upsert_connected_account(
                    user_id=user.id, provider="google",
                    access_token=access_token, refresh_token=account.refresh_token,
                    token_expiry=expiry, email=account.email,
                )

    now = datetime.utcnow()
    time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    cal_res = requests.get(
        _GOOGLE_CALENDAR_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 20,
        },
    )
    if not cal_res.ok:
        return {"events": [], "connected": True, "auto_join": account.auto_join}

    items = cal_res.json().get("items", [])
    events = []
    for item in items:
        meet_url = None
        if item.get("hangoutLink"):
            meet_url = item["hangoutLink"]
        elif item.get("conferenceData"):
            for ep in item["conferenceData"].get("entryPoints", []):
                if ep.get("entryPointType") == "video":
                    meet_url = ep.get("uri")
                    break

        if not meet_url:
            continue

        event_id = item["id"]
        start_raw = item.get("start", {})
        start_str = start_raw.get("dateTime") or start_raw.get("date", "")

        session = get_session_by_user_event(user.id, event_id)
        recording_status = session.status if session else None

        attendees = [
            a.get("email", "") for a in item.get("attendees", []) if not a.get("self")
        ]

        events.append({
            "id": event_id,
            "title": item.get("summary", "Untitled Meeting"),
            "start": start_str,
            "meet_url": meet_url,
            "recording_status": recording_status,
            "attendees": attendees,
        })

    return {"events": events, "connected": True, "auto_join": account.auto_join}


# ── Manage ────────────────────────────────────────────────────────────────────

_ALL_PROVIDERS = {
    "google", "zoom", "slack", "notion", "hubspot",
    "linear", "outlook", "webhook", "google_drive",
}


@router.delete("/{provider}")
def disconnect_integration(provider: str, user: User = Depends(get_current_user)):
    if provider not in _ALL_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unknown provider")
    delete_connected_account(user.id, provider)
    return {"ok": True}


@router.patch("/{provider}")
def update_integration(provider: str, body: UpdateIntegrationBody, user: User = Depends(get_current_user)):
    if provider not in _ALL_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unknown provider")
    if body.auto_join is not None:
        account = update_auto_join(user.id, provider, body.auto_join)
        if not account:
            raise HTTPException(status_code=404, detail="Integration not connected")
    account = get_connected_account(user.id, provider)
    if not account:
        raise HTTPException(status_code=404, detail="Integration not connected")
    return {"connected": True, "email": account.email, "auto_join": account.auto_join}
