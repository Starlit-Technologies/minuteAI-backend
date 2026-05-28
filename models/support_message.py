import uuid
from sqlalchemy import Column, DateTime, String, Text, func
from db.base import Base


def _uuid():
    return str(uuid.uuid4())


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id          = Column(String, primary_key=True, default=_uuid)
    user_id     = Column(String, nullable=True, index=True)
    name        = Column(String, nullable=False)
    email       = Column(String, nullable=False)
    subject     = Column(String, nullable=False)
    message     = Column(Text, nullable=False)
    status      = Column(String, nullable=False, default="open")   # open | resolved
    ip_address  = Column(String, nullable=True)
    created_at  = Column(DateTime, server_default=func.now())
    resolved_at = Column(DateTime, nullable=True)
