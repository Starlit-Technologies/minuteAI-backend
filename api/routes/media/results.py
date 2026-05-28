import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from middleware.auth import get_current_user
from models.job import store
from models.user import User

router = APIRouter()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply_speaker_map(obj, speaker_map: dict):
    """Recursively replace speaker labels in any JSON-serialisable structure."""
    if not speaker_map:
        return obj
    if isinstance(obj, str):
        for old, new in speaker_map.items():
            if new:
                obj = obj.replace(old, new)
        return obj
    if isinstance(obj, list):
        return [_apply_speaker_map(item, speaker_map) for item in obj]
    if isinstance(obj, dict):
        return {k: _apply_speaker_map(v, speaker_map) for k, v in obj.items()}
    return obj


def _apply_speaker_map_to_segments(segments: list[dict] | None, speaker_map: dict) -> list[dict]:
    if not segments:
        return []
    mapped = []
    for segment in segments:
        item = dict(segment)
        speaker = item.get("speaker")
        if speaker_map and speaker in speaker_map and speaker_map[speaker]:
            item["speaker"] = speaker_map[speaker]
        mapped.append(item)
    return mapped


def _extract_speakers(transcript: str) -> list[dict]:
    """
    Parse 'Speaker X: utterance' lines from the stored transcript.
    Returns speakers in order of first appearance with up to 2 sample quotes each.
    """
    speakers: dict[str, list[str]] = {}
    order: list[str] = []
    pattern = re.compile(r'^(Speaker [^\:]+):\s*(.+)$', re.MULTILINE)
    for m in pattern.finditer(transcript):
        label = m.group(1).strip()
        text  = m.group(2).strip()
        if label not in speakers:
            speakers[label] = []
            order.append(label)
        if len(speakers[label]) < 2 and len(text) > 15:
            speakers[label].append(text[:140] + ("…" if len(text) > 140 else ""))
    return [{"label": lbl, "samples": speakers[lbl]} for lbl in order]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/results/{job_id}")
def get_results(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status == "error":
        raise HTTPException(status_code=500, detail=job.message or "Processing failed.")
    if job.status != "done":
        raise HTTPException(status_code=202, detail="Processing not complete yet.")

    speaker_map = job.speaker_map or {}
    result = _apply_speaker_map(dict(job.minutes or {}), speaker_map)
    if job.transcript:
        result["transcript"] = _apply_speaker_map(job.transcript, speaker_map)
    if job.transcript_segments:
        result["transcript_segments"] = _apply_speaker_map_to_segments(job.transcript_segments, speaker_map)
    result["speaker_map"] = speaker_map
    result["has_audio"] = bool(job.audio_path or job.r2_audio_key)
    return result


@router.get("/results/{job_id}/speakers")
def get_speakers(job_id: str, current_user: User = Depends(get_current_user)):
    job = store.get(job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.transcript:
        return {"speakers": [], "speaker_map": {}}
    speakers = _extract_speakers(job.transcript)
    return {"speakers": speakers, "speaker_map": job.speaker_map or {}}


@router.get("/results/{job_id}/speaker-suggestions")
def get_speaker_suggestions(job_id: str, current_user: User = Depends(get_current_user)):
    """
    Return per-speaker name suggestions and the attendee list from the bot session.
    Suggestions come from voice profile matching (if embeddings stored) or
    attendee × known-profile heuristics.
    """
    job = store.get(job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found.")

    if not job.transcript:
        return {"suggestions": {}, "attendees": [], "known_profiles": []}

    workspace_id = job.workspace_id
    if not workspace_id:
        return {"suggestions": {}, "attendees": [], "known_profiles": []}

    speaker_labels = [s["label"] for s in _extract_speakers(job.transcript)]

    # Fetch attendees from the linked bot session
    attendees: list[str] = []
    try:
        from db.base import SessionLocal
        from models.meeting_bot import MeetingBotSessionRecord
        with SessionLocal() as db:
            sess = db.query(MeetingBotSessionRecord).filter(
                MeetingBotSessionRecord.job_id == job_id
            ).first()
            if sess and sess.attendees_json:
                import json as _json
                attendees = _json.loads(sess.attendees_json) or []
    except Exception:
        pass

    # Resolve audio path (local or skip if R2)
    audio_path: Optional[str] = job.audio_path if job.audio_path and os.path.exists(job.audio_path or "") else None

    from services.ai.voice_profile_service import auto_suggest_speakers
    from models.voice_profile import get_profiles

    suggestions = auto_suggest_speakers(
        workspace_id=workspace_id,
        speaker_labels=speaker_labels,
        attendees=attendees,
        audio_path=audio_path,
        segments=job.transcript_segments,
    )
    known_profiles = [p["person_name"] for p in get_profiles(workspace_id)]

    return {
        "suggestions": suggestions,    # {"Speaker A": {"name": ..., "confidence": ..., "source": ...}}
        "attendees":   attendees,       # raw list from calendar invite
        "known_profiles": known_profiles,
    }


class SpeakerMapIn(BaseModel):
    speaker_map: dict[str, str]


class TranscriptIn(BaseModel):
    transcript: str


class ShareIn(BaseModel):
    providers: list[str]


@router.post("/results/{job_id}/share")
def share_result(
    job_id: str,
    body: ShareIn,
    current_user: User = Depends(get_current_user),
):
    """Manually push a meeting to one or more connected integrations."""
    job = store.get(job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "done":
        raise HTTPException(status_code=400, detail="Meeting is not ready yet.")

    import json as _json
    from models.connected_account import get_connected_account
    from services.integrations.runner import _dispatch, APP_URL

    minutes = {}
    if job.minutes_json:
        try:
            minutes = _json.loads(job.minutes_json)
        except Exception:
            pass
    elif job.minutes:
        minutes = job.minutes

    from models.meeting_thread import get_thread_for_job
    thread = get_thread_for_job(job_id)
    meeting_context = {"category": thread.category, "title": minutes.get("title", "")} if thread else {}

    results = {}
    for provider in body.providers:
        account = get_connected_account(current_user.id, provider)
        if not account:
            results[provider] = "not_connected"
            continue
        try:
            settings = account.settings or {}
            _dispatch(provider, account.access_token, settings, job_id, minutes, meeting_context)
            results[provider] = "ok"
        except Exception as exc:
            results[provider] = str(exc)

    return {"results": results}


@router.post("/results/{job_id}/speakers/apply")
def apply_speaker_names(
    job_id: str,
    body: SpeakerMapIn,
    current_user: User = Depends(get_current_user),
):
    job = store.get(job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found.")

    clean_map = {k: v.strip() for k, v in body.speaker_map.items() if v.strip()}
    store.update(job_id, speaker_map=clean_map if clean_map else None)

    # Learn from this assignment: upsert voice profiles in background
    if clean_map and job.workspace_id:
        audio_path = job.audio_path if job.audio_path and os.path.exists(job.audio_path or "") else None
        try:
            from services.ai.voice_profile_service import update_profiles_from_assignment
            update_profiles_from_assignment(
                workspace_id=job.workspace_id,
                speaker_map=clean_map,
                audio_path=audio_path,
                segments=job.transcript_segments,
            )
        except Exception:
            pass  # never fail the save because of profile learning

    return {"speaker_map": clean_map}


@router.patch("/results/{job_id}/transcript")
def update_transcript(
    job_id: str,
    body: TranscriptIn,
    current_user: User = Depends(get_current_user),
):
    job = store.get(job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "done":
        raise HTTPException(status_code=400, detail="Meeting is not ready yet.")

    transcript = body.transcript.strip()
    store.update(job_id, transcript=transcript or None, transcript_segments=None)
    return {"transcript": transcript}


class FollowUpEmailIn(BaseModel):
    tone: str = "professional"          # professional | friendly | concise
    recipient_name: Optional[str] = None
    context: Optional[str] = None       # optional extra context from user


@router.post("/results/{job_id}/follow-up-email")
def generate_follow_up_email(
    job_id: str,
    body: FollowUpEmailIn,
    current_user: User = Depends(get_current_user),
):
    """Generate a follow-up email from meeting minutes using Claude."""
    job = store.get(job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "done":
        raise HTTPException(status_code=400, detail="Meeting is not ready yet.")

    mins = job.minutes or {}
    if not mins:
        raise HTTPException(status_code=400, detail="No minutes available for this meeting.")

    tone_desc = {
        "professional": "professional and formal",
        "friendly":     "warm and conversational",
        "concise":      "short, direct, and bullet-point-driven",
    }.get(body.tone, "professional and formal")

    title        = mins.get("title", "our meeting")
    date         = (mins.get("metadata") or {}).get("date", "")
    overview     = mins.get("overview", "")
    decisions    = mins.get("keyDecisions", [])
    action_items = mins.get("actionItems", [])
    next_steps   = mins.get("nextSteps", [])

    decisions_text = "\n".join(
        f"- {d.get('decision', d) if isinstance(d, dict) else d}"
        for d in decisions[:6]
    )
    actions_text = "\n".join(
        f"- {a.get('task', a) if isinstance(a, dict) else a}"
        + (f" (owner: {a['owner']})" if isinstance(a, dict) and a.get('owner') else "")
        + (f" by {a['deadline']}" if isinstance(a, dict) and a.get('deadline') and a.get('deadline') != 'TBD' else "")
        for a in action_items[:8]
    )
    next_steps_text = "\n".join(
        f"- {s.get('step', s) if isinstance(s, dict) else s}"
        for s in next_steps[:5]
    )

    recipient_line = f"The email is addressed to {body.recipient_name}." if body.recipient_name else ""
    context_line   = f"Additional context: {body.context}" if body.context else ""

    prompt = f"""You are a professional meeting follow-up writer.

MEETING DETAILS:
Title: {title}
{f'Date: {date}' if date else ''}
Overview: {overview}

{f'Key decisions:{chr(10)}{decisions_text}' if decisions_text else ''}
{f'Action items:{chr(10)}{actions_text}' if actions_text else ''}
{f'Next steps:{chr(10)}{next_steps_text}' if next_steps_text else ''}

INSTRUCTIONS:
- Write a follow-up email in a {tone_desc} tone
- {recipient_line}
- {context_line}
- Include a clear subject line
- Reference specific decisions and action items from the meeting
- Do NOT include internal notes, financials, or anything marked confidential
- Keep it professional — this is going to an external or team recipient
- End with a clear call to action or next step

OUTPUT FORMAT (strictly follow, no extra text):
SUBJECT: <subject line here>
BODY:
<email body here>"""

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = msg.content[0].text.strip()

    # Parse subject + body
    subject = ""
    email_body = raw
    if raw.startswith("SUBJECT:"):
        lines = raw.split("\n", 1)
        subject = lines[0].replace("SUBJECT:", "").strip()
        rest = lines[1].strip() if len(lines) > 1 else ""
        if rest.startswith("BODY:"):
            email_body = rest[len("BODY:"):].strip()
        else:
            email_body = rest

    return {"subject": subject, "body": email_body}
