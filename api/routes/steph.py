import json
import os

import anthropic
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from middleware.auth import get_current_user, get_db
from models.job import JobRecord
from models.user import User

router = APIRouter(prefix="/steph", tags=["steph"])

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

_SYSTEM = """You are Steph, a friendly and knowledgeable AI meeting assistant for MinuteAI. \
You help users get the most out of their meetings by answering questions, surfacing insights, \
and tracking action items from their recorded sessions.

You can:
- Summarize or find details from past meetings
- Identify action items, owners, and deadlines
- Highlight key decisions or discussion points
- Help users prepare for upcoming meetings
- Offer productivity tips around meetings

Keep responses concise and warm. Address the user by first name when you know it. \
When you reference meeting content, be specific — cite titles, dates, or owners where relevant."""


class ChatRequest(BaseModel):
    message: str
    conversation_history: list[dict] = []
    workspace_id: str | None = None


def _build_meeting_context(db: Session, user_id: str, workspace_id: str | None) -> str:
    q = (
        db.query(JobRecord)
        .filter(JobRecord.user_id == user_id, JobRecord.status == "done")
    )
    if workspace_id:
        q = q.filter(JobRecord.workspace_id == workspace_id)
    recent = q.order_by(JobRecord.created_at.desc()).limit(8).all()

    if not recent:
        return ""

    parts = []
    for job in recent:
        minutes = json.loads(job.minutes_json) if job.minutes_json else {}
        title = minutes.get("title") or getattr(job, "filename", None) or "Untitled Meeting"
        date = (minutes.get("metadata") or {}).get("date") or str(job.created_at.date())

        lines = [f"• {title} ({date})"]
        sections = minutes.get("sections", [])
        if sections:
            summaries = "; ".join(s.get("content", "")[:120] for s in sections[:3])
            lines.append(f"  Topics: {summaries}")
        action_items = minutes.get("action_items", [])
        if action_items:
            tasks = "; ".join(
                f"{a.get('task', '')} ({a.get('owner', 'TBD')})" for a in action_items[:5]
            )
            lines.append(f"  Action items: {tasks}")
        parts.append("\n".join(lines))

    return "User's recent meetings:\n" + "\n\n".join(parts)


@router.post("/chat")
def chat(
    body: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    meeting_context = _build_meeting_context(db, current_user.id, body.workspace_id)

    system_parts = [_SYSTEM]
    if current_user.name:
        first = current_user.name.split()[0]
        system_parts.append(f"The user's name is {first}.")
    if meeting_context:
        system_parts.append(meeting_context)

    system_prompt = "\n\n".join(system_parts)

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in body.conversation_history[-20:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    history.append({"role": "user", "content": body.message})

    response = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_prompt,
        messages=history,
    )

    return {"reply": response.content[0].text}
