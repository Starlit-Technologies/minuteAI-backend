from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from models.job import store

router = APIRouter()

MIME_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


@router.get("/download/{job_id}/{fmt}")
def download_file(job_id: str, fmt: str):
    if fmt not in MIME_TYPES:
        raise HTTPException(status_code=400, detail="Format must be 'docx' or 'pdf'.")

    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "done":
        raise HTTPException(status_code=202, detail="Documents not ready yet.")

    path = job.docx_path if fmt == "docx" else job.pdf_path
    if not path:
        raise HTTPException(status_code=404, detail="File not found.")

    title = (job.minutes or {}).get("title", "meeting-minutes")
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title).strip()
    filename = f"{safe_title}.{fmt}"

    return FileResponse(
        path=path,
        media_type=MIME_TYPES[fmt],
        filename=filename,
    )
