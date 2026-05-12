import os
import re
import subprocess
import tempfile
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
import imageio_ffmpeg

from api.deps import get_current_user

router = APIRouter()
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

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


def _convert_to_mp3(input_path: str, output_path: str) -> None:
    cmd = [
        FFMPEG, "-y", "-i", input_path,
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail="Could not process audio — ensure the browser supports WebM/Opus.")


def _transcribe_with_language(mp3_path: str, hint_language: str | None = None) -> dict:
    """Returns {text, language, no_speech_prob}.
    no_speech_prob is Whisper's own estimate that the clip contains no real speech (0–1).
    """
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

        # Average no_speech_prob across all segments (Groq may or may not return this)
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


# Whisper commonly hallucinates these exact strings on silence
_HALLUCINATION_PHRASES = {
    "thank you.", "thank you", "thanks for watching.", "thanks for watching",
    "you", "bye.", "bye", "goodbye.", "please subscribe.", "subtitles by",
    "[music]", "(music)", "[applause]", "♪", "...", ". . .", "you.",
}


def _is_meaningful(text: str) -> bool:
    """Return False for silence/noise artifacts.

    Catches:
    - Empty / punctuation-only output
    - Whisper hallucination phrases
    - Repeated-character garbage  (e.g. "wuqqqqqqqq")
    - Mixed Unicode scripts       (e.g. Latin + CJK from encoding corruption)
    """
    stripped = text.strip()
    if not stripped:
        return False

    # Known verbatim hallucinations
    if stripped.lower() in _HALLUCINATION_PHRASES:
        return False

    # Must contain at least 4 real word characters
    letters_only = re.sub(r"[^\w]", "", stripped, flags=re.UNICODE)
    if len(letters_only) < 4:
        return False

    # Must have at least one word of 2+ characters
    words = [w for w in re.split(r"\s+", stripped) if re.search(r"\w{2,}", w, re.UNICODE)]
    if not words:
        return False

    # Repeated-character hallucination: any single char > 35 % of all letters
    lower_letters = letters_only.lower()
    max_freq = max(lower_letters.count(c) for c in set(lower_letters))
    if max_freq / len(lower_letters) > 0.35:
        return False

    # Mixed-script hallucination: classify each alpha char by Unicode block
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


def _translate_with_claude(text: str, source_lang_name: str, target_lang_name: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Translate the following text from {source_lang_name} to {target_lang_name}. "
                f"Return only the translation, nothing else.\n\n{text}"
            ),
        }],
    )
    return msg.content[0].text.strip()


@router.post("/translate/audio")
async def translate_audio(
    audio: UploadFile = File(...),
    target_language: str = Form("en"),
    source_language: str = Form(""),   # empty string = auto-detect
    current_user=Depends(get_current_user),
):
    if target_language not in LANGUAGE_NAMES:
        raise HTTPException(status_code=400, detail="Unsupported target language.")
    hint = source_language.strip() if source_language and source_language in LANGUAGE_NAMES else None

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
        source_language = result["language"]

        # Whisper's own confidence that there was no real speech — discard if too high
        if result.get("no_speech_prob", 0.0) > 0.6:
            return {"source_language": source_language, "source_text": "", "translated_text": ""}

        if not _is_meaningful(source_text):
            return {"source_language": source_language, "source_text": "", "translated_text": ""}

        if source_language == target_language:
            return {
                "source_language": source_language,
                "source_text": source_text,
                "translated_text": source_text,
            }

        source_lang_name = LANGUAGE_NAMES.get(source_language, source_language.title())
        target_lang_name = LANGUAGE_NAMES[target_language]
        translated = _translate_with_claude(source_text, source_lang_name, target_lang_name)

        return {
            "source_language": source_language,
            "source_text": source_text,
            "translated_text": translated,
        }
