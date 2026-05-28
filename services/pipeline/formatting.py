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
    "description": "Output comprehensive, structured meeting minutes extracted from the transcript.",
    "input_schema": {
        "type": "object",
        "required": [
            "title", "overview", "metadata",
            "key_decisions", "action_items", "sections",
            "speaker_summaries", "questions_raised", "next_steps", "plain_text",
        ],
        "properties": {
            "title": {
                "type": "string",
                "description": "Concise meeting title, e.g. 'Q2 Planning Meeting'"
            },
            "overview": {
                "type": "string",
                "description": (
                    "3–5 sentence executive summary: purpose of the meeting, key outcomes, "
                    "main decisions, and immediate next steps. Written for someone who missed the meeting."
                )
            },
            "metadata": {
                "type": "object",
                "required": ["date", "duration", "participants"],
                "properties": {
                    "date": {"type": "string", "description": "Date (infer from transcript or 'Unknown')"},
                    "duration": {"type": "string", "description": "Approximate duration, e.g. '45 minutes'"},
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "All identified speakers or known attendees"
                    }
                }
            },
            "key_decisions": {
                "type": "array",
                "description": "Every concrete decision made, even informal ones",
                "items": {
                    "type": "object",
                    "required": ["decision", "context"],
                    "properties": {
                        "decision": {"type": "string", "description": "The decision made"},
                        "made_by": {"type": "string", "description": "Speaker who made or proposed it"},
                        "context": {"type": "string", "description": "Reasoning or context given in the meeting"}
                    }
                }
            },
            "sections": {
                "type": "array",
                "description": "Logical agenda sections / topics discussed",
                "items": {
                    "type": "object",
                    "required": ["title", "summary", "key_points"],
                    "properties": {
                        "title": {"type": "string"},
                        "summary": {
                            "type": "string",
                            "description": "3–5 sentence dense prose summary of the discussion"
                        },
                        "key_points": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "5–12 specific bullet points capturing what was said, proposed, "
                                "agreed, or questioned. Include speaker attribution in brackets, "
                                "e.g. 'Confirmed the deadline is Friday (Speaker A)'"
                            )
                        }
                    }
                }
            },
            "action_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["task", "owner", "deadline", "priority"],
                    "properties": {
                        "task": {"type": "string"},
                        "owner": {"type": "string"},
                        "deadline": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "Priority inferred from urgency or importance signalled in conversation"
                        }
                    }
                }
            },
            "speaker_summaries": {
                "type": "array",
                "description": "Summary of each speaker's role and contributions",
                "items": {
                    "type": "object",
                    "required": ["speaker", "key_contributions"],
                    "properties": {
                        "speaker": {"type": "string"},
                        "role": {"type": "string", "description": "Inferred role or title based on what they discussed"},
                        "key_contributions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "3–5 bullets: what they reported, proposed, questioned, or committed to"
                        }
                    }
                }
            },
            "questions_raised": {
                "type": "array",
                "description": "Important questions asked during the meeting",
                "items": {
                    "type": "object",
                    "required": ["question", "status"],
                    "properties": {
                        "question": {"type": "string"},
                        "asked_by": {"type": "string"},
                        "answer_summary": {
                            "type": "string",
                            "description": "How it was answered, or 'Not answered in this meeting'"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["answered", "unresolved", "deferred"]
                        }
                    }
                }
            },
            "next_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3–6 high-level follow-up items beyond individual action items"
            },
            "plain_text": {
                "type": "string",
                "description": "Complete minutes as clean plain text ready to paste into email or Confluence"
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

    user_role = (config.get("user_role") or "").strip()
    user_title = (config.get("user_title") or "").strip()
    if user_role or user_title:
        role_label = " / ".join(part for part in [user_role, user_title] if part)
        lines.append(
            f"Primary user role/use case: {role_label}. Prioritize details this user is likely to care about, "
            "while still preserving all important decisions, risks, and action items."
        )

    attendees = (config.get("attendees") or "").strip()
    if attendees:
        lines.append(f"Known attendees: {attendees}")

    audio_duration = (config.get("_audio_duration") or "").strip()
    if audio_duration:
        lines.append(f"Exact meeting duration: {audio_duration} (use this for the duration metadata field)")

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

            def _parse_list(key):
                val = data.get(key, [])
                return [json.loads(v) if isinstance(v, str) else v for v in val]

            metadata = data.get("metadata", {})
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            sections      = _parse_list("sections")
            action_items  = _parse_list("action_items")
            key_decisions = _parse_list("key_decisions")
            spk_summaries = _parse_list("speaker_summaries")
            questions     = _parse_list("questions_raised")
            next_steps    = data.get("next_steps", [])
            if isinstance(next_steps, str):
                next_steps = [next_steps]

            return {
                "title": data.get("title", "Meeting"),
                "overview": data.get("overview", ""),
                "metadata": {
                    "date":         metadata.get("date", "Unknown"),
                    "duration":     metadata.get("duration", "Unknown"),
                    "participants": metadata.get("participants", []),
                },
                "keyDecisions":    key_decisions,
                "sections":        sections,
                "actionItems":     action_items,
                "speakerSummaries": spk_summaries,
                "questionsRaised": questions,
                "nextSteps":       next_steps,
                "plainText":       data.get("plain_text", ""),
                "template":        (config or {}).get("template", "standard"),
                "outputLanguage":  (config or {}).get("output_language", "en") or "en",
            }

    raise RuntimeError("Claude did not return a produce_minutes tool call")
