"""AI Chief of Staff — daily morning briefing + pattern detection.

Runs every day at BRIEFING_HOUR UTC (default 07:00).
"""

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

import anthropic

logger = logging.getLogger(__name__)

BRIEFING_HOUR = int(os.getenv("BRIEFING_HOUR", "7"))

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


def _seconds_until_next() -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=BRIEFING_HOUR, minute=0, second=0, microsecond=0)
    if now.hour >= BRIEFING_HOUR:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _refresh_google_token(account) -> str | None:
    """Refresh a Google OAuth access token using the stored refresh token."""
    try:
        import requests
        import os as _os
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     _os.environ.get("GOOGLE_CLIENT_ID", ""),
                "client_secret": _os.environ.get("GOOGLE_CLIENT_SECRET", ""),
                "refresh_token": account.refresh_token,
                "grant_type":    "refresh_token",
            },
            timeout=10,
        )
        if r.ok:
            return r.json().get("access_token")
    except Exception:
        pass
    return None


def _todays_calendar_events(user_id: str) -> list[dict]:
    """Fetch today's events from the user's connected Google Calendar."""
    try:
        from models.connected_account import get_connected_account
        import requests

        account = get_connected_account(user_id, "google")
        if not account or not account.access_token:
            return []

        token = account.access_token
        if account.token_expiry and datetime.utcnow() > account.token_expiry - timedelta(minutes=5):
            refreshed = _refresh_google_token(account)
            if refreshed:
                token = refreshed

        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        r = requests.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "timeMin":      today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeMax":      today_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "singleEvents": "true",
                "orderBy":      "startTime",
                "maxResults":   "20",
            },
            timeout=10,
        )
        if not r.ok:
            return []

        result = []
        for item in r.json().get("items", []):
            if item.get("status") == "cancelled":
                continue
            title = item.get("summary") or "Meeting"
            start_val = (item.get("start") or {}).get("dateTime") or (item.get("start") or {}).get("date")
            scheduled_for = ""
            if start_val:
                try:
                    dt = datetime.fromisoformat(start_val.replace("Z", "+00:00"))
                    scheduled_for = dt.astimezone(timezone.utc).strftime("%H:%M UTC")
                except Exception:
                    pass
            attendees = item.get("attendees") or []
            result.append({
                "title": title,
                "scheduled_for": scheduled_for,
                "attendee_count": len(attendees),
                "_calendar_event_id": item.get("id", ""),
            })
        return result
    except Exception as exc:
        logger.debug("Calendar events fetch failed for %s: %s", user_id, exc)
        return []


def _todays_sessions(db, user_id: str, workspace_id: str | None) -> list[dict]:
    """Return today's meetings from both bot sessions and connected Google Calendar."""
    from models.meeting_bot import MeetingBotSessionRecord
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    q = db.query(MeetingBotSessionRecord).filter(
        MeetingBotSessionRecord.user_id == user_id,
        MeetingBotSessionRecord.scheduled_for >= today_start,
        MeetingBotSessionRecord.scheduled_for < today_end,
    )
    if workspace_id:
        q = q.filter(MeetingBotSessionRecord.workspace_id == workspace_id)

    result = []
    seen_event_ids: set[str] = set()

    for s in q.order_by(MeetingBotSessionRecord.scheduled_for).all():
        attendees = []
        try:
            if s.attendees_json:
                attendees = json.loads(s.attendees_json)
        except Exception:
            pass
        if s.calendar_event_id:
            seen_event_ids.add(s.calendar_event_id)
        result.append({
            "title": s.title or "Meeting",
            "scheduled_for": s.scheduled_for.strftime("%H:%M UTC") if s.scheduled_for else "",
            "attendee_count": len(attendees) if isinstance(attendees, list) else 0,
        })

    # Merge calendar events that don't already have a bot session
    for ev in _todays_calendar_events(user_id):
        if ev["_calendar_event_id"] and ev["_calendar_event_id"] in seen_event_ids:
            continue
        result.append({
            "title": ev["title"],
            "scheduled_for": ev["scheduled_for"],
            "attendee_count": ev["attendee_count"],
        })

    result.sort(key=lambda x: x["scheduled_for"])
    return result


def _open_tasks(db, user_id: str) -> tuple[list[dict], list[dict]]:
    """Returns (overdue_tasks, all_open_tasks)."""
    from models.task import TaskRecord
    from models.job import JobRecord
    today_str = date.today().isoformat()
    rows = db.query(TaskRecord).filter(
        TaskRecord.status != "done",
        TaskRecord.assignee_id == user_id,
    ).all()
    overdue, all_open = [], []
    for t in rows:
        job = db.get(JobRecord, t.job_id) if t.job_id else None
        meeting_title = None
        if job and job.minutes_json:
            try:
                meeting_title = json.loads(job.minutes_json).get("title")
            except Exception:
                pass
        item = {
            "title": t.title,
            "status": t.status,
            "deadline": t.deadline,
            "meeting_title": meeting_title,
        }
        all_open.append(item)
        if t.deadline and t.deadline < today_str:
            try:
                delta = (date.today() - date.fromisoformat(t.deadline)).days
                overdue.append({**item, "days_overdue": delta})
            except Exception:
                overdue.append(item)
    overdue.sort(key=lambda x: x.get("days_overdue", 0), reverse=True)
    return overdue, all_open


def _recent_meetings(db, user_id: str, workspace_id: str | None, days: int = 14) -> list[dict]:
    from models.job import JobRecord
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = db.query(JobRecord).filter(
        JobRecord.user_id == user_id,
        JobRecord.status == "done",
        JobRecord.created_at >= cutoff,
    )
    if workspace_id:
        q = q.filter(JobRecord.workspace_id == workspace_id)
    result = []
    for job in q.order_by(JobRecord.created_at.desc()).limit(12).all():
        try:
            mins = json.loads(job.minutes_json) if job.minutes_json else {}
        except Exception:
            mins = {}
        result.append({
            "title": mins.get("title") or "Untitled",
            "date": str(job.created_at.date()),
            "decisions": [
                d.get("decision", d) if isinstance(d, dict) else str(d)
                for d in (mins.get("keyDecisions") or [])[:3]
            ],
            "actions": [
                a.get("task", a) if isinstance(a, dict) else str(a)
                for a in (mins.get("actionItems") or [])[:4]
            ],
            "overview": (mins.get("overview") or "")[:300],
        })
    return result


def _ai_insight(
    user_name: str | None,
    todays_meetings: list[dict],
    overdue_tasks: list[dict],
    recent_meetings: list[dict],
) -> str:
    try:
        lines = []
        if todays_meetings:
            lines.append("Today's meetings: " + ", ".join(m["title"] for m in todays_meetings[:3]) + ".")
        if overdue_tasks:
            lines.append(
                f"Overdue tasks ({len(overdue_tasks)}): "
                + "; ".join(t["title"] for t in overdue_tasks[:3]) + "."
            )
        for m in recent_meetings[:5]:
            if m["decisions"]:
                lines.append(f"[{m['date']}] {m['title']} decisions: {'; '.join(m['decisions'][:2])}.")
            if m["overview"]:
                lines.append(f"[{m['date']}] {m['title']}: {m['overview'][:150]}")

        if not lines:
            return "No meetings or tasks today — a great day to plan ahead."

        name = user_name.split()[0] if user_name else "there"
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=(
                "You are an AI Chief of Staff. Given meeting and task context, "
                "write a concise 2-3 sentence morning briefing insight. "
                "Be specific — cite concrete meeting names, decisions, or overdue items. "
                "Be warm and actionable. No bullet points."
            ),
            messages=[{"role": "user", "content": f"User: {name}\n\nContext:\n" + "\n".join(lines)}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        logger.warning("AI insight generation failed: %s", exc)
        if todays_meetings:
            return f"You have {len(todays_meetings)} meeting(s) today. Review your open tasks and come prepared."
        return "Good morning — check your open tasks and prepare for the day ahead."


def detect_patterns(db, user_id: str, workspace_id: str | None) -> list[dict]:
    """Detect recurring topics and deferred decisions across recent meetings."""
    meetings = _recent_meetings(db, user_id, workspace_id, days=30)
    if len(meetings) < 3:
        return []
    try:
        ctx_lines = []
        for m in meetings[:10]:
            ctx_lines.append(f"Meeting: {m['title']} ({m['date']})")
            if m["decisions"]:
                ctx_lines.append("  Decisions: " + "; ".join(m["decisions"]))
            if m["actions"]:
                ctx_lines.append("  Actions: " + "; ".join(m["actions"][:3]))
            if m["overview"]:
                ctx_lines.append("  Summary: " + m["overview"][:120])

        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=(
                "You are an AI Chief of Staff analyzing meeting patterns. "
                "Given a list of recent meetings with decisions and action items, "
                "identify 2-3 patterns: recurring topics, deferred decisions, or unresolved commitments. "
                "Return a JSON array of objects with keys: type (string), description (string). "
                "Types: 'deferred_decision', 'recurring_topic', 'stale_commitment'. "
                "Output only the JSON array, no other text."
            ),
            messages=[{"role": "user", "content": "\n".join(ctx_lines)}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("["):
            patterns = json.loads(text)
            return patterns[:3] if isinstance(patterns, list) else []
    except Exception as exc:
        logger.warning("Pattern detection failed: %s", exc)
    return []


def generate_briefing(db, user_id: str, user_name: str | None, workspace_id: str | None) -> dict:
    """Return structured daily briefing dict for the given user."""
    todays = _todays_sessions(db, user_id, workspace_id)
    overdue, all_open = _open_tasks(db, user_id)
    recent = _recent_meetings(db, user_id, workspace_id)
    insight = _ai_insight(user_name, todays, overdue, recent)
    return {
        "date": datetime.now(timezone.utc).strftime("%A, %B %d").replace(" 0", " "),
        "todays_meetings": todays,
        "overdue_tasks": overdue[:5],
        "open_task_count": len(all_open),
        "overdue_count": len(overdue),
        "ai_insight": insight,
        "recent_meeting_count": len(recent),
    }


def get_thread_continuity(db, job_id: str, user_id: str) -> dict:
    """Return thread-level continuity context for a completed meeting job.

    Returns decisions, next steps, and open tasks from previous sessions in the
    same recurring meeting thread. Returns {has_continuity: False} when no thread
    history exists.
    """
    from models.job import JobRecord
    from models.meeting_thread import MeetingThreadRecord, ThreadSessionRecord
    from models.task import TaskRecord

    job = db.get(JobRecord, job_id)
    if not job or job.user_id != user_id:
        return {"has_continuity": False}

    # Find which thread session this job belongs to
    thread_session = (
        db.query(ThreadSessionRecord)
        .filter(ThreadSessionRecord.job_id == job_id)
        .first()
    )
    if not thread_session:
        return {"has_continuity": False}

    thread = db.get(MeetingThreadRecord, thread_session.thread_id)
    if not thread:
        return {"has_continuity": False}

    # All previous sessions in this thread (exclude the current one)
    prior_sessions = (
        db.query(ThreadSessionRecord)
        .filter(
            ThreadSessionRecord.thread_id == thread.id,
            ThreadSessionRecord.job_id != job_id,
        )
        .order_by(ThreadSessionRecord.session_number.desc())
        .all()
    )
    if not prior_sessions:
        return {"has_continuity": False, "session_count": 1}

    # Previous meeting minutes
    prev_minutes: dict = {}
    for ps in prior_sessions:
        prev_job = db.get(JobRecord, ps.job_id)
        if prev_job and prev_job.minutes_json:
            try:
                prev_minutes = json.loads(prev_job.minutes_json)
                break
            except Exception:
                pass

    # Open tasks linked to any job in this thread
    prior_job_ids = [ps.job_id for ps in prior_sessions]
    open_tasks = (
        db.query(TaskRecord)
        .filter(
            TaskRecord.assignee_id == user_id,
            TaskRecord.status != "done",
            TaskRecord.job_id.in_(prior_job_ids),
        )
        .limit(6)
        .all()
    )

    key_decisions = []
    next_steps = []
    if prev_minutes:
        key_decisions = [
            d.get("decision", d) if isinstance(d, dict) else d
            for d in (prev_minutes.get("keyDecisions") or [])[:4]
        ]
        next_steps = [
            s.get("step", s) if isinstance(s, dict) else s
            for s in (prev_minutes.get("nextSteps") or [])[:4]
        ]

    return {
        "has_continuity": True,
        "session_count": len(prior_sessions) + 1,
        "thread_title": thread.title or "Recurring meeting",
        "prev_meeting_title": prev_minutes.get("title"),
        "prev_meeting_date": prev_minutes.get("metadata", {}).get("date") if prev_minutes else None,
        "key_decisions": key_decisions,
        "next_steps": next_steps,
        "open_tasks_from_thread": [
            {"title": t.title, "deadline": t.deadline, "status": t.status}
            for t in open_tasks
        ],
    }


def _generate_talking_points(
    user_name: str | None,
    event_title: str,
    attendees: list[str],
    thread_history: dict | None,
    open_tasks: list,
) -> list[str]:
    try:
        lines = [f"Meeting: {event_title}"]
        if attendees:
            lines.append(f"Attendees: {', '.join(attendees[:6])}")
        if thread_history:
            overview = (thread_history.get("overview") or "")[:300]
            if overview:
                lines.append(f"Last session summary: {overview}")
            decisions = [
                d.get("decision", d) if isinstance(d, dict) else str(d)
                for d in (thread_history.get("keyDecisions") or [])[:3]
            ]
            if decisions:
                lines.append("Previous decisions: " + "; ".join(decisions))
        if open_tasks:
            lines.append("Open tasks: " + "; ".join(t.title for t in open_tasks[:4]))

        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=(
                "You are an AI Chief of Staff preparing a pre-meeting brief. "
                "Given the meeting context, generate 3-4 concise, specific talking points "
                "the user should raise or be ready for. "
                "Return a JSON array of short strings (under 15 words each). "
                "Output only the JSON array, no other text."
            ),
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("["):
            points = json.loads(text)
            return points[:4] if isinstance(points, list) else []
    except Exception as exc:
        logger.warning("Talking points generation failed: %s", exc)
    return []


def generate_pre_meeting_brief(
    db,
    user_id: str,
    user_name: str | None,
    event_title: str,
    attendees: list[str],
) -> dict:
    """Generate a focused pre-meeting brief scoped to the matching meeting thread."""
    from models.meeting_thread import MeetingThreadRecord, ThreadSessionRecord
    from models.job import JobRecord
    from models.task import TaskRecord

    # Find matching thread by title similarity
    threads = db.query(MeetingThreadRecord).filter(
        MeetingThreadRecord.user_id == user_id
    ).all()

    matched_thread = None
    event_lower = event_title.lower()
    for thread in threads:
        t_name = (getattr(thread, "name", None) or getattr(thread, "title", None) or "").lower()
        if not t_name:
            continue
        if t_name in event_lower or event_lower in t_name:
            matched_thread = thread
            break
        event_words = set(event_lower.split())
        thread_words = set(t_name.split())
        if len(event_words & thread_words) >= 2:
            matched_thread = thread
            break

    thread_history: dict | None = None
    open_tasks: list = []

    if matched_thread:
        last_session = (
            db.query(ThreadSessionRecord)
            .filter(ThreadSessionRecord.thread_id == matched_thread.id)
            .order_by(ThreadSessionRecord.session_number.desc())
            .first()
        )
        if last_session:
            prev_job = db.get(JobRecord, last_session.job_id)
            if prev_job and getattr(prev_job, "minutes_json", None):
                try:
                    thread_history = json.loads(prev_job.minutes_json)
                except Exception:
                    pass

        all_sessions = db.query(ThreadSessionRecord).filter(
            ThreadSessionRecord.thread_id == matched_thread.id
        ).all()
        job_ids = [s.job_id for s in all_sessions]
        open_tasks = (
            db.query(TaskRecord)
            .filter(
                TaskRecord.assignee_id == user_id,
                TaskRecord.status != "done",
                TaskRecord.job_id.in_(job_ids),
            )
            .limit(6)
            .all()
        )

    talking_points = _generate_talking_points(
        user_name, event_title, attendees, thread_history, open_tasks
    )

    result: dict = {
        "event_title": event_title,
        "attendees": attendees,
        "has_history": matched_thread is not None,
        "thread_name": (getattr(matched_thread, "name", None) if matched_thread else None),
        "talking_points": talking_points,
        "open_tasks": [{"title": t.title, "deadline": t.deadline} for t in open_tasks],
    }

    if thread_history:
        result["last_meeting_overview"] = (thread_history.get("overview") or "")[:400]
        result["last_decisions"] = [
            d.get("decision", d) if isinstance(d, dict) else str(d)
            for d in (thread_history.get("keyDecisions") or [])[:4]
        ]

    return result


def _run_briefings():
    try:
        from db.base import SessionLocal
        from models.user import User
        from services.notifications.email import send_daily_briefing

        with SessionLocal() as db:
            users = db.query(User).filter(User.email.isnot(None)).all()
            for user in users:
                try:
                    ws = getattr(user, "active_workspace_id", None)
                    briefing = generate_briefing(db, user.id, user.name, ws)
                    send_daily_briefing(
                        to_email=user.email,
                        user_name=user.name,
                        briefing=briefing,
                    )
                    logger.info("Sent daily briefing to %s", user.email)
                except Exception as exc:
                    logger.error("Briefing failed for %s: %s", user.email, exc)
    except Exception as exc:
        logger.error("Daily briefing run failed: %s", exc)


def _loop():
    while True:
        wait = _seconds_until_next()
        logger.info("Chief of Staff: next briefing in %.0f s (%.1fh)", wait, wait / 3600)
        time.sleep(wait)
        _run_briefings()
        time.sleep(60)


def start_daily_briefing_loop():
    t = threading.Thread(target=_loop, daemon=True, name="chief-of-staff")
    t.start()
    logger.info("Chief of Staff daily briefing scheduler started (hour=%d UTC)", BRIEFING_HOUR)
