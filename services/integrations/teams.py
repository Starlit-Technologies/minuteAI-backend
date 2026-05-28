"""Microsoft Teams incoming webhook integration.

Posts a meeting recap as an Adaptive Card when minutes are ready.
Teams incoming webhook URL is stored in settings["webhook_url"].
"""

import requests


def post_meeting_recap(
    settings: dict,
    job_id: str,
    minutes: dict,
    app_url: str,
    meeting_context: dict | None = None,
) -> None:
    """Post meeting recap to a Teams channel via incoming webhook."""
    webhook_url = settings.get("webhook_url")
    if not webhook_url:
        return

    title         = minutes.get("title", "Meeting")
    overview      = (minutes.get("overview") or "")[:400]
    action_items  = minutes.get("actionItems") or []
    key_decisions = minutes.get("keyDecisions") or []
    results_url   = f"{app_url}/results/{job_id}"

    # ── Body facts ────────────────────────────────────────────────────────────
    facts = []
    if action_items:
        items_text = "\n".join(
            f"• {a.get('task', a) if isinstance(a, dict) else a}"
            + (f" — *{a['owner']}*" if isinstance(a, dict) and a.get("owner") else "")
            for a in action_items[:5]
        )
        facts.append({"name": "Action items", "value": items_text})

    if key_decisions:
        dec_text = "\n".join(
            f"• {d.get('decision', d) if isinstance(d, dict) else d}"
            for d in key_decisions[:3]
        )
        facts.append({"name": "Key decisions", "value": dec_text})

    # ── Adaptive Card payload (Teams) ─────────────────────────────────────────
    body_blocks = [
        {
            "type": "TextBlock",
            "text": overview or "_No overview available._",
            "wrap": True,
            "color": "Default",
        }
    ]
    for f in facts:
        body_blocks.append({
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column", "width": "auto",
                    "items": [{"type": "TextBlock", "text": f["name"], "weight": "Bolder", "wrap": True}],
                },
                {
                    "type": "Column", "width": "stretch",
                    "items": [{"type": "TextBlock", "text": f["value"], "wrap": True}],
                },
            ],
        })

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "Container",
                            "style": "emphasis",
                            "items": [
                                {
                                    "type": "TextBlock",
                                    "text": f"📋 {title}",
                                    "weight": "Bolder",
                                    "size": "Large",
                                },
                                {
                                    "type": "TextBlock",
                                    "text": "Meeting notes are ready",
                                    "spacing": "None",
                                    "isSubtle": True,
                                },
                            ],
                        },
                        *body_blocks,
                    ],
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "View full notes",
                            "url": results_url,
                            "style": "positive",
                        }
                    ],
                },
            }
        ],
    }

    requests.post(webhook_url, json=payload, timeout=10)
