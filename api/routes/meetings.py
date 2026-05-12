from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from middleware.auth import get_db, get_current_user
from models.job import JobRecord
from models.user import User
from services.meetings import get_meeting_stats, job_to_dict

router = APIRouter(prefix="/meetings", tags=["meetings"])


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
    return [job_to_dict(r) for r in records]


@router.get("/stats")
def meeting_stats(
    workspace_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return get_meeting_stats(db, current_user.id, workspace_id)


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
