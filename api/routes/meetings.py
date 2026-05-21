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


@router.get("/{job_id}/audio")
def get_audio_url(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return a short-lived presigned URL to stream the meeting's original audio from R2."""
    record = db.query(JobRecord).filter(
        JobRecord.id == job_id, JobRecord.user_id == current_user.id
    ).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    r2_key = getattr(record, "r2_audio_key", None)
    if not r2_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audio not available — this meeting was processed before R2 was enabled.")

    try:
        from services.storage_r2 import get_presigned_url, _is_configured
        if not _is_configured():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Cloud storage is not configured on this server.")
        url = get_presigned_url(r2_key, expires_in=3600)
        return {"url": url, "expires_in": 3600}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not generate audio URL: {exc}")


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


@router.delete("/{job_id}/recording", status_code=204)
def delete_recording(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete only the audio/video file — keeps the meeting, minutes, and transcript."""
    record = db.query(JobRecord).filter(
        JobRecord.id == job_id, JobRecord.user_id == current_user.id
    ).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    r2_key = getattr(record, "r2_audio_key", None)
    if r2_key:
        try:
            from services.storage_r2 import delete_file
            delete_file(r2_key)
        except Exception:
            pass

    local_path = getattr(record, "audio_path", None)
    if local_path:
        try:
            from pathlib import Path as _Path
            _Path(local_path).unlink(missing_ok=True)
        except Exception:
            pass

    record.audio_path = None
    record.r2_audio_key = None
    db.commit()


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

    # Also remove from R2 if present
    r2_key = getattr(record, "r2_audio_key", None)
    if r2_key:
        try:
            from services.storage_r2 import delete_file
            delete_file(r2_key)
        except Exception:
            pass

    db.delete(record)
    db.commit()
