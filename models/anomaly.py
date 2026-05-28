import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from db.base import Base


class ApiAnomalyRecord(Base):
    __tablename__ = "api_anomalies"

    id              = Column(String,     primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint        = Column(String,     nullable=False, index=True)
    method          = Column(String(10), nullable=False)
    status_code     = Column(Integer,    nullable=False)
    error_type      = Column(String(120),nullable=False, default="UnknownError")
    error_message   = Column(Text,       nullable=False, default="")
    stack_trace     = Column(Text,       nullable=True)
    request_body    = Column(Text,       nullable=True)
    user_id         = Column(String,     nullable=True)
    user_email      = Column(String,     nullable=True)
    ip_address      = Column(String(45), nullable=True)

    # Resolution
    resolved          = Column(Boolean, nullable=False, default=False)
    resolved_at       = Column(DateTime, nullable=True)
    resolved_by_id    = Column(String,   nullable=True)
    resolved_by_email = Column(String,   nullable=True)
    notes             = Column(Text,     nullable=True)

    # Deduplication counters
    count         = Column(Integer,  nullable=False, default=1)
    first_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at  = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at    = Column(DateTime, nullable=False, default=datetime.utcnow)
