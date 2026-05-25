import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from db.base import SessionLocal
from models.bot_account import BotAccountRecord, list_bot_accounts
from models.connected_account import ConnectedAccountRecord
from models.meeting_bot import create_session, get_session_by_calendar_event
from models.user import User, Workspace, TeamMember
from services.meeting_bots.platforms import detect_platform
from services.meeting_bots.runner import start_meeting_bot_session


CALENDAR_SYNC_ENABLED   = os.environ.get("MEETING_BOT_CALENDAR_SYNC", "").strip().lower() in {"1", "true", "yes"}
SERVICE_ACCOUNT_FILE    = os.environ.get("GOOGLE_CALENDAR_SERVICE_ACCOUNT_FILE", "").strip()
SERVICE_ACCOUNT_JSON    = os.environ.get("GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON", "").strip()
SYNC_INTERVAL_SECONDS   = int(os.environ.get("MEETING_BOT_CALENDAR_SYNC_SECONDS", "60"))
LOOKAHEAD_DAYS          = int(os.environ.get("MEETING_BOT_CALENDAR_LOOKAHEAD_DAYS", "14"))
_SERVICE_ACCOUNT_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


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


# ── Service-account path (legacy / multi-workspace dedicated bots) ─────────────

def _service_account_token(subject: str) -> str:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    if SERVICE_ACCOUNT_JSON:
        info = json.loads(SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=_SERVICE_ACCOUNT_SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=_SERVICE_ACCOUNT_SCOPES)
    delegated = creds.with_subject(subject)
    delegated.refresh(Request())
    return delegated.token


def _list_events(access_token: str) -> list[dict]:
    now = _utcnow()
    params = urllib.parse.urlencode({
        "timeMin":      now.isoformat().replace("+00:00", "Z"),
        "timeMax":      (now + timedelta(days=LOOKAHEAD_DAYS)).isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy":      "startTime",
        "maxResults":   "50",
    })
    req = urllib.request.Request(
        f"https://www.googleapis.com/calendar/v3/calendars/primary/events?{params}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read()).get("items", [])


def sync_bot_calendar(bot_account) -> None:
    """Service-account-based sync for a specific bot account email (legacy path)."""
    token = _service_account_token(bot_account.email)
    with SessionLocal() as db:
        workspace = db.query(Workspace).filter(Workspace.id == bot_account.workspace_id).first()
        owner_id = workspace.owner_id if workspace else bot_account.workspace_id

    for event in _list_events(token):
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
        end   = _parse_google_time(event.get("end",   {}).get("dateTime"))
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


# ── Bot OAuth path (primary — uses stored bot refresh token) ───────────────────

def _bot_oauth_access_token() -> Optional[str]:
    """Exchange the bot's stored refresh token for a fresh access token."""
    try:
        from models.bot_config import load_bot_config
        cfg = load_bot_config()
        refresh_token = cfg.get("bot_refresh_token", "")
        if not refresh_token:
            return None
        client_id     = os.environ.get("GOOGLE_CLIENT_ID", "")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            return None
        data = urllib.parse.urlencode({
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("access_token")
    except Exception:
        return None


def _find_workspace_for_event(event: dict, bot_email: str) -> Optional[tuple]:
    """
    Match event attendees/organizer to a user in the system.
    Returns (user_id, workspace_id, bot_account_id, bot_name) or None.
    """
    candidate_emails: list[str] = []
    organizer = (event.get("organizer") or {}).get("email", "")
    if organizer:
        candidate_emails.append(organizer.lower())
    for att in event.get("attendees") or []:
        e = att.get("email", "")
        if e and e.lower() != bot_email.lower():
            candidate_emails.append(e.lower())

    with SessionLocal() as db:
        for email in candidate_emails:
            user = db.query(User).filter(User.email == email).first()
            if not user:
                # The organiser/attendee may have a different Google account email
                # from the one they registered with — check connected accounts.
                linked = db.query(ConnectedAccountRecord).filter(
                    ConnectedAccountRecord.email == email,
                    ConnectedAccountRecord.provider == "google",
                ).first()
                if linked:
                    user = db.query(User).filter(User.id == linked.user_id).first()
            if not user:
                continue
            # Prefer workspace the user owns
            ws = db.query(Workspace).filter(Workspace.owner_id == user.id).first()
            if not ws:
                member = db.query(TeamMember).filter(TeamMember.user_id == user.id).first()
                if member:
                    ws = db.query(Workspace).filter(Workspace.id == member.workspace_id).first()
            if not ws:
                continue
            bot_rec = db.query(BotAccountRecord).filter(BotAccountRecord.workspace_id == ws.id).first()
            if bot_rec:
                return (user.id, ws.id, bot_rec.id, bot_rec.display_name)

        # Fallback: use the first available workspace bot
        bot_rec = db.query(BotAccountRecord).first()
        if bot_rec:
            ws = db.query(Workspace).filter(Workspace.id == bot_rec.workspace_id).first()
            if ws:
                return (ws.owner_id, ws.id, bot_rec.id, bot_rec.display_name)

    return None


def sync_bot_calendar_oauth() -> None:
    """
    Read the global bot's Google Calendar using its stored OAuth refresh token
    and auto-schedule sessions for any invited meetings.
    """
    from models.bot_config import load_bot_config
    cfg = load_bot_config()
    bot_email = cfg.get("bot_oauth_email") or cfg.get("email") or ""
    if not bot_email:
        return

    access_token = _bot_oauth_access_token()
    if not access_token:
        return

    try:
        events = _list_events(access_token)
    except Exception:
        return

    for event in events:
        if event.get("status") == "cancelled":
            continue
        event_id = event.get("id")
        if not event_id:
            continue

        meeting_url = _event_meeting_url(event)
        if not meeting_url:
            continue
        try:
            platform = detect_platform(meeting_url)
        except ValueError:
            continue

        workspace_info = _find_workspace_for_event(event, bot_email)
        if not workspace_info:
            continue
        user_id, workspace_id, bot_account_id, bot_name = workspace_info

        if get_session_by_calendar_event(bot_account_id, event_id):
            continue

        start = _parse_google_time(event.get("start", {}).get("dateTime"))
        end   = _parse_google_time(event.get("end",   {}).get("dateTime"))
        if not start:
            continue

        duration_minutes = 30
        if end and end > start:
            duration_minutes = max(5, min(480, int((end - start).total_seconds() // 60)))

        session = create_session(
            user_id=user_id,
            workspace_id=workspace_id,
            bot_account_id=bot_account_id,
            calendar_event_id=event_id,
            source="google_calendar",
            platform=platform,
            meeting_url=meeting_url,
            title=event.get("summary") or "Google Calendar meeting",
            bot_name=bot_name,
            scheduled_for=start,
            duration_minutes=duration_minutes,
        )
        start_meeting_bot_session(session.id)


# ── Orchestration ──────────────────────────────────────────────────────────────

def _has_bot_oauth() -> bool:
    try:
        from models.bot_config import load_bot_config
        return bool(load_bot_config().get("bot_refresh_token", ""))
    except Exception:
        return False


def sync_all_bot_calendars_once() -> None:
    if SERVICE_ACCOUNT_FILE or SERVICE_ACCOUNT_JSON:
        # Legacy service-account path — one sync per workspace bot account
        for bot_account in list_bot_accounts():
            try:
                sync_bot_calendar(bot_account)
            except Exception:
                continue
        return

    # Primary OAuth path — reads the single global bot calendar
    if CALENDAR_SYNC_ENABLED or _has_bot_oauth():
        try:
            sync_bot_calendar_oauth()
        except Exception:
            pass


def _loop() -> None:
    while True:
        sync_all_bot_calendars_once()
        time.sleep(max(15, SYNC_INTERVAL_SECONDS))


def start_calendar_sync_loop() -> None:
    # Start the loop if explicitly enabled, a service account is configured,
    # or the bot already has a stored OAuth refresh token.
    if CALENDAR_SYNC_ENABLED or SERVICE_ACCOUNT_FILE or SERVICE_ACCOUNT_JSON or _has_bot_oauth():
        threading.Thread(target=_loop, daemon=True).start()
