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


def format_minutes(transcript: str) -> dict:
    """Send the transcript to Claude and return structured minutes as a dict."""
    last_err = None
    for attempt in range(5):
        try:
            return _call_claude(transcript)
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


def _call_claude(transcript: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        tools=[MINUTES_TOOL],
        tool_choice={"type": "tool", "name": "produce_minutes"},
        messages=[
            {
                "role": "user",
                "content": f"Here is the meeting transcript:\n\n{transcript}"
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
            }

    raise RuntimeError("Claude did not return a produce_minutes tool call")
