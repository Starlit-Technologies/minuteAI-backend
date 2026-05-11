from urllib.parse import urlparse


def detect_platform(meeting_url: str) -> str:
    parsed = urlparse(meeting_url)
    hostname = (parsed.hostname or "").lower()
    if "meet.google.com" in hostname:
        return "google_meet"
    if "zoom.us" in hostname:
        return "zoom"
    raise ValueError("Only Google Meet and Zoom links are supported right now.")
