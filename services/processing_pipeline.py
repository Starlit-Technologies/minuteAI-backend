import os
import threading
import uuid
from pathlib import Path
from typing import Optional

from models.job import Job, store
from services.document import generate_docx, generate_pdf
from services.email import send_error_email, send_minutes_ready
from services.formatting import format_minutes
from services.notifications import create_notification
from services.transcription import transcribe

UPLOAD_DIR = Path(__file__).parent.parent / "storage" / "uploads"
OUTPUT_DIR = Path(__file__).parent.parent / "storage" / "outputs"


def create_processing_job_from_media(
    *,
    media_path: str,
    original_filename: str,
    email: Optional[str] = None,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> str:
    job_id = str(uuid.uuid4())
    suffix = Path(original_filename).suffix.lower() or Path(media_path).suffix.lower() or ".mp4"
    final_media_path = str(UPLOAD_DIR / f"{job_id}{suffix}")

    if os.path.abspath(media_path) != os.path.abspath(final_media_path):
        os.replace(media_path, final_media_path)
    else:
        final_media_path = media_path

    store.create(Job(
        id=job_id,
        status="uploading",
        email=email,
        user_id=user_id,
        workspace_id=workspace_id,
        audio_path=final_media_path,
    ))
    start_pipeline(job_id)
    return job_id


def start_pipeline(job_id: str):
    worker = threading.Thread(target=run_pipeline, args=(job_id,), daemon=True)
    worker.start()


def run_pipeline(job_id: str):
    job = store.get(job_id)
    try:
        transcript = job.transcript
        if not transcript:
            audio_path = job.audio_path
            if not audio_path or not os.path.exists(audio_path):
                raise RuntimeError("Audio file not found — please re-upload.")

            store.update(job_id, status="transcribing", message="Transcribing audio with Whisper…")

            def on_partial(text: str):
                store.update(job_id, transcript=text)

            transcript = transcribe(audio_path, on_partial=on_partial)
            if not transcript or not transcript.strip():
                raise RuntimeError("Transcription returned no text. The audio may be silent or unreadable.")
            store.update(job_id, transcript=transcript)

            try:
                os.unlink(audio_path)
            except OSError:
                pass
            store.update(job_id, audio_path=None)

        minutes = job.minutes
        if not minutes:
            store.update(job_id, status="formatting", message="Formatting minutes with Claude…")
            minutes = format_minutes(transcript)
            store.update(job_id, minutes=minutes)

        docx_path = job.docx_path
        pdf_path = job.pdf_path
        if not (docx_path and pdf_path and os.path.exists(docx_path) and os.path.exists(pdf_path)):
            store.update(job_id, status="generating", message="Generating .docx and PDF…")
            docx_path = str(OUTPUT_DIR / f"{job_id}.docx")
            pdf_path = str(OUTPUT_DIR / f"{job_id}.pdf")
            generate_docx(minutes, docx_path)
            generate_pdf(minutes, pdf_path)

        store.update(job_id, status="done", docx_path=docx_path, pdf_path=pdf_path, message=None)

        try:
            from services.task_assignment import auto_assign_tasks
            auto_assign_tasks(job_id)
        except Exception:
            pass

        meeting_title = minutes.get("title") or "Your meeting"
        if job.user_id:
            create_notification(
                user_id=job.user_id,
                type="meeting_done",
                title=f"Meeting ready: {meeting_title}",
                body="Your minutes and action items have been generated.",
                link=f"/results/{job_id}",
            )

        if job.email:
            send_minutes_ready(job.email, job_id, minutes.get("title", "Meeting"))

    except Exception as exc:
        store.update(job_id, status="error", message=str(exc))
        if job.email:
            try:
                send_error_email(job.email, job_id, str(exc))
            except Exception:
                pass
