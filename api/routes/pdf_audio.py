import threading
import uuid

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
        }

    background_tasks.add_task(_process, job_id, pdf_bytes, purpose, duration_minutes, voice)
    return {"job_id": job_id}


@router.get("/{job_id}")
def get_status(job_id: str, current_user=Depends(get_current_user)):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job.get("owner") != current_user.id:
        raise HTTPException(403, "Not your job.")
    # Strip large binary field from status response
    return {k: v for k, v in job.items() if k not in ("audio_bytes", "owner")}


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
        })

    except Exception as exc:
        update({"status": "error", "step": "Failed", "error": str(exc)})
