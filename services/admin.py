import json
import os
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.job import JobRecord
from models.task import TaskRecord
from models.user import AdminGrant, PromoCode, SubscriptionPlan, TeamMember, User, Workspace


def configured_admin_emails() -> set[str]:
    return {
        email.strip().lower()
        for email in os.environ.get("ADMIN_EMAILS", "").split(",")
        if email.strip()
    }


def admin_state_for_email(email: str, db: Session) -> dict:
    normalized = (email or "").strip().lower()
    env_admins = configured_admin_emails()
    grant = db.query(AdminGrant).filter(AdminGrant.email == normalized).first()
    if normalized in env_admins:
        return {"is_admin": True, "admin_source": "env", "admin_grant_id": grant.id if grant else None}
    if grant:
        return {"is_admin": True, "admin_source": "database", "admin_grant_id": grant.id}
    return {"is_admin": False, "admin_source": "none", "admin_grant_id": None}


def promo_status(code: PromoCode) -> str:
    now = datetime.utcnow()
    if not code.active:
        return "inactive"
    if code.expires_at and code.expires_at <= now:
        return "expired"
    if code.max_uses is not None and code.uses_count >= code.max_uses:
        return "exhausted"
    return "active"


def safe_minutes(record: Optional[JobRecord]) -> dict:
    if not record:
        return {}
    try:
        return json.loads(record.minutes_json) if record.minutes_json else {}
    except Exception:
        return {}


def meeting_title(record: Optional[JobRecord]) -> str:
    return safe_minutes(record).get("title") or "Untitled Meeting"


def meeting_participants(record: Optional[JobRecord]) -> int:
    meta = safe_minutes(record).get("metadata", {})
    participants = meta.get("participants", [])
    return len(participants) if isinstance(participants, list) else 0


def meeting_actions(record: Optional[JobRecord]) -> int:
    data = safe_minutes(record)
    action_items = data.get("action_items", data.get("actionItems", []))
    return len(action_items) if isinstance(action_items, list) else 0


def meeting_duration(record: Optional[JobRecord]) -> str:
    meta = safe_minutes(record).get("metadata", {})
    return meta.get("duration", "—")


def duration_minutes(record: Optional[JobRecord]) -> int:
    raw = meeting_duration(record)
    if not raw or raw == "—":
        return 0
    text = str(raw).strip().lower()
    total = 0
    hour_match = re.search(r"(\d+)\s*(hour|hr|hrs)", text)
    minute_match = re.search(r"(\d+)\s*(minute|min|mins)", text)
    if hour_match:
        total += int(hour_match.group(1)) * 60
    if minute_match:
        total += int(minute_match.group(1))
    if total:
        return total
    numeric_match = re.search(r"(\d+)", text)
    return int(numeric_match.group(1)) if numeric_match else 0


def user_row(user: User, db: Session) -> dict:
    workspace_count = db.query(func.count(Workspace.id)).filter(Workspace.owner_id == user.id).scalar() or 0
    meeting_count = db.query(func.count(JobRecord.id)).filter(JobRecord.user_id == user.id).scalar() or 0
    assigned_tasks = db.query(func.count(TaskRecord.id)).filter(TaskRecord.assignee_id == user.id).scalar() or 0
    active_memberships = db.query(func.count(TeamMember.id)).filter(
        TeamMember.user_id == user.id, TeamMember.status == "active",
    ).scalar() or 0
    return {
        "id": user.id, "name": user.name, "email": user.email, "plan": user.plan,
        "email_verified": bool(user.email_verified),
        "workspace_count": workspace_count, "meeting_count": meeting_count,
        "assigned_tasks": assigned_tasks, "team_memberships": active_memberships,
        "stripe_customer_id": user.stripe_customer_id,
        "stripe_subscription_id": user.stripe_subscription_id,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        **admin_state_for_email(user.email, db),
    }


def workspace_row(workspace: Workspace, owner: Optional[User], db: Session) -> dict:
    member_count = db.query(func.count(TeamMember.id)).filter(
        TeamMember.workspace_id == workspace.id, TeamMember.status == "active",
    ).scalar() or 0
    meeting_count = db.query(func.count(JobRecord.id)).filter(JobRecord.workspace_id == workspace.id).scalar() or 0
    task_count = db.query(func.count(TaskRecord.id)).filter(TaskRecord.workspace_id == workspace.id).scalar() or 0
    return {
        "id": workspace.id, "name": workspace.name,
        "owner_id": workspace.owner_id,
        "owner_name": owner.name if owner else "Unknown owner",
        "owner_email": owner.email if owner else "",
        "member_count": member_count + 1,
        "meeting_count": meeting_count, "task_count": task_count,
        "created_at": workspace.created_at.isoformat() if workspace.created_at else None,
    }


def meeting_row(meeting: JobRecord, db: Session) -> dict:
    owner = db.get(User, meeting.user_id) if meeting.user_id else None
    return {
        "id": meeting.id, "title": meeting_title(meeting),
        "status": meeting.status,
        "owner_id": owner.id if owner else None,
        "owner_name": owner.name if owner else None,
        "owner_email": owner.email if owner else meeting.email,
        "workspace_id": meeting.workspace_id,
        "participants": meeting_participants(meeting),
        "actions": meeting_actions(meeting),
        "duration": meeting_duration(meeting),
        "starred": bool(meeting.starred),
        "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
    }


def task_row(task: TaskRecord, db: Session) -> dict:
    assignee = db.get(User, task.assignee_id) if task.assignee_id else None
    meeting = db.get(JobRecord, task.job_id) if task.job_id else None
    return {
        "id": task.id, "title": task.title, "status": task.status,
        "deadline": task.deadline, "workspace_id": task.workspace_id,
        "meeting_id": task.job_id,
        "meeting_title": meeting_title(meeting) if meeting else None,
        "assignee_id": assignee.id if assignee else None,
        "assignee_name": assignee.name if assignee else None,
        "assignee_email": assignee.email if assignee else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def admin_grant_row(grant: AdminGrant, db: Session) -> dict:
    creator = db.get(User, grant.created_by_id) if grant.created_by_id else None
    linked_user = db.query(User).filter(User.email == grant.email).first()
    return {
        "id": grant.id, "email": grant.email,
        "created_by_id": grant.created_by_id,
        "created_by_name": creator.name if creator else None,
        "linked_user_id": linked_user.id if linked_user else None,
        "linked_user_name": linked_user.name if linked_user else None,
        "created_at": grant.created_at.isoformat() if grant.created_at else None,
        "source": "database",
    }


def promo_code_row(code: PromoCode, db: Session) -> dict:
    creator = db.get(User, code.created_by_id) if code.created_by_id else None
    status_value = promo_status(code)
    return {
        "id": code.id, "code": code.code, "description": code.description,
        "discount_type": code.discount_type, "discount_value": code.discount_value,
        "active": bool(code.active), "status": status_value,
        "max_uses": code.max_uses, "uses_count": code.uses_count,
        "remaining_uses": None if code.max_uses is None else max(code.max_uses - code.uses_count, 0),
        "expires_at": code.expires_at.isoformat() if code.expires_at else None,
        "created_by_name": creator.name if creator else None,
        "created_at": code.created_at.isoformat() if code.created_at else None,
    }


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def plan_row(plan: SubscriptionPlan, db: Session) -> dict:
    creator = db.get(User, plan.created_by_id) if plan.created_by_id else None
    features = []
    if plan.features_json:
        try:
            features = json.loads(plan.features_json)
        except Exception:
            features = []
    return {
        "id": plan.id,
        "name": plan.name,
        "display_name": plan.display_name,
        "description": plan.description,
        "price_monthly": plan.price_monthly,
        "currency": (plan.currency or "usd").lower(),
        "interval": plan.interval or "month",
        "features": features,
        "meetings_per_month":    plan.meetings_per_month,     # None = unlimited
        "audio_hours_per_month": plan.audio_hours_per_month,  # None = unlimited
        "max_workspaces":        plan.max_workspaces,         # None = unlimited
        "max_members":           plan.max_members,            # None = unlimited
        "credits_per_month":     plan.credits_per_month,      # None = unlimited; Steph AI copilot chats
        "stripe_product_id": plan.stripe_product_id,
        "stripe_price_id": plan.stripe_price_id,
        "stripe_synced": bool(plan.stripe_product_id and plan.stripe_price_id),
        "is_active": bool(plan.is_active),
        "is_popular": bool(plan.is_popular),
        "sort_order": plan.sort_order or 0,
        "created_by_name": creator.name if creator else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
    }


def sync_plan_to_stripe(plan: SubscriptionPlan, db: Session):
    import os, stripe as _stripe
    _stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not _stripe.api_key:
        raise ValueError("STRIPE_SECRET_KEY is not configured.")

    if plan.stripe_product_id:
        kwargs: dict = {"name": plan.display_name}
        if plan.description:
            kwargs["description"] = plan.description
        _stripe.Product.modify(plan.stripe_product_id, **kwargs)
    else:
        kwargs = {"name": plan.display_name, "metadata": {"plan_name": plan.name, "plan_id": plan.id}}
        if plan.description:
            kwargs["description"] = plan.description
        product = _stripe.Product.create(**kwargs)
        plan.stripe_product_id = product.id

    # Stripe prices are immutable — always create a new one when price/interval changes
    price = _stripe.Price.create(
        product=plan.stripe_product_id,
        unit_amount=plan.price_monthly,
        currency=(plan.currency or "usd").lower(),
        recurring={"interval": plan.interval or "month"},
        metadata={"plan_name": plan.name, "plan_id": plan.id},
    )
    plan.stripe_price_id = price.id
    db.commit()
