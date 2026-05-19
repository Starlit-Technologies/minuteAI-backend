import requests


def post_webhook(access_token: str, settings: dict, job_id: str, minutes: dict, app_url: str) -> None:
    """POST meeting data to a user-configured webhook URL."""
    url = settings.get("url")
    if not url:
        return

    results_url = f"{app_url}/results/{job_id}"
    payload = {
        "event": "meeting.minutes_ready",
        "job_id": job_id,
        "meeting_title": minutes.get("title", ""),
        "overview": minutes.get("overview", ""),
        "action_items": minutes.get("actionItems", []),
        "key_decisions": minutes.get("keyDecisions", []),
        "next_steps": minutes.get("nextSteps", []),
        "results_url": results_url,
        "metadata": minutes.get("metadata", {}),
    }

    requests.post(
        url,
        json=payload,
        timeout=10,
        headers={"Content-Type": "application/json"},
    )
