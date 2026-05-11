from fastapi import APIRouter, HTTPException
from models.job import store

router = APIRouter()


@router.get("/results/{job_id}")
def get_results(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status == "error":
        raise HTTPException(status_code=500, detail=job.message or "Processing failed.")
    if job.status != "done":
        raise HTTPException(status_code=202, detail="Processing not complete yet.")
    return job.minutes
