"""
Live audio translation route.

Improvements over the original:
  1. Sentence buffering — accumulates transcription results across chunks
     and only translates at natural sentence boundaries (punctuation or
     500ms-equivalent silence detected by the client sending a flush=true param).
     Word-by-word translation produces broken output; sentence-level is accurate.

  2. DeepL-first translation — routes to DeepL for European languages (much
     cheaper than Claude), falls back to Claude for anything else.

  3. Confidence filtering — still skips no_speech and low-quality chunks
     before they reach the translation API.

Session state is stored in a lightweight in-process dict keyed by a
session_id that the client generates (UUID). This is stateless across
server restarts, which is acceptable for live translation sessions.
"""

import os
import re
import subprocess
import tempfile
import threading
from collections import defaultdict
from typing import Optional

import imageio_ffmpeg
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException

from middleware.auth import get_current_user

router = APIRouter()
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# ── Language tables ───────────────────────────────────────────────────────────

LANGUAGE_NAMES = {
    "af": "Afrikaans", "ar": "Arabic", "bg": "Bulgarian", "bn": "Bengali",
    "ca": "Catalan", "cs": "Czech", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian",
    "fa": "Persian", "fi": "Finnish", "fr": "French", "gu": "Gujarati",
    "ha": "Hausa", "he": "Hebrew", "hi": "Hindi", "hr": "Croatian",
    "hu": "Hungarian", "hy": "Armenian", "id": "Indonesian", "it": "Italian",
    "ja": "Japanese", "ka": "Georgian", "ko": "Korean", "lt": "Lithuanian",
    "lv": "Latvian", "ms": "Malay", "nl": "Dutch", "no": "Norwegian",
    "pl": "Polish", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian",
    "sk": "Slovak", "sl": "Slovenian", "sq": "Albanian", "sr": "Serbian",
    "sv": "Swedish", "sw": "Swahili", "ta": "Tamil", "te": "Telugu",
    "th": "Thai", "tl": "Filipino", "tr": "Turkish", "uk": "Ukrainian",
    "ur": "Urdu", "vi": "Vietnamese", "yo": "Yoruba", "zh": "Chinese",
}

# Sentence-ending punctuation — used to detect natural translation boundaries
_SENTENCE_END = re.compile(r'[.!?।۔。！？]\s*$')

# ── Per-session sentence buffer ───────────────────────────────────────────────
# Maps session_id → {"buffer": str, "source_lang": str}
_session_buffers: dict[str, dict] = defaultdict(lambda: {"buffer": "", "source_lang": ""})
_buffer_lock = threading.Lock()


def _get_buffer(session_id: str) -> dict:
    with _buffer_lock:
        return _session_buffers[session_id]


def _update_buffer(session_id: str, text: str, source_lang: str) -> None:
    with _buffer_lock:
        buf = _session_buffers[session_id]
        buf["buffer"] = (buf["buffer"] + " " + text).strip()
        buf["source_lang"] = source_lang


def _flush_buffer(session_id: str) -> tuple[str, str]:
    """Return (buffered_text, source_lang) and clear the buffer."""
    with _buffer_lock:
        buf = _session_buffers[session_id]
        text = buf.get("buffer", "")
        lang = buf.get("source_lang", "")
        buf["buffer"] = ""
        return text, lang


def _clear_session(session_id: str) -> None:
    with _buffer_lock:
        _session_buffers.pop(session_id, None)


# ── Audio helpers ─────────────────────────────────────────────────────────────

def _convert_to_mp3(input_path: str, output_path: str) -> None:
    cmd = [
        FFMPEG, "-y", "-i", input_path,
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        import logging
        logging.getLogger(__name__).warning(
            "ffmpeg failed: %s", result.stderr[-800:] if result.stderr else "no stderr"
        )
        raise HTTPException(status_code=400, detail="Could not process audio — ensure the browser supports WebM/Opus.")


def _transcribe_with_language(mp3_path: str, hint_language: Optional[str] = None) -> dict:
    """Returns {text, language, no_speech_prob}."""
    backend = os.environ.get("WHISPER_BACKEND", "local").lower()
    api_key = os.environ.get("GROQ_API_KEY", "")

    if backend == "groq" and api_key:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
        kwargs = dict(model="whisper-large-v3-turbo", response_format="verbose_json")
        if hint_language:
            kwargs["language"] = hint_language
        with open(mp3_path, "rb") as f:
            response = client.audio.transcriptions.create(file=f, **kwargs)

        no_speech_prob = 0.0
        segs = getattr(response, "segments", None) or []
        if segs:
            vals = []
            for s in segs:
                v = s.get("no_speech_prob") if isinstance(s, dict) else getattr(s, "no_speech_prob", None)
                if v is not None:
                    vals.append(float(v))
            if vals:
                no_speech_prob = sum(vals) / len(vals)

        return {
            "text": (response.text or "").strip(),
            "language": hint_language or response.language or "en",
            "no_speech_prob": no_speech_prob,
        }
    else:
        import whisper
        model_name = os.environ.get("WHISPER_MODEL", "base")
        model = whisper.load_model(model_name)
        opts = {"fp16": False}
        if hint_language:
            opts["language"] = hint_language
        result = model.transcribe(mp3_path, **opts)
        segs = result.get("segments") or []
        vals = [s["no_speech_prob"] for s in segs if "no_speech_prob" in s]
        no_speech_prob = sum(vals) / len(vals) if vals else 0.0
        return {
            "text": (result.get("text") or "").strip(),
            "language": hint_language or result.get("language") or "en",
            "no_speech_prob": no_speech_prob,
        }


# ── Quality filtering ─────────────────────────────────────────────────────────

_HALLUCINATION_PHRASES = {
    "thank you.", "thank you", "thanks for watching.", "thanks for watching",
    "you", "bye.", "bye", "goodbye.", "please subscribe.", "subtitles by",
    "[music]", "(music)", "[applause]", "♪", "...", ". . .", "you.",
    "[unclear]",
}


def _is_meaningful(text: str) -> bool:
    """Return False for silence/noise artifacts."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.lower() in _HALLUCINATION_PHRASES:
        return False

    letters_only = re.sub(r"[^\w]", "", stripped, flags=re.UNICODE)
    if len(letters_only) < 4:
        return False

    words = [w for w in re.split(r"\s+", stripped) if re.search(r"\w{2,}", w, re.UNICODE)]
    if not words:
        return False

    lower_letters = letters_only.lower()
    max_freq = max(lower_letters.count(c) for c in set(lower_letters))
    if max_freq / len(lower_letters) > 0.35:
        return False

    scripts: set[str] = set()
    for ch in stripped:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        if   0x0041 <= cp <= 0x024F: scripts.add("latin")
        elif 0x0400 <= cp <= 0x04FF: scripts.add("cyrillic")
        elif 0x0600 <= cp <= 0x06FF: scripts.add("arabic")
        elif 0x0900 <= cp <= 0x097F: scripts.add("devanagari")
        elif 0x0E00 <= cp <= 0x0E7F: scripts.add("thai")
        elif 0x4E00 <= cp <= 0x9FFF: scripts.add("cjk")
        elif 0x3040 <= cp <= 0x30FF: scripts.add("japanese")
        elif 0xAC00 <= cp <= 0xD7FF: scripts.add("korean")
    if len(scripts) > 1:
        return False

    return True


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/translate/audio")
async def translate_audio(
    audio: UploadFile = File(...),
    target_language: str = Form("en"),
    source_language: str = Form(""),        # empty = auto-detect
    session_id: str = Form(""),             # client UUID for sentence buffering
    flush: str = Form("false"),             # "true" = force translate buffered text now
    current_user=Depends(get_current_user),
):
    """
    Transcribe an audio chunk and translate it to the target language.

    Sentence buffering:
      - Each chunk's transcription is appended to a per-session buffer.
      - Translation only fires when a sentence boundary is detected (ends with
        . ! ? etc.) OR when the client sends flush=true (e.g. on pause/silence).
      - This avoids the garbage-in-garbage-out problem of translating partial words.

    Returns:
      {
        "source_language": "fr",
        "source_text": "Bonjour tout le monde.",     ← full buffered sentence
        "translated_text": "Hello everyone.",
        "backend": "deepl",                           ← which engine was used
        "buffering": false                            ← true when buffer not flushed yet
      }
    """
    if target_language not in LANGUAGE_NAMES:
        raise HTTPException(status_code=400, detail="Unsupported target language.")

    hint = source_language.strip() if source_language and source_language in LANGUAGE_NAMES else None
    force_flush = flush.strip().lower() == "true"
    sid = session_id.strip() or "default"

    empty_response = {
        "source_language": hint or "en",
        "source_text": "",
        "translated_text": "",
        "backend": "none",
        "buffering": True,
    }

    # ── Step 1: transcribe this chunk ────────────────────────────────────────
    raw_bytes = await audio.read()
    if len(raw_bytes) < 500:
        raise HTTPException(status_code=400, detail="Audio chunk too small.")

    with tempfile.TemporaryDirectory() as tmpdir:
        suffix = os.path.splitext(audio.filename or "chunk.webm")[1] or ".webm"
        raw_path = os.path.join(tmpdir, f"input{suffix}")
        mp3_path = os.path.join(tmpdir, "audio.mp3")

        with open(raw_path, "wb") as f:
            f.write(raw_bytes)

        _convert_to_mp3(raw_path, mp3_path)

        result = _transcribe_with_language(mp3_path, hint_language=hint)

    source_text = result["text"]
    detected_lang = result["language"]

    # Discard silence / hallucinations
    if result.get("no_speech_prob", 0.0) > 0.6:
        if force_flush:
            buffered, lang = _flush_buffer(sid)
            if not buffered:
                return empty_response
            source_text, detected_lang = buffered, lang or detected_lang
        else:
            return {**empty_response, "source_language": detected_lang}

    elif not _is_meaningful(source_text):
        if force_flush:
            buffered, lang = _flush_buffer(sid)
            if not buffered:
                return empty_response
            source_text, detected_lang = buffered, lang or detected_lang
        else:
            return {**empty_response, "source_language": detected_lang}

    else:
        # Accumulate in buffer
        _update_buffer(sid, source_text, detected_lang)

        # Decide whether to translate now or wait
        buf = _get_buffer(sid)
        buffered_text = buf["buffer"]
        has_sentence_end = bool(_SENTENCE_END.search(buffered_text))

        if not force_flush and not has_sentence_end:
            # Keep buffering — return buffering=True so UI shows partial text
            return {
                "source_language": detected_lang,
                "source_text": buffered_text,
                "translated_text": "",
                "backend": "none",
                "buffering": True,
            }

        # Flush the buffer for translation
        source_text, detected_lang = _flush_buffer(sid)
        if detected_lang:
            hint = hint or detected_lang

    # ── Step 2: same language? skip translation ───────────────────────────────
    if not source_text.strip():
        return empty_response

    effective_source = hint or detected_lang
    if effective_source == target_language:
        return {
            "source_language": effective_source,
            "source_text": source_text,
            "translated_text": source_text,
            "backend": "passthrough",
            "buffering": False,
        }

    # ── Step 3: translate via DeepL → Claude fallback ────────────────────────
    source_lang_name = LANGUAGE_NAMES.get(effective_source, effective_source.upper())
    target_lang_name = LANGUAGE_NAMES[target_language]

    from services.translation import translate_text
    translated, backend_used = translate_text(
        source_text,
        source_lang=effective_source,
        target_lang=target_language,
        source_lang_name=source_lang_name,
        target_lang_name=target_lang_name,
    )

    return {
        "source_language": effective_source,
        "source_text": source_text,
        "translated_text": translated,
        "backend": backend_used,
        "buffering": False,
    }


@router.post("/translate/flush")
async def flush_session(
    session_id: str = Form(...),
    current_user=Depends(get_current_user),
):
    """
    Clear the sentence buffer for a session (call when the user stops recording).
    Returns any remaining buffered text so the client can display it.
    """
    buffered, lang = _flush_buffer(session_id)
    _clear_session(session_id)
    return {"flushed_text": buffered, "source_language": lang}
