import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
CHUNK_SECONDS = 90
# Groq free tier: 20 RPM. Spacing chunks 3.5s apart keeps us at ~17 RPM.
# 90s chunks = 30 requests for a 45-min file (~5 min total transcription).
GROQ_INTER_CHUNK_DELAY = 3.5

# Set WHISPER_BACKEND=groq in production for fast, free cloud transcription.
# Leave unset (or set to "local") for local Whisper during development.
BACKEND = os.environ.get("WHISPER_BACKEND", "local").lower()


# ── Local Whisper (dev) ──────────────────────────────────────────────────────

_local_model = None

def _get_local_model():
    global _local_model
    if _local_model is None:
        import whisper
        model_name = os.environ.get("WHISPER_MODEL", "turbo")
        _local_model = whisper.load_model(model_name)
    return _local_model

def _transcribe_chunk_local(chunk_path: str) -> str:
    model = _get_local_model()
    result = model.transcribe(chunk_path, fp16=False)
    return result["text"].strip()


# ── Groq Whisper API (production) ────────────────────────────────────────────

_groq_client = None

def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from openai import OpenAI
        _groq_client = OpenAI(
            api_key=os.environ["GROQ_API_KEY"],
            base_url="https://api.groq.com/openai/v1",
        )
    return _groq_client

def _transcribe_chunk_groq(chunk_path: str) -> str:
    client = _get_groq_client()
    for attempt in range(6):
        try:
            with open(chunk_path, "rb") as f:
                response = client.audio.transcriptions.create(
                    model="whisper-large-v3-turbo",
                    file=f,
                    response_format="text",
                )
            return response.strip() if isinstance(response, str) else response.text.strip()
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate_limit" in msg.lower():
                wait = 5 * (attempt + 1)
                print(f"Groq rate limit — waiting {wait}s before retry {attempt + 1}/5")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Groq rate limit: exceeded max retries")


# ── Shared: ffmpeg chunking ───────────────────────────────────────────────────

def _extract_audio_to_mp3(media_path: str, output_path: str) -> None:
    cmd = [
        FFMPEG, "-y",
        "-i", media_path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "32k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "")[-700:]
        if "Output file does not contain any stream" in stderr or "Stream map 'a" in stderr:
            raise RuntimeError(
                "This recording does not contain an audio stream. "
                "The meeting bot capture was video-only, so MinuteAI could not transcribe it."
            )
        raise RuntimeError(f"ffmpeg audio extraction failed: {stderr}")


def _split_audio(audio_path: str, chunk_dir: str) -> list[str]:
    out_pattern = os.path.join(chunk_dir, "chunk_%04d.mp3")
    cmd = [
        FFMPEG, "-y",
        "-i", audio_path,
        "-f", "segment",
        "-segment_time", str(CHUNK_SECONDS),
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "32k",
        "-vn",
        out_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-500:]}")
    chunks = sorted(Path(chunk_dir).glob("chunk_*.mp3"))
    if not chunks:
        raise RuntimeError("ffmpeg produced no output chunks.")
    return [str(c) for c in chunks]


def _transcribe_chunk(chunk_path: str) -> str:
    if BACKEND == "groq":
        return _transcribe_chunk_groq(chunk_path)
    return _transcribe_chunk_local(chunk_path)


# ── Public API ────────────────────────────────────────────────────────────────

def transcribe(
    audio_path: str,
    on_partial: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Transcribe audio in 30-second chunks.
    Calls on_partial(transcript_so_far) after each chunk for live UI updates.
    Backend is controlled by WHISPER_BACKEND env var: 'local' (default) or 'groq'.
    """
    with tempfile.TemporaryDirectory() as chunk_dir:
        normalized_audio_path = os.path.join(chunk_dir, "normalized_input.mp3")
        _extract_audio_to_mp3(audio_path, normalized_audio_path)
        chunks = _split_audio(normalized_audio_path, chunk_dir)
        full_text = ""
        for i, chunk_path in enumerate(chunks):
            if i > 0 and BACKEND == "groq":
                time.sleep(GROQ_INTER_CHUNK_DELAY)
            chunk_text = _transcribe_chunk(chunk_path)
            if chunk_text:
                full_text = (full_text + " " + chunk_text).strip()
            if on_partial and full_text:
                on_partial(full_text)
        return full_text
