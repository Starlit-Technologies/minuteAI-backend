import os
from models.connected_account import list_connected_accounts

APP_URL = os.environ.get("APP_URL", "http://localhost:5173")

# Providers that handle calendar sync — not post-meeting actions
_CALENDAR_PROVIDERS = {"google", "zoom", "outlook"}


def run_integrations(user_id: str, job_id: str, minutes: dict) -> None:
    """Fire all post-meeting integrations for a user. Never raises."""
    accounts = list_connected_accounts(user_id)
    for account in accounts:
        if account.provider in _CALENDAR_PROVIDERS:
            continue
        try:
            settings = account.settings or {}
            _dispatch(account.provider, account.access_token, settings, job_id, minutes)
        except Exception:
            pass


def _dispatch(provider, access_token, settings, job_id, minutes):
    if provider == "slack":
        from services.integrations.slack import post_meeting_recap
        post_meeting_recap(access_token, settings, job_id, minutes, APP_URL)
    elif provider == "notion":
        from services.integrations.notion import create_meeting_page
        create_meeting_page(access_token, settings, job_id, minutes, APP_URL)
    elif provider == "hubspot":
        from services.integrations.hubspot import log_meeting_note
        log_meeting_note(access_token, settings, job_id, minutes, APP_URL)
    elif provider == "linear":
        from services.integrations.linear import create_issues
        create_issues(access_token, settings, job_id, minutes, APP_URL)
    elif provider == "webhook":
        from services.integrations.webhook import post_webhook
        post_webhook(access_token, settings, job_id, minutes, APP_URL)
    elif provider == "google_drive":
        from services.integrations.google_drive import upload_to_drive
        upload_to_drive(access_token, settings, job_id, minutes, APP_URL)
