"""PDF → Audio service.

Flow:
  1. extract_pdf_text()   — pdfplumber parses the PDF bytes
  2. generate_script()    — Claude writes a natural speech script
  3. text_to_speech()     — Deepgram Aura TTS converts script → MP3 bytes
"""

import io
import json
import os
import re
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
    "slide_deck": {
        "label": "Slide Deck Narration",
        "desc": "a presenter narrating slide content as a voiceover for a slide deck or video",
        "tone": "crisp, paced, and slide-aware. Each major point flows as if transitioning between slides. No filler — every sentence earns its place.",
    },
}

MODES = {
    "full": {
        "label": "Full Script",
        "description": "Complete narration covering the entire document",
        "duration_override": None,
        "prompt_instruction": "Cover all major sections of the document thoroughly.",
    },
    "summary": {
        "label": "5-Minute Summary",
        "description": "Focused summary hitting only the key points",
        "duration_override": 5,
        "prompt_instruction": "Summarise the document concisely in 5 minutes. Include: the core problem/topic, 3-5 key points, and the conclusion. Skip supporting details and examples.",
    },
    "brief": {
        "label": "60-Second Brief",
        "description": "Ultra-short elevator pitch of the document",
        "duration_override": 1,
        "prompt_instruction": "Deliver the single most important insight from this document as a 60-second spoken brief. Think: what would you say in an elevator to someone who hasn't read this?",
    },
    "active_recall": {
        "label": "Active Recall",
        "description": "Socratic audio that prompts you to recall key concepts as you listen",
        "duration_override": None,
        "prompt_instruction": (
            "Structure this as an active recall listening session. "
            "For each key concept or section: (1) introduce the concept in 1-2 sentences, "
            "(2) say 'Pause here and try to recall...' followed by a specific question the listener "
            "should mentally answer before continuing, then (3) give the full explanation and answer. "
            "Alternate between teaching and prompting throughout. "
            "This trains the listener to actively retrieve information rather than passively absorb it. "
            "Do NOT include '[pause]' or stage directions — just speak naturally as if prompting a study partner."
        ),
    },
    "deep_dive": {
        "label": "Deep Dive",
        "description": "Expert-level analysis with full context, nuance, and real-world implications",
        "duration_override": None,
        "prompt_instruction": (
            "Go beyond the surface. For each major concept: explain the WHY behind it, "
            "connect it to broader context and real-world implications, surface non-obvious "
            "insights or counterpoints, and draw conclusions the document may not state explicitly. "
            "Use concrete analogies and examples to ground abstract ideas. "
            "Write for a curious, intelligent listener who wants to truly understand, "
            "not just know the summary. Think expert commentary, not narration."
        ),
    },
}

VOICES = {
    # Female voices
    "asteria": "aura-asteria-en",
    "luna":    "aura-luna-en",
    "stella":  "aura-stella-en",
    "athena":  "aura-athena-en",
    # Male voices
    "orion":   "aura-orion-en",
    "arcas":   "aura-arcas-en",
    "helios":  "aura-helios-en",
    "zeus":    "aura-zeus-en",
    # Legacy aliases
    "female":  "aura-asteria-en",
    "male":    "aura-orion-en",
}

VOICE_META = {
    "asteria": {"label": "Asteria", "gender": "female", "style": "Warm & professional"},
    "luna":    {"label": "Luna",    "gender": "female", "style": "Soft & clear"},
    "stella":  {"label": "Stella",  "gender": "female", "style": "Upbeat & friendly"},
    "athena":  {"label": "Athena",  "gender": "female", "style": "Authoritative"},
    "orion":   {"label": "Orion",   "gender": "male",   "style": "Deep & confident"},
    "arcas":   {"label": "Arcas",   "gender": "male",   "style": "Steady & calm"},
    "helios":  {"label": "Helios",  "gender": "male",   "style": "Bright & energetic"},
    "zeus":    {"label": "Zeus",    "gender": "male",   "style": "Bold & commanding"},
}

VALID_VOICES = set(VOICES.keys())
VALID_MODES = set(MODES.keys())


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


def generate_script(text: str, purpose: str, duration_minutes: int, mode: str = "full") -> str:
    import anthropic

    p = PURPOSES.get(purpose) or PURPOSES["project_defense"]
    m = MODES.get(mode) or MODES["full"]

    effective_duration = m["duration_override"] if m["duration_override"] is not None else duration_minutes
    target_words = effective_duration * WORDS_PER_MINUTE
    max_tokens   = min(target_words * 3, 8000)

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
- Length: approximately {target_words} words ({effective_duration} minutes at 130 wpm)
- Tone: {p['tone']}
- Voice: first-person throughout ("I", "my", "we", "our")
- Structure: strong opening hook → logical flow through key points → memorable closing
- Style: sounds like authentic, confident speech — NOT a document being read aloud
- No jargon dumps; explain ideas clearly and succinctly
- Mode instruction: {m['prompt_instruction']}

OUTPUT RULES (strictly follow):
- Output ONLY the spoken words
- No stage directions, [pause], (applause), section headers, or any metadata
- No markdown formatting
- Plain text only — every word will be fed directly to a text-to-speech engine""",
        }],
    )
    return msg.content[0].text.strip()


def generate_quiz(script: str) -> list[dict]:
    """Generate comprehension quiz from a script. Returns list of question objects."""
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": f"""Based on the following presentation script, generate a comprehension quiz.

SCRIPT:
---
{script[:8000]}
---

Generate exactly:
- 5 multiple-choice questions (4 options each, one correct answer)
- 2 short-answer questions

Return ONLY valid JSON in this exact structure (no markdown, no extra text):
{{
  "mcq": [
    {{
      "question": "...",
      "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
      "answer": "A"
    }}
  ],
  "short_answer": [
    {{
      "question": "...",
      "sample_answer": "..."
    }}
  ]
}}""",
        }],
    )
    raw = msg.content[0].text.strip()
    # Strip markdown code fences if present
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    return json.loads(raw)


def generate_flashcards(script: str) -> list[dict]:
    """Generate flashcards from a script. Returns list of {front, back} dicts."""
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": f"""Based on the following presentation script, generate study flashcards.

SCRIPT:
---
{script[:8000]}
---

Generate 12 flashcards covering the key concepts, terms, and ideas from the script.

Return ONLY valid JSON — an array of objects (no markdown, no extra text):
[
  {{"front": "Term or question", "back": "Definition or answer"}},
  ...
]""",
        }],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    return json.loads(raw)


def translate_script(script: str, target_language: str) -> str:
    """Translate a script to target_language using the translation service."""
    from services.media.translation import translate_text
    return translate_text(script, source_lang="EN", target_lang=target_language)


def split_text_into_chunks(text: str, max_chars: int = 1900) -> list[str]:
    """
    Split text into chunks of at most max_chars characters.
    It tries to split on sentence boundaries (., ?, !) first,
    then on spaces (words), and finally on characters if needed.
    """
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return []

    if len(text) <= max_chars:
        return [text]

    sentences = re.split(r'(?<=[.!?])\s+', text)

    chunks = []
    current_chunk = []
    current_length = 0

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        if len(sentence) <= max_chars:
            space_needed = 1 if current_chunk else 0
            if current_length + space_needed + len(sentence) <= max_chars:
                current_chunk.append(sentence)
                current_length += space_needed + len(sentence)
            else:
                if current_chunk:
                    chunks.append(" ".join(current_chunk))
                current_chunk = [sentence]
                current_length = len(sentence)
        else:
            words = sentence.split(' ')
            for word in words:
                word = word.strip()
                if not word:
                    continue

                if len(word) <= max_chars:
                    space_needed = 1 if current_chunk else 0
                    if current_length + space_needed + len(word) <= max_chars:
                        current_chunk.append(word)
                        current_length += space_needed + len(word)
                    else:
                        if current_chunk:
                            chunks.append(" ".join(current_chunk))
                        current_chunk = [word]
                        current_length = len(word)
                else:
                    for i in range(0, len(word), max_chars):
                        part = word[i:i+max_chars]
                        space_needed = 1 if current_chunk else 0
                        if current_length + space_needed + len(part) <= max_chars:
                            current_chunk.append(part)
                            current_length += space_needed + len(part)
                        else:
                            if current_chunk:
                                chunks.append(" ".join(current_chunk))
                            current_chunk = [part]
                            current_length = len(part)

    if current_chunk:
        chunks.append(" ".join(current_chunk))

    return chunks


def text_to_speech(script: str, voice: str = "asteria") -> bytes:
    import urllib.error

    chunks = split_text_into_chunks(script)
    if not chunks:
        return b""

    model = VOICES.get(voice, VOICES["asteria"])
    url   = f"https://api.deepgram.com/v1/speak?model={model}&encoding=mp3"

    audio_chunks = []
    for chunk in chunks:
        data  = json.dumps({"text": chunk}).encode("utf-8")
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
                audio_chunks.append(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Deepgram TTS error {e.code}: {detail}")

    return b"".join(audio_chunks)
