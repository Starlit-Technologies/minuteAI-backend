import requests


def log_meeting_note(access_token: str, settings: dict, job_id: str, minutes: dict, app_url: str) -> None:
    """Create a HubSpot note engagement with the meeting recap."""
    title = minutes.get("title", "Meeting")
    overview = minutes.get("overview", "")
    action_items = minutes.get("actionItems", [])
    results_url = f"{app_url}/results/{job_id}"

    items_text = ""
    if action_items:
        lines = [f"- {a.get('task', '')} ({a.get('owner', 'TBD')})" for a in action_items[:20]]
        items_text = "\n\nAction Items:\n" + "\n".join(lines)

    body = (
        f"Meeting: {title}\n\n"
        f"{overview}"
        f"{items_text}"
        f"\n\nFull notes: {results_url}"
    )

    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": None,
        }
    }
    # Remove None values
    payload["properties"] = {k: v for k, v in payload["properties"].items() if v is not None}
    # hs_timestamp is required — use current time in ms
    import time
    payload["properties"]["hs_timestamp"] = str(int(time.time() * 1000))

    requests.post(
        "https://api.hubapi.com/crm/v3/objects/notes",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )
