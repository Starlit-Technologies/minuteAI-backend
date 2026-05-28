"""
Meeting Thread auto-assignment service.

After each meeting is processed, this service groups it with previous meetings
that had the same set of participants — creating a persistent "thread" that
tracks a recurring meeting over time.

Thread identity is a short SHA-256 of sorted participant identifiers
(emails preferred, falling back to real names) scoped by workspace.
Generic speaker labels ("Speaker A") are excluded — if only generics remain,
no thread is assigned.
"""

import hashlib
import json
import uuid
from typing import Optional

from db.base import SessionLocal
from models.meeting_thread import MeetingThreadRecord, ThreadSessionRecord


def _participants_hash(participants: list[str], workspace_id: Optional[str]) -> str:
    normalized = sorted(p.lower().strip() for p in participants if p.strip())
    key = f"{workspace_id or ''}|{','.join(normalized)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _extract_participants(minutes: dict, job_config: Optional[dict]) -> list[str]:
    """
    Build the canonical participant list used for thread fingerprinting.

    Priority:
    1. Attendee emails from job_config (calendar-sourced — most stable identifier)
    2. Real names from Claude's participant list (filters out "Speaker X" generics)
    """
    attendees = (job_config or {}).get("attendees")
    if attendees and isinstance(attendees, list):
        emails = [str(a).lower() for a in attendees if "@" in str(a)]
        if len(emails) >= 2:
            return emails

    raw = minutes.get("metadata", {}).get("participants", [])
    real = [p.strip() for p in raw if p.strip() and not p.lower().startswith("speaker ")]
    return [p.lower() for p in real]


def auto_assign_thread(
    job_id: str,
    minutes: dict,
    user_id: str,
    workspace_id: Optional[str],
    job_config: Optional[dict] = None,
) -> Optional[str]:
    """
    Find or create a thread for this meeting and link the job to it.

    Returns the thread_id on success, None if participants are insufficient
    (fewer than 2 identifiable, non-generic participants).
    """
    participants = _extract_participants(minutes, job_config)
    if len(participants) < 2:
        return None

    h = _participants_hash(participants, workspace_id)

    with SessionLocal() as db:
        # Idempotent: don't assign twice
        existing = db.query(ThreadSessionRecord).filter(
            ThreadSessionRecord.job_id == job_id
        ).first()
        if existing:
            return existing.thread_id

        # Find matching thread (same participant fingerprint + workspace)
        thread = db.query(MeetingThreadRecord).filter(
            MeetingThreadRecord.participants_hash == h,
            MeetingThreadRecord.workspace_id == workspace_id,
        ).first()

        if not thread:
            thread = MeetingThreadRecord(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                created_by=user_id,
                name=minutes.get("title", "Meeting Series"),
                participants_hash=h,
                participants_json=json.dumps(participants),
                is_locked=False,
            )
            db.add(thread)
            db.flush()

        # Determine next session number
        last = (
            db.query(ThreadSessionRecord)
            .filter(ThreadSessionRecord.thread_id == thread.id)
            .order_by(ThreadSessionRecord.session_number.desc())
            .first()
        )
        next_num = (last.session_number + 1) if last else 1

        db.add(ThreadSessionRecord(
            id=str(uuid.uuid4()),
            thread_id=thread.id,
            job_id=job_id,
            session_number=next_num,
        ))
        db.commit()
        return thread.id
