import requests


def post_meeting_recap(access_token: str, settings: dict, job_id: str, minutes: dict, app_url: str) -> None:
    """Post meeting recap to Slack via incoming webhook URL stored in settings."""
    webhook_url = settings.get("webhook_url")
    if not webhook_url:
        return
    title = minutes.get("title", "Meeting")
    overview = minutes.get("overview", "")
    action_items = minutes.get("actionItems", [])
    results_url = f"{app_url}/results/{job_id}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📋 {title}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": overview[:500] or "_No overview_"}},
    ]
    if action_items:
        items_text = "\n".join(f"• {a.get('task','')} — _{a.get('owner','TBD')}_" for a in action_items[:5])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Action Items:*\n{items_text}"}})
    blocks.append({
        "type": "actions",
        "elements": [{"type": "button", "text": {"type": "plain_text", "text": "View Full Notes"}, "url": results_url, "style": "primary"}]
    })
    requests.post(webhook_url, json={"blocks": blocks}, timeout=10)
