from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from middleware.auth import get_admin_user, get_db
from models.job import JobRecord
from models.notification import NotificationRecord
from models.task import TaskRecord
from models.user import AdminGrant, OTPCode, PromoCode, SubscriptionPlan, TeamMember, User, Workspace
from schemas.admin import (
    CreateAdminGrantIn, CreatePlanIn, CreatePromoCodeIn, UpdateAdminTaskIn,
    UpdateAdminUserIn, UpdateAdminWorkspaceIn, UpdatePlanIn, UpdatePromoCodeIn,
)
import services.admin as svc

router = APIRouter(prefix="/admin", tags=["admin"])

VALID_PLANS = {"free", "pro", "team"}  # used only as fallback; DB plans take precedence
VALID_TASK_STATUSES = {"todo", "in_progress", "done"}
VALID_PROMO_TYPES = {"percent", "fixed"}


@router.get("/me")
def admin_me(current_user: User = Depends(get_admin_user)):
    return {
        "id": current_user.id, "name": current_user.name,
        "email": current_user.email, "plan": current_user.plan,
        "avatar_url": current_user.avatar_url,
    }


@router.get("/admins")
def admin_grants(db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    env_admins = []
    for email in sorted(svc.configured_admin_emails()):
        linked_user = db.query(User).filter(User.email == email).first()
        env_admins.append({
            "id": f"env:{email}", "email": email,
            "created_by_id": None, "created_by_name": None,
            "linked_user_id": linked_user.id if linked_user else None,
            "linked_user_name": linked_user.name if linked_user else None,
            "created_at": None, "source": "env",
        })
    db_admins = db.query(AdminGrant).order_by(AdminGrant.created_at.desc()).all()
    return {
        "env_admins": env_admins,
        "database_admins": [svc.admin_grant_row(grant, db) for grant in db_admins],
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
    return svc.admin_grant_row(grant, db)


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
def admin_overview(db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    from datetime import datetime
    users     = db.query(User).all()
    workspaces = db.query(Workspace).all()
    meetings  = db.query(JobRecord).all()
    tasks     = db.query(TaskRecord).all()

    from collections import Counter
    plan_counts = Counter((u.plan or "free") for u in users)
    plan_breakdown = dict(plan_counts)
    meeting_status = {
        "done":       sum(1 for m in meetings if m.status == "done"),
        "processing": sum(1 for m in meetings if m.status not in {"done", "error"}),
        "error":      sum(1 for m in meetings if m.status == "error"),
    }
    task_status = {
        "todo":        sum(1 for t in tasks if t.status == "todo"),
        "in_progress": sum(1 for t in tasks if t.status == "in_progress"),
        "done":        sum(1 for t in tasks if t.status == "done"),
    }
    free_plan_names = {
        p.name for p in db.query(SubscriptionPlan).filter(SubscriptionPlan.price_monthly == 0).all()
    } | {"free"}
    billing = {
        "paid_customers":       sum(1 for u in users if (u.plan or "free") not in free_plan_names),
        "stripe_customers":     sum(1 for u in users if bool(u.stripe_customer_id)),
        "active_subscriptions": sum(1 for u in users if bool(u.stripe_subscription_id)),
    }

    month_labels = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    usage_by_month = [
        {"label": label, "minutes_generated": 0, "meetings": 0, "action_items": 0}
        for label in month_labels
    ]
    total_minutes_generated = total_action_items = total_participants = 0

    for meeting in meetings:
        created_at = meeting.created_at or datetime.utcnow()
        idx = created_at.month - 1
        dur = svc.duration_minutes(meeting)
        ais = svc.meeting_actions(meeting)
        pts = svc.meeting_participants(meeting)
        usage_by_month[idx]["minutes_generated"] += dur
        usage_by_month[idx]["meetings"] += 1
        usage_by_month[idx]["action_items"] += ais
        total_minutes_generated += dur
        total_action_items += ais
        total_participants += pts

    count = len(meetings) or 1
    recent_users    = db.query(User).order_by(User.created_at.desc()).limit(5).all()
    recent_meetings = db.query(JobRecord).order_by(JobRecord.created_at.desc()).limit(5).all()

    return {
        "stats": {
            "users": len(users), "verified_users": sum(1 for u in users if u.email_verified),
            "workspaces": len(workspaces), "meetings": len(meetings), "tasks": len(tasks),
        },
        "plan_breakdown": plan_breakdown,
        "meeting_status": meeting_status,
        "task_status": task_status,
        "billing": billing,
        "usage": {
            "total_minutes_generated": total_minutes_generated,
            "avg_minutes_per_meeting": round(total_minutes_generated / count, 1),
            "avg_action_items_per_meeting": round(total_action_items / count, 1),
            "avg_participants_per_meeting": round(total_participants / count, 1),
            "monthly": usage_by_month,
        },
        "recent_users": [
            {"id": u.id, "name": u.name, "email": u.email, "plan": u.plan,
             "created_at": u.created_at.isoformat() if u.created_at else None}
            for u in recent_users
        ],
        "recent_meetings": [svc.meeting_row(m, db) for m in recent_meetings],
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
    return [svc.user_row(u, db) for u in users]


@router.get("/users/{user_id}")
def admin_user_detail(user_id: str, db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    owned_workspaces = db.query(Workspace).filter(Workspace.owner_id == user.id).order_by(Workspace.created_at.desc()).all()
    user_meetings    = db.query(JobRecord).filter(JobRecord.user_id == user.id).order_by(JobRecord.created_at.desc()).all()
    assigned_tasks   = db.query(TaskRecord).filter(TaskRecord.assignee_id == user.id).order_by(TaskRecord.created_at.desc()).all()
    membership_rows  = db.query(TeamMember).filter(TeamMember.user_id == user.id, TeamMember.status == "active").order_by(TeamMember.created_at.desc()).all()

    membership_workspaces = []
    for membership in membership_rows:
        if not membership.workspace_id:
            continue
        ws    = db.get(Workspace, membership.workspace_id)
        owner = db.get(User, ws.owner_id) if ws else None
        if ws:
            membership_workspaces.append({
                **svc.workspace_row(ws, owner, db),
                "membership_role": membership.role,
                "membership_status": membership.status,
            })

    return {
        **svc.user_row(user, db),
        "workspaces": [svc.workspace_row(ws, user, db) for ws in owned_workspaces],
        "member_workspaces": membership_workspaces,
        "meetings": [svc.meeting_row(m, db) for m in user_meetings],
        "tasks": [svc.task_row(t, db) for t in assigned_tasks],
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
        db_plan_names = {p.name for p in db.query(SubscriptionPlan).all()}
        allowed = db_plan_names | VALID_PLANS
        if body.plan not in allowed:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"'{body.plan}' is not a recognised plan. Create it in the Plans section first.",
            )
        user.plan = body.plan
    if body.email_verified is not None:
        user.email_verified = body.email_verified
    db.commit()
    db.refresh(user)
    return svc.user_row(user, db)


@router.post("/users/{user_id}/admin")
def admin_promote_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    state = svc.admin_state_for_email(user.email, db)
    if state["admin_source"] == "env":
        raise HTTPException(status.HTTP_409_CONFLICT, "This user is already an admin via ADMIN_EMAILS.")
    if state["admin_source"] == "database":
        return svc.user_row(user, db)
    grant = AdminGrant(email=user.email.lower(), created_by_id=current_admin.id)
    db.add(grant)
    db.commit()
    db.refresh(user)
    return svc.user_row(user, db)


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
    state = svc.admin_state_for_email(user.email, db)
    if state["admin_source"] == "env":
        raise HTTPException(status.HTTP_409_CONFLICT, "This user is an env admin. Remove them from ADMIN_EMAILS instead.")
    if not state["admin_grant_id"]:
        return svc.user_row(user, db)
    grant = db.get(AdminGrant, state["admin_grant_id"])
    if grant:
        db.delete(grant)
        db.commit()
    db.refresh(user)
    return svc.user_row(user, db)


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

    owned_ws_ids = [ws.id for ws in db.query(Workspace).filter(Workspace.owner_id == user.id).all()]
    if owned_ws_ids:
        db.query(TeamMember).filter(TeamMember.workspace_id.in_(owned_ws_ids)).delete(synchronize_session=False)
        db.query(TaskRecord).filter(TaskRecord.workspace_id.in_(owned_ws_ids)).delete(synchronize_session=False)
        db.query(JobRecord).filter(JobRecord.workspace_id.in_(owned_ws_ids)).delete(synchronize_session=False)
        db.query(Workspace).filter(Workspace.id.in_(owned_ws_ids)).delete(synchronize_session=False)

    db.query(TaskRecord).filter(TaskRecord.assignee_id == user.id).delete(synchronize_session=False)
    db.query(JobRecord).filter(JobRecord.user_id == user.id).delete(synchronize_session=False)
    db.query(NotificationRecord).filter(NotificationRecord.user_id == user.id).delete(synchronize_session=False)
    db.query(OTPCode).filter(OTPCode.user_id == user.id).delete(synchronize_session=False)
    db.query(TeamMember).filter(
        or_(TeamMember.user_id == user.id, TeamMember.owner_id == user.id, TeamMember.invited_email == user.email)
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
    return [svc.workspace_row(ws, owner, db) for ws, owner in rows]


@router.get("/workspaces/{workspace_id}")
def admin_workspace_detail(workspace_id: str, db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    workspace = db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
    owner   = db.get(User, workspace.owner_id)
    members = db.query(TeamMember).filter(TeamMember.workspace_id == workspace_id).order_by(TeamMember.created_at.desc()).all()
    meetings = db.query(JobRecord).filter(JobRecord.workspace_id == workspace_id).order_by(JobRecord.created_at.desc()).limit(8).all()
    tasks    = db.query(TaskRecord).filter(TaskRecord.workspace_id == workspace_id).order_by(TaskRecord.created_at.desc()).limit(8).all()
    return {
        **svc.workspace_row(workspace, owner, db),
        "members": [
            {"id": m.id, "email": m.invited_email, "name": m.name, "role": m.role,
             "status": m.status, "user_id": m.user_id,
             "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in members
        ],
        "recent_meetings": [svc.meeting_row(m, db) for m in meetings],
        "recent_tasks":    [svc.task_row(t, db)    for t in tasks],
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
    return svc.workspace_row(workspace, owner, db)


@router.delete("/workspaces/{workspace_id}", status_code=204)
def admin_delete_workspace(workspace_id: str, db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
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
    search = q.strip().lower()
    result = []
    for meeting in meetings:
        row = svc.meeting_row(meeting, db)
        haystack = " ".join(filter(None, [row["title"], row["owner_name"], row["owner_email"]])).lower()
        if search and search not in haystack:
            continue
        result.append(row)
    return result


@router.get("/meetings/{meeting_id}")
def admin_meeting_detail(meeting_id: str, db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    meeting = db.get(JobRecord, meeting_id)
    if not meeting:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")
    tasks = db.query(TaskRecord).filter(TaskRecord.job_id == meeting_id).order_by(TaskRecord.created_at.desc()).all()
    data = svc.safe_minutes(meeting)
    return {
        **svc.meeting_row(meeting, db),
        "transcript": meeting.transcript,
        "message":    meeting.message,
        "minutes":    data,
        "docx_path":  meeting.docx_path,
        "pdf_path":   meeting.pdf_path,
        "tasks":      [svc.task_row(t, db) for t in tasks],
    }


@router.delete("/meetings/{meeting_id}", status_code=204)
def admin_delete_meeting(meeting_id: str, db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
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
    search = q.strip().lower()
    result = []
    for task in tasks:
        row = svc.task_row(task, db)
        haystack = " ".join(filter(None, [row["title"], row["meeting_title"], row["assignee_name"], row["assignee_email"]])).lower()
        if search and search not in haystack:
            continue
        result.append(row)
    return result


@router.get("/tasks/{task_id}")
def admin_task_detail(task_id: str, db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    task = db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found.")
    return svc.task_row(task, db)


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
    return svc.task_row(task, db)


@router.delete("/tasks/{task_id}", status_code=204)
def admin_delete_task(task_id: str, db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    task = db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found.")
    db.delete(task)
    db.commit()


@router.get("/billing/summary")
def admin_billing_summary(db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    users = db.query(User).all()
    return {
        "totals": {
            "free":  sum(1 for u in users if (u.plan or "free") == "free"),
            "pro":   sum(1 for u in users if u.plan == "pro"),
            "team":  sum(1 for u in users if u.plan == "team"),
            "stripe_customers":     sum(1 for u in users if bool(u.stripe_customer_id)),
            "active_subscriptions": sum(1 for u in users if bool(u.stripe_subscription_id)),
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
        {"id": u.id, "name": u.name, "email": u.email, "plan": u.plan,
         "stripe_customer_id": u.stripe_customer_id,
         "stripe_subscription_id": u.stripe_subscription_id,
         "created_at": u.created_at.isoformat() if u.created_at else None}
        for u in users
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
    rows = [svc.promo_code_row(code, db) for code in codes]
    return {
        "stats": {
            "total":       len(rows),
            "active":      sum(1 for r in rows if r["status"] == "active"),
            "expired":     sum(1 for r in rows if r["status"] == "expired"),
            "exhausted":   sum(1 for r in rows if r["status"] == "exhausted"),
            "inactive":    sum(1 for r in rows if r["status"] == "inactive"),
            "redemptions": sum(r["uses_count"] for r in rows),
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
        expires_at=svc.parse_datetime(body.expires_at),
        created_by_id=current_admin.id,
    )
    db.add(promo)
    db.commit()
    db.refresh(promo)
    return svc.promo_code_row(promo, db)


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
        promo.expires_at = svc.parse_datetime(body.expires_at)
    db.commit()
    db.refresh(promo)
    return svc.promo_code_row(promo, db)


@router.delete("/promocodes/{promo_id}", status_code=204)
def admin_delete_promo_code(promo_id: str, db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    promo = db.get(PromoCode, promo_id)
    if not promo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Promo code not found.")
    db.delete(promo)
    db.commit()


# ── Subscription Plans ────────────────────────────────────────────────────────

@router.get("/plans")
def admin_list_plans(db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    plans = db.query(SubscriptionPlan).order_by(SubscriptionPlan.sort_order, SubscriptionPlan.created_at).all()
    rows = [svc.plan_row(p, db) for p in plans]
    return {
        "stats": {
            "total":         len(rows),
            "active":        sum(1 for r in rows if r["is_active"]),
            "stripe_synced": sum(1 for r in rows if r["stripe_synced"]),
            "free_plans":    sum(1 for r in rows if r["price_monthly"] == 0),
        },
        "plans": rows,
    }


@router.get("/plans/{plan_id}")
def admin_get_plan(plan_id: str, db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    plan = db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found.")
    return svc.plan_row(plan, db)


@router.post("/plans", status_code=201)
def admin_create_plan(
    body: CreatePlanIn,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    import json as _json
    name_key = body.name.strip().lower().replace(" ", "_")
    if not name_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Plan name is required.")
    if body.price_monthly < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Price cannot be negative.")
    if db.query(SubscriptionPlan).filter(SubscriptionPlan.name == name_key).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "A plan with this name already exists.")

    plan = SubscriptionPlan(
        name=name_key,
        display_name=body.display_name.strip(),
        description=body.description.strip() if body.description else None,
        price_monthly=body.price_monthly,
        currency=body.currency.lower(),
        interval=body.interval,
        features_json=_json.dumps(body.features),
        meetings_per_month=body.meetings_per_month,
        audio_hours_per_month=body.audio_hours_per_month,
        max_workspaces=body.max_workspaces,
        max_members=body.max_members,
        is_active=True,
        is_popular=body.is_popular,
        sort_order=body.sort_order,
        created_by_id=current_admin.id,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    if body.sync_stripe and body.price_monthly >= 0:
        try:
            svc.sync_plan_to_stripe(plan, db)
            db.refresh(plan)
        except Exception as exc:
            # Plan was saved; Stripe sync failed — return with warning
            row = svc.plan_row(plan, db)
            row["stripe_warning"] = str(exc)
            return row

    return svc.plan_row(plan, db)


@router.patch("/plans/{plan_id}")
def admin_update_plan(
    plan_id: str,
    body: UpdatePlanIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    import json as _json
    plan = db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found.")

    if body.display_name is not None:
        plan.display_name = body.display_name.strip()
    if body.description is not None:
        plan.description = body.description.strip() if body.description else None
    if body.price_monthly is not None:
        if body.price_monthly < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Price cannot be negative.")
        plan.price_monthly = body.price_monthly
    if body.currency is not None:
        plan.currency = body.currency.lower()
    if body.interval is not None:
        plan.interval = body.interval
    if body.features is not None:
        plan.features_json = _json.dumps(body.features)
    if body.is_active is not None:
        plan.is_active = body.is_active
    if body.is_popular is not None:
        plan.is_popular = body.is_popular
    if body.sort_order is not None:
        plan.sort_order = body.sort_order
    if "meetings_per_month" in body.__fields_set__:
        plan.meetings_per_month = body.meetings_per_month
    if "audio_hours_per_month" in body.__fields_set__:
        plan.audio_hours_per_month = body.audio_hours_per_month
    if "max_workspaces" in body.__fields_set__:
        plan.max_workspaces = body.max_workspaces
    if "max_members" in body.__fields_set__:
        plan.max_members = body.max_members

    db.commit()
    db.refresh(plan)

    if body.sync_stripe:
        try:
            svc.sync_plan_to_stripe(plan, db)
            db.refresh(plan)
        except Exception as exc:
            row = svc.plan_row(plan, db)
            row["stripe_warning"] = str(exc)
            return row

    return svc.plan_row(plan, db)


@router.delete("/plans/{plan_id}", status_code=204)
def admin_delete_plan(plan_id: str, db: Session = Depends(get_db), _: User = Depends(get_admin_user)):
    plan = db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found.")
    db.delete(plan)
    db.commit()
