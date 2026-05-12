from sqlalchemy import Column, String, Text, Boolean, DateTime, func

from db.base import Base


class NotificationRecord(Base):
    __tablename__ = "notifications"

    id         = Column(String,  primary_key=True)
    user_id    = Column(String,  nullable=False, index=True)
    type       = Column(String,  nullable=False)
    title      = Column(Text,    nullable=False)
    body       = Column(Text,    nullable=True)
    link       = Column(String,  nullable=True)
    read       = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, server_default=func.now())
