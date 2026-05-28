import json
import os
import re
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models.job import JobRecord
from models.task import TaskRecord
from models.user import TeamMember, User, Workspace

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def parse_deadline(raw: str) -> Optional[str]:
    """
    Convert a natural-language deadline string to a YYYY-MM-DD ISO date.

    Returns None if the input is blank, already a valid ISO date, or unparseable.
    Always returns None rather than raising, so task creation never fails due to
    this step.
    """
    if not raw or not raw.strip():
        return None

    cleaned = raw.strip()

    # Already a valid ISO date — pass through unchanged
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        return cleaned

    if not ANTHROPIC_API_KEY:
        return cleaned  # can't parse without the API; return raw

    today = date.today().isoformat()
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{
                "role": "user",
                "content": (
                    f"Today is {today}. Convert this deadline to a YYYY-MM-DD date: \"{cleaned}\"\n"
                    "Rules:\n"
                    "- Output ONLY the date in YYYY-MM-DD format, nothing else\n"
                    "- If unparseable output the word null\n"
                    "- Do NOT include any explanation"
                ),
            }],
        )
        result = msg.content[0].text.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", result):
            return result
        return None
    except Exception:
        return cleaned  # fallback: return as-is


def job_title(job: Optional[JobRecord]) -> str:
    if not job or not job.minutes_json:
        return "Meeting"
    try:
        return json.loads(job.minutes_json).get("title") or "Meeting"
    except Exception:
        return "Meeting"


def user_snippet(u: Optional[User]) -> Optional[dict]:
    if not u:
        return None
    initials = "".join(w[0] for w in (u.name or "U").split()[:2]).upper()
    return {"id": u.id, "name": u.name, "email": u.email,
            "initials": initials, "avatar_url": u.avatar_url}


def task_dict(t: TaskRecord, assignee: Optional[User] = None, job: Optional[JobRecord] = None) -> dict:
    return {
        "id":            t.id,
        "job_id":        t.job_id,
        "workspace_id":  t.workspace_id,
        "title":         t.title,
        "owner_name":    t.owner_name,
        "assignee":      user_snippet(assignee),
        "status":        t.status,
        "deadline":      t.deadline,
        "meeting_title": job_title(job) if job else None,
        "created_at":    (t.created_at.isoformat() + "Z") if t.created_at else None,
    }


def is_workspace_owner(workspace_id: str, user: User, db: Session) -> bool:
    ws = db.get(Workspace, workspace_id)
    return bool(ws and ws.owner_id == user.id)


def is_workspace_member(workspace_id: str, user: User, db: Session) -> bool:
    m = db.query(TeamMember).filter(
        TeamMember.workspace_id == workspace_id,
        TeamMember.user_id == user.id,
        TeamMember.status == "active",
    ).first()
    return bool(m)


def can_access_task(t: TaskRecord, user: User, db: Session) -> bool:
    if t.job_id:
        job = db.get(JobRecord, t.job_id)
        if job and job.user_id == user.id:
            return True
    if t.assignee_id == user.id:
        return True
    if t.workspace_id:
        if is_workspace_owner(t.workspace_id, user, db):
            return True
        if is_workspace_member(t.workspace_id, user, db):
            return True
    return False
