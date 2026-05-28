import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from middleware.auth import get_db, get_current_user
from models.job import JobRecord
from models.user import User
from services.core.meetings import get_meeting_stats, job_to_dict

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.get("")
def list_meetings(
    workspace_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(JobRecord).filter(JobRecord.user_id == current_user.id)
    if workspace_id:
        q = q.filter(JobRecord.workspace_id == workspace_id)
    records = q.order_by(JobRecord.created_at.desc()).all()

    # Batch-load thread info so we can show category on each meeting card
    from models.meeting_thread import ThreadSessionRecord, MeetingThreadRecord
    job_ids = [r.id for r in records]
    thread_map: dict[str, dict] = {}
    if job_ids:
        sessions = db.query(ThreadSessionRecord).filter(
            ThreadSessionRecord.job_id.in_(job_ids)
        ).all()
        thread_ids = list({s.thread_id for s in sessions})
        threads = db.query(MeetingThreadRecord).filter(
            MeetingThreadRecord.id.in_(thread_ids)
        ).all()
        threads_by_id = {t.id: t for t in threads}
        for s in sessions:
            t = threads_by_id.get(s.thread_id)
            if t:
                thread_map[s.job_id] = {"name": t.name, "category": t.category}

    return [job_to_dict(r, thread_map.get(r.id)) for r in records]


@router.get("/stats")
def meeting_stats(
    workspace_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return get_meeting_stats(db, current_user.id, workspace_id)


def _parse_duration_minutes(s: str) -> float:
    """Parse '1h 30m', '45 minutes', '2 hours', '1:30:00' → float minutes."""
    import re
    if not s:
        return 0
    s = s.strip().lower()
    m = re.match(r'^(\d+):(\d+)(?::(\d+))?$', s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    total = 0.0
    m = re.search(r'(\d+(?:\.\d+)?)\s*h(?:our)?s?', s)
    if m:
        total += float(m.group(1)) * 60
    m = re.search(r'(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?(?!\s*h)', s)
    if m:
        total += float(m.group(1))
    return total


@router.get("/analytics")
def meeting_analytics(
    days: int = Query(84, ge=7, le=365),
    workspace_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    base_q = db.query(JobRecord).filter(
        JobRecord.user_id == current_user.id,
        JobRecord.created_at >= cutoff,
    )
    if workspace_id:
        base_q = base_q.filter(JobRecord.workspace_id == workspace_id)
    records = base_q.order_by(JobRecord.created_at.asc()).all()

    DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

    weekly: dict[str, int] = defaultdict(int)
    action_weekly: dict[str, int] = defaultdict(int)
    status_counts: dict[str, int] = defaultdict(int)
    day_counts: dict[str, int] = defaultdict(int)
    participant_counts: dict[str, int] = defaultdict(int)
    total_actions = 0
    total_decisions = 0
    total_duration_min = 0.0
    duration_count = 0
    meetings_no_actions = 0

    for r in records:
        created = r.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if not created:
            continue

        yr, wk, _ = created.isocalendar()
        week_key = f"{yr}-W{wk:02d}"
        day_counts[DAY_NAMES[created.weekday()]] += 1
        status_counts[r.status] += 1
        if r.status == "done":
            weekly[week_key] += 1

        minutes = None
        if getattr(r, "minutes_json", None):
            try:
                minutes = json.loads(r.minutes_json) if isinstance(r.minutes_json, str) else r.minutes_json
            except Exception:
                pass
        if minutes:
            items = minutes.get("actionItems") or minutes.get("action_items") or []
            n = len(items)
            total_actions += n
            action_weekly[week_key] += n
            if n == 0 and r.status == "done":
                meetings_no_actions += 1

            decisions = minutes.get("key_decisions") or minutes.get("keyDecisions") or []
            total_decisions += len(decisions)

            meta = minutes.get("metadata") or {}
            dur = _parse_duration_minutes(meta.get("duration") or "")
            if dur > 0:
                total_duration_min += dur
                duration_count += 1

            for p in (meta.get("participants") or []):
                if isinstance(p, str) and p.strip():
                    participant_counts[p.strip()] += 1

    # Bot vs upload
    done_ids = {r.id for r in records if r.status == "done"}
    bot_count = 0
    try:
        from models.meeting_bot import MeetingBotSessionRecord
        bot_job_ids = {
            row.job_id for row in
            db.query(MeetingBotSessionRecord.job_id)
              .filter(MeetingBotSessionRecord.job_id.in_(done_ids))
              .all()
            if row.job_id
        }
        bot_count = len(bot_job_ids)
    except Exception:
        pass

    # Task completion rate
    completion_rate = None
    try:
        from models.task import TaskRecord
        tq = db.query(TaskRecord).filter(TaskRecord.job_id.in_(done_ids))
        if workspace_id:
            tq = tq.filter(TaskRecord.workspace_id == workspace_id)
        all_tasks = tq.all()
        if all_tasks:
            completion_rate = round(sum(1 for t in all_tasks if t.status == "done") / len(all_tasks) * 100, 1)
    except Exception:
        pass

    # Week series
    num_weeks = (days // 7) + 1
    week_labels: list[str] = []
    for i in range(num_weeks - 1, -1, -1):
        d = now - timedelta(weeks=i)
        yr, wk, _ = d.isocalendar()
        week_labels.append(f"{yr}-W{wk:02d}")
    week_labels = list(dict.fromkeys(week_labels))
    weekly_series = [{"week": w, "meetings": weekly.get(w, 0), "actions": action_weekly.get(w, 0)} for w in week_labels]

    return {
        "total_meetings":    status_counts.get("done", 0),
        "total_actions":     total_actions,
        "total_decisions":   total_decisions,
        "total_hours":       round(total_duration_min / 60, 1),
        "avg_duration_min":  round(total_duration_min / duration_count, 1) if duration_count else None,
        "meetings_no_actions": meetings_no_actions,
        "by_status":         dict(status_counts),
        "weekly_series":     weekly_series,
        "busiest_day_series": [{"day": d, "count": day_counts.get(d, 0)} for d in DAY_NAMES],
        "top_participants":  sorted(
            [{"name": k, "count": v} for k, v in participant_counts.items()],
            key=lambda x: x["count"], reverse=True
        )[:8],
        "bot_count":         bot_count,
        "upload_count":      max(status_counts.get("done", 0) - bot_count, 0),
        "completion_rate":   completion_rate,
    }


@router.get("/{job_id}/audio")
def get_audio_url(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return a short-lived presigned URL to stream the meeting's original audio from R2."""
    record = db.query(JobRecord).filter(
        JobRecord.id == job_id, JobRecord.user_id == current_user.id
    ).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    r2_key = getattr(record, "r2_audio_key", None)
    if not r2_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audio not available — this meeting was processed before R2 was enabled.")

    try:
        from services.core.storage_r2 import get_presigned_url, _is_configured
        if not _is_configured():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Cloud storage is not configured on this server.")
        url = get_presigned_url(r2_key, expires_in=3600)
        return {"url": url, "expires_in": 3600}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not generate audio URL: {exc}")


@router.patch("/{job_id}/star")
def toggle_star(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = db.query(JobRecord).filter(
        JobRecord.id == job_id, JobRecord.user_id == current_user.id
    ).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")
    record.starred = not bool(getattr(record, "starred", False))
    db.commit()
    return {"starred": record.starred}


@router.delete("/{job_id}/recording", status_code=204)
def delete_recording(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete only the audio/video file — keeps the meeting, minutes, and transcript."""
    record = db.query(JobRecord).filter(
        JobRecord.id == job_id, JobRecord.user_id == current_user.id
    ).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    r2_key = getattr(record, "r2_audio_key", None)
    if r2_key:
        try:
            from services.core.storage_r2 import delete_file
            delete_file(r2_key)
        except Exception:
            pass

    local_path = getattr(record, "audio_path", None)
    if local_path:
        try:
            from pathlib import Path as _Path
            _Path(local_path).unlink(missing_ok=True)
        except Exception:
            pass

    record.audio_path = None
    record.r2_audio_key = None
    db.commit()


@router.get("/search")
def search_meetings(
    q: str = Query(..., min_length=1),
    workspace_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full-content search across meeting titles, overviews, decisions, and action items."""
    needle = q.lower()

    base_q = db.query(JobRecord).filter(JobRecord.user_id == current_user.id)
    if workspace_id:
        base_q = base_q.filter(JobRecord.workspace_id == workspace_id)
    records = base_q.order_by(JobRecord.created_at.desc()).all()

    results = []
    for r in records:
        minutes = None
        if getattr(r, "minutes_json", None):
            try:
                minutes = json.loads(r.minutes_json) if isinstance(r.minutes_json, str) else r.minutes_json
            except Exception:
                pass

        snippet = _find_snippet(r, minutes, needle)
        if snippet is not None:
            d = job_to_dict(r, None)
            d["snippet"] = snippet
            results.append(d)

    return results


def _find_snippet(record: JobRecord, minutes: dict | None, needle: str) -> str | None:
    def _excerpt(text: str) -> str:
        idx = text.lower().find(needle)
        start = max(0, idx - 60)
        end = min(len(text), idx + len(needle) + 60)
        excerpt = text[start:end].strip()
        if start > 0:
            excerpt = "…" + excerpt
        if end < len(text):
            excerpt = excerpt + "…"
        return excerpt

    title = getattr(record, "title", None) or ""
    if needle in title.lower():
        return _excerpt(title)

    if not minutes:
        return None

    overview = minutes.get("overview") or minutes.get("summary") or ""
    if needle in overview.lower():
        return _excerpt(overview)

    for decision in (minutes.get("keyDecisions") or minutes.get("key_decisions") or []):
        text = decision if isinstance(decision, str) else (decision.get("text") or decision.get("decision") or "")
        if needle in text.lower():
            return _excerpt(text)

    for item in (minutes.get("actionItems") or minutes.get("action_items") or []):
        text = item if isinstance(item, str) else (item.get("task") or item.get("text") or item.get("title") or "")
        if needle in text.lower():
            return _excerpt(text)

    return None


@router.delete("/{job_id}", status_code=204)
def delete_meeting(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = db.query(JobRecord).filter(
        JobRecord.id == job_id, JobRecord.user_id == current_user.id
    ).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    # Also remove from R2 if present
    r2_key = getattr(record, "r2_audio_key", None)
    if r2_key:
        try:
            from services.core.storage_r2 import delete_file
            delete_file(r2_key)
        except Exception:
            pass

    db.delete(record)
    db.commit()
