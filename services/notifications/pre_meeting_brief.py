"""Pre-Meeting Brief scheduler.

Polls every 2 minutes for scheduled bot sessions that start in 10-25 minutes
and haven't had a brief sent yet.  For each qualifying session the host receives
an email containing:
  - Meeting title + start time + attendees
  - Continuity from the previous session in the same thread (decisions / next steps)
  - Their open action items
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

BRIEF_WINDOW_MIN = 10   # minutes before start — earliest
BRIEF_WINDOW_MAX = 25   # minutes before start — latest


def _run_brief_check():
    try:
        from db.base import SessionLocal
        from models.meeting_bot import MeetingBotSessionRecord
        from models.user import User
        from models.task import TaskRecord
        from models.job import JobRecord
        from models.meeting_thread import MeetingThreadRecord, ThreadSessionRecord
        from services.notifications.email import send_pre_meeting_brief

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        window_start = now + timedelta(minutes=BRIEF_WINDOW_MIN)
        window_end   = now + timedelta(minutes=BRIEF_WINDOW_MAX)

        with SessionLocal() as db:
            sessions = (
                db.query(MeetingBotSessionRecord)
                .filter(
                    MeetingBotSessionRecord.scheduled_for.isnot(None),
                    MeetingBotSessionRecord.scheduled_for >= window_start,
                    MeetingBotSessionRecord.scheduled_for <= window_end,
                    MeetingBotSessionRecord.brief_sent_at.is_(None),
                    MeetingBotSessionRecord.status.in_(["queued", "scheduled"]),
                )
                .all()
            )

            for session in sessions:
                try:
                    user = db.get(User, session.user_id)
                    if not user or not user.email:
                        continue

                    attendees = json.loads(session.attendees_json) if session.attendees_json else []

                    # ── thread continuity ─────────────────────────────────
                    last_minutes = None
                    if len(attendees) >= 2 and session.workspace_id:
                        from services.core.thread_service import _participants_hash
                        h = _participants_hash(attendees, session.workspace_id)
                        thread = db.query(MeetingThreadRecord).filter(
                            MeetingThreadRecord.participants_hash == h,
                            MeetingThreadRecord.workspace_id == session.workspace_id,
                        ).first()
                        if thread:
                            last_ts = (
                                db.query(ThreadSessionRecord)
                                .filter(ThreadSessionRecord.thread_id == thread.id)
                                .order_by(ThreadSessionRecord.session_number.desc())
                                .first()
                            )
                            if last_ts:
                                last_job = db.get(JobRecord, last_ts.job_id)
                                if last_job and last_job.minutes_json:
                                    try:
                                        last_minutes = json.loads(last_job.minutes_json)
                                    except Exception:
                                        pass

                    # ── open tasks ────────────────────────────────────────
                    open_tasks = (
                        db.query(TaskRecord)
                        .filter(
                            TaskRecord.assignee_id == session.user_id,
                            TaskRecord.status != "done",
                        )
                        .limit(8)
                        .all()
                    )
                    task_dicts = [
                        {"title": t.title, "deadline": t.deadline, "status": t.status}
                        for t in open_tasks
                    ]

                    send_pre_meeting_brief(
                        to_email=user.email,
                        user_name=user.name,
                        session_title=session.title or "Upcoming Meeting",
                        scheduled_for=session.scheduled_for,
                        attendees=attendees,
                        last_minutes=last_minutes,
                        open_tasks=task_dicts,
                    )

                    session.brief_sent_at = now
                    db.commit()
                    logger.info("Pre-meeting brief sent: session=%s user=%s", session.id, user.email)

                except Exception as exc:
                    logger.error("Brief failed for session %s: %s", session.id, exc)

    except Exception as exc:
        logger.error("Pre-meeting brief check error: %s", exc)


def _brief_loop():
    while True:
        _run_brief_check()
        time.sleep(120)


def start_pre_meeting_brief_loop():
    t = threading.Thread(target=_brief_loop, daemon=True, name="pre-meeting-brief")
    t.start()
    logger.info(
        "Pre-meeting brief scheduler started (window=%d-%d min before start)",
        BRIEF_WINDOW_MIN, BRIEF_WINDOW_MAX,
    )
