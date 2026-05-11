from fastapi import APIRouter, HTTPException
from models.job import store

router = APIRouter()


@router.get("/status/{job_id}")
def get_status(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        "status": job.status,
        "message": job.message,
        "transcript": job.transcript,   # None until transcription completes
    }
