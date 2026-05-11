import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_db, get_current_user
from models.user import User, TeamMember, Workspace
from models.database import TaskRecord, JobRecord
from services.notifications import create_notification

router = APIRouter(prefix="/tasks", tags=["tasks"])

VALID_STATUSES = {"todo", "in_progress", "done"}


class CreateTaskIn(BaseModel):
    title:        str
    workspace_id: Optional[str] = None
    job_id:       Optional[str] = None
    assignee_id:  Optional[str] = None
    status:       str = "todo"
    deadline:     Optional[str] = None


class UpdateTaskIn(BaseModel):
    status:      Optional[str] = None
    assignee_id: Optional[str] = None
    title:       Optional[str] = None
    deadline:    Optional[str] = None
    job_id:      Optional[str] = None  # empty string = remove meeting (General)


# ── helpers ───────────────────────────────────────────────────────────────────

def _job_title(job: JobRecord | None) -> str:
    if not job or not job.minutes_json:
        return "Meeting"
    try:
        return json.loads(job.minutes_json).get("title") or "Meeting"
    except Exception:
        return "Meeting"


def _user_snippet(u: User | None) -> dict | None:
    if not u:
        return None
    initials = "".join(w[0] for w in (u.name or "U").split()[:2]).upper()
    return {"id": u.id, "name": u.name, "email": u.email,
            "initials": initials, "avatar_url": u.avatar_url}


def _task_dict(t: TaskRecord, assignee: User | None = None, job: JobRecord | None = None) -> dict:
    return {
        "id":            t.id,
        "job_id":        t.job_id,
        "workspace_id":  t.workspace_id,
        "title":         t.title,
        "owner_name":    t.owner_name,
        "assignee":      _user_snippet(assignee),
        "status":        t.status,
        "deadline":      t.deadline,
        "meeting_title": _job_title(job) if job else None,
        "created_at":    (t.created_at.isoformat() + "Z") if t.created_at else None,
    }


def _is_workspace_owner(workspace_id: str, user: User, db: Session) -> bool:
    ws = db.get(Workspace, workspace_id)
    return bool(ws and ws.owner_id == user.id)


def _is_workspace_member(workspace_id: str, user: User, db: Session) -> bool:
    m = db.query(TeamMember).filter(
        TeamMember.workspace_id == workspace_id,
        TeamMember.user_id == user.id,
        TeamMember.status == "active",
    ).first()
    return bool(m)


def _can_access(t: TaskRecord, user: User, db: Session) -> bool:
    if t.job_id:
        job = db.get(JobRecord, t.job_id)
        if job and job.user_id == user.id:
            return True
    if t.assignee_id == user.id:
        return True
    if t.workspace_id:
        if _is_workspace_owner(t.workspace_id, user, db):
            return True
        if _is_workspace_member(t.workspace_id, user, db):
            return True
    return False


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def my_tasks(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Tasks assigned to the current user."""
    tasks = (
        db.query(TaskRecord)
        .filter(TaskRecord.assignee_id == current_user.id)
        .order_by(TaskRecord.created_at.desc())
        .all()
    )
    result = []
    for t in tasks:
        assignee = db.get(User, t.assignee_id) if t.assignee_id else None
        job      = db.get(JobRecord, t.job_id) if t.job_id else None
        result.append(_task_dict(t, assignee, job))
    return result


@router.post("", status_code=201)
def create_task(
    body: CreateTaskIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.title.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Title is required.")

    # Validate workspace access
    if body.workspace_id:
        ws = db.get(Workspace, body.workspace_id)
        if not ws:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
        if ws.owner_id != current_user.id and not _is_workspace_member(body.workspace_id, current_user, db):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized.")

    # Validate meeting if provided
    job = None
    if body.job_id:
        job = db.get(JobRecord, body.job_id)
        if not job:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    # Validate assignee if provided
    if body.assignee_id:
        if not db.get(User, body.assignee_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    task = TaskRecord(
        id=str(uuid.uuid4()),
        job_id=body.job_id or None,
        workspace_id=body.workspace_id or None,
        title=body.title.strip(),
        owner_name=None,
        assignee_id=body.assignee_id or None,
        status=body.status if body.status in VALID_STATUSES else "todo",
        deadline=body.deadline or None,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    assignee = db.get(User, task.assignee_id) if task.assignee_id else None
    if task.assignee_id and task.assignee_id != current_user.id:
        meeting_name = _job_title(job) if job else None
        body = f"From meeting: {meeting_name}" if meeting_name else None
        create_notification(
            user_id=task.assignee_id,
            type="task_assigned",
            title=f"New task: {task.title}",
            body=body,
            link="/tasks",
        )
    return _task_dict(task, assignee, job)


@router.get("/meeting/{job_id}")
def meeting_tasks(job_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """All tasks extracted from a specific meeting."""
    job = db.get(JobRecord, job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    is_owner  = job.user_id == current_user.id
    is_member = False
    if job.workspace_id:
        is_member = _is_workspace_member(job.workspace_id, current_user, db)
    if not is_owner and not is_member:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized.")

    tasks = db.query(TaskRecord).filter(TaskRecord.job_id == job_id).order_by(TaskRecord.created_at).all()
    return [_task_dict(t, db.get(User, t.assignee_id) if t.assignee_id else None, job) for t in tasks]


@router.get("/workspace/{workspace_id}")
def workspace_tasks(workspace_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """All tasks in a workspace."""
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
    if ws.owner_id != current_user.id and not _is_workspace_member(workspace_id, current_user, db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized.")

    tasks = (
        db.query(TaskRecord)
        .filter(TaskRecord.workspace_id == workspace_id)
        .order_by(TaskRecord.created_at.desc())
        .all()
    )
    return [
        _task_dict(
            t,
            db.get(User, t.assignee_id) if t.assignee_id else None,
            db.get(JobRecord, t.job_id) if t.job_id else None,
        )
        for t in tasks
    ]


@router.patch("/{task_id}")
def update_task(
    task_id: str,
    body: UpdateTaskIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found.")
    if not _can_access(task, current_user, db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized.")

    if body.status is not None:
        if body.status not in VALID_STATUSES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Status must be one of: {', '.join(VALID_STATUSES)}")
        task.status = body.status

    new_assignee_id = None
    if body.assignee_id is not None:
        if body.assignee_id == "":
            task.assignee_id = None
        else:
            if not db.get(User, body.assignee_id):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
            if body.assignee_id != task.assignee_id:
                new_assignee_id = body.assignee_id
            task.assignee_id = body.assignee_id

    if body.title is not None:
        if not body.title.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Title cannot be empty.")
        task.title = body.title.strip()

    if body.deadline is not None:
        task.deadline = body.deadline or None

    if body.job_id is not None:
        if body.job_id == "":
            task.job_id = None
        else:
            if not db.get(JobRecord, body.job_id):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")
            task.job_id = body.job_id

    db.commit()
    db.refresh(task)
    assignee = db.get(User, task.assignee_id) if task.assignee_id else None
    job      = db.get(JobRecord, task.job_id) if task.job_id else None
    if new_assignee_id and new_assignee_id != current_user.id:
        meeting_name = _job_title(job) if job else None
        body_text = f"From meeting: {meeting_name}" if meeting_name else None
        create_notification(
            user_id=new_assignee_id,
            type="task_assigned",
            title=f"Task assigned to you: {task.title}",
            body=body_text,
            link="/tasks",
        )
    return _task_dict(task, assignee, job)


@router.delete("/{task_id}", status_code=204)
def delete_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found.")

    can_delete = False
    if task.workspace_id and _is_workspace_owner(task.workspace_id, current_user, db):
        can_delete = True
    if task.job_id:
        job = db.get(JobRecord, task.job_id)
        if job and job.user_id == current_user.id:
            can_delete = True
    if task.assignee_id == current_user.id:
        can_delete = True

    if not can_delete:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized.")

    db.delete(task)
    db.commit()
