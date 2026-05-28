import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from middleware.auth import get_current_user, get_db
from models.support_message import SupportMessage
from models.user import User

router = APIRouter(prefix="/support", tags=["support"])


def _uuid():
    return str(uuid.uuid4())


class ContactIn(BaseModel):
    name: str
    email: str
    subject: str
    message: str


@router.post("/contact")
def contact_support(
    body: ContactIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ip = request.headers.get("x-forwarded-for") or (
        request.client.host if request.client else None
    )
    record = SupportMessage(
        id=_uuid(),
        user_id=current_user.id,
        name=body.name,
        email=body.email,
        subject=body.subject,
        message=body.message,
        ip_address=ip,
    )
    db.add(record)
    db.commit()
    return {"ok": True}
