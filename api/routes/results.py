import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from middleware.auth import get_current_user
from models.job import store
from models.user import User

router = APIRouter()


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


class SpeakerMapIn(BaseModel):
    speaker_map: dict[str, str]


@router.post("/results/{job_id}/speakers/apply")
def apply_speaker_names(
    job_id: str,
    body: SpeakerMapIn,
    current_user: User = Depends(get_current_user),
):
    job = store.get(job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found.")

    # Only keep entries that have a non-empty name
    clean_map = {k: v.strip() for k, v in body.speaker_map.items() if v.strip()}
    store.update(job_id, speaker_map=clean_map if clean_map else None)

    return {"speaker_map": clean_map}
