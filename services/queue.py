"""
Redis + RQ job queue for meeting bot sessions.

If REDIS_URL is not set (local dev without Redis), falls back to direct
thread execution so development works without running Redis.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

REDIS_URL  = os.environ.get("REDIS_URL", "")
QUEUE_NAME = "minuteai-bots"

# Job timeout: 3 hours (max meeting length we'll ever record)
JOB_TIMEOUT = 10_800

_redis_conn = None
_rq_queue   = None


def _conn():
    global _redis_conn
    if _redis_conn is None:
        import redis
        _redis_conn = redis.from_url(REDIS_URL, decode_responses=False)
    return _redis_conn


def get_queue():
    global _rq_queue
    if _rq_queue is None:
        from rq import Queue
        _rq_queue = Queue(QUEUE_NAME, connection=_conn(), default_timeout=JOB_TIMEOUT)
    return _rq_queue


def is_redis_available() -> bool:
    if not REDIS_URL:
        return False
    try:
        _conn().ping()
        return True
    except Exception:
        return False


def enqueue_session(session_id: str, run_at: Optional[datetime] = None) -> bool:
    """
    Enqueue _run_session to Redis/RQ.  Returns True if queued, False if
    Redis is unavailable (caller should fall back to threads).

    run_at — UTC datetime when the job should start executing.
             If None the job is queued immediately.
             Uses RQ's built-in scheduler (requires --with-scheduler worker flag).
    """
    if not is_redis_available():
        return False

    from services.meeting_bots.runner import _run_session
    q = get_queue()

    try:
        # Use session_id as job_id for idempotency — RQ rejects duplicate job IDs
        # if the job is still queued/running, so we never double-launch.
        job_id = f"bot-session-{session_id}"

        if run_at:
            # Convert to UTC-aware datetime if naive
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
            q.enqueue_at(run_at, _run_session, session_id,
                         job_id=job_id, job_timeout=JOB_TIMEOUT)
            log.info("Enqueued bot session %s to run at %s", session_id, run_at)
        else:
            q.enqueue(_run_session, session_id,
                      job_id=job_id, job_timeout=JOB_TIMEOUT)
            log.info("Enqueued bot session %s immediately", session_id)

        return True

    except Exception as exc:
        # Duplicate job ID raises an error — that's fine, session is already queued
        if "already exists" in str(exc).lower():
            log.debug("Bot session %s already queued, skipping", session_id)
            return True
        log.warning("Failed to enqueue bot session %s: %s", session_id, exc)
        return False


def cancel_queued_session(session_id: str) -> None:
    """Remove a scheduled/queued job from Redis when a session is cancelled."""
    if not is_redis_available():
        return
    try:
        from rq.job import Job
        job = Job.fetch(f"bot-session-{session_id}", connection=_conn())
        job.cancel()
        log.info("Cancelled queued job for session %s", session_id)
    except Exception:
        pass  # job already started or doesn't exist — nothing to cancel
