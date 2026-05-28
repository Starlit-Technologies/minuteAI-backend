"""Weekly action item digest scheduler.

Runs every Monday at 08:00 UTC (configurable via DIGEST_DAY / DIGEST_HOUR env vars).
For each user who has open (non-done) tasks, sends a weekly digest email.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 0=Monday … 6=Sunday
DIGEST_DAY  = int(os.getenv("DIGEST_DAY",  "0"))   # Monday
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "8"))   # 08:00 UTC


def _seconds_until_next_send() -> float:
    """Return seconds until the next scheduled send window."""
    now = datetime.now(timezone.utc)
    days_ahead = (DIGEST_DAY - now.weekday()) % 7
    if days_ahead == 0 and now.hour >= DIGEST_HOUR:
        days_ahead = 7  # already past today's window — wait until next week
    target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    import datetime as dt
    target += dt.timedelta(days=days_ahead)
    return (target - now).total_seconds()


def _task_to_dict(t, db) -> dict:
    from models.job import JobRecord
    import json as _json
    job = db.get(JobRecord, t.job_id) if t.job_id else None
    meeting_title = None
    if job and job.minutes_json:
        try:
            meeting_title = _json.loads(job.minutes_json).get("title")
        except Exception:
            pass
    return {"title": t.title, "status": t.status, "deadline": t.deadline, "meeting_title": meeting_title}


def _run_digest():
    """Fetch tasks per user (open + completed last week) and send digest emails."""
    try:
        from datetime import date, timedelta
        from db.base import SessionLocal
        from models.task import TaskRecord
        from models.user import User
        from services.notifications.email import send_weekly_digest

        today      = date.today()
        week_ago   = today - timedelta(days=7)
        end_of_week = today + timedelta(days=(6 - today.weekday()))  # coming Sunday
        week_label  = today.strftime("week of %b %d")

        with SessionLocal() as db:
            # All open tasks assigned to someone
            open_tasks = (
                db.query(TaskRecord)
                .filter(TaskRecord.status != "done", TaskRecord.assignee_id.isnot(None))
                .all()
            )
            # Tasks completed in the last 7 days
            recently_done = (
                db.query(TaskRecord)
                .filter(
                    TaskRecord.status == "done",
                    TaskRecord.assignee_id.isnot(None),
                    TaskRecord.created_at >= str(week_ago),
                )
                .all()
            )

            # Group by assignee
            by_user: dict[str, dict] = {}
            for t in open_tasks:
                uid = t.assignee_id
                bucket = by_user.setdefault(uid, {"overdue": [], "due_this_week": [], "other": [], "completed": []})
                d = _task_to_dict(t, db)
                if t.deadline and t.deadline < str(today):
                    bucket["overdue"].append(d)
                elif t.deadline and t.deadline <= str(end_of_week):
                    bucket["due_this_week"].append(d)
                else:
                    bucket["other"].append(d)

            for t in recently_done:
                uid = t.assignee_id
                bucket = by_user.setdefault(uid, {"overdue": [], "due_this_week": [], "other": [], "completed": []})
                bucket["completed"].append(_task_to_dict(t, db))

            for user_id, buckets in by_user.items():
                total = sum(len(v) for v in buckets.values())
                if total == 0:
                    continue
                user = db.get(User, user_id)
                if not user or not user.email:
                    continue
                try:
                    send_weekly_digest(
                        to_email=user.email,
                        user_name=user.name,
                        week_label=week_label,
                        overdue=buckets["overdue"],
                        due_this_week=buckets["due_this_week"],
                        other_open=buckets["other"],
                        completed_last_week=buckets["completed"],
                    )
                    open_count = len(buckets["overdue"]) + len(buckets["due_this_week"]) + len(buckets["other"])
                    logger.info("Sent weekly digest to %s (%d open, %d done)", user.email, open_count, len(buckets["completed"]))
                except Exception as exc:
                    logger.error("Failed to send digest to %s: %s", user.email, exc)

    except Exception as exc:
        logger.error("Weekly digest run failed: %s", exc)


def _digest_loop():
    while True:
        wait = _seconds_until_next_send()
        logger.info("Weekly digest: next send in %.0f seconds (%.1f hours)", wait, wait / 3600)
        time.sleep(wait)
        _run_digest()
        time.sleep(60)  # prevent double-firing within same minute


def start_weekly_digest_loop():
    t = threading.Thread(target=_digest_loop, daemon=True, name="weekly-digest")
    t.start()
    logger.info("Weekly digest scheduler started (day=%d hour=%d UTC)", DIGEST_DAY, DIGEST_HOUR)
