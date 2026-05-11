import json
import os
import re
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from api.deps import get_admin_user, get_db
from models.database import JobRecord, NotificationRecord, TaskRecord
from models.user import AdminGrant, OTPCode, PromoCode, TeamMember, User, Workspace

router = APIRouter(prefix="/admin", tags=["admin"])


class UpdateAdminUserIn(BaseModel):
    name: Optional[str] = None
    plan: Optional[str] = None
    email_verified: Optional[bool] = None


class UpdateAdminWorkspaceIn(BaseModel):
    name: str


class UpdateAdminTaskIn(BaseModel):
    status: Optional[str] = None
    deadline: Optional[str] = None


class CreateAdminGrantIn(BaseModel):
    email: str


class CreatePromoCodeIn(BaseModel):
    code: str
    description: Optional[str] = None
    discount_type: str = "percent"
    discount_value: int
    active: bool = True
    max_uses: Optional[int] = None
    expires_at: Optional[str] = None


class UpdatePromoCodeIn(BaseModel):
    description: Optional[str] = None
    discount_type: Optional[str] = None
    discount_value: Optional[int] = None
    active: Optional[bool] = None
    max_uses: Optional[int] = None
    expires_at: Optional[str] = None


VALID_PLANS = {"free", "pro", "team"}
VALID_TASK_STATUSES = {"todo", "in_progress", "done"}
VALID_PROMO_TYPES = {"percent", "fixed"}


def _configured_admin_emails() -> set[str]:
    return {
        email.strip().lower()
        for email in os.environ.get("ADMIN_EMAILS", "").split(",")
        if email.strip()
    }


def _promo_status(code: PromoCode) -> str:
    now = datetime.utcnow()
    if not code.active:
        return "inactive"
    if code.expires_at and code.expires_at <= now:
        return "expired"
    if code.max_uses is not None and code.uses_count >= code.max_uses:
        return "exhausted"
    return "active"


def _admin_state_for_email(email: str, db: Session) -> dict:
    normalized = (email or "").strip().lower()
    env_admins = _configured_admin_emails()
    grant = db.query(AdminGrant).filter(AdminGrant.email == normalized).first()
    if normalized in env_admins:
        return {"is_admin": True, "admin_source": "env", "admin_grant_id": grant.id if grant else None}
    if grant:
        return {"is_admin": True, "admin_source": "database", "admin_grant_id": grant.id}
    return {"is_admin": False, "admin_source": "none", "admin_grant_id": None}


def _safe_minutes(record: JobRecord | None) -> dict:
    if not record:
        return {}
    try:
        return json.loads(record.minutes_json) if record.minutes_json else {}
    except Exception:
        return {}


def _meeting_title(record: JobRecord | None) -> str:
    data = _safe_minutes(record)
    return data.get("title") or "Untitled Meeting"


def _meeting_participants(record: JobRecord | None) -> int:
    meta = _safe_minutes(record).get("metadata", {})
    participants = meta.get("participants", [])
    return len(participants) if isinstance(participants, list) else 0


def _meeting_actions(record: JobRecord | None) -> int:
    data = _safe_minutes(record)
    action_items = data.get("action_items", data.get("actionItems", []))
    return len(action_items) if isinstance(action_items, list) else 0


def _meeting_duration(record: JobRecord | None) -> str:
    meta = _safe_minutes(record).get("metadata", {})
    return meta.get("duration", "—")


def _duration_minutes(record: JobRecord | None) -> int:
    raw = _meeting_duration(record)
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


def _user_row(user: User, db: Session) -> dict:
    workspace_count = db.query(func.count(Workspace.id)).filter(Workspace.owner_id == user.id).scalar() or 0
    meeting_count = db.query(func.count(JobRecord.id)).filter(JobRecord.user_id == user.id).scalar() or 0
    assigned_tasks = db.query(func.count(TaskRecord.id)).filter(TaskRecord.assignee_id == user.id).scalar() or 0
    active_memberships = db.query(func.count(TeamMember.id)).filter(
        TeamMember.user_id == user.id,
        TeamMember.status == "active",
    ).scalar() or 0
    admin_state = _admin_state_for_email(user.email, db)
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "plan": user.plan,
        "email_verified": bool(user.email_verified),
        "workspace_count": workspace_count,
        "meeting_count": meeting_count,
        "assigned_tasks": assigned_tasks,
        "team_memberships": active_memberships,
        "stripe_customer_id": user.stripe_customer_id,
        "stripe_subscription_id": user.stripe_subscription_id,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        **admin_state,
    }


def _workspace_row(workspace: Workspace, owner: User | None, db: Session) -> dict:
    member_count = db.query(func.count(TeamMember.id)).filter(
        TeamMember.workspace_id == workspace.id,
        TeamMember.status == "active",
    ).scalar() or 0
    meeting_count = db.query(func.count(JobRecord.id)).filter(JobRecord.workspace_id == workspace.id).scalar() or 0
    task_count = db.query(func.count(TaskRecord.id)).filter(TaskRecord.workspace_id == workspace.id).scalar() or 0
    return {
        "id": workspace.id,
        "name": workspace.name,
        "owner_id": workspace.owner_id,
        "owner_name": owner.name if owner else "Unknown owner",
        "owner_email": owner.email if owner else "",
        "member_count": member_count + 1,
        "meeting_count": meeting_count,
        "task_count": task_count,
        "created_at": workspace.created_at.isoformat() if workspace.created_at else None,
    }


def _meeting_row(meeting: JobRecord, db: Session) -> dict:
    owner = db.get(User, meeting.user_id) if meeting.user_id else None
    return {
        "id": meeting.id,
        "title": _meeting_title(meeting),
        "status": meeting.status,
        "owner_id": owner.id if owner else None,
        "owner_name": owner.name if owner else None,
        "owner_email": owner.email if owner else meeting.email,
        "workspace_id": meeting.workspace_id,
        "participants": _meeting_participants(meeting),
        "actions": _meeting_actions(meeting),
        "duration": _meeting_duration(meeting),
        "starred": bool(meeting.starred),
        "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
    }


def _task_row(task: TaskRecord, db: Session) -> dict:
    assignee = db.get(User, task.assignee_id) if task.assignee_id else None
    meeting = db.get(JobRecord, task.job_id) if task.job_id else None
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "deadline": task.deadline,
        "workspace_id": task.workspace_id,
        "meeting_id": task.job_id,
        "meeting_title": _meeting_title(meeting) if meeting else None,
        "assignee_id": assignee.id if assignee else None,
        "assignee_name": assignee.name if assignee else None,
        "assignee_email": assignee.email if assignee else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def _admin_grant_row(grant: AdminGrant, db: Session) -> dict:
    creator = db.get(User, grant.created_by_id) if grant.created_by_id else None
    linked_user = db.query(User).filter(User.email == grant.email).first()
    return {
        "id": grant.id,
        "email": grant.email,
        "created_by_id": grant.created_by_id,
        "created_by_name": creator.name if creator else None,
        "linked_user_id": linked_user.id if linked_user else None,
        "linked_user_name": linked_user.name if linked_user else None,
        "created_at": grant.created_at.isoformat() if grant.created_at else None,
        "source": "database",
    }


def _promo_code_row(code: PromoCode, db: Session) -> dict:
    creator = db.get(User, code.created_by_id) if code.created_by_id else None
    status_value = _promo_status(code)
    return {
        "id": code.id,
        "code": code.code,
        "description": code.description,
        "discount_type": code.discount_type,
        "discount_value": code.discount_value,
        "active": bool(code.active),
        "status": status_value,
        "max_uses": code.max_uses,
        "uses_count": code.uses_count,
        "remaining_uses": None if code.max_uses is None else max(code.max_uses - code.uses_count, 0),
        "expires_at": code.expires_at.isoformat() if code.expires_at else None,
        "created_by_name": creator.name if creator else None,
        "created_at": code.created_at.isoformat() if code.created_at else None,
    }


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


@router.get("/me")
def admin_me(current_user: User = Depends(get_admin_user)):
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "plan": current_user.plan,
        "avatar_url": current_user.avatar_url,
    }


@router.get("/admins")
def admin_grants(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    env_admins = []
    for email in sorted(_configured_admin_emails()):
        linked_user = db.query(User).filter(User.email == email).first()
        env_admins.append({
            "id": f"env:{email}",
            "email": email,
            "created_by_id": None,
            "created_by_name": None,
            "linked_user_id": linked_user.id if linked_user else None,
            "linked_user_name": linked_user.name if linked_user else None,
            "created_at": None,
            "source": "env",
        })
    db_admins = db.query(AdminGrant).order_by(AdminGrant.created_at.desc()).all()
    return {
        "env_admins": env_admins,
        "database_admins": [_admin_grant_row(grant, db) for grant in db_admins],
    }


@router.post("/admins", status_code=201)
def admin_create_grant(
    body: CreateAdminGrantIn,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email is required.")
    existing = db.query(AdminGrant).filter(AdminGrant.email == email).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "This email already has a database admin grant.")

    grant = AdminGrant(email=email, created_by_id=current_admin.id)
    db.add(grant)
    db.commit()
    db.refresh(grant)
    return _admin_grant_row(grant, db)


@router.delete("/admins/{grant_id}", status_code=204)
def admin_delete_grant(
    grant_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    grant = db.get(AdminGrant, grant_id)
    if not grant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Admin grant not found.")
    db.delete(grant)
    db.commit()


@router.get("/overview")
def admin_overview(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    users = db.query(User).all()
    workspaces = db.query(Workspace).all()
    meetings = db.query(JobRecord).all()
    tasks = db.query(TaskRecord).all()

    plan_breakdown = {
        "free": sum(1 for user in users if (user.plan or "free") == "free"),
        "pro": sum(1 for user in users if user.plan == "pro"),
        "team": sum(1 for user in users if user.plan == "team"),
    }
    meeting_status = {
        "done": sum(1 for meeting in meetings if meeting.status == "done"),
        "processing": sum(1 for meeting in meetings if meeting.status not in {"done", "error"}),
        "error": sum(1 for meeting in meetings if meeting.status == "error"),
    }
    task_status = {
        "todo": sum(1 for task in tasks if task.status == "todo"),
        "in_progress": sum(1 for task in tasks if task.status == "in_progress"),
        "done": sum(1 for task in tasks if task.status == "done"),
    }
    billing = {
        "paid_customers": sum(1 for user in users if user.plan in {"pro", "team"}),
        "stripe_customers": sum(1 for user in users if bool(user.stripe_customer_id)),
        "active_subscriptions": sum(1 for user in users if bool(user.stripe_subscription_id)),
    }

    month_labels = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    usage_by_month = [
        {"label": label, "minutes_generated": 0, "meetings": 0, "action_items": 0}
        for label in month_labels
    ]

    total_minutes_generated = 0
    total_action_items = 0
    total_participants = 0

    for meeting in meetings:
        created_at = meeting.created_at or datetime.utcnow()
        month_index = created_at.month - 1
        duration_minutes = _duration_minutes(meeting)
        action_items = _meeting_actions(meeting)
        participants = _meeting_participants(meeting)

        usage_by_month[month_index]["minutes_generated"] += duration_minutes
        usage_by_month[month_index]["meetings"] += 1
        usage_by_month[month_index]["action_items"] += action_items

        total_minutes_generated += duration_minutes
        total_action_items += action_items
        total_participants += participants

    avg_minutes_per_meeting = round(total_minutes_generated / len(meetings), 1) if meetings else 0
    avg_action_items_per_meeting = round(total_action_items / len(meetings), 1) if meetings else 0
    avg_participants_per_meeting = round(total_participants / len(meetings), 1) if meetings else 0

    recent_users = db.query(User).order_by(User.created_at.desc()).limit(5).all()
    recent_meetings = db.query(JobRecord).order_by(JobRecord.created_at.desc()).limit(5).all()

    return {
        "stats": {
            "users": len(users),
            "verified_users": sum(1 for user in users if user.email_verified),
            "workspaces": len(workspaces),
            "meetings": len(meetings),
            "tasks": len(tasks),
        },
        "plan_breakdown": plan_breakdown,
        "meeting_status": meeting_status,
        "task_status": task_status,
        "billing": billing,
        "usage": {
            "total_minutes_generated": total_minutes_generated,
            "avg_minutes_per_meeting": avg_minutes_per_meeting,
            "avg_action_items_per_meeting": avg_action_items_per_meeting,
            "avg_participants_per_meeting": avg_participants_per_meeting,
            "monthly": usage_by_month,
        },
        "recent_users": [
            {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "plan": user.plan,
                "created_at": user.created_at.isoformat() if user.created_at else None,
            }
            for user in recent_users
        ],
        "recent_meetings": [_meeting_row(meeting, db) for meeting in recent_meetings],
    }


@router.get("/users")
def admin_users(
    q: str = Query("", max_length=100),
    plan: str = Query(""),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    query = db.query(User)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(User.name.ilike(like), User.email.ilike(like)))
    if plan.strip():
        query = query.filter(User.plan == plan.strip())
    users = query.order_by(User.created_at.desc()).limit(250).all()
    return [_user_row(user, db) for user in users]


@router.get("/users/{user_id}")
def admin_user_detail(
    user_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    owned_workspaces = db.query(Workspace).filter(Workspace.owner_id == user.id).order_by(Workspace.created_at.desc()).all()
    user_meetings = db.query(JobRecord).filter(JobRecord.user_id == user.id).order_by(JobRecord.created_at.desc()).all()
    assigned_tasks = db.query(TaskRecord).filter(TaskRecord.assignee_id == user.id).order_by(TaskRecord.created_at.desc()).all()
    membership_rows = db.query(TeamMember).filter(
        TeamMember.user_id == user.id,
        TeamMember.status == "active",
    ).order_by(TeamMember.created_at.desc()).all()

    membership_workspaces = []
    for membership in membership_rows:
        if not membership.workspace_id:
            continue
        workspace = db.get(Workspace, membership.workspace_id)
        owner = db.get(User, workspace.owner_id) if workspace else None
        if workspace:
            membership_workspaces.append({
                **_workspace_row(workspace, owner, db),
                "membership_role": membership.role,
                "membership_status": membership.status,
            })

    return {
        **_user_row(user, db),
        "workspaces": [_workspace_row(workspace, user, db) for workspace in owned_workspaces],
        "member_workspaces": membership_workspaces,
        "meetings": [_meeting_row(meeting, db) for meeting in user_meetings],
        "tasks": [_task_row(task, db) for task in assigned_tasks],
    }


@router.patch("/users/{user_id}")
def admin_update_user(
    user_id: str,
    body: UpdateAdminUserIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name cannot be empty.")
        user.name = body.name.strip()
    if body.plan is not None:
        if body.plan not in VALID_PLANS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Plan must be one of: free, pro, team.")
        user.plan = body.plan
    if body.email_verified is not None:
        user.email_verified = body.email_verified

    db.commit()
    db.refresh(user)
    return _user_row(user, db)


@router.post("/users/{user_id}/admin")
def admin_promote_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    state = _admin_state_for_email(user.email, db)
    if state["admin_source"] == "env":
        raise HTTPException(status.HTTP_409_CONFLICT, "This user is already an admin via ADMIN_EMAILS.")
    if state["admin_source"] == "database":
        return _user_row(user, db)

    grant = AdminGrant(email=user.email.lower(), created_by_id=current_admin.id)
    db.add(grant)
    db.commit()
    db.refresh(user)
    return _user_row(user, db)


@router.delete("/users/{user_id}/admin")
def admin_demote_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    if user.id == current_admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot revoke your own active admin access.")

    state = _admin_state_for_email(user.email, db)
    if state["admin_source"] == "env":
        raise HTTPException(status.HTTP_409_CONFLICT, "This user is an env admin. Remove them from ADMIN_EMAILS instead.")
    if not state["admin_grant_id"]:
        return _user_row(user, db)

    grant = db.get(AdminGrant, state["admin_grant_id"])
    if grant:
        db.delete(grant)
        db.commit()
    db.refresh(user)
    return _user_row(user, db)


@router.delete("/users/{user_id}", status_code=204)
def admin_delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    if user.id == current_admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete the currently signed-in admin.")

    owned_workspace_ids = [workspace.id for workspace in db.query(Workspace).filter(Workspace.owner_id == user.id).all()]
    if owned_workspace_ids:
        db.query(TeamMember).filter(TeamMember.workspace_id.in_(owned_workspace_ids)).delete(synchronize_session=False)
        db.query(TaskRecord).filter(TaskRecord.workspace_id.in_(owned_workspace_ids)).delete(synchronize_session=False)
        db.query(JobRecord).filter(JobRecord.workspace_id.in_(owned_workspace_ids)).delete(synchronize_session=False)
        db.query(Workspace).filter(Workspace.id.in_(owned_workspace_ids)).delete(synchronize_session=False)

    db.query(TaskRecord).filter(TaskRecord.assignee_id == user.id).delete(synchronize_session=False)
    db.query(JobRecord).filter(JobRecord.user_id == user.id).delete(synchronize_session=False)
    db.query(NotificationRecord).filter(NotificationRecord.user_id == user.id).delete(synchronize_session=False)
    db.query(OTPCode).filter(OTPCode.user_id == user.id).delete(synchronize_session=False)
    db.query(TeamMember).filter(
        or_(
            TeamMember.user_id == user.id,
            TeamMember.owner_id == user.id,
            TeamMember.invited_email == user.email,
        )
    ).delete(synchronize_session=False)

    db.delete(user)
    db.commit()


@router.get("/workspaces")
def admin_workspaces(
    q: str = Query("", max_length=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    query = db.query(Workspace, User).join(User, User.id == Workspace.owner_id)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(Workspace.name.ilike(like), User.email.ilike(like), User.name.ilike(like)))
    rows = query.order_by(Workspace.created_at.desc()).limit(250).all()
    return [_workspace_row(workspace, owner, db) for workspace, owner in rows]


@router.get("/workspaces/{workspace_id}")
def admin_workspace_detail(
    workspace_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    workspace = db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
    owner = db.get(User, workspace.owner_id)
    members = db.query(TeamMember).filter(TeamMember.workspace_id == workspace_id).order_by(TeamMember.created_at.desc()).all()
    meetings = db.query(JobRecord).filter(JobRecord.workspace_id == workspace_id).order_by(JobRecord.created_at.desc()).limit(8).all()
    tasks = db.query(TaskRecord).filter(TaskRecord.workspace_id == workspace_id).order_by(TaskRecord.created_at.desc()).limit(8).all()

    return {
        **_workspace_row(workspace, owner, db),
        "members": [
            {
                "id": member.id,
                "email": member.invited_email,
                "name": member.name,
                "role": member.role,
                "status": member.status,
                "user_id": member.user_id,
                "created_at": member.created_at.isoformat() if member.created_at else None,
            }
            for member in members
        ],
        "recent_meetings": [_meeting_row(meeting, db) for meeting in meetings],
        "recent_tasks": [_task_row(task, db) for task in tasks],
    }


@router.patch("/workspaces/{workspace_id}")
def admin_update_workspace(
    workspace_id: str,
    body: UpdateAdminWorkspaceIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    workspace = db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
    if not body.name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Workspace name cannot be empty.")
    workspace.name = body.name.strip()
    db.commit()
    db.refresh(workspace)
    owner = db.get(User, workspace.owner_id)
    return _workspace_row(workspace, owner, db)


@router.delete("/workspaces/{workspace_id}", status_code=204)
def admin_delete_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    workspace = db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")

    db.query(TeamMember).filter(TeamMember.workspace_id == workspace_id).delete(synchronize_session=False)
    db.query(TaskRecord).filter(TaskRecord.workspace_id == workspace_id).delete(synchronize_session=False)
    db.query(JobRecord).filter(JobRecord.workspace_id == workspace_id).delete(synchronize_session=False)
    db.delete(workspace)
    db.commit()


@router.get("/meetings")
def admin_meetings(
    q: str = Query("", max_length=100),
    status_filter: str = Query("", alias="status"),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    query = db.query(JobRecord)
    if status_filter.strip():
        query = query.filter(JobRecord.status == status_filter.strip())

    meetings = query.order_by(JobRecord.created_at.desc()).limit(300).all()
    result = []
    search = q.strip().lower()
    for meeting in meetings:
        row = _meeting_row(meeting, db)
        haystack = " ".join(filter(None, [row["title"], row["owner_name"], row["owner_email"]])).lower()
        if search and search not in haystack:
            continue
        result.append(row)
    return result


@router.get("/meetings/{meeting_id}")
def admin_meeting_detail(
    meeting_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    meeting = db.get(JobRecord, meeting_id)
    if not meeting:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")
    tasks = db.query(TaskRecord).filter(TaskRecord.job_id == meeting_id).order_by(TaskRecord.created_at.desc()).all()
    data = _safe_minutes(meeting)
    return {
        **_meeting_row(meeting, db),
        "transcript": meeting.transcript,
        "message": meeting.message,
        "minutes": data,
        "docx_path": meeting.docx_path,
        "pdf_path": meeting.pdf_path,
        "tasks": [_task_row(task, db) for task in tasks],
    }


@router.delete("/meetings/{meeting_id}", status_code=204)
def admin_delete_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    meeting = db.get(JobRecord, meeting_id)
    if not meeting:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")
    db.query(TaskRecord).filter(TaskRecord.job_id == meeting_id).delete(synchronize_session=False)
    db.delete(meeting)
    db.commit()


@router.get("/tasks")
def admin_tasks(
    q: str = Query("", max_length=100),
    status_filter: str = Query("", alias="status"),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    query = db.query(TaskRecord)
    if status_filter.strip():
        query = query.filter(TaskRecord.status == status_filter.strip())

    tasks = query.order_by(TaskRecord.created_at.desc()).limit(300).all()
    result = []
    search = q.strip().lower()
    for task in tasks:
        row = _task_row(task, db)
        haystack = " ".join(filter(None, [row["title"], row["meeting_title"], row["assignee_name"], row["assignee_email"]])).lower()
        if search and search not in haystack:
            continue
        result.append(row)
    return result


@router.get("/tasks/{task_id}")
def admin_task_detail(
    task_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    task = db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found.")
    return _task_row(task, db)


@router.patch("/tasks/{task_id}")
def admin_update_task(
    task_id: str,
    body: UpdateAdminTaskIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    task = db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found.")

    if body.status is not None:
        if body.status not in VALID_TASK_STATUSES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Status must be one of: todo, in_progress, done.")
        task.status = body.status
    if body.deadline is not None:
        task.deadline = body.deadline or None

    db.commit()
    db.refresh(task)
    return _task_row(task, db)


@router.delete("/tasks/{task_id}", status_code=204)
def admin_delete_task(
    task_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    task = db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found.")
    db.delete(task)
    db.commit()


@router.get("/billing/summary")
def admin_billing_summary(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    users = db.query(User).all()
    return {
        "totals": {
            "free": sum(1 for user in users if (user.plan or "free") == "free"),
            "pro": sum(1 for user in users if user.plan == "pro"),
            "team": sum(1 for user in users if user.plan == "team"),
            "stripe_customers": sum(1 for user in users if bool(user.stripe_customer_id)),
            "active_subscriptions": sum(1 for user in users if bool(user.stripe_subscription_id)),
        }
    }


@router.get("/billing/customers")
def admin_billing_customers(
    q: str = Query("", max_length=100),
    plan: str = Query(""),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    query = db.query(User)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(User.name.ilike(like), User.email.ilike(like)))
    if plan.strip():
        query = query.filter(User.plan == plan.strip())

    users = query.order_by(User.created_at.desc()).limit(250).all()
    return [
        {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "plan": user.plan,
            "stripe_customer_id": user.stripe_customer_id,
            "stripe_subscription_id": user.stripe_subscription_id,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }
        for user in users
    ]


@router.get("/promocodes")
def admin_promo_codes(
    q: str = Query("", max_length=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    query = db.query(PromoCode)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(PromoCode.code.ilike(like), PromoCode.description.ilike(like)))
    codes = query.order_by(PromoCode.created_at.desc()).all()
    rows = [_promo_code_row(code, db) for code in codes]
    return {
        "stats": {
            "total": len(rows),
            "active": sum(1 for row in rows if row["status"] == "active"),
            "expired": sum(1 for row in rows if row["status"] == "expired"),
            "exhausted": sum(1 for row in rows if row["status"] == "exhausted"),
            "inactive": sum(1 for row in rows if row["status"] == "inactive"),
            "redemptions": sum(row["uses_count"] for row in rows),
        },
        "codes": rows,
    }


@router.post("/promocodes", status_code=201)
def admin_create_promo_code(
    body: CreatePromoCodeIn,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    code_value = body.code.strip().upper()
    if not code_value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Promo code is required.")
    if body.discount_type not in VALID_PROMO_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Discount type must be percent or fixed.")
    if body.discount_value <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Discount value must be greater than zero.")
    if body.discount_type == "percent" and body.discount_value > 100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Percent discounts cannot exceed 100.")
    if body.max_uses is not None and body.max_uses < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Max uses must be at least 1.")
    if db.query(PromoCode).filter(PromoCode.code == code_value).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Promo code already exists.")

    promo = PromoCode(
        code=code_value,
        description=body.description.strip() if body.description else None,
        discount_type=body.discount_type,
        discount_value=body.discount_value,
        active=body.active,
        max_uses=body.max_uses,
        expires_at=_parse_datetime(body.expires_at),
        created_by_id=current_admin.id,
    )
    db.add(promo)
    db.commit()
    db.refresh(promo)
    return _promo_code_row(promo, db)


@router.patch("/promocodes/{promo_id}")
def admin_update_promo_code(
    promo_id: str,
    body: UpdatePromoCodeIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    promo = db.get(PromoCode, promo_id)
    if not promo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Promo code not found.")
    if body.description is not None:
        promo.description = body.description.strip() if body.description else None
    if body.discount_type is not None:
        if body.discount_type not in VALID_PROMO_TYPES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Discount type must be percent or fixed.")
        promo.discount_type = body.discount_type
    if body.discount_value is not None:
        if body.discount_value <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Discount value must be greater than zero.")
        check_type = body.discount_type or promo.discount_type
        if check_type == "percent" and body.discount_value > 100:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Percent discounts cannot exceed 100.")
        promo.discount_value = body.discount_value
    if body.active is not None:
        promo.active = body.active
    if body.max_uses is not None:
        if body.max_uses < 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Max uses must be at least 1.")
        promo.max_uses = body.max_uses
    if body.expires_at is not None:
        promo.expires_at = _parse_datetime(body.expires_at)
    db.commit()
    db.refresh(promo)
    return _promo_code_row(promo, db)


@router.delete("/promocodes/{promo_id}", status_code=204)
def admin_delete_promo_code(
    promo_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    promo = db.get(PromoCode, promo_id)
    if not promo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Promo code not found.")
    db.delete(promo)
    db.commit()
