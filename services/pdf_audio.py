"""PDF → Audio service.

Flow:
  1. extract_pdf_text()   — pdfplumber parses the PDF bytes
  2. generate_script()    — Claude writes a natural speech script
  3. text_to_speech()     — Deepgram Aura TTS converts script → MP3 bytes
"""

import io
import json
import os
import urllib.request

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

WORDS_PER_MINUTE = 130  # natural presentation pace

PURPOSES = {
    "project_defense": {
        "label": "Project / Thesis Defense",
        "desc": "a student defending their final-year project or thesis to a panel of examiners",
        "tone": "confident, structured, and scholarly. Emphasise the problem, methodology, key findings, and conclusions. Show ownership of the work.",
    },
    "class_presentation": {
        "label": "Class Presentation",
        "desc": "a student presenting to classmates and a lecturer",
        "tone": "clear, engaging, and educational. Keep it approachable while demonstrating understanding.",
    },
    "conference_talk": {
        "label": "Academic Conference",
        "desc": "a researcher presenting their work at an academic conference",
        "tone": "formal yet engaging, highlighting novelty and contribution to the field.",
    },
    "study_review": {
        "label": "Study Review / Revision",
        "desc": "a student recording a personal revision summary to listen back to",
        "tone": "conversational and thorough, covering all key concepts as if explaining to yourself.",
    },
    "business_pitch": {
        "label": "Business / Startup Pitch",
        "desc": "an entrepreneur pitching their idea to potential investors",
        "tone": "compelling, energetic, and persuasive. Lead with the problem and the opportunity.",
    },
    "podcast": {
        "label": "Podcast Explainer",
        "desc": "a content creator explaining a topic to a general audience in podcast style",
        "tone": "warm, conversational, and story-driven. Use analogies to make complex ideas accessible.",
    },
}

VOICES = {
    "female": "aura-asteria-en",
    "male":   "aura-orion-en",
}


def extract_pdf_text(file_bytes: bytes) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                parts.append(text.strip())
    full = "\n\n".join(parts)
    if not full.strip():
        raise ValueError(
            "No readable text found in the PDF. "
            "The file may be a scanned image — please use a text-based PDF."
        )
    return full


def generate_script(text: str, purpose: str, duration_minutes: int) -> str:
    import anthropic

    p = PURPOSES.get(purpose) or PURPOSES["project_defense"]
    target_words = duration_minutes * WORDS_PER_MINUTE
    max_tokens   = min(target_words * 3, 8000)

    # Truncate source text to avoid hitting token limits
    source = text[:16_000]

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": f"""You are a professional speech-writing coach helping {p['desc']}.

SOURCE DOCUMENT:
---
{source}
---

TASK: Write a natural, spoken-word presentation script.

REQUIREMENTS:
- Length: approximately {target_words} words ({duration_minutes} minutes at 130 wpm)
- Tone: {p['tone']}
- Voice: first-person throughout ("I", "my", "we", "our")
- Structure: strong opening hook → logical flow through key points → memorable closing
- Style: sounds like authentic, confident speech — NOT a document being read aloud
- No jargon dumps; explain ideas clearly and succinctly

OUTPUT RULES (strictly follow):
- Output ONLY the spoken words
- No stage directions, [pause], (applause), section headers, or any metadata
- No markdown formatting
- Plain text only — every word will be fed directly to a text-to-speech engine""",
        }],
    )
    return msg.content[0].text.strip()


def text_to_speech(script: str, voice: str = "female") -> bytes:
    import urllib.error

    model = VOICES.get(voice, VOICES["female"])
    url   = f"https://api.deepgram.com/v1/speak?model={model}&encoding=mp3"
    data  = json.dumps({"text": script}).encode("utf-8")
    req   = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Deepgram TTS error {e.code}: {detail}")
