import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile
from fastapi.responses import Response

from middleware.auth import get_current_user

router = APIRouter(prefix="/pdf-to-audio", tags=["pdf-audio"])

# In-memory job store (same pattern as the upload job store)
_jobs: dict = {}
_lock = threading.Lock()

VALID_PURPOSES = {
    "project_defense", "class_presentation", "conference_talk",
    "study_review", "business_pitch", "podcast",
}


def _public_job(job_id: str, job: dict, include_script: bool = False) -> dict:
    payload = {
        "job_id":             job_id,
        "status":             job.get("status"),
        "step":               job.get("step"),
        "filename":           job.get("filename"),
        "purpose":            job.get("purpose"),
        "duration_minutes":   job.get("duration_minutes"),
        "voice":              job.get("voice"),
        "created_at":         job.get("created_at"),
        "completed_at":       job.get("completed_at"),
        "word_count":         job.get("word_count"),
        "estimated_duration": job.get("estimated_duration"),
        "error":              job.get("error"),
        "has_audio":          bool(job.get("audio_bytes")),
    }
    if include_script:
        payload["script"] = job.get("script")
    return payload


@router.post("")
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    purpose: str = Form(...),
    duration_minutes: int = Form(...),
    voice: str = Form("female"),
    current_user=Depends(get_current_user),
):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported.")
    if purpose not in VALID_PURPOSES:
        raise HTTPException(400, f"Invalid purpose. Choose from: {', '.join(VALID_PURPOSES)}")
    if not 1 <= duration_minutes <= 60:
        raise HTTPException(400, "Duration must be between 1 and 60 minutes.")
    if voice not in ("female", "male"):
        raise HTTPException(400, "Voice must be 'female' or 'male'.")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > 50 * 1024 * 1024:
        raise HTTPException(400, "PDF must be smaller than 50 MB.")

    job_id = str(uuid.uuid4())
    with _lock:
        _jobs[job_id] = {
            "status": "processing",
            "step":   "Extracting PDF text…",
            "owner":  current_user.id,
            "filename": file.filename or "presentation.pdf",
            "purpose": purpose,
            "duration_minutes": duration_minutes,
            "voice": voice,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    background_tasks.add_task(_process, job_id, pdf_bytes, purpose, duration_minutes, voice)
    return {"job_id": job_id}


@router.get("")
def list_jobs(current_user=Depends(get_current_user)):
    with _lock:
        items = [
            _public_job(job_id, job)
            for job_id, job in _jobs.items()
            if job.get("owner") == current_user.id
        ]
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {"items": items}


@router.get("/{job_id}")
def get_status(job_id: str, current_user=Depends(get_current_user)):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job.get("owner") != current_user.id:
        raise HTTPException(403, "Not your job.")
    return _public_job(job_id, job, include_script=True)


@router.get("/{job_id}/audio")
def download_audio(job_id: str, current_user=Depends(get_current_user)):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job.get("owner") != current_user.id:
        raise HTTPException(403, "Not your job.")
    if job.get("status") != "done":
        raise HTTPException(404, "Audio not ready yet.")
    return Response(
        content=job["audio_bytes"],
        media_type="audio/mpeg",
        headers={"Content-Disposition": 'attachment; filename="presentation.mp3"'},
    )


def _process(job_id: str, pdf_bytes: bytes, purpose: str, duration_minutes: int, voice: str):
    def update(patch: dict):
        with _lock:
            _jobs[job_id].update(patch)

    try:
        from services.pdf_audio import extract_pdf_text, generate_script, text_to_speech

        update({"step": "Extracting PDF text…"})
        text = extract_pdf_text(pdf_bytes)

        update({"step": "Generating presentation script with AI…"})
        script = generate_script(text, purpose, duration_minutes)

        update({"step": "Converting script to audio…"})
        audio_bytes = text_to_speech(script, voice)

        word_count   = len(script.split())
        est_duration = round(word_count / 130, 1)

        update({
            "status":             "done",
            "step":               "Complete",
            "script":             script,
            "audio_bytes":        audio_bytes,
            "word_count":         word_count,
            "estimated_duration": est_duration,
            "completed_at":       datetime.now(timezone.utc).isoformat(),
        })

    except Exception as exc:
        update({"status": "error", "step": "Failed", "error": str(exc)})
