import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from middleware.auth import get_current_user
from models.meeting_thread import (
    get_thread,
    list_threads_for_user,
    list_sessions_for_thread,
    get_thread_for_job,
    rename_thread,
    set_thread_category,
    set_thread_locked,
    delete_thread,
)
from models.task import TaskRecord
from models.job import JobRecord
from models.user import User
from db.base import SessionLocal

router = APIRouter(prefix="/threads", tags=["threads"])


class UpdateThreadBody(BaseModel):
    name: Optional[str] = None
    is_locked: Optional[bool] = None
    category: Optional[str] = None


def _thread_dict(t) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "category": getattr(t, "category", None),
        "workspace_id": t.workspace_id,
        "is_locked": t.is_locked,
        "participants": t.participants,
        "session_count": t.session_count,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "last_session_at": t.last_session_at.isoformat() if getattr(t, "last_session_at", None) else None,
    }


def _session_dict(s, open_task_count: int = 0) -> dict:
    return {
        "id": s.id,
        "thread_id": s.thread_id,
        "job_id": s.job_id,
        "session_number": s.session_number,
        "title": s.title,
        "overview": s.minutes_overview,
        "open_task_count": open_task_count,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _open_task_count(job_id: str) -> int:
    with SessionLocal() as db:
        return db.query(TaskRecord).filter(
            TaskRecord.job_id == job_id,
            TaskRecord.status != "done",
        ).count()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
def get_threads(
    workspace_id: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    threads = list_threads_for_user(user.id, workspace_id=workspace_id)
    return [_thread_dict(t) for t in threads]


@router.get("/for-job/{job_id}")
def get_thread_for_meeting(job_id: str, user: User = Depends(get_current_user)):
    """Return the thread a specific meeting belongs to (if any)."""
    thread = get_thread_for_job(job_id)
    if not thread:
        return None
    sessions = list_sessions_for_thread(thread.id)
    current = next((s for s in sessions if s.job_id == job_id), None)
    return {
        **_thread_dict(thread),
        "sessions": [_session_dict(s, _open_task_count(s.job_id)) for s in sessions],
        "current_session": _session_dict(current) if current else None,
    }


@router.get("/{thread_id}/continuity")
def get_continuity(
    thread_id: str,
    before_job_id: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    """
    Return the previous session's data for the continuity briefing panel.
    before_job_id: current meeting's job_id — returns the session immediately before it.
    If omitted, returns data for the second-to-last session.
    """
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")

    sessions = list_sessions_for_thread(thread_id)
    if len(sessions) < 2:
        return {"has_previous": False}

    if before_job_id:
        idx = next((i for i, s in enumerate(sessions) if s.job_id == before_job_id), None)
        if idx is None or idx == 0:
            return {"has_previous": False}
        prev = sessions[idx - 1]
    else:
        prev = sessions[-2]

    with SessionLocal() as db:
        job = db.get(JobRecord, prev.job_id)
        if not job or not job.minutes_json:
            return {"has_previous": False}
        try:
            minutes = json.loads(job.minutes_json)
        except Exception:
            return {"has_previous": False}

        open_tasks = db.query(TaskRecord).filter(
            TaskRecord.job_id == prev.job_id,
            TaskRecord.status != "done",
        ).all()

    return {
        "has_previous": True,
        "session_number": prev.session_number,
        "title": minutes.get("title", "Previous Meeting"),
        "date": minutes.get("metadata", {}).get("date", ""),
        "overview": minutes.get("overview", ""),
        "key_decisions": minutes.get("keyDecisions", [])[:5],
        "next_steps": minutes.get("nextSteps", [])[:5],
        "open_action_items": [
            {
                "id": t.id,
                "title": t.title,
                "owner_name": t.owner_name,
                "deadline": t.deadline,
                "status": t.status,
            }
            for t in open_tasks
        ],
    }


@router.get("/{thread_id}")
def get_thread_detail(thread_id: str, user: User = Depends(get_current_user)):
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")

    sessions = list_sessions_for_thread(thread_id)
    return {
        **_thread_dict(thread),
        "sessions": [_session_dict(s, _open_task_count(s.job_id)) for s in sessions],
    }


@router.patch("/{thread_id}")
def update_thread(
    thread_id: str,
    body: UpdateThreadBody,
    user: User = Depends(get_current_user),
):
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    if thread.created_by != user.id:
        raise HTTPException(403, "Not authorized")

    if body.name is not None:
        if thread.is_locked:
            raise HTTPException(400, "Thread is locked — unlock it before renaming")
        rename_thread(thread_id, body.name.strip() or thread.name)

    if body.is_locked is not None:
        set_thread_locked(thread_id, body.is_locked)

    if body.category is not None:
        set_thread_category(thread_id, body.category or None)

    thread = get_thread(thread_id)
    return _thread_dict(thread)


@router.delete("/{thread_id}", status_code=204)
def remove_thread(thread_id: str, user: User = Depends(get_current_user)):
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    if thread.created_by != user.id:
        raise HTTPException(403, "Not authorized")
    delete_thread(thread_id)
