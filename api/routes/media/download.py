from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from models.job import store

router = APIRouter()

MIME_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}

AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".webm": "video/webm",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".opus": "audio/opus",
}
VALID_TEMPLATES = {"standard", "modern", "executive", "minimal"}
OUTPUT_DIR = Path("storage/outputs")


def _serve_file(local_path: str, r2_key: Optional[str], media_type: str, filename: str):
    """
    Serve a file from local disk if present, otherwise redirect to an R2 presigned URL.
    Raises 404 if neither source is available.
    """
    if local_path and Path(local_path).exists():
        return FileResponse(path=local_path, media_type=media_type, filename=filename)

    if r2_key:
        try:
            from services.core.storage_r2 import get_presigned_url, _is_configured
            if _is_configured():
                url = get_presigned_url(r2_key, expires_in=3600)
                return RedirectResponse(url=url, status_code=302)
        except Exception:
            pass

    raise HTTPException(status_code=404, detail="File not found.")


@router.get("/download/{job_id}/audio")
def download_audio(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    local_path = job.audio_path or ""
    r2_key = job.r2_audio_key or ""

    if not local_path and not r2_key:
        raise HTTPException(status_code=404, detail="Recording not available.")

    suffix = Path(local_path).suffix or Path(r2_key).suffix or ".mp4"
    media_type = AUDIO_MIME.get(suffix.lower(), "application/octet-stream")
    filename = f"recording{suffix}"

    return _serve_file(local_path, r2_key, media_type, filename)


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
        r2_key = None
        if not Path(out_path).exists():
            from services.media.document import generate_docx, generate_pdf
            minutes_copy = {**minutes, "template": effective_template}
            try:
                if fmt == "docx":
                    generate_docx(minutes_copy, out_path)
                else:
                    generate_pdf(minutes_copy, out_path)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to generate document: {exc}")

            # Upload variant to R2
            try:
                from services.core.storage_r2 import _is_configured, upload_file, r2_key_for_output
                if _is_configured():
                    r2_key = r2_key_for_output(job_id, fmt, template=effective_template)
                    upload_file(out_path, r2_key, content_type=MIME_TYPES[fmt])
            except Exception:
                pass

        return _serve_file(out_path, r2_key, MIME_TYPES[fmt], filename)

    # Standard template — serve from local or R2
    local_path = job.docx_path if fmt == "docx" else job.pdf_path
    from services.core.storage_r2 import r2_key_for_output
    r2_key = r2_key_for_output(job_id, fmt)
    return _serve_file(local_path or "", r2_key, MIME_TYPES[fmt], filename)
