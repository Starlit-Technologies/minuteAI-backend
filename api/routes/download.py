from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from models.job import store

router = APIRouter()

MIME_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}
VALID_TEMPLATES = {"standard", "modern", "executive", "minimal"}
OUTPUT_DIR = Path("storage/outputs")


@router.get("/download/{job_id}/{fmt}")
def download_file(job_id: str, fmt: str, template: Optional[str] = Query(None)):
    if fmt not in MIME_TYPES:
        raise HTTPException(status_code=400, detail="Format must be 'docx' or 'pdf'.")

    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "done":
        raise HTTPException(status_code=202, detail="Documents not ready yet.")

    minutes = job.minutes or {}
    title = minutes.get("title", "meeting-minutes")
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title).strip()
    filename = f"{safe_title}.{fmt}"

    # If a different template was requested, regenerate with that palette
    effective_template = template.lower() if template and template.lower() in VALID_TEMPLATES else None
    original_template = (minutes.get("template") or "standard").lower()

    if effective_template and effective_template != original_template:
        out_path = str(OUTPUT_DIR / f"{job_id}_{effective_template}.{fmt}")
        if not Path(out_path).exists():
            from services.document import generate_docx, generate_pdf
            minutes_copy = {**minutes, "template": effective_template}
            try:
                if fmt == "docx":
                    generate_docx(minutes_copy, out_path)
                else:
                    generate_pdf(minutes_copy, out_path)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to generate document: {exc}")
        return FileResponse(path=out_path, media_type=MIME_TYPES[fmt], filename=filename)

    path = job.docx_path if fmt == "docx" else job.pdf_path
    if not path:
        raise HTTPException(status_code=404, detail="File not found.")

    return FileResponse(path=path, media_type=MIME_TYPES[fmt], filename=filename)
