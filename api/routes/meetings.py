import json
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import Optional

from api.deps import get_db, get_current_user
from models.user import User
from models.database import JobRecord

router = APIRouter(prefix="/meetings", tags=["meetings"])


def _parse_meta(record: JobRecord) -> dict:
    """Extract display metadata from the stored minutes JSON."""
    try:
        data = json.loads(record.minutes_json) if record.minutes_json else {}
    except Exception:
        data = {}

    meta = data.get("metadata", {})
    participants = meta.get("participants", [])
    action_items = data.get("action_items", data.get("actionItems", []))

    return {
        "title":        data.get("title", "Untitled Meeting"),
        "duration":     meta.get("duration", "—"),
        "participants": len(participants) if isinstance(participants, list) else 0,
        "actions":      len(action_items) if isinstance(action_items, list) else 0,
    }


def _job_to_dict(record: JobRecord) -> dict:
    meta = _parse_meta(record) if record.status == "done" else {}
    return {
        "id":           record.id,
        "title":        meta.get("title", "Processing…") if meta else "Processing…",
        "date":         (record.created_at.isoformat() + "Z") if record.created_at else None,
        "duration":     meta.get("duration", "—"),
        "participants": meta.get("participants", 0),
        "actions":      meta.get("actions", 0),
        "status":       "done" if record.status == "done" else (
                        "error" if record.status == "error" else "processing"),
        "starred":      bool(getattr(record, "starred", False)),
    }


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_meetings(
    workspace_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(JobRecord).filter(JobRecord.user_id == current_user.id)
    if workspace_id:
        q = q.filter(JobRecord.workspace_id == workspace_id)
    records = q.order_by(JobRecord.created_at.desc()).all()
    return [_job_to_dict(r) for r in records]


@router.get("/stats")
def meeting_stats(
    workspace_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from datetime import datetime, timezone
    from calendar import monthrange

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    q = db.query(JobRecord).filter(JobRecord.user_id == current_user.id)
    if workspace_id:
        q = q.filter(JobRecord.workspace_id == workspace_id)
    all_jobs = q.all()
    done = [j for j in all_jobs if j.status == "done"]
    this_month = [j for j in done if j.created_at and j.created_at >= month_start.replace(tzinfo=None)]

    total_actions = 0
    for j in done:
        try:
            data = json.loads(j.minutes_json) if j.minutes_json else {}
            ai = data.get("action_items", data.get("actionItems", []))
            total_actions += len(ai) if isinstance(ai, list) else 0
        except Exception:
            pass

    return {
        "total_meetings":      len(done),
        "meetings_this_month": len(this_month),
        "total_action_items":  total_actions,
        "processing":          sum(1 for j in all_jobs if j.status not in ("done", "error")),
    }


@router.patch("/{job_id}/star")
def toggle_star(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = db.query(JobRecord).filter(
        JobRecord.id == job_id, JobRecord.user_id == current_user.id
    ).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")
    record.starred = not bool(getattr(record, "starred", False))
    db.commit()
    return {"starred": record.starred}


@router.delete("/{job_id}", status_code=204)
def delete_meeting(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = db.query(JobRecord).filter(
        JobRecord.id == job_id, JobRecord.user_id == current_user.id
    ).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")
    db.delete(record)
    db.commit()
