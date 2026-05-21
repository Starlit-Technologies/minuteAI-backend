import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from models.job import JobRecord


def parse_meta(record: JobRecord) -> dict:
    try:
        data = json.loads(record.minutes_json) if record.minutes_json else {}
    except Exception:
        data = {}
    meta = data.get("metadata", {})
    participants = meta.get("participants", [])
    action_items = data.get("action_items", data.get("actionItems", []))
    return {
        "title":        data.get("title", "Untitled Meeting"),
        "duration":     meta.get("duration", "—"),
        "participants": len(participants) if isinstance(participants, list) else 0,
        "actions":      len(action_items) if isinstance(action_items, list) else 0,
    }


_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".opus"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov"}


def _audio_type(record: JobRecord) -> str | None:
    path = getattr(record, "r2_audio_key", None) or getattr(record, "audio_path", None)
    if not path:
        return None
    suffix = Path(path).suffix.lower()
    if suffix in _AUDIO_EXTS:
        return "audio"
    if suffix in _VIDEO_EXTS:
        return "video"
    return "audio"  # default for unknown


def job_to_dict(record: JobRecord) -> dict:
    meta = parse_meta(record) if record.status == "done" else {}
    has_audio = bool(getattr(record, "audio_path", None) or getattr(record, "r2_audio_key", None))
    return {
        "id":           record.id,
        "title":        meta.get("title", "Processing…") if meta else "Processing…",
        "date":         (record.created_at.isoformat() + "Z") if record.created_at else None,
        "duration":     meta.get("duration", "—"),
        "participants": meta.get("participants", 0),
        "actions":      meta.get("actions", 0),
        "status":       "done" if record.status == "done" else (
                        "error" if record.status == "error" else "processing"),
        "starred":      bool(getattr(record, "starred", False)),
        "has_audio":    has_audio,
        "audio_type":   _audio_type(record) if has_audio else None,
    }


def get_meeting_stats(db: Session, user_id: str, workspace_id: str | None) -> dict:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    q = db.query(JobRecord).filter(JobRecord.user_id == user_id)
    if workspace_id:
        q = q.filter(JobRecord.workspace_id == workspace_id)
    all_jobs = q.all()
    done = [j for j in all_jobs if j.status == "done"]
    this_month = [j for j in done if j.created_at and j.created_at >= month_start.replace(tzinfo=None)]

    total_actions = 0
    for j in done:
        try:
            data = json.loads(j.minutes_json) if j.minutes_json else {}
            ai = data.get("action_items", data.get("actionItems", []))
            total_actions += len(ai) if isinstance(ai, list) else 0
        except Exception:
            pass

    return {
        "total_meetings":      len(done),
        "meetings_this_month": len(this_month),
        "total_action_items":  total_actions,
        "processing":          sum(1 for j in all_jobs if j.status not in ("done", "error")),
    }
