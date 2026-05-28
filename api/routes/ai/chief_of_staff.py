from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from middleware.auth import get_current_user, get_db
from models.user import User

router = APIRouter(prefix="/chief-of-staff", tags=["chief-of-staff"])


@router.get("/briefing")
def get_briefing(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from services.ai.chief_of_staff import generate_briefing
    ws = getattr(current_user, "active_workspace_id", None)
    return generate_briefing(db, current_user.id, current_user.name, ws)


@router.get("/patterns")
def get_patterns(
    workspace_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from services.ai.chief_of_staff import detect_patterns
    return {"patterns": detect_patterns(db, current_user.id, workspace_id)}


@router.get("/continuity/{job_id}")
def get_continuity(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return thread continuity context for a completed meeting.

    Returns previous decisions, next steps, and open tasks from prior sessions
    in the same recurring meeting thread.
    """
    from services.ai.chief_of_staff import get_thread_continuity
    return get_thread_continuity(db, job_id, current_user.id)


@router.get("/pre-meeting")
def get_pre_meeting_brief(
    title: str = Query(..., min_length=1),
    attendees: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate a focused brief for an upcoming calendar event."""
    from services.ai.chief_of_staff import generate_pre_meeting_brief
    att_list = [a.strip() for a in (attendees or "").split(",") if a.strip()]
    return generate_pre_meeting_brief(db, current_user.id, current_user.name, title, att_list)


@router.post("/briefing/send")
def trigger_briefing(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually trigger a briefing email for the current user."""
    from services.ai.chief_of_staff import generate_briefing
    from services.notifications.email import send_daily_briefing
    ws = getattr(current_user, "active_workspace_id", None)
    briefing = generate_briefing(db, current_user.id, current_user.name, ws)
    send_daily_briefing(
        to_email=current_user.email,
        user_name=current_user.name,
        briefing=briefing,
    )
    return {"ok": True}
