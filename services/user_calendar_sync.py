import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from models.connected_account import ConnectedAccount, list_all_for_sync, upsert_connected_account
from models.meeting_bot import create_session, get_session_by_user_event
from services.meeting_bots.runner import start_meeting_bot_session

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ZOOM_CLIENT_ID       = os.environ.get("ZOOM_OAUTH_CLIENT_ID", "")
ZOOM_CLIENT_SECRET   = os.environ.get("ZOOM_OAUTH_CLIENT_SECRET", "")

_GOOGLE_TOKEN_URL    = "https://oauth2.googleapis.com/token"
_ZOOM_TOKEN_URL      = "https://zoom.us/oauth/token"
_GOOGLE_CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_ZOOM_MEETINGS_URL   = "https://api.zoom.us/v2/users/me/meetings"

SYNC_INTERVAL     = 300   # 5 minutes
LOOKAHEAD_MINUTES = 180   # schedule bot up to 3 hours ahead


def _refresh_google_token(account: ConnectedAccount) -> Optional[str]:
    if not account.refresh_token:
        return None
    res = requests.post(_GOOGLE_TOKEN_URL, data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": account.refresh_token,
        "grant_type": "refresh_token",
    })
    if not res.ok:
        logger.warning("Google token refresh failed for user %s", account.user_id)
        return None
    data = res.json()
    new_token = data["access_token"]
    expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))
    upsert_connected_account(
        user_id=account.user_id, provider="google",
        access_token=new_token, refresh_token=account.refresh_token,
        token_expiry=expiry, email=account.email,
    )
    return new_token


def _refresh_zoom_token(account: ConnectedAccount) -> Optional[str]:
    if not account.refresh_token:
        return None
    import base64
    creds = base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode()
    res = requests.post(
        _ZOOM_TOKEN_URL,
        headers={"Authorization": f"Basic {creds}"},
        data={"grant_type": "refresh_token", "refresh_token": account.refresh_token},
    )
    if not res.ok:
        logger.warning("Zoom token refresh failed for user %s", account.user_id)
        return None
    data = res.json()
    new_token     = data["access_token"]
    new_refresh   = data.get("refresh_token", account.refresh_token)
    expiry        = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))
    upsert_connected_account(
        user_id=account.user_id, provider="zoom",
        access_token=new_token, refresh_token=new_refresh,
        token_expiry=expiry, email=account.email,
    )
    return new_token


def _valid_token(account: ConnectedAccount) -> Optional[str]:
    if account.token_expiry and datetime.utcnow() > account.token_expiry - timedelta(minutes=5):
        return _refresh_google_token(account) if account.provider == "google" else _refresh_zoom_token(account)
    return account.access_token


def _user_workspace(user_id: str) -> Optional[str]:
    from models.user import Workspace
    from db.base import SessionLocal
    with SessionLocal() as db:
        ws = db.query(Workspace).filter(Workspace.owner_id == user_id).first()
        return ws.id if ws else None


def _fetch_google_events(token: str, time_min: str, time_max: str):
    return requests.get(
        _GOOGLE_CALENDAR_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                "orderBy": "startTime", "maxResults": 20},
    )


def _sync_google(account: ConnectedAccount):
    token = _valid_token(account)
    if not token:
        return

    now      = datetime.utcnow()
    # Look back 10 minutes so meetings that just started (between sync ticks) are not missed
    time_min = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(minutes=LOOKAHEAD_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")

    res = _fetch_google_events(token, time_min, time_max)

    if res.status_code == 401:
        # Token expired mid-flight — force a refresh and retry once
        token = _refresh_google_token(account)
        if not token:
            logger.warning("Google Calendar: token refresh failed for user %s", account.user_id)
            return
        res = _fetch_google_events(token, time_min, time_max)

    if not res.ok:
        try:
            detail = res.json().get("error", {})
            reason = detail.get("errors", [{}])[0].get("reason", "") or detail.get("status", "")
            message = detail.get("message", res.text[:200])
        except Exception:
            reason, message = "", res.text[:200]
        logger.warning(
            "Google Calendar fetch failed for user %s: %s%s — %s",
            account.user_id, res.status_code,
            f" ({reason})" if reason else "",
            message,
        )
        return

    workspace_id = _user_workspace(account.user_id)

    for event in res.json().get("items", []):
        event_id = event.get("id")
        if not event_id:
            continue

        # Extract Google Meet link
        meeting_url = event.get("hangoutLink")
        for entry in event.get("conferenceData", {}).get("entryPoints", []):
            if entry.get("entryPointType") == "video" and "meet.google.com" in entry.get("uri", ""):
                meeting_url = entry["uri"]
                break
        if not meeting_url:
            continue

        if get_session_by_user_event(account.user_id, event_id):
            continue

        start_str = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date")
        if not start_str:
            continue
        try:
            scheduled = datetime.fromisoformat(start_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue

        duration = None
        end_str = (event.get("end") or {}).get("dateTime")
        if end_str:
            try:
                end_dt   = datetime.fromisoformat(end_str.replace("Z", "+00:00")).replace(tzinfo=None)
                duration = max(1, int((end_dt - scheduled).total_seconds() / 60))
            except ValueError:
                pass

        session = create_session(
            user_id=account.user_id, workspace_id=workspace_id,
            platform="google_meet", meeting_url=meeting_url,
            bot_name="MinuteAI Recorder", calendar_event_id=event_id,
            source="user_calendar", title=event.get("summary") or "Meeting",
            scheduled_for=scheduled, duration_minutes=duration,
        )
        start_meeting_bot_session(session.id)
        logger.info("Scheduled bot for Google Meet event %s (user %s)", event_id, account.user_id)


def _sync_zoom(account: ConnectedAccount):
    token = _valid_token(account)
    if not token:
        return

    res = requests.get(
        _ZOOM_MEETINGS_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"type": "scheduled", "page_size": 30},
    )
    if not res.ok:
        logger.warning("Zoom meetings fetch failed for user %s: %s", account.user_id, res.status_code)
        return

    now          = datetime.utcnow()
    lookahead    = now + timedelta(minutes=LOOKAHEAD_MINUTES)
    workspace_id = _user_workspace(account.user_id)

    for meeting in res.json().get("meetings", []):
        event_id = str(meeting.get("id", ""))
        join_url = meeting.get("join_url")
        if not event_id or not join_url:
            continue

        start_str = meeting.get("start_time")
        if not start_str:
            continue
        try:
            scheduled = datetime.fromisoformat(start_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue

        if not (now <= scheduled <= lookahead):
            continue

        if get_session_by_user_event(account.user_id, event_id):
            continue

        session = create_session(
            user_id=account.user_id, workspace_id=workspace_id,
            platform="zoom", meeting_url=join_url,
            bot_name="MinuteAI Recorder", calendar_event_id=event_id,
            source="user_calendar", title=meeting.get("topic") or "Zoom Meeting",
            scheduled_for=scheduled, duration_minutes=meeting.get("duration"),
        )
        start_meeting_bot_session(session.id)
        logger.info("Scheduled bot for Zoom meeting %s (user %s)", event_id, account.user_id)


def sync_all_users():
    for account in list_all_for_sync():
        try:
            if account.provider == "google":
                _sync_google(account)
            elif account.provider == "zoom":
                _sync_zoom(account)
        except Exception:
            logger.exception("Calendar sync error for user %s / %s", account.user_id, account.provider)


def _sync_loop():
    while True:
        try:
            sync_all_users()
        except Exception:
            logger.exception("User calendar sync loop error")
        time.sleep(SYNC_INTERVAL)


def start_user_calendar_sync_loop():
    t = threading.Thread(target=_sync_loop, name="user-calendar-sync", daemon=True)
    t.start()
    logger.info("User calendar sync loop started (interval=%ds)", SYNC_INTERVAL)
