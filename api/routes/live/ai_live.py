"""
Live meeting AI analysis — called by the Chrome extension sidebar during an active meeting.

POST /ai/live/analyze  — action items, summary, topics from current transcript
POST /ai/live/chat     — answer a question about the current transcript
"""

import json
import logging
import os

import anthropic
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from middleware.auth import get_current_user
from models.user import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/ai/live", tags=["ai_live"])

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

_ANALYZE_PROMPT = """\
Analyze this live meeting transcript and return a single JSON object (no markdown fences) with:
- "action_items": array of {{"task": str, "owner": str}} — explicit or implied tasks
- "summary": 2–3 sentence overview of what has been discussed so far
- "topics": array of 3–6 concise topic strings

Keep it factual and brief. If the transcript is too short, return empty arrays/strings.

Transcript:
{transcript}"""

_CHAT_SYSTEM = """\
You are a live meeting assistant embedded in a sidebar. Answer questions about the following \
meeting transcript concisely (2–4 sentences). If the answer is not in the transcript, say so.

Transcript so far:
{transcript}"""


class AnalyzeRequest(BaseModel):
    transcript: str


class ChatRequest(BaseModel):
    transcript: str
    question: str
    history: list[dict] = []


@router.post("/analyze")
def analyze_transcript(body: AnalyzeRequest, _user: User = Depends(get_current_user)):
    transcript = body.transcript.strip()
    if len(transcript) < 80:
        return {"action_items": [], "summary": "", "topics": []}

    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            messages=[{"role": "user", "content": _ANALYZE_PROMPT.format(transcript=transcript[:4000])}],
        )
        text = resp.content[0].text.strip()
        start, end = text.find("{"), text.rfind("}") + 1
        if start == -1:
            raise ValueError("No JSON in response")
        return json.loads(text[start:end])
    except Exception as exc:
        log.warning("Live analyze error: %s", exc)
        return {"action_items": [], "summary": "", "topics": []}


@router.post("/chat")
def chat_with_transcript(body: ChatRequest, _user: User = Depends(get_current_user)):
    transcript = body.transcript.strip()
    if not transcript:
        return {"answer": "No transcript available yet — start speaking and try again."}

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in body.history[-12:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    history.append({"role": "user", "content": body.question})

    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=_CHAT_SYSTEM.format(transcript=transcript[:3000]),
            messages=history,
        )
        return {"answer": resp.content[0].text}
    except Exception as exc:
        log.warning("Live chat error: %s", exc)
        return {"answer": "Sorry, something went wrong. Please try again."}
