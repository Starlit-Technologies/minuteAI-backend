import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from middleware.auth import get_db, get_current_user
from models.job import JobRecord
from models.task import TaskRecord
from models.user import User, Workspace
from schemas.tasks import CreateTaskIn, UpdateTaskIn
from services.core.notifications import create_notification
from services.tasks.tasks import (
    can_access_task, is_workspace_member, is_workspace_owner,
    job_title, parse_deadline, task_dict, user_snippet,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])

VALID_STATUSES = {"todo", "in_progress", "done"}


@router.get("")
def my_tasks(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
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
        result.append(task_dict(t, assignee, job))
    return result


@router.post("", status_code=201)
def create_task(
    body: CreateTaskIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.title.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Title is required.")

    if body.workspace_id:
        ws = db.get(Workspace, body.workspace_id)
        if not ws:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
        if ws.owner_id != current_user.id and not is_workspace_member(body.workspace_id, current_user, db):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized.")

    job = None
    if body.job_id:
        job = db.get(JobRecord, body.job_id)
        if not job:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    if body.assignee_id and not db.get(User, body.assignee_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    if body.deadline:
        deadline = parse_deadline(body.deadline)
    else:
        from services.tasks.deadline_utils import extract_deadline_from_title
        deadline = extract_deadline_from_title(body.title)

    task = TaskRecord(
        id=str(uuid.uuid4()),
        job_id=body.job_id or None,
        workspace_id=body.workspace_id or None,
        title=body.title.strip(),
        owner_name=None,
        assignee_id=body.assignee_id or None,
        status=body.status if body.status in VALID_STATUSES else "todo",
        deadline=deadline,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    assignee = db.get(User, task.assignee_id) if task.assignee_id else None
    if task.assignee_id and task.assignee_id != current_user.id:
        meeting_name = job_title(job) if job else None
        create_notification(
            user_id=task.assignee_id,
            type="task_assigned",
            title=f"New task: {task.title}",
            body=f"From meeting: {meeting_name}" if meeting_name else None,
            link="/tasks",
        )
    return task_dict(task, assignee, job)


@router.get("/meeting/{job_id}")
def meeting_tasks(job_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    job = db.get(JobRecord, job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    is_owner  = job.user_id == current_user.id
    is_member = bool(job.workspace_id and is_workspace_member(job.workspace_id, current_user, db))
    if not is_owner and not is_member:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized.")

    tasks = db.query(TaskRecord).filter(TaskRecord.job_id == job_id).order_by(TaskRecord.created_at).all()
    return [task_dict(t, db.get(User, t.assignee_id) if t.assignee_id else None, job) for t in tasks]


@router.get("/workspace/{workspace_id}")
def workspace_tasks(workspace_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
    if ws.owner_id != current_user.id and not is_workspace_member(workspace_id, current_user, db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized.")

    tasks = (
        db.query(TaskRecord)
        .filter(TaskRecord.workspace_id == workspace_id)
        .order_by(TaskRecord.created_at.desc())
        .all()
    )
    return [
        task_dict(
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
    if not can_access_task(task, current_user, db):
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
        task.deadline = parse_deadline(body.deadline) if body.deadline else None

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
        meeting_name = job_title(job) if job else None
        create_notification(
            user_id=new_assignee_id,
            type="task_assigned",
            title=f"Task assigned to you: {task.title}",
            body=f"From meeting: {meeting_name}" if meeting_name else None,
            link="/tasks",
        )
    return task_dict(task, assignee, job)


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
    if task.workspace_id and is_workspace_owner(task.workspace_id, current_user, db):
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
