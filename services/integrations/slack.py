import requests


def _resolve_webhook(settings: dict, meeting_context: dict) -> str | None:
    """
    Pick the webhook URL to post to.

    If `destinations` is configured, route by category match → keyword match → default.
    Otherwise fall back to the single `webhook_url` stored from OAuth.
    """
    destinations = settings.get("destinations") or []
    if not destinations:
        return settings.get("webhook_url")

    category = (meeting_context.get("category") or "").lower()
    title = (meeting_context.get("title") or "").lower()
    overview = (meeting_context.get("overview") or "").lower()
    search_text = f"{title} {overview}"

    # Priority 1: category match
    if category:
        for dest in destinations:
            cats = [c.lower() for c in (dest.get("categories") or [])]
            if category in cats:
                return dest.get("webhook_url")

    # Priority 2: keyword match in title/overview
    for dest in destinations:
        keywords = [k.lower().strip() for k in (dest.get("keywords") or []) if k.strip()]
        if any(kw in search_text for kw in keywords):
            return dest.get("webhook_url")

    # Priority 3: default destination
    for dest in destinations:
        if dest.get("is_default"):
            return dest.get("webhook_url")

    # Priority 4: first destination
    return destinations[0].get("webhook_url")


def post_meeting_recap(
    access_token: str,
    settings: dict,
    job_id: str,
    minutes: dict,
    app_url: str,
    meeting_context: dict | None = None,
) -> None:
    """Post meeting recap to Slack. Supports multi-destination routing."""
    ctx = meeting_context or {}
    ctx.setdefault("title", minutes.get("title", ""))
    ctx.setdefault("overview", minutes.get("overview", ""))

    webhook_url = _resolve_webhook(settings, ctx)
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
