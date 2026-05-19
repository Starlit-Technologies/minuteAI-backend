import requests


_NOTION_VERSION = "2022-06-28"


def create_meeting_page(access_token: str, settings: dict, job_id: str, minutes: dict, app_url: str) -> None:
    """Create a Notion page in the user's configured database with the meeting recap."""
    database_id = settings.get("database_id")
    if not database_id:
        return

    title = minutes.get("title", "Meeting")
    overview = minutes.get("overview", "")
    action_items = minutes.get("actionItems", [])
    results_url = f"{app_url}/results/{job_id}"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }

    children = []

    # Overview heading + paragraph
    if overview:
        children.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "Overview"}}]
            },
        })
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": overview[:2000]}}]
            },
        })

    # Action items heading + to_do blocks
    if action_items:
        children.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "Action Items"}}]
            },
        })
        for item in action_items[:50]:
            task = item.get("task", "")
            owner = item.get("owner", "")
            text = f"{task} — {owner}" if owner else task
            children.append({
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
                    "checked": False,
                },
            })

    # Link back to MinuteAI
    children.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {"type": "text", "text": {"content": "View full notes on MinuteAI: "}},
                {
                    "type": "text",
                    "text": {"content": results_url, "link": {"url": results_url}},
                },
            ]
        },
    })

    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": title}}]
            }
        },
        "children": children,
    }

    requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json=payload,
        timeout=15,
    )
