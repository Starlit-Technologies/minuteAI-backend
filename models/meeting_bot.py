import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from models.database import MeetingBotSessionRecord, SessionLocal


@dataclass
class MeetingBotSession:
    id: str
    user_id: str
    workspace_id: Optional[str]
    bot_account_id: Optional[str]
    calendar_event_id: Optional[str]
    source: Optional[str]
    platform: str
    meeting_url: str
    title: Optional[str]
    bot_name: str
    status: str
    scheduled_for: Optional[datetime]
    duration_minutes: Optional[int]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    failure_reason: Optional[str]
    recording_path: Optional[str]
    job_id: Optional[str]
    created_at: Optional[datetime]


def _to_dataclass(record: MeetingBotSessionRecord) -> MeetingBotSession:
    return MeetingBotSession(
        id=record.id,
        user_id=record.user_id,
        workspace_id=record.workspace_id,
        bot_account_id=record.bot_account_id,
        calendar_event_id=record.calendar_event_id,
        source=record.source,
        platform=record.platform,
        meeting_url=record.meeting_url,
        title=record.title,
        bot_name=record.bot_name,
        status=record.status,
        scheduled_for=record.scheduled_for,
        duration_minutes=record.duration_minutes,
        started_at=record.started_at,
        ended_at=record.ended_at,
        failure_reason=record.failure_reason,
        recording_path=record.recording_path,
        job_id=record.job_id,
        created_at=record.created_at,
    )


def create_session(
    *,
    user_id: str,
    workspace_id: Optional[str],
    platform: str,
    meeting_url: str,
    bot_name: str,
    bot_account_id: Optional[str] = None,
    calendar_event_id: Optional[str] = None,
    source: Optional[str] = None,
    title: Optional[str] = None,
    scheduled_for: Optional[datetime] = None,
    duration_minutes: Optional[int] = None,
) -> MeetingBotSession:
    session_id = str(uuid.uuid4())
    with SessionLocal() as db:
        record = MeetingBotSessionRecord(
            id=session_id,
            user_id=user_id,
            workspace_id=workspace_id,
            bot_account_id=bot_account_id,
            calendar_event_id=calendar_event_id,
            source=source,
            platform=platform,
            meeting_url=meeting_url,
            title=title,
            bot_name=bot_name,
            status="queued",
            scheduled_for=scheduled_for,
            duration_minutes=duration_minutes,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return _to_dataclass(record)


def get_session(session_id: str) -> Optional[MeetingBotSession]:
    with SessionLocal() as db:
        record = db.get(MeetingBotSessionRecord, session_id)
        return _to_dataclass(record) if record else None


def get_session_by_calendar_event(bot_account_id: str, calendar_event_id: str) -> Optional[MeetingBotSession]:
    with SessionLocal() as db:
        record = (
            db.query(MeetingBotSessionRecord)
            .filter(
                MeetingBotSessionRecord.bot_account_id == bot_account_id,
                MeetingBotSessionRecord.calendar_event_id == calendar_event_id,
            )
            .first()
        )
        return _to_dataclass(record) if record else None


def list_sessions(*, user_id: str, workspace_id: Optional[str] = None) -> list[MeetingBotSession]:
    with SessionLocal() as db:
        query = db.query(MeetingBotSessionRecord).filter(MeetingBotSessionRecord.user_id == user_id)
        if workspace_id:
            query = query.filter(MeetingBotSessionRecord.workspace_id == workspace_id)
        records = query.order_by(MeetingBotSessionRecord.created_at.desc()).all()
        return [_to_dataclass(record) for record in records]


def list_workspace_sessions(*, workspace_id: str) -> list[MeetingBotSession]:
    with SessionLocal() as db:
        records = (
            db.query(MeetingBotSessionRecord)
            .filter(MeetingBotSessionRecord.workspace_id == workspace_id)
            .order_by(MeetingBotSessionRecord.created_at.desc())
            .all()
        )
        return [_to_dataclass(record) for record in records]


def list_upcoming_workspace_sessions(*, workspace_id: str, limit: int = 5) -> list[MeetingBotSession]:
    now = datetime.utcnow()
    with SessionLocal() as db:
        records = (
            db.query(MeetingBotSessionRecord)
            .filter(
                MeetingBotSessionRecord.workspace_id == workspace_id,
                MeetingBotSessionRecord.scheduled_for.isnot(None),
                MeetingBotSessionRecord.scheduled_for >= now,
                MeetingBotSessionRecord.status.in_(["queued", "preparing", "joining", "waiting_for_host", "recording"]),
            )
            .order_by(MeetingBotSessionRecord.scheduled_for.asc())
            .limit(limit)
            .all()
        )
        return [_to_dataclass(record) for record in records]


def list_startable_sessions() -> list[MeetingBotSession]:
    with SessionLocal() as db:
        records = (
            db.query(MeetingBotSessionRecord)
            .filter(MeetingBotSessionRecord.status.in_(["queued", "preparing", "joining", "waiting_for_host"]))
            .order_by(MeetingBotSessionRecord.scheduled_for.asc().nullsfirst(), MeetingBotSessionRecord.created_at.asc())
            .all()
        )
        return [_to_dataclass(record) for record in records]


def recover_processing_sessions():
    with SessionLocal() as db:
        records = (
            db.query(MeetingBotSessionRecord)
            .filter(MeetingBotSessionRecord.status == "processing", MeetingBotSessionRecord.job_id.is_(None))
            .all()
        )
        for record in records:
            record.status = "failed"
            record.ended_at = record.ended_at or datetime.utcnow()
            record.failure_reason = (
                "The bot was stopped, but no recording artifact was finalized. "
                "Please retry the session after confirming the recorder browser has closed cleanly."
            )
        if records:
            db.commit()


def update_session(session_id: str, **kwargs) -> Optional[MeetingBotSession]:
    with SessionLocal() as db:
        record = db.get(MeetingBotSessionRecord, session_id)
        if not record:
            return None
        for key, value in kwargs.items():
            if hasattr(record, key):
                setattr(record, key, value)
        db.commit()
        db.refresh(record)
        return _to_dataclass(record)
