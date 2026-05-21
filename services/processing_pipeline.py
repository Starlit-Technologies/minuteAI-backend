import os
import threading
import uuid
from pathlib import Path
from typing import Optional

from models.job import Job, store
from db.base import SessionLocal
from models.user import User
from services.document import generate_docx, generate_pdf
from services.email import send_error_email, send_minutes_ready
from services.formatting import format_minutes
from services.notifications import create_notification
from services.transcription import transcribe

UPLOAD_DIR = Path(__file__).parent.parent / "storage" / "uploads"
OUTPUT_DIR = Path(__file__).parent.parent / "storage" / "outputs"


def _send_expo_push(token: str, title: str, body: str, data: dict = None):
    import json as _json, urllib.request as _req
    payload = _json.dumps({
        "to": token, "title": title, "body": body,
        "data": data or {}, "sound": "default",
    }).encode()
    r = _req.Request(
        "https://exp.host/--/api/v2/push/send",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with _req.urlopen(r, timeout=10):
        pass

# ── Backend selection ─────────────────────────────────────────────────────────
# TRANSCRIPTION_BACKEND=assemblyai  → diarization, speaker labels, higher accuracy
# TRANSCRIPTION_BACKEND=groq        → fast, no diarization (default)
TRANSCRIPTION_BACKEND = os.environ.get("TRANSCRIPTION_BACKEND", "groq").lower()


def create_processing_job_from_media(
    *,
    media_path: str,
    original_filename: str,
    email: Optional[str] = None,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    attendees: Optional[list[str]] = None,
    meeting_title: Optional[str] = None,
) -> str:
    job_id = str(uuid.uuid4())
    suffix = Path(original_filename).suffix.lower() or Path(media_path).suffix.lower() or ".mp4"
    final_media_path = str(UPLOAD_DIR / f"{job_id}{suffix}")

    if os.path.abspath(media_path) != os.path.abspath(final_media_path):
        os.replace(media_path, final_media_path)
    else:
        final_media_path = media_path

    config: dict = {}
    if attendees:
        config["attendees"] = attendees
    if meeting_title:
        config["meeting_title"] = meeting_title
    if user_id:
        try:
            with SessionLocal() as db:
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    if user.role:
                        config["user_role"] = user.role
                    if user.title:
                        config["user_title"] = user.title
                    if user.language:
                        config["output_language"] = user.language
        except Exception:
            pass

    store.create(Job(
        id=job_id,
        status="uploading",
        email=email,
        user_id=user_id,
        workspace_id=workspace_id,
        audio_path=final_media_path,
        job_config=config if config else None,
    ))

    # Also upload to R2 if configured
    try:
        from services.storage_r2 import _is_configured, upload_file, r2_key_for_audio
        if _is_configured():
            r2_key = r2_key_for_audio(job_id, suffix)
            upload_file(final_media_path, r2_key)
            store.update(job_id, r2_audio_key=r2_key)
    except Exception:
        pass

    start_pipeline(job_id)
    return job_id


def start_pipeline(job_id: str):
    worker = threading.Thread(target=run_pipeline, args=(job_id,), daemon=True)
    worker.start()


def _resolve_audio(job: Job) -> tuple[str, bool]:
    """
    Return (local_audio_path, is_temp).

    If the file exists locally, return it directly (is_temp=False).
    If an R2 key is stored and the local file is gone, download from R2
    to a temp file (is_temp=True — caller must delete after use).
    """
    local = job.audio_path
    if local and os.path.exists(local):
        return local, False

    r2_key = job.r2_audio_key
    if r2_key:
        try:
            from services.storage_r2 import download_to_temp
            suffix = Path(r2_key).suffix or ".mp3"
            tmp_path = download_to_temp(r2_key, suffix=suffix)
            return tmp_path, True
        except Exception as exc:
            raise RuntimeError(f"Audio file not found locally and R2 download failed: {exc}")

    raise RuntimeError("Audio file not found — please re-upload.")


def _transcribe_groq(job: Job, on_partial) -> str:
    """Transcribe using Groq Whisper (fast, no diarization)."""
    audio_path, is_temp = _resolve_audio(job)
    try:
        lang = (job.job_config or {}).get("audio_language") or None
        return transcribe(audio_path, on_partial=on_partial, language=lang or None), is_temp, audio_path
    except Exception:
        if is_temp:
            try:
                os.unlink(audio_path)
            except OSError:
                pass
        raise


def _transcribe_assemblyai(job: Job, on_partial) -> tuple[str, bool, str]:
    """
    Transcribe using AssemblyAI with speaker diarization.

    Prefers to pass an R2 presigned URL directly to AssemblyAI
    (avoids double-downloading large files). Falls back to local file.

    Returns (diarized_transcript_text, is_temp, audio_path_or_empty).
    """
    from services.transcription_assemblyai import transcribe_with_diarization, format_diarized_transcript

    lang = (job.job_config or {}).get("audio_language") or None
    audio_source = None
    is_temp = False
    local_path = ""

    # Prefer R2 presigned URL — AssemblyAI can download it directly
    if job.r2_audio_key:
        try:
            from services.storage_r2 import get_presigned_url, _is_configured
            if _is_configured():
                # Give AssemblyAI 4 hours — large files can take time
                audio_source = get_presigned_url(job.r2_audio_key, expires_in=14400)
        except Exception:
            pass

    # Fall back to local/downloaded file
    if not audio_source:
        local_path, is_temp = _resolve_audio(job)
        audio_source = local_path

    result = transcribe_with_diarization(audio_source, language=lang, on_partial=on_partial)

    # Store audio duration in job config for Claude context
    if result.audio_duration_s:
        config = dict(job.job_config or {})
        mins = int(result.audio_duration_s // 60)
        secs = int(result.audio_duration_s % 60)
        config["_audio_duration"] = f"{mins}m {secs}s"
        store.update(job.id, job_config=config)

    diarized_text = format_diarized_transcript(result)
    return diarized_text, is_temp, local_path


def run_pipeline(job_id: str):
    job = store.get(job_id)
    _tmp_audio: Optional[str] = None
    try:
        transcript = job.transcript
        if not transcript:
            lang = (job.job_config or {}).get("audio_language") or None
            # AssemblyAI does not support Nigerian/African languages — route those through Whisper
            WHISPER_ONLY_LANGS = {"yo", "ig", "ha"}
            backend = "groq" if lang in WHISPER_ONLY_LANGS else TRANSCRIPTION_BACKEND
            backend_label = "AssemblyAI (with speaker diarization)" if backend == "assemblyai" else "Whisper"
            store.update(job_id, status="transcribing",
                         message=f"Transcribing audio with {backend_label}…")

            def on_partial(text: str):
                store.update(job_id, transcript=text)

            if backend == "assemblyai":
                transcript, is_temp, local_path = _transcribe_assemblyai(job, on_partial)
                if is_temp and local_path:
                    _tmp_audio = local_path
            else:
                audio_path, is_temp = _resolve_audio(job)
                if is_temp:
                    _tmp_audio = audio_path
                transcript = transcribe(audio_path, on_partial=on_partial, language=lang or None)

            if not transcript or not transcript.strip():
                raise RuntimeError("Transcription returned no text. The audio may be silent or unreadable.")
            store.update(job_id, transcript=transcript)

            # Clean up temp download (R2 is the permanent record — do NOT delete it)
            if is_temp and _tmp_audio:
                try:
                    os.unlink(_tmp_audio)
                except OSError:
                    pass
                _tmp_audio = None

            # Remove local copy only if R2 has a permanent copy
            if job.r2_audio_key and job.audio_path and os.path.exists(job.audio_path or ""):
                try:
                    os.unlink(job.audio_path)
                except OSError:
                    pass
                store.update(job_id, audio_path=None)

        minutes = job.minutes
        if not minutes:
            store.update(job_id, status="formatting", message="Formatting minutes with Claude…")
            # Re-fetch job in case config was updated with audio duration
            job = store.get(job_id)
            minutes = format_minutes(transcript, config=job.job_config)
            store.update(job_id, minutes=minutes)

        docx_path = job.docx_path
        pdf_path = job.pdf_path
        if not (docx_path and pdf_path and os.path.exists(docx_path) and os.path.exists(pdf_path)):
            store.update(job_id, status="generating", message="Generating .docx and PDF…")
            docx_path = str(OUTPUT_DIR / f"{job_id}.docx")
            pdf_path = str(OUTPUT_DIR / f"{job_id}.pdf")
            generate_docx(minutes, docx_path)
            generate_pdf(minutes, pdf_path)

            # Upload documents to R2 for persistent access
            try:
                from services.storage_r2 import _is_configured, upload_file, r2_key_for_output
                if _is_configured():
                    upload_file(docx_path, r2_key_for_output(job_id, "docx"),
                                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                    upload_file(pdf_path, r2_key_for_output(job_id, "pdf"),
                                content_type="application/pdf")
            except Exception:
                pass  # local copies still work

        store.update(job_id, status="done", docx_path=docx_path, pdf_path=pdf_path, message=None)

        try:
            from services.task_assignment import auto_assign_tasks
            auto_assign_tasks(job_id)
        except Exception:
            pass

        # Run post-meeting integrations (Slack, Notion, HubSpot, Linear, etc.)
        if job.user_id:
            try:
                from services.integrations.runner import run_integrations
                run_integrations(job.user_id, job_id, minutes)
            except Exception:
                pass

        meeting_title = minutes.get("title") or "Your meeting"

        # Expo push notification (mobile app)
        if job.user_id:
            try:
                from db.base import SessionLocal as _SL
                from models.user import User as _User
                with _SL() as _db:
                    _u = _db.get(_User, job.user_id)
                    if _u and _u.push_token:
                        _send_expo_push(
                            token=_u.push_token,
                            title=f"Minutes ready: {meeting_title}",
                            body="Your meeting minutes have been generated.",
                            data={"jobId": job_id},
                        )
            except Exception:
                pass

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

        # Email all meeting attendees (stored from calendar event)
        cfg          = job.job_config or {}
        attendees    = cfg.get("attendees") or []
        owner_email  = job.email or ""
        owner_name   = None
        if job.user_id:
            try:
                from models.user import User
                from db.base import SessionLocal as _SL
                with _SL() as _db:
                    u = _db.get(User, job.user_id)
                    if u:
                        owner_name  = u.name
                        owner_email = owner_email or u.email
            except Exception:
                pass

        if attendees:
            from services.email import send_attendee_recap
            for addr in attendees:
                if addr.lower() == (owner_email or "").lower():
                    continue  # owner already got the full minutes email
                try:
                    send_attendee_recap(
                        to_email=addr,
                        job_id=job_id,
                        meeting_title=meeting_title,
                        recorded_by_name=owner_name,
                        recorded_by_email=owner_email,
                    )
                except Exception:
                    pass

    except Exception as exc:
        store.update(job_id, status="error", message=str(exc))
        if job.email:
            try:
                send_error_email(job.email, job_id, str(exc))
            except Exception:
                pass
    finally:
        # Safety: clean up any dangling temp download
        if _tmp_audio:
            try:
                os.unlink(_tmp_audio)
            except OSError:
                pass

