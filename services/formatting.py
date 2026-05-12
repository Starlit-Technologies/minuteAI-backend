import os
import json
import time
from pathlib import Path
import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "minutes_formatter.txt").read_text()

# Tool schema — Claude fills this in, giving us validated structured output
MINUTES_TOOL = {
    "name": "produce_minutes",
    "description": "Output the structured meeting minutes extracted from the transcript.",
    "input_schema": {
        "type": "object",
        "required": ["title", "metadata", "sections", "action_items", "plain_text"],
        "properties": {
            "title": {
                "type": "string",
                "description": "A concise title for the meeting, e.g. 'Q2 Planning Meeting'"
            },
            "metadata": {
                "type": "object",
                "required": ["date", "duration", "participants"],
                "properties": {
                    "date": {"type": "string", "description": "Date of the meeting (infer from transcript or use 'Unknown')"},
                    "duration": {"type": "string", "description": "Approximate duration, e.g. '45 minutes'"},
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names or roles of all identified speakers"
                    }
                }
            },
            "sections": {
                "type": "array",
                "description": "Agenda sections / topics discussed",
                "items": {
                    "type": "object",
                    "required": ["title", "content"],
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string", "description": "2-4 sentence prose summary of discussion"}
                    }
                }
            },
            "action_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["task", "owner", "deadline"],
                    "properties": {
                        "task": {"type": "string"},
                        "owner": {"type": "string"},
                        "deadline": {"type": "string"}
                    }
                }
            },
            "plain_text": {
                "type": "string",
                "description": "The full minutes as clean plain text, suitable for pasting into an email"
            }
        }
    }
}


_MEETING_TYPE_HINTS = {
    "standup":    "This is a daily standup. Focus on: what was completed, what's next, and any blockers.",
    "client":     "This is a client-facing call. Emphasise commitments, agreements, and next steps. Use a professional tone.",
    "interview":  "This is a job interview. Note the candidate's responses and the interviewers' assessments.",
    "workshop":   "This is a workshop or training session. Highlight learning objectives, exercises, and key takeaways.",
    "lecture":    "This is a lecture or webinar. Focus on key concepts presented and any Q&A highlights.",
    "brainstorm": "This is a brainstorming session. Capture all ideas mentioned, even brief ones.",
    "one_on_one": "This is a one-on-one meeting. Focus on personal goals, feedback, and agreed actions.",
}

_OUTPUT_LANGUAGE_NAMES = {
    "en": "English", "fr": "French", "es": "Spanish", "de": "German",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish",
    "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
    "tr": "Turkish", "ro": "Romanian", "hu": "Hungarian", "cs": "Czech",
    "sk": "Slovak", "uk": "Ukrainian", "ru": "Russian", "ar": "Arabic",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "hi": "Hindi",
    "sw": "Swahili", "yo": "Yoruba", "ha": "Hausa", "id": "Indonesian",
    "vi": "Vietnamese", "th": "Thai",
}

_TEMPLATE_HINTS = {
    "executive": (
        "Format for an executive audience: open with a 2–3 sentence executive summary, "
        "keep each section to 2 sentences maximum, and ensure every action item has a clear owner."
    ),
    "minimal": (
        "Be extremely concise. Limit each section to 1–2 sentences. "
        "Include only the most critical information."
    ),
    "modern": "Use active, direct language throughout. Avoid passive voice.",
}


def _build_user_message(transcript: str, config: dict | None) -> str:
    if not config:
        return f"Here is the meeting transcript:\n\n{transcript}"

    lines = []
    title = (config.get("meeting_title") or "").strip()
    if title:
        lines.append(f"Meeting title: {title}")

    mtype = (config.get("meeting_type") or "general").strip()
    hint = _MEETING_TYPE_HINTS.get(mtype)
    if hint:
        lines.append(f"Meeting context: {hint}")

    attendees = (config.get("attendees") or "").strip()
    if attendees:
        lines.append(f"Known attendees: {attendees}")

    custom = (config.get("custom_instructions") or "").strip()
    if custom:
        lines.append(f"Additional instructions: {custom}")

    template = (config.get("template") or "standard").strip()
    t_hint = _TEMPLATE_HINTS.get(template)
    if t_hint:
        lines.append(t_hint)

    lang_code = (config.get("output_language") or "en").strip()
    lang_name = _OUTPUT_LANGUAGE_NAMES.get(lang_code, "English")
    if lang_code != "en":
        lines.append(
            f"IMPORTANT: Write ALL output — title, section titles, section content, "
            f"action item text, and plain_text — entirely in {lang_name}. "
            f"Do not use English anywhere in your output."
        )

    prefix = "\n".join(lines)
    return f"{prefix}\n\nHere is the meeting transcript:\n\n{transcript}"


def format_minutes(transcript: str, config: dict | None = None) -> dict:
    """Send the transcript to Claude and return structured minutes as a dict."""
    last_err = None
    for attempt in range(5):
        try:
            return _call_claude(transcript, config)
        except Exception as e:
            msg = str(e)
            if "529" in msg or "overloaded" in msg.lower() or "529" in repr(e):
                wait = 10 * (attempt + 1)
                print(f"Anthropic overloaded — waiting {wait}s before retry {attempt + 1}/5")
                time.sleep(wait)
                last_err = e
            else:
                raise
    raise last_err


def _call_claude(transcript: str, config: dict | None = None) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        tools=[MINUTES_TOOL],
        tool_choice={"type": "tool", "name": "produce_minutes"},
        messages=[
            {
                "role": "user",
                "content": _build_user_message(transcript, config),
            }
        ]
    )

    # Extract the tool use block
    for block in response.content:
        if block.type == "tool_use" and block.name == "produce_minutes":
            data = block.input
            if isinstance(data, str):
                data = json.loads(data)

            # Claude occasionally returns nested objects as JSON strings — parse them
            metadata = data.get("metadata", {})
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            sections = data.get("sections", [])
            sections = [json.loads(s) if isinstance(s, str) else s for s in sections]

            action_items = data.get("action_items", [])
            action_items = [json.loads(a) if isinstance(a, str) else a for a in action_items]

            return {
                "title": data.get("title", "Meeting"),
                "metadata": {
                    "date": metadata.get("date", "Unknown"),
                    "duration": metadata.get("duration", "Unknown"),
                    "participants": metadata.get("participants", []),
                },
                "sections": sections,
                "actionItems": action_items,
                "plainText": data.get("plain_text", ""),
                "template": (config or {}).get("template", "standard"),
                "outputLanguage": (config or {}).get("output_language", "en") or "en",
            }

    raise RuntimeError("Claude did not return a produce_minutes tool call")
