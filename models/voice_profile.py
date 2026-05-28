import json
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, String, Text, Integer, DateTime, func

from db.base import Base, SessionLocal


class VoiceProfileRecord(Base):
    __tablename__ = "voice_profiles"

    id           = Column(String, primary_key=True)
    workspace_id = Column(String, nullable=False, index=True)
    person_name  = Column(String, nullable=False)
    person_email = Column(String, nullable=True)
    embedding_json = Column(Text, nullable=True)   # JSON float array (Resemblyzer 256-dim)
    confirm_count  = Column(Integer, default=1)
    updated_at   = Column(DateTime, server_default=func.now())


def get_profiles(workspace_id: str) -> list[dict]:
    with SessionLocal() as db:
        records = (
            db.query(VoiceProfileRecord)
            .filter(VoiceProfileRecord.workspace_id == workspace_id)
            .order_by(VoiceProfileRecord.confirm_count.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "person_name": r.person_name,
                "person_email": r.person_email,
                "embedding": json.loads(r.embedding_json) if r.embedding_json else None,
                "confirm_count": r.confirm_count or 1,
            }
            for r in records
        ]


def upsert_profile(
    workspace_id: str,
    person_name: str,
    person_email: Optional[str] = None,
    embedding: Optional[list] = None,
) -> None:
    with SessionLocal() as db:
        existing = (
            db.query(VoiceProfileRecord)
            .filter(
                VoiceProfileRecord.workspace_id == workspace_id,
                VoiceProfileRecord.person_name == person_name,
            )
            .first()
        )
        if existing:
            existing.confirm_count = (existing.confirm_count or 1) + 1
            if embedding:
                if existing.embedding_json:
                    # Online averaging — smooth the profile over repeated confirmations
                    old = json.loads(existing.embedding_json)
                    n = existing.confirm_count
                    avg = [(old[i] * (n - 1) + embedding[i]) / n for i in range(len(embedding))]
                    existing.embedding_json = json.dumps(avg)
                else:
                    existing.embedding_json = json.dumps(embedding)
            if person_email and not existing.person_email:
                existing.person_email = person_email
            existing.updated_at = datetime.utcnow()
        else:
            db.add(VoiceProfileRecord(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                person_name=person_name,
                person_email=person_email,
                embedding_json=json.dumps(embedding) if embedding else None,
                confirm_count=1,
            ))
        db.commit()
