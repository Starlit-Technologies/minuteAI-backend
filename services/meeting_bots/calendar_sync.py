import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.oauth2 import service_account
from google.auth.transport.requests import Request

from models.bot_account import list_bot_accounts
from db.base import SessionLocal
from models.meeting_bot import create_session, get_session_by_calendar_event
from models.user import Workspace
from services.meeting_bots.platforms import detect_platform
from services.meeting_bots.runner import start_meeting_bot_session


CALENDAR_SYNC_ENABLED = os.environ.get("MEETING_BOT_CALENDAR_SYNC", "").strip().lower() in {"1", "true", "yes"}
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_CALENDAR_SERVICE_ACCOUNT_FILE", "").strip()
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON", "").strip()
SYNC_INTERVAL_SECONDS = int(os.environ.get("MEETING_BOT_CALENDAR_SYNC_SECONDS", "60"))
LOOKAHEAD_DAYS = int(os.environ.get("MEETING_BOT_CALENDAR_LOOKAHEAD_DAYS", "14"))
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_google_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _credentials_for(subject: str):
    if SERVICE_ACCOUNT_JSON:
        info = json.loads(SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return creds.with_subject(subject)


def _access_token(subject: str) -> str:
    creds = _credentials_for(subject)
    creds.refresh(Request())
    return creds.token


def _event_meeting_url(event: dict) -> Optional[str]:
    if event.get("hangoutLink"):
        return event["hangoutLink"]
    for entry in event.get("conferenceData", {}).get("entryPoints", []) or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"]
    location = event.get("location") or ""
    for part in location.split():
        if part.startswith("https://meet.google.com/") or "zoom.us/" in part:
            return part
    return None


def _list_events(bot_email: str) -> list[dict]:
    now = _utcnow()
    params = urllib.parse.urlencode({
        "timeMin": now.isoformat().replace("+00:00", "Z"),
        "timeMax": (now + timedelta(days=LOOKAHEAD_DAYS)).isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "50",
    })
    req = urllib.request.Request(
        f"https://www.googleapis.com/calendar/v3/calendars/primary/events?{params}",
        headers={"Authorization": f"Bearer {_access_token(bot_email)}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read()).get("items", [])


def sync_bot_calendar(bot_account):
    with SessionLocal() as db:
        workspace = db.query(Workspace).filter(Workspace.id == bot_account.workspace_id).first()
        owner_id = workspace.owner_id if workspace else bot_account.workspace_id

    for event in _list_events(bot_account.email):
        if event.get("status") == "cancelled":
            continue
        event_id = event.get("id")
        if not event_id or get_session_by_calendar_event(bot_account.id, event_id):
            continue

        meeting_url = _event_meeting_url(event)
        if not meeting_url:
            continue

        try:
            platform = detect_platform(meeting_url)
        except ValueError:
            continue

        start = _parse_google_time(event.get("start", {}).get("dateTime"))
        end = _parse_google_time(event.get("end", {}).get("dateTime"))
        if not start:
            continue

        duration_minutes = 30
        if end and end > start:
            duration_minutes = max(5, min(480, int((end - start).total_seconds() // 60)))

        session = create_session(
            user_id=owner_id,
            workspace_id=bot_account.workspace_id,
            bot_account_id=bot_account.id,
            calendar_event_id=event_id,
            source="google_calendar",
            platform=platform,
            meeting_url=meeting_url,
            title=event.get("summary") or "Google Calendar meeting",
            bot_name=bot_account.display_name,
            scheduled_for=start,
            duration_minutes=duration_minutes,
        )
        start_meeting_bot_session(session.id)


def sync_all_bot_calendars_once():
    if not CALENDAR_SYNC_ENABLED or not (SERVICE_ACCOUNT_FILE or SERVICE_ACCOUNT_JSON):
        return
    for bot_account in list_bot_accounts():
        try:
            sync_bot_calendar(bot_account)
        except Exception:
            continue


def _loop():
    while True:
        sync_all_bot_calendars_once()
        time.sleep(max(15, SYNC_INTERVAL_SECONDS))


def start_calendar_sync_loop():
    if not CALENDAR_SYNC_ENABLED or not (SERVICE_ACCOUNT_FILE or SERVICE_ACCOUNT_JSON):
        return
    threading.Thread(target=_loop, daemon=True).start()
