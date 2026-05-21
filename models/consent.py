import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, func
from db.base import Base, SessionLocal


def _uuid():
    return str(uuid.uuid4())


class ConsentEventRecord(Base):
    __tablename__ = "consent_events"

    id           = Column(String, primary_key=True, default=_uuid)
    user_id      = Column(String, nullable=True, index=True)
    job_id       = Column(String, nullable=True, index=True)
    session_id   = Column(String, nullable=True, index=True)  # meeting bot session
    event_type   = Column(String, nullable=False)              # 'upload' | 'bot' | 'extension'
    legal_basis  = Column(String, nullable=False, default="contract")
    user_name    = Column(String, nullable=True)
    user_email   = Column(String, nullable=True)
    ip_address   = Column(String, nullable=True)
    consented_at = Column(DateTime, server_default=func.now())


def log_consent(
    *,
    event_type: str,
    user_id: str | None = None,
    job_id: str | None = None,
    session_id: str | None = None,
    legal_basis: str = "contract",
    user_name: str | None = None,
    user_email: str | None = None,
    ip_address: str | None = None,
) -> ConsentEventRecord:
    with SessionLocal() as db:
        record = ConsentEventRecord(
            id=_uuid(),
            user_id=user_id,
            job_id=job_id,
            session_id=session_id,
            event_type=event_type,
            legal_basis=legal_basis,
            user_name=user_name,
            user_email=user_email,
            ip_address=ip_address,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def get_consent_events(job_id: str = None, session_id: str = None) -> list[ConsentEventRecord]:
    with SessionLocal() as db:
        query = db.query(ConsentEventRecord)
        if job_id:
            query = query.filter(ConsentEventRecord.job_id == job_id)
        if session_id:
            query = query.filter(ConsentEventRecord.session_id == session_id)
        return query.order_by(ConsentEventRecord.consented_at.asc()).all()
