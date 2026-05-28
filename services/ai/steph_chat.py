"""Core Steph chat logic — used by the HTTP route and the wake word handler."""

import os
import logging

import anthropic

log = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

_SYSTEM = """You are Steph, a friendly and knowledgeable AI meeting assistant for MinuteAI.
You help users get the most out of their meetings by answering questions, surfacing insights,
and tracking action items from their recorded sessions.

You can:
- Answer questions about past meetings, decisions, and action items
- Identify owners and deadlines from past discussions
- Summarise or find details from any recorded session
- Help users prepare for upcoming meetings
- Offer productivity tips around meetings

Keep responses concise and warm. Address the user by first name when you know it.
When you reference meeting content, be specific — cite titles, dates, or owners where relevant.
If the provided meeting context does not contain enough information to answer, say so honestly."""


def _semantic_context(message: str, user_id: str, workspace_id: str | None) -> str:
    try:
        from services.ai.memory_search import search_memory
        chunks = search_memory(query=message, user_id=user_id, workspace_id=workspace_id, n_results=6)
        if not chunks:
            return ""
        parts = []
        for c in chunks:
            header = f"[{c['meeting_title']}" + (f" · {c['meeting_date']}" if c['meeting_date'] else "") + "]"
            parts.append(f"{header}\n{c['text']}")
        return "Relevant content from past meetings:\n\n" + "\n\n---\n".join(parts)
    except Exception:
        return ""


def _fallback_context(db, user_id: str, workspace_id: str | None) -> str:
    import json
    from models.job import JobRecord
    q = db.query(JobRecord).filter(JobRecord.user_id == user_id, JobRecord.status == "done")
    if workspace_id:
        q = q.filter(JobRecord.workspace_id == workspace_id)
    recent = q.order_by(JobRecord.created_at.desc()).limit(6).all()
    if not recent:
        return ""
    parts = []
    for job in recent:
        minutes = json.loads(job.minutes_json) if job.minutes_json else {}
        title = minutes.get("title") or "Untitled Meeting"
        date = (minutes.get("metadata") or {}).get("date") or str(job.created_at.date())
        decisions = minutes.get("keyDecisions") or []
        actions = minutes.get("actionItems") or []
        lines = [f"• {title} ({date})"]
        if decisions:
            lines.append("  Decisions: " + "; ".join(
                (d.get("decision", d) if isinstance(d, dict) else str(d))[:100]
                for d in decisions[:3]
            ))
        if actions:
            lines.append("  Actions: " + "; ".join(
                (a.get("task", a) if isinstance(a, dict) else str(a))[:80]
                for a in actions[:4]
            ))
        parts.append("\n".join(lines))
    return "Recent meetings:\n" + "\n\n".join(parts)


def get_reply(
    *,
    message: str,
    user_id: str,
    workspace_id: str | None,
    history: list[dict] | None = None,
    live_transcript: str | None = None,
    user_name: str | None = None,
    db=None,
    cross_thread: bool = False,
) -> str:
    """Generate a Steph reply. If db is None, opens its own session."""
    from db.base import SessionLocal

    # When cross_thread is authorised, search all workspaces for this user
    effective_workspace = None if cross_thread else workspace_id

    def _run(db_session):
        meeting_context = _semantic_context(message, user_id, effective_workspace)
        if not meeting_context:
            meeting_context = _fallback_context(db_session, user_id, effective_workspace)

        system_parts = [_SYSTEM]
        if user_name:
            system_parts.append(f"The user's name is {user_name.split()[0]}.")
        if live_transcript and live_transcript.strip():
            system_parts.append(
                "Current live meeting transcript (most recent):\n"
                + live_transcript[-2000:]
            )
        if meeting_context:
            system_parts.append(meeting_context)

        system_prompt = "\n\n".join(system_parts)

        msgs = [
            {"role": m["role"], "content": m["content"]}
            for m in (history or [])[-20:]
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        msgs.append({"role": "user", "content": message})

        response = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=msgs,
        )
        return response.content[0].text

    if db is not None:
        return _run(db)
    with SessionLocal() as session:
        return _run(session)
