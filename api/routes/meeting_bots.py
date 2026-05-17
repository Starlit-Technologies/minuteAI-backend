import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from middleware.auth import get_current_user, get_db
from models.bot_account import ensure_workspace_bot_account
from models.meeting_bot import (
    create_session,
    get_session,
    list_sessions,
    list_upcoming_workspace_sessions,
    list_workspace_sessions,
    update_session,
)
from models.user import User
from models.user import TeamMember, Workspace
from services.meeting_bots.platforms import detect_platform
from services.meeting_bots.runner import start_meeting_bot_session

router = APIRouter(prefix="/meeting-bots", tags=["meeting-bots"])
MEETING_BOT_RECORDER_NAME = os.environ.get("MEETING_BOT_RECORDER_NAME", "MinuteAI Recorder").strip() or "MinuteAI Recorder"


SUPPORTED_STATUSES = {
    "queued",
    "preparing",
    "joining",
    "waiting_for_host",
    "recording",
    "processing",
    "completed",
    "failed",
    "cancelled",
}


class CreateMeetingBotSessionPayload(BaseModel):
    meeting_url: str = Field(min_length=8)
    title: Optional[str] = Field(default=None, max_length=160)
    workspace_id: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(default=30, ge=5, le=480)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _serialize(session):
    return {
        "id": session.id,
        "bot_account_id": session.bot_account_id,
        "calendar_event_id": session.calendar_event_id,
        "source": session.source,
        "platform": session.platform,
        "meeting_url": session.meeting_url,
        "title": session.title,
        "bot_name": session.bot_name,
        "workspace_id": session.workspace_id,
        "status": session.status,
        "scheduled_for": session.scheduled_for.isoformat() + "Z" if session.scheduled_for else None,
        "duration_minutes": session.duration_minutes,
        "started_at": session.started_at.isoformat() + "Z" if session.started_at else None,
        "ended_at": session.ended_at.isoformat() + "Z" if session.ended_at else None,
        "failure_reason": session.failure_reason,
        "recording_path": session.recording_path,
        "job_id": session.job_id,
        "logs": session.logs,
        "created_at": session.created_at.isoformat() + "Z" if session.created_at else None,
    }


def _workspace_accessible(workspace_id: str, current_user: User, db: Session) -> bool:
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        return False
    if ws.owner_id == current_user.id:
        return True
    return db.query(TeamMember).filter(
        TeamMember.workspace_id == workspace_id,
        TeamMember.user_id == current_user.id,
        TeamMember.status == "active",
    ).first() is not None


def _ensure_workspace_access(workspace_id: str, current_user: User, db: Session):
    if not _workspace_accessible(workspace_id, current_user, db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this workspace.")


@router.post("")
def create_meeting_bot_session(
    payload: CreateMeetingBotSessionPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        platform = detect_platform(payload.meeting_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    bot_account = None
    if payload.workspace_id:
        _ensure_workspace_access(payload.workspace_id, current_user, db)
        bot_account = ensure_workspace_bot_account(payload.workspace_id)

    session = create_session(
        user_id=current_user.id,
        workspace_id=payload.workspace_id,
        bot_account_id=bot_account.id if bot_account else None,
        source="manual",
        platform=platform,
        meeting_url=payload.meeting_url.strip(),
        title=(payload.title or "").strip() or None,
        bot_name=bot_account.display_name if bot_account else MEETING_BOT_RECORDER_NAME,
        scheduled_for=_utc(payload.scheduled_for) if payload.scheduled_for else None,
        duration_minutes=payload.duration_minutes,
    )
    start_meeting_bot_session(session.id)
    return _serialize(session)


@router.get("")
def get_meeting_bot_sessions(
    workspace_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if workspace_id:
        _ensure_workspace_access(workspace_id, current_user, db)
        sessions = list_workspace_sessions(workspace_id=workspace_id)
    else:
        sessions = list_sessions(user_id=current_user.id)
    return [_serialize(session) for session in sessions]


@router.get("/upcoming")
def get_upcoming_meeting_bot_sessions(
    workspace_id: str = Query(...),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_workspace_access(workspace_id, current_user, db)
    sessions = list_upcoming_workspace_sessions(workspace_id=workspace_id, limit=limit)
    return [_serialize(session) for session in sessions]


@router.get("/{session_id}")
def get_meeting_bot_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = get_session(session_id)
    if not session or (
        session.user_id != current_user.id
        and not (session.workspace_id and _workspace_accessible(session.workspace_id, current_user, db))
    ):
        raise HTTPException(status_code=404, detail="Meeting bot session not found.")
    return _serialize(session)


@router.get("/{session_id}/calendar.ics")
def download_meeting_bot_calendar_event(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = get_session(session_id)
    if not session or (
        session.user_id != current_user.id
        and not (session.workspace_id and _workspace_accessible(session.workspace_id, current_user, db))
    ):
        raise HTTPException(status_code=404, detail="Meeting bot session not found.")
    if not session.scheduled_for:
        raise HTTPException(status_code=400, detail="Only scheduled bot sessions can be added to a calendar.")

    title = (session.title or "MinuteAI recorded meeting").replace("\n", " ").strip()
    start = session.scheduled_for
    end = start + timedelta(minutes=session.duration_minutes or 30)

    def ics_dt(value: datetime) -> str:
        return value.strftime("%Y%m%dT%H%M%SZ")

    def ics_text(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;")
        )

    body = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//MinuteAI//Meeting Bot//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{session.id}@minuteai",
        f"DTSTAMP:{ics_dt(datetime.utcnow())}",
        f"DTSTART:{ics_dt(start)}",
        f"DTEND:{ics_dt(end)}",
        f"SUMMARY:{ics_text(title)}",
        f"DESCRIPTION:{ics_text('MinuteAI Recorder is scheduled to join this meeting automatically. Meeting link: ' + session.meeting_url)}",
        f"LOCATION:{ics_text(session.meeting_url)}",
        f"URL:{ics_text(session.meeting_url)}",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])
    filename = f"minuteai-{session.id}.ics"
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{session_id}/cancel")
def cancel_meeting_bot_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = get_session(session_id)
    if not session or (
        session.user_id != current_user.id
        and not (session.workspace_id and _workspace_accessible(session.workspace_id, current_user, db))
    ):
        raise HTTPException(status_code=404, detail="Meeting bot session not found.")
    if session.status in {"completed", "failed", "cancelled"}:
        return _serialize(session)

    updated = update_session(
        session_id,
        status="cancelled",
        ended_at=datetime.utcnow(),
        failure_reason="Cancelled by user.",
    )
    return _serialize(updated)


@router.post("/{session_id}/end")
def end_meeting_bot_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = get_session(session_id)
    if not session or (
        session.user_id != current_user.id
        and not (session.workspace_id and _workspace_accessible(session.workspace_id, current_user, db))
    ):
        raise HTTPException(status_code=404, detail="Meeting bot session not found.")
    if session.status in {"completed", "failed", "cancelled"}:
        return _serialize(session)

    # If already stuck in "processing" with no job and no recording, the bot
    # thread is gone — move it to failed so the UI doesn't hang indefinitely.
    if session.status == "processing" and not session.job_id and not session.recording_path:
        updated = update_session(
            session_id,
            status="failed",
            ended_at=datetime.utcnow(),
            failure_reason="Recording ended without capturing audio — the bot may have crashed or lost connection.",
        )
        return _serialize(updated)

    updated = update_session(
        session_id,
        status="processing",
        ended_at=datetime.utcnow(),
        failure_reason=None,
    )
    return _serialize(updated)
