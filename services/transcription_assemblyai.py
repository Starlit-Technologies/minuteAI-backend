"""
AssemblyAI transcription service — post-meeting, high-accuracy.

Key advantages over Groq Whisper:
  - Speaker diarization: returns who said what
  - Higher accuracy on multi-speaker audio
  - Built-in confidence scores per word

Required env var:
    ASSEMBLYAI_API_KEY

Optional env var:
    ASSEMBLYAI_SPEAKERS_EXPECTED  – integer hint (improves diarization when known)

The main entry point is `transcribe_with_diarization()`.
It returns a TranscriptResult dataclass with:
  - text:        full plain-text transcript
  - utterances:  list of {speaker, text, start_ms, end_ms, confidence}
  - speakers:    sorted list of unique speaker labels (e.g. ["A", "B", "C"])
  - language:    detected language code (e.g. "en")
  - audio_duration_s: audio length in seconds
"""

import os
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Utterance:
    speaker: str        # "A", "B", "C" … (AssemblyAI labels)
    text: str
    start_ms: int
    end_ms: int
    confidence: float


@dataclass
class TranscriptResult:
    text: str
    utterances: list[Utterance] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    language: str = "en"
    audio_duration_s: float = 0.0


def _get_client():
    import assemblyai as aai
    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("ASSEMBLYAI_API_KEY is not set.")
    aai.settings.api_key = api_key
    return aai


def _build_config(aai, language: Optional[str]) -> object:
    """Build AssemblyAI TranscriptionConfig with diarization enabled."""
    speakers_expected = os.environ.get("ASSEMBLYAI_SPEAKERS_EXPECTED", "")
    kwargs = dict(
        speaker_labels=True,
        # Language detection: let AssemblyAI auto-detect unless hinted
        language_code=language or None,
        language_detection=not bool(language),
    )
    if speakers_expected and speakers_expected.isdigit():
        kwargs["speakers_expected"] = int(speakers_expected)

    return aai.TranscriptionConfig(**kwargs)


def transcribe_with_diarization(
    audio_source: str,          # local file path OR https:// presigned URL
    language: Optional[str] = None,
    on_partial: Optional[callable] = None,
) -> TranscriptResult:
    """
    Transcribe audio using AssemblyAI with speaker diarization.

    audio_source can be:
      - A local file path  → uploaded to AssemblyAI automatically
      - An https:// URL    → passed directly (ideal for R2 presigned URLs)

    on_partial(text): optional callback called periodically with the transcript
                      so far while polling (mimics the Groq chunked behaviour).
    """
    aai = _get_client()
    config = _build_config(aai, language)
    transcriber = aai.Transcriber(config=config)

    log.info("AssemblyAI: submitting audio (%s)…", audio_source[:80])

    # Submit — SDK handles both local files and URLs transparently
    transcript = transcriber.submit(audio_source)

    # Poll until done, calling on_partial every ~5 s so the UI stays alive
    poll_interval = 5
    last_partial_text = ""
    while True:
        transcript = transcript.wait_for_completion(timeout=poll_interval)

        if transcript.status == aai.TranscriptStatus.completed:
            break
        if transcript.status == aai.TranscriptStatus.error:
            raise RuntimeError(f"AssemblyAI transcription failed: {transcript.error}")

        # Still processing — fire partial with whatever text we have so far
        partial_text = transcript.text or last_partial_text
        if on_partial and partial_text and partial_text != last_partial_text:
            on_partial(partial_text)
            last_partial_text = partial_text

    full_text = transcript.text or ""

    # Build structured utterances from diarization results
    utterances: list[Utterance] = []
    seen_speakers: list[str] = []

    for u in (transcript.utterances or []):
        speaker = f"Speaker {u.speaker}"
        utterances.append(Utterance(
            speaker=speaker,
            text=u.text,
            start_ms=u.start,
            end_ms=u.end,
            confidence=getattr(u, "confidence", 1.0),
        ))
        if speaker not in seen_speakers:
            seen_speakers.append(speaker)

    audio_duration_s = 0.0
    if transcript.audio_duration:
        audio_duration_s = float(transcript.audio_duration)

    detected_lang = getattr(transcript, "language_code", None) or language or "en"

    log.info(
        "AssemblyAI: done. %d speakers, %d utterances, %.0fs audio.",
        len(seen_speakers), len(utterances), audio_duration_s,
    )

    return TranscriptResult(
        text=full_text,
        utterances=utterances,
        speakers=seen_speakers,
        language=detected_lang,
        audio_duration_s=audio_duration_s,
    )


def format_diarized_transcript(result: TranscriptResult) -> str:
    """
    Convert a TranscriptResult into a clean speaker-labeled string
    suitable for passing to Claude as the transcript.

    Example output:
        Speaker A: Good morning everyone, let's get started.
        Speaker B: Thanks for joining. Today's agenda has three items…
    """
    if not result.utterances:
        return result.text  # fall back to plain text if no diarization

    lines = []
    for u in result.utterances:
        lines.append(f"{u.speaker}: {u.text}")
    return "\n".join(lines)
