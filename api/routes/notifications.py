from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.deps import get_db, get_current_user
from models.database import NotificationRecord
from models.user import User

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _dict(n: NotificationRecord) -> dict:
    return {
        "id":         n.id,
        "type":       n.type,
        "title":      n.title,
        "body":       n.body,
        "link":       n.link,
        "read":       n.read,
        "created_at": (n.created_at.isoformat() + "Z") if n.created_at else None,
    }


@router.get("")
def list_notifications(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(NotificationRecord)
        .filter(NotificationRecord.user_id == current_user.id)
        .order_by(NotificationRecord.created_at.desc())
        .limit(50)
        .all()
    )
    return [_dict(n) for n in rows]


@router.post("/{notif_id}/read")
def mark_read(
    notif_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    n = db.get(NotificationRecord, notif_id)
    if n and n.user_id == current_user.id:
        n.read = True
        db.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db.query(NotificationRecord).filter(
        NotificationRecord.user_id == current_user.id,
        NotificationRecord.read == False,  # noqa: E712
    ).update({"read": True})
    db.commit()
    return {"ok": True}
