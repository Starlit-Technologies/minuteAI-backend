import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile
from fastapi.responses import Response

from middleware.auth import get_current_user

router = APIRouter(prefix="/pdf-to-audio", tags=["pdf-audio"])

# In-memory job store
_jobs: dict = {}
_lock = threading.Lock()

VALID_PURPOSES = {
    "project_defense", "class_presentation", "conference_talk",
    "study_review", "business_pitch", "podcast", "slide_deck",
}

VALID_VOICES = {
    "asteria", "luna", "stella", "athena",
    "orion", "arcas", "helios", "zeus",
    "female", "male",  # legacy aliases
}

VALID_MODES = {"full", "summary", "brief"}


def _public_job(job_id: str, job: dict, include_script: bool = False) -> dict:
    payload = {
        "job_id":             job_id,
        "status":             job.get("status"),
        "step":               job.get("step"),
        "filename":           job.get("filename"),
        "purpose":            job.get("purpose"),
        "mode":               job.get("mode"),
        "duration_minutes":   job.get("duration_minutes"),
        "voice":              job.get("voice"),
        "language":           job.get("language"),
        "created_at":         job.get("created_at"),
        "completed_at":       job.get("completed_at"),
        "word_count":         job.get("word_count"),
        "estimated_duration": job.get("estimated_duration"),
        "error":              job.get("error"),
        "has_audio":          bool(job.get("audio_bytes")),
        "has_quiz":           bool(job.get("quiz")),
        "has_flashcards":     bool(job.get("flashcards")),
    }
    if include_script:
        payload["script"] = job.get("script")
    return payload


@router.get("/meta")
def get_meta():
    """Return available voices, modes, and purposes for the frontend."""
    from services.media.pdf_audio import VOICE_META, MODES, PURPOSES
    return {
        "voices": VOICE_META,
        "modes": {k: {"label": v["label"], "description": v["description"]} for k, v in MODES.items()},
        "purposes": {k: {"label": v["label"]} for k, v in PURPOSES.items()},
    }


@router.post("")
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    purpose: str = Form(...),
    duration_minutes: int = Form(...),
    voice: str = Form("asteria"),
    mode: str = Form("full"),
    language: str = Form("EN"),
    generate_quiz: bool = Form(False),
    generate_flashcards: bool = Form(False),
    current_user=Depends(get_current_user),
):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported.")
    if purpose not in VALID_PURPOSES:
        raise HTTPException(400, f"Invalid purpose. Choose from: {', '.join(sorted(VALID_PURPOSES))}")
    if not 1 <= duration_minutes <= 60:
        raise HTTPException(400, "Duration must be between 1 and 60 minutes.")
    if voice not in VALID_VOICES:
        raise HTTPException(400, f"Invalid voice. Choose from: {', '.join(sorted(VALID_VOICES - {'female', 'male'}))}")
    if mode not in VALID_MODES:
        raise HTTPException(400, f"Invalid mode. Choose from: {', '.join(VALID_MODES)}")

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
            "mode": mode,
            "duration_minutes": duration_minutes,
            "voice": voice,
            "language": language.upper(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generate_quiz": generate_quiz,
            "generate_flashcards": generate_flashcards,
        }

    background_tasks.add_task(
        _process, job_id, pdf_bytes, purpose, duration_minutes, voice, mode,
        language.upper(), generate_quiz, generate_flashcards,
    )
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


@router.get("/{job_id}/quiz")
def get_quiz(job_id: str, current_user=Depends(get_current_user)):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job.get("owner") != current_user.id:
        raise HTTPException(403, "Not your job.")
    if not job.get("quiz"):
        raise HTTPException(404, "Quiz not available for this job.")
    return job["quiz"]


@router.get("/{job_id}/flashcards")
def get_flashcards(job_id: str, current_user=Depends(get_current_user)):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job.get("owner") != current_user.id:
        raise HTTPException(403, "Not your job.")
    if not job.get("flashcards"):
        raise HTTPException(404, "Flashcards not available for this job.")
    return job["flashcards"]


def _process(
    job_id: str,
    pdf_bytes: bytes,
    purpose: str,
    duration_minutes: int,
    voice: str,
    mode: str,
    language: str,
    do_quiz: bool,
    do_flashcards: bool,
):
    def update(patch: dict):
        with _lock:
            _jobs[job_id].update(patch)

    try:
        from services.media.pdf_audio import (
            extract_pdf_text, generate_script, text_to_speech,
            generate_quiz, generate_flashcards, translate_script,
            MODES,
        )

        update({"step": "Extracting PDF text…"})
        text = extract_pdf_text(pdf_bytes)

        update({"step": "Generating presentation script with AI…"})
        script = generate_script(text, purpose, duration_minutes, mode)

        # Translate if not English
        if language and language.upper() not in ("EN", "EN-US", "EN-GB"):
            update({"step": f"Translating script to {language}…"})
            script = translate_script(script, language)

        update({"step": "Converting script to audio…"})
        audio_bytes = text_to_speech(script, voice)

        word_count   = len(script.split())
        mode_info    = MODES.get(mode, MODES["full"])
        effective_dur = mode_info["duration_override"] if mode_info["duration_override"] is not None else duration_minutes
        est_duration = round(word_count / 130, 1)

        updates = {
            "status":             "done",
            "step":               "Complete",
            "script":             script,
            "audio_bytes":        audio_bytes,
            "word_count":         word_count,
            "estimated_duration": est_duration,
            "completed_at":       datetime.now(timezone.utc).isoformat(),
        }

        if do_quiz:
            update({"step": "Generating comprehension quiz…"})
            try:
                updates["quiz"] = generate_quiz(script)
            except Exception:
                updates["quiz"] = None

        if do_flashcards:
            update({"step": "Generating flashcards…"})
            try:
                updates["flashcards"] = generate_flashcards(script)
            except Exception:
                updates["flashcards"] = None

        update(updates)

    except Exception as exc:
        update({"status": "error", "step": "Failed", "error": str(exc)})
