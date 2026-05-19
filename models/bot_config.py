"""
Persistent bot configuration stored as JSON at storage/bot_config.json.
Runtime values override env vars so the admin panel can change them without
a server restart.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

_CONFIG_PATH = Path(__file__).parent.parent / "storage" / "bot_config.json"

# Module-level reference to an in-progress setup process (one at a time).
_setup_proc: Optional[subprocess.Popen] = None


def _defaults() -> dict:
    return {
        "email":             os.environ.get("BOT_EMAIL", ""),
        "bot_name":          os.environ.get("MEETING_BOT_RECORDER_NAME", "MinuteAI Recorder"),
        "profile_dir":       os.environ.get("MEETING_BOT_PROFILE_DIR", ""),
        "bot_refresh_token": os.environ.get("BOT_REFRESH_TOKEN", ""),
        "bot_oauth_email":   "",
    }


def load_bot_config() -> dict:
    try:
        stored = json.loads(_CONFIG_PATH.read_text())
        return {**_defaults(), **stored}
    except Exception:
        return _defaults()


def save_bot_config(updates: dict) -> dict:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = load_bot_config()
    current.update({k: v for k, v in updates.items() if v is not None})
    _CONFIG_PATH.write_text(json.dumps(current, indent=2))
    return current


def profile_status(cfg: dict) -> dict:
    profile_dir = cfg.get("profile_dir", "")
    path = Path(profile_dir) if profile_dir else None
    exists = bool(path and path.is_dir() and any(path.iterdir()))
    updated_at = None
    signed_in_email = None
    signed_in_name  = None
    if exists:
        try:
            updated_at = path.stat().st_mtime
        except Exception:
            pass
        # Read the actual signed-in account from Chrome's Preferences JSON.
        # Chrome writes account_info after a successful Google sign-in.
        try:
            import json as _json
            prefs_path = path / "Default" / "Preferences"
            prefs = _json.loads(prefs_path.read_text(encoding="utf-8", errors="ignore"))
            accounts = prefs.get("account_info", [])
            if accounts:
                signed_in_email = accounts[0].get("email", "")
                signed_in_name  = accounts[0].get("full_name", "")
        except Exception:
            pass
    return {
        "exists":          exists,
        "path":            profile_dir,
        "updated_at":      updated_at,
        "signed_in_email": signed_in_email,
        "signed_in_name":  signed_in_name,
    }


def default_profile_dir() -> str:
    return str(Path(__file__).parent.parent / "storage" / "meeting_bot_recorder_profile")


def start_setup(profile_dir: str) -> int:
    global _setup_proc
    # Kill any existing setup process
    if _setup_proc and _setup_proc.poll() is None:
        try:
            _setup_proc.stdin.write(b"\n")
            _setup_proc.stdin.flush()
        except Exception:
            pass

    script = Path(__file__).parent.parent / "scripts" / "bootstrap_meeting_bot_google.py"
    env = os.environ.copy()
    env["MEETING_BOT_PROFILE_DIR"] = profile_dir

    _setup_proc = subprocess.Popen(
        [sys.executable, str(script), "--profile-dir", profile_dir],
        stdin=subprocess.PIPE,
        env=env,
    )
    return _setup_proc.pid


def complete_setup() -> bool:
    """Send Enter to the waiting bootstrap script so it closes the browser and saves."""
    global _setup_proc
    if _setup_proc and _setup_proc.poll() is None:
        try:
            _setup_proc.stdin.write(b"\n")
            _setup_proc.stdin.flush()
            _setup_proc.wait(timeout=10)
        except Exception:
            pass
    _setup_proc = None
    return True


def cancel_setup() -> None:
    global _setup_proc
    if _setup_proc and _setup_proc.poll() is None:
        try:
            _setup_proc.terminate()
        except Exception:
            pass
    _setup_proc = None


def setup_running() -> bool:
    return bool(_setup_proc and _setup_proc.poll() is None)


# ── Session cookie storage ────────────────────────────────────────────────────
# Cookies are exported from a locally-bootstrapped Chrome profile and stored
# here so the bot can inject them into a fresh headless context on any server.

_COOKIES_PATH = _CONFIG_PATH.parent / "bot_session_cookies.json"


def save_session_cookies(cookies: list[dict]) -> None:
    import json as _json
    _COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _COOKIES_PATH.write_text(_json.dumps(cookies))


def load_session_cookies() -> list[dict] | None:
    import json as _json
    try:
        data = _json.loads(_COOKIES_PATH.read_text())
        return data if isinstance(data, list) and data else None
    except Exception:
        return None


def clear_session_cookies() -> None:
    try:
        _COOKIES_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def session_cookies_info() -> dict:
    """Return metadata about stored cookies (for admin UI) without the raw values."""
    import json as _json
    try:
        cookies = _json.loads(_COOKIES_PATH.read_text())
        if not isinstance(cookies, list) or not cookies:
            return {"stored": False}
        # Find the latest expiry across session cookies
        session_names = {"SID", "HSID", "SSID", "APISID", "SAPISID",
                         "__Secure-1PSID", "__Secure-3PSID"}
        session_cookies = [c for c in cookies if c.get("name") in session_names]
        expires = [c["expires"] for c in session_cookies if c.get("expires", -1) > 0]
        email = None
        # Try to read the signed-in email from an OAuth cookie if present
        for c in cookies:
            if c.get("name") in {"COMPASS"} and "email" in str(c.get("value", "")):
                pass
        return {
            "stored": True,
            "count": len(cookies),
            "expires_at": max(expires) if expires else None,
            "imported_at": _COOKIES_PATH.stat().st_mtime if _COOKIES_PATH.exists() else None,
        }
    except Exception:
        return {"stored": False}
