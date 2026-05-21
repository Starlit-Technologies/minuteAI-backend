from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from middleware.auth import get_current_user, get_db
from models.consent import get_consent_events, log_consent
from models.user import User

router = APIRouter(prefix="/consent", tags=["consent"])


@router.get("/meeting/{job_id}")
def get_meeting_consent(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    events = get_consent_events(job_id=job_id)
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "legal_basis": e.legal_basis,
            "user_name": e.user_name,
            "user_email": e.user_email,
            "consented_at": e.consented_at.isoformat() + "Z" if e.consented_at else None,
        }
        for e in events
    ]


@router.get("/session/{session_id}")
def get_session_consent(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    events = get_consent_events(session_id=session_id)
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "legal_basis": e.legal_basis,
            "user_name": e.user_name,
            "user_email": e.user_email,
            "consented_at": e.consented_at.isoformat() + "Z" if e.consented_at else None,
        }
        for e in events
    ]
