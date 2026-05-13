import uuid
import os
import shutil
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException

from sqlalchemy.orm import Session

from middleware.auth import get_db, get_optional_user
from models.job import Job, JobRecord, store
from models.user import User
from services.processing_pipeline import start_pipeline
from services.users import _plan_limits

router = APIRouter()

UPLOAD_DIR = Path(__file__).parent.parent.parent / "storage" / "uploads"
CHUNK_DIR = Path(__file__).parent.parent.parent / "storage" / "chunks"

ALLOWED_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".wav", ".webm", ".ogg"}


@router.post("/upload/init")
async def init_upload(
    filename: str = Form(...),
    filesize: int = Form(...),
    email: Optional[str] = Form(None),
    workspace_id: Optional[str] = Form(None),
    # Pre-processing config
    meeting_type: Optional[str] = Form(None),
    meeting_title: Optional[str] = Form(None),
    attendees: Optional[str] = Form(None),
    audio_language: Optional[str] = Form(None),
    custom_instructions: Optional[str] = Form(None),
    template: Optional[str] = Form("standard"),
    output_language: Optional[str] = Form("en"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    # Enforce plan meeting limits for authenticated users
    if current_user:
        limits = _plan_limits(db, current_user.plan or "free")
        meetings_limit = limits.get("meetings")
        if meetings_limit is not None:
            used = db.query(JobRecord).filter(
                JobRecord.user_id == current_user.id,
                JobRecord.status == "done",
            ).count()
            if used >= meetings_limit:
                raise HTTPException(
                    status_code=403,
                    detail=f"You have used all {meetings_limit} meetings included in your plan. Upgrade to continue.",
                )

    job_id = str(uuid.uuid4())
    (CHUNK_DIR / job_id).mkdir(parents=True, exist_ok=True)

    job_config = {
        "meeting_type": meeting_type or "general",
        "meeting_title": meeting_title or "",
        "attendees": attendees or "",
        "audio_language": audio_language or "",
        "custom_instructions": custom_instructions or "",
        "template": template or "standard",
        "output_language": output_language or "en",
    }

    store.create(Job(
        id=job_id,
        status="uploading",
        email=email or (current_user.email if current_user else None),
        user_id=current_user.id if current_user else None,
        workspace_id=workspace_id,
        job_config=job_config,
    ))
    return {"jobId": job_id, "chunkSize": 5 * 1024 * 1024}


@router.post("/upload/chunk/{job_id}")
async def upload_chunk(
    job_id: str,
    index: int = Form(...),
    total: int = Form(...),
    chunk: UploadFile = File(...),
):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "uploading":
        raise HTTPException(status_code=400, detail="Job is not in uploading state.")

    chunk_path = CHUNK_DIR / job_id / f"{index:05d}"
    if chunk_path.exists():
        return {"received": index, "skipped": True}

    data = await chunk.read()
    if len(data) == 0:
        raise HTTPException(status_code=400, detail=f"Empty chunk at index {index}.")

    async with aiofiles.open(str(chunk_path), "wb") as handle:
        await handle.write(data)

    return {"received": index}


@router.post("/upload/complete/{job_id}")
async def complete_upload(
    job_id: str,
    filename: str = Form(...),
):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "uploading":
        raise HTTPException(status_code=400, detail="Job is not in uploading state.")

    chunk_dir = CHUNK_DIR / job_id
    chunks = sorted(chunk_dir.glob("*"), key=lambda path: int(path.name))
    if not chunks:
        raise HTTPException(status_code=400, detail="No chunks found; upload may have failed.")

    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        suffix = ".mp3"
    audio_path = str(UPLOAD_DIR / f"{job_id}{suffix}")

    async with aiofiles.open(audio_path, "wb") as output:
        for chunk_path in chunks:
            async with aiofiles.open(str(chunk_path), "rb") as chunk_file:
                await output.write(await chunk_file.read())

    shutil.rmtree(str(chunk_dir), ignore_errors=True)
    store.update(job_id, audio_path=audio_path)
    start_pipeline(job_id)
    return {"jobId": job_id}


@router.post("/retry/{job_id}")
async def retry_job(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "error":
        raise HTTPException(status_code=400, detail="Job is not in an error state.")

    if job.transcript:
        store.update(job_id, status="formatting", message="Retrying...", docx_path=None, pdf_path=None)
    elif job.audio_path and os.path.exists(job.audio_path):
        store.update(job_id, status="uploading", message="Retrying...")
    else:
        raise HTTPException(status_code=400, detail="Audio file is no longer available; please re-upload.")

    start_pipeline(job_id)
    return {"jobId": job_id}
