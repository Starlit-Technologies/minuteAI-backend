import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, String, Text, Boolean, DateTime, func

from db.base import Base, SessionLocal


class ConnectedAccountRecord(Base):
    __tablename__ = "connected_accounts"

    id            = Column(String, primary_key=True)
    user_id       = Column(String, nullable=False, index=True)
    provider      = Column(String, nullable=False)
    access_token  = Column(Text,   nullable=False)
    refresh_token = Column(Text,   nullable=True)
    token_expiry  = Column(DateTime, nullable=True)
    email         = Column(String, nullable=True)
    auto_join     = Column(Boolean, nullable=False, default=True)
    created_at    = Column(DateTime, server_default=func.now())


@dataclass
class ConnectedAccount:
    id: str
    user_id: str
    provider: str
    access_token: str
    refresh_token: Optional[str]
    token_expiry: Optional[datetime]
    email: Optional[str]
    auto_join: bool
    created_at: Optional[datetime]


def _to_dataclass(record: ConnectedAccountRecord) -> ConnectedAccount:
    return ConnectedAccount(
        id=record.id,
        user_id=record.user_id,
        provider=record.provider,
        access_token=record.access_token,
        refresh_token=record.refresh_token,
        token_expiry=record.token_expiry,
        email=record.email,
        auto_join=bool(record.auto_join),
        created_at=record.created_at,
    )


def get_connected_account(user_id: str, provider: str) -> Optional[ConnectedAccount]:
    with SessionLocal() as db:
        record = (
            db.query(ConnectedAccountRecord)
            .filter(
                ConnectedAccountRecord.user_id == user_id,
                ConnectedAccountRecord.provider == provider,
            )
            .first()
        )
        return _to_dataclass(record) if record else None


def list_connected_accounts(user_id: str) -> list[ConnectedAccount]:
    with SessionLocal() as db:
        records = (
            db.query(ConnectedAccountRecord)
            .filter(ConnectedAccountRecord.user_id == user_id)
            .all()
        )
        return [_to_dataclass(r) for r in records]


def upsert_connected_account(
    *,
    user_id: str,
    provider: str,
    access_token: str,
    refresh_token: Optional[str],
    token_expiry: Optional[datetime],
    email: Optional[str],
) -> ConnectedAccount:
    with SessionLocal() as db:
        record = (
            db.query(ConnectedAccountRecord)
            .filter(
                ConnectedAccountRecord.user_id == user_id,
                ConnectedAccountRecord.provider == provider,
            )
            .first()
        )
        if record:
            record.access_token = access_token
            if refresh_token:
                record.refresh_token = refresh_token
            record.token_expiry = token_expiry
            if email:
                record.email = email
        else:
            record = ConnectedAccountRecord(
                id=str(uuid.uuid4()),
                user_id=user_id,
                provider=provider,
                access_token=access_token,
                refresh_token=refresh_token,
                token_expiry=token_expiry,
                email=email,
                auto_join=True,
            )
            db.add(record)
        db.commit()
        db.refresh(record)
        return _to_dataclass(record)


def delete_connected_account(user_id: str, provider: str) -> None:
    with SessionLocal() as db:
        record = (
            db.query(ConnectedAccountRecord)
            .filter(
                ConnectedAccountRecord.user_id == user_id,
                ConnectedAccountRecord.provider == provider,
            )
            .first()
        )
        if record:
            db.delete(record)
            db.commit()


def update_auto_join(user_id: str, provider: str, auto_join: bool) -> Optional[ConnectedAccount]:
    with SessionLocal() as db:
        record = (
            db.query(ConnectedAccountRecord)
            .filter(
                ConnectedAccountRecord.user_id == user_id,
                ConnectedAccountRecord.provider == provider,
            )
            .first()
        )
        if not record:
            return None
        record.auto_join = auto_join
        db.commit()
        db.refresh(record)
        return _to_dataclass(record)


def list_all_for_sync() -> list[ConnectedAccount]:
    with SessionLocal() as db:
        records = (
            db.query(ConnectedAccountRecord)
            .filter(ConnectedAccountRecord.auto_join == True)  # noqa: E712
            .all()
        )
        return [_to_dataclass(r) for r in records]
