import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, String, Text, Boolean, Integer, DateTime, ForeignKey, func

from db.base import Base, SessionLocal


class MeetingThreadRecord(Base):
    __tablename__ = "meeting_threads"

    id                = Column(String,  primary_key=True)
    workspace_id      = Column(String,  nullable=True,  index=True)
    created_by        = Column(String,  nullable=False)
    name              = Column(String,  nullable=False)
    category          = Column(String,  nullable=True)
    participants_hash  = Column(String,  nullable=False, index=True)
    participants_json  = Column(Text,    nullable=True)   # JSON array of participant identifiers
    is_locked         = Column(Boolean, nullable=False, default=False)
    created_at        = Column(DateTime, server_default=func.now())


class ThreadSessionRecord(Base):
    __tablename__ = "thread_sessions"

    id             = Column(String,  primary_key=True)
    thread_id      = Column(String,  ForeignKey("meeting_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id         = Column(String,  ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True)
    session_number = Column(Integer, nullable=False, default=1)
    created_at     = Column(DateTime, server_default=func.now())


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class MeetingThread:
    id: str
    workspace_id: Optional[str]
    created_by: str
    name: str
    participants_hash: str
    participants: list
    is_locked: bool
    created_at: Optional[datetime]
    category: Optional[str] = None
    session_count: int = 0
    last_session_at: Optional[datetime] = None


@dataclass
class ThreadSession:
    id: str
    thread_id: str
    job_id: str
    session_number: int
    created_at: Optional[datetime]
    title: Optional[str] = None
    minutes_overview: Optional[str] = None


# ── Converters ────────────────────────────────────────────────────────────────

def _to_thread(r: MeetingThreadRecord, session_count: int = 0, last_session_at: Optional[datetime] = None) -> MeetingThread:
    try:
        participants = json.loads(r.participants_json) if r.participants_json else []
    except Exception:
        participants = []
    return MeetingThread(
        id=r.id,
        workspace_id=r.workspace_id,
        created_by=r.created_by,
        name=r.name,
        category=r.category,
        participants_hash=r.participants_hash,
        participants=participants,
        is_locked=bool(r.is_locked),
        created_at=r.created_at,
        session_count=session_count,
        last_session_at=last_session_at,
    )


def _to_session(r: ThreadSessionRecord) -> ThreadSession:
    return ThreadSession(
        id=r.id,
        thread_id=r.thread_id,
        job_id=r.job_id,
        session_number=r.session_number,
        created_at=r.created_at,
    )


# ── Queries ───────────────────────────────────────────────────────────────────

def _latest_session_at(db, thread_id: str) -> Optional[datetime]:
    last = (
        db.query(ThreadSessionRecord)
        .filter(ThreadSessionRecord.thread_id == thread_id)
        .order_by(ThreadSessionRecord.session_number.desc())
        .first()
    )
    return last.created_at if last else None


def get_thread(thread_id: str) -> Optional[MeetingThread]:
    with SessionLocal() as db:
        r = db.get(MeetingThreadRecord, thread_id)
        if not r:
            return None
        count = db.query(ThreadSessionRecord).filter(ThreadSessionRecord.thread_id == r.id).count()
        return _to_thread(r, count, _latest_session_at(db, r.id))


def list_threads_for_user(user_id: str, workspace_id: Optional[str] = None) -> list[MeetingThread]:
    with SessionLocal() as db:
        q = db.query(MeetingThreadRecord)
        if workspace_id:
            q = q.filter(MeetingThreadRecord.workspace_id == workspace_id)
        else:
            q = q.filter(MeetingThreadRecord.created_by == user_id)
        records = q.order_by(MeetingThreadRecord.created_at.desc()).all()
        result = []
        for r in records:
            count = db.query(ThreadSessionRecord).filter(ThreadSessionRecord.thread_id == r.id).count()
            result.append(_to_thread(r, count, _latest_session_at(db, r.id)))
        return result


def list_sessions_for_thread(thread_id: str) -> list[ThreadSession]:
    from models.job import JobRecord
    with SessionLocal() as db:
        records = (
            db.query(ThreadSessionRecord)
            .filter(ThreadSessionRecord.thread_id == thread_id)
            .order_by(ThreadSessionRecord.session_number)
            .all()
        )
        sessions = []
        for r in records:
            s = _to_session(r)
            job = db.get(JobRecord, r.job_id)
            if job and job.minutes_json:
                try:
                    m = json.loads(job.minutes_json)
                    s.title = m.get("title")
                    s.minutes_overview = (m.get("overview") or "")[:300]
                except Exception:
                    pass
            sessions.append(s)
        return sessions


def get_thread_for_job(job_id: str) -> Optional[MeetingThread]:
    with SessionLocal() as db:
        sess = db.query(ThreadSessionRecord).filter(ThreadSessionRecord.job_id == job_id).first()
        if not sess:
            return None
        r = db.get(MeetingThreadRecord, sess.thread_id)
        if not r:
            return None
        count = db.query(ThreadSessionRecord).filter(ThreadSessionRecord.thread_id == r.id).count()
        return _to_thread(r, count, _latest_session_at(db, r.id))


def rename_thread(thread_id: str, name: str) -> Optional[MeetingThread]:
    with SessionLocal() as db:
        r = db.get(MeetingThreadRecord, thread_id)
        if not r:
            return None
        r.name = name
        db.commit()
        db.refresh(r)
        count = db.query(ThreadSessionRecord).filter(ThreadSessionRecord.thread_id == r.id).count()
        return _to_thread(r, count, _latest_session_at(db, r.id))


def set_thread_category(thread_id: str, category: Optional[str]) -> Optional[MeetingThread]:
    with SessionLocal() as db:
        r = db.get(MeetingThreadRecord, thread_id)
        if not r:
            return None
        r.category = category or None
        db.commit()
        db.refresh(r)
        count = db.query(ThreadSessionRecord).filter(ThreadSessionRecord.thread_id == r.id).count()
        return _to_thread(r, count, _latest_session_at(db, r.id))


def set_thread_locked(thread_id: str, is_locked: bool) -> Optional[MeetingThread]:
    with SessionLocal() as db:
        r = db.get(MeetingThreadRecord, thread_id)
        if not r:
            return None
        r.is_locked = is_locked
        db.commit()
        db.refresh(r)
        count = db.query(ThreadSessionRecord).filter(ThreadSessionRecord.thread_id == r.id).count()
        return _to_thread(r, count, _latest_session_at(db, r.id))


def delete_thread(thread_id: str) -> None:
    with SessionLocal() as db:
        r = db.get(MeetingThreadRecord, thread_id)
        if r:
            db.delete(r)
            db.commit()
