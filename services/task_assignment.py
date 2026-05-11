import json
import uuid
from collections import defaultdict

from models.database import SessionLocal, TaskRecord, JobRecord
from models.user import User, Workspace, TeamMember
from services.notifications import create_notification


def _match_name(raw: str, members: list[tuple]) -> str | None:
    """Return user_id of the best name match, or None."""
    if not raw or not members:
        return None
    needle = raw.lower().strip()
    for user_id, full_name, _ in members:
        if not full_name:
            continue
        haystack = full_name.lower()
        first    = haystack.split()[0]
        if needle == haystack or needle == first:
            return user_id
        if needle in haystack or haystack.startswith(needle):
            return user_id
    return None


def auto_assign_tasks(job_id: str) -> None:
    """Create TaskRecords from action items and auto-assign by name match."""
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        if not job or not job.minutes_json:
            return
        try:
            minutes = json.loads(job.minutes_json)
        except Exception:
            return

        items = minutes.get("action_items", minutes.get("actionItems", []))
        if not isinstance(items, list) or not items:
            return

        # Delete any existing tasks for this job (idempotent on retry)
        session.query(TaskRecord).filter(TaskRecord.job_id == job_id).delete()

        # Build member roster: (user_id, name, email)
        roster: list[tuple] = []
        if job.workspace_id:
            ws = session.get(Workspace, job.workspace_id)
            if ws:
                owner = session.get(User, ws.owner_id)
                if owner:
                    roster.append((owner.id, owner.name, owner.email))
            for m in session.query(TeamMember).filter(
                TeamMember.workspace_id == job.workspace_id,
                TeamMember.status == "active",
                TeamMember.user_id.isnot(None),
            ).all():
                u = session.get(User, m.user_id)
                if u:
                    roster.append((u.id, u.name, u.email))
        elif job.user_id:
            u = session.get(User, job.user_id)
            if u:
                roster.append((u.id, u.name, u.email))

        assigned_counts: dict[str, int] = defaultdict(int)

        for item in items:
            if isinstance(item, str):
                title      = item
                owner_name = None
                deadline   = None
            else:
                title      = item.get("task") or item.get("description") or str(item)
                owner_name = (item.get("owner") or "").strip() or None
                deadline   = (item.get("deadline") or "").strip() or None

            assignee_id = _match_name(owner_name, roster) if owner_name else None
            if assignee_id:
                assigned_counts[assignee_id] += 1

            session.add(TaskRecord(
                id           = str(uuid.uuid4()),
                job_id       = job_id,
                workspace_id = job.workspace_id,
                title        = title,
                owner_name   = owner_name,
                assignee_id  = assignee_id,
                status       = "todo",
                deadline     = deadline,
            ))

        session.commit()

        meeting_title = minutes.get("title") or "a meeting"
        for uid, count in assigned_counts.items():
            task_word = "task" if count == 1 else "tasks"
            create_notification(
                user_id=uid,
                type="task_assigned",
                title=f"{count} {task_word} assigned from \"{meeting_title}\"",
                body="Open My Tasks to view and manage them.",
                link="/tasks",
            )
