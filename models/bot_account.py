import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from models.database import BotAccountRecord, SessionLocal
from models.user import Workspace


BOT_EMAIL_DOMAIN = os.environ.get("MEETING_BOT_EMAIL_DOMAIN", "bots.minuteai.local").strip() or "bots.minuteai.local"
PROFILE_ROOT = Path(
    os.environ.get(
        "MEETING_BOT_PROFILE_ROOT",
        str(Path(__file__).parent.parent / "storage" / "meeting_bot_profiles"),
    )
)


@dataclass
class BotAccount:
    id: str
    workspace_id: str
    email: str
    display_name: str
    profile_dir: str
    provider: str
    status: str
    kind: str
    created_at: Optional[object]


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return value[:36].strip("-") or "workspace"


def _to_dataclass(record: BotAccountRecord) -> BotAccount:
    return BotAccount(
        id=record.id,
        workspace_id=record.workspace_id,
        email=record.email,
        display_name=record.display_name,
        profile_dir=record.profile_dir,
        provider=record.provider,
        status=record.status,
        kind=record.kind,
        created_at=record.created_at,
    )


def _unique_email(db, workspace: Workspace) -> str:
    base = _slug(workspace.name)
    suffix = workspace.id.replace("-", "")[:8]
    candidate = f"{base}-{suffix}@{BOT_EMAIL_DOMAIN}"
    existing = db.query(BotAccountRecord).filter(BotAccountRecord.email == candidate).first()
    if not existing:
        return candidate
    return f"{base}-{uuid.uuid4().hex[:8]}@{BOT_EMAIL_DOMAIN}"


def ensure_workspace_bot_account(workspace_id: str) -> Optional[BotAccount]:
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        existing = db.query(BotAccountRecord).filter(BotAccountRecord.workspace_id == workspace_id).first()
        if existing:
            return _to_dataclass(existing)

        workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if not workspace:
            return None

        bot_id = str(uuid.uuid4())
        profile_dir = str(PROFILE_ROOT / bot_id)
        record = BotAccountRecord(
            id=bot_id,
            workspace_id=workspace_id,
            email=_unique_email(db, workspace),
            display_name="MinuteAI Recorder",
            profile_dir=profile_dir,
            provider="google",
            status="needs_setup",
            kind="dedicated",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return _to_dataclass(record)


def get_workspace_bot_account(workspace_id: str) -> Optional[BotAccount]:
    with SessionLocal() as db:
        record = db.query(BotAccountRecord).filter(BotAccountRecord.workspace_id == workspace_id).first()
        return _to_dataclass(record) if record else None


def get_bot_account(bot_account_id: str) -> Optional[BotAccount]:
    with SessionLocal() as db:
        record = db.get(BotAccountRecord, bot_account_id)
        return _to_dataclass(record) if record else None


def list_bot_accounts() -> list[BotAccount]:
    with SessionLocal() as db:
        records = db.query(BotAccountRecord).filter(BotAccountRecord.status != "disabled").all()
        return [_to_dataclass(record) for record in records]


def ensure_bot_accounts_for_all_workspaces():
    with SessionLocal() as db:
        workspace_ids = [row[0] for row in db.query(Workspace.id).all()]
    for workspace_id in workspace_ids:
        ensure_workspace_bot_account(workspace_id)
