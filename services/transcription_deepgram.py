"""
Deepgram live streaming transcription service.

Provides a streaming transcription session that wraps Deepgram's WebSocket
API. Designed to be used from the /ws/live-translate FastAPI endpoint.

Key features vs Groq Whisper batch:
  - True streaming: words appear as they are spoken
  - Per-word confidence scores to gate translation
  - Interim results (partial) + final results (stable)
  - Automatic endpointing: detects sentence boundaries

Required env var:
    DEEPGRAM_API_KEY

Language codes: Deepgram uses BCP-47 codes (e.g. "en-US", "fr", "es").
We normalise from the app's 2-letter codes.
"""

import os
import asyncio
import json
import logging
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)

# Mapping from the app's 2-letter ISO codes to Deepgram BCP-47 codes
_LANG_MAP = {
    "en": "en",  "fr": "fr",  "es": "es",  "de": "de",  "it": "it",
    "pt": "pt",  "nl": "nl",  "pl": "pl",  "ru": "ru",  "uk": "uk",
    "ar": "ar",  "hi": "hi",  "zh": "zh",  "ja": "ja",  "ko": "ko",
    "sv": "sv",  "da": "da",  "no": "no",  "fi": "fi",  "tr": "tr",
    "cs": "cs",  "ro": "ro",  "hu": "hu",  "sk": "sk",  "bg": "bg",
    "el": "el",  "id": "id",  "vi": "vi",  "th": "th",  "ms": "ms",
}

# Words with confidence below this threshold are flagged as low-quality
CONFIDENCE_THRESHOLD = 0.75


def get_deepgram_lang(iso_code: str) -> str:
    """Convert app language code to Deepgram language code."""
    return _LANG_MAP.get(iso_code.lower(), iso_code)


def is_configured() -> bool:
    return bool(os.environ.get("DEEPGRAM_API_KEY", "").strip())


def _build_url(language: Optional[str]) -> str:
    """Build the Deepgram streaming WebSocket URL with query params."""
    api_key = os.environ["DEEPGRAM_API_KEY"]
    lang = get_deepgram_lang(language or "en") if language else "en"

    params = [
        f"model=nova-2",            # Best accuracy model
        f"language={lang}",
        "smart_format=true",        # Adds punctuation + capitalization automatically
        "diarize=false",            # We don't need diarization for live (AssemblyAI handles post-meeting)
        "interim_results=true",     # Stream partial words while speaking
        "endpointing=500",          # Detect sentence end after 500ms silence
        "utterance_end_ms=1000",    # Fire utterance_end event after 1s silence
        "encoding=linear16",        # PCM 16-bit audio from Expo Audio
        "sample_rate=16000",
        "channels=1",
    ]
    query = "&".join(params)
    return f"wss://api.deepgram.com/v1/listen?{query}", api_key


class DeepgramWord:
    """A single transcribed word with timing and confidence."""
    __slots__ = ("word", "start", "end", "confidence", "punctuated_word")

    def __init__(self, raw: dict):
        self.word = raw.get("word", "")
        self.punctuated_word = raw.get("punctuated_word", self.word)
        self.start = raw.get("start", 0.0)
        self.end = raw.get("end", 0.0)
        self.confidence = raw.get("confidence", 1.0)

    @property
    def is_reliable(self) -> bool:
        return self.confidence >= CONFIDENCE_THRESHOLD


class DeepgramTranscript:
    """Parsed result from a Deepgram response message."""
    __slots__ = ("text", "words", "is_final", "speech_final", "confidence")

    def __init__(self, text: str, words: list[DeepgramWord], is_final: bool, speech_final: bool):
        self.text = text
        self.words = words
        self.is_final = is_final
        self.speech_final = speech_final
        self.confidence = (
            sum(w.confidence for w in words) / len(words)
            if words else 0.0
        )

    @property
    def reliable_text(self) -> str:
        """Text with low-confidence words replaced by [unclear]."""
        if not self.words:
            return self.text
        parts = []
        for w in self.words:
            parts.append(w.punctuated_word if w.is_reliable else "[unclear]")
        return " ".join(parts)

    @property
    def has_unclear_words(self) -> bool:
        return any(not w.is_reliable for w in self.words)


def parse_deepgram_message(raw_message: str) -> Optional[DeepgramTranscript]:
    """
    Parse a raw Deepgram WebSocket message into a DeepgramTranscript.
    Returns None for keepalive / metadata / error messages.
    """
    try:
        data = json.loads(raw_message)
    except json.JSONDecodeError:
        return None

    msg_type = data.get("type", "")

    if msg_type == "Results":
        channel = data.get("channel", {})
        alternatives = channel.get("alternatives", [])
        if not alternatives:
            return None

        alt = alternatives[0]
        text = alt.get("transcript", "").strip()
        words = [DeepgramWord(w) for w in alt.get("words", [])]
        is_final = data.get("is_final", False)
        speech_final = data.get("speech_final", False)

        if not text:
            return None

        return DeepgramTranscript(
            text=text,
            words=words,
            is_final=is_final,
            speech_final=speech_final,
        )

    if msg_type == "UtteranceEnd":
        # Signal that the speaker has paused — caller should flush the buffer
        return DeepgramTranscript(
            text="",
            words=[],
            is_final=True,
            speech_final=True,
        )

    return None  # SpeechStarted, Metadata, error, etc.
