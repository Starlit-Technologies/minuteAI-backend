"""
Bot profile health check service.

Periodically opens the saved Chromium profile in headless mode and verifies
that the Google session cookies are still valid. Saves the result to
storage/bot_health.json so the admin dashboard can show a live indicator.

An email alert is sent (once) when the session transitions from healthy to
unhealthy so you know before the next real meeting fails.
"""
import asyncio
import logging
import threading
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 3600   # check every hour
_last_alert_sent: Optional[str] = None  # tracks last alerted "unhealthy" email


# ── Storage ───────────────────────────────────────────────────────────────────

def _save(result: dict) -> None:
    from models.bot_config import _CONFIG_PATH
    import json
    path = _CONFIG_PATH.parent / "bot_health.json"
    try:
        path.write_text(json.dumps(result, default=str))
    except Exception as exc:
        log.warning("Could not save bot health status: %s", exc)


def get_health_status() -> dict:
    from models.bot_config import _CONFIG_PATH
    import json
    path = _CONFIG_PATH.parent / "bot_health.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"healthy": None, "checked_at": None, "email": None, "error": None}


# ── Core check ────────────────────────────────────────────────────────────────

async def _check_async() -> dict:
    """
    Verify the bot's authentication is still valid.

    Two auth modes are supported:
    1. **OAuth refresh token** (production) — validates by refreshing the token
       with Google.  No Playwright or profile directory needed.
    2. **Persistent Chromium profile** (local dev) — opens the profile headlessly
       and checks for Google session cookies.

    The function tries OAuth first (since it's the primary prod method), then
    falls back to the profile-based check.
    """
    import os
    from models.bot_config import load_bot_config, profile_status

    mode = os.environ.get("MEETING_BOT_MODE", "playwright").strip().lower()
    if mode != "playwright":
        return {"healthy": None, "checked_at": datetime.utcnow().isoformat(),
                "email": None, "error": "Bot is in stub mode — health check skipped."}

    cfg = load_bot_config()
    ps  = profile_status(cfg)
    checked_at = datetime.utcnow().isoformat()

    # ── Method 1: OAuth refresh token (primary production auth) ───────────
    bot_refresh_token = cfg.get("bot_refresh_token", "") or ""
    if bot_refresh_token:
        google_client_id     = os.environ.get("GOOGLE_CLIENT_ID", "")
        google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        if google_client_id and google_client_secret:
            try:
                import requests as _req
                resp = _req.post("https://oauth2.googleapis.com/token", data={
                    "client_id":     google_client_id,
                    "client_secret": google_client_secret,
                    "refresh_token": bot_refresh_token,
                    "grant_type":    "refresh_token",
                }, timeout=15)
                if resp.ok and resp.json().get("access_token"):
                    email = cfg.get("bot_oauth_email") or cfg.get("email") or None
                    return {
                        "healthy":    True,
                        "checked_at": checked_at,
                        "email":      email,
                        "error":      None,
                        "auth_mode":  "oauth",
                    }
                else:
                    error_detail = resp.json().get("error_description", resp.text[:200]) if resp.text else f"HTTP {resp.status_code}"
                    return {
                        "healthy":    False,
                        "checked_at": checked_at,
                        "email":      cfg.get("bot_oauth_email") or None,
                        "error":      f"Bot OAuth refresh token is invalid or expired: {error_detail}. "
                                      f"Re-connect via Admin → Bot Config → Connect Google.",
                        "auth_mode":  "oauth",
                    }
            except Exception as exc:
                log.warning("Bot OAuth health check failed: %s", exc)
                return {
                    "healthy":    False,
                    "checked_at": checked_at,
                    "email":      cfg.get("bot_oauth_email") or None,
                    "error":      f"OAuth health check error: {exc}",
                    "auth_mode":  "oauth",
                }

    # ── Method 2: Persistent Chromium profile (local dev / legacy) ────────
    if not ps["exists"]:
        return {"healthy": False, "checked_at": checked_at,
                "email": None,
                "error": "No bot authentication configured. "
                         "Either connect a Google account via Admin → Bot Config → Connect Google (recommended), "
                         "or run the local profile setup."}

    profile_dir = cfg.get("profile_dir", "")

    # Don't run the check while a real session is using the same profile
    # (Playwright locks the profile directory).
    from services.meeting_bots.runner import _running_sessions
    if _running_sessions:
        return {"healthy": None, "checked_at": checked_at,
                "email": ps.get("signed_in_email"),
                "error": "Skipped — bot session is currently active."}

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"healthy": None, "checked_at": checked_at,
                "email": None, "error": "Playwright not installed."}

    try:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                profile_dir,
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                timeout=20000,
            )
            page = await ctx.new_page()
            try:
                await page.goto(
                    "https://accounts.google.com/", wait_until="domcontentloaded", timeout=15000
                )
                await page.wait_for_timeout(2000)

                # Check for valid Google session cookies
                cookies = await ctx.cookies(urls=["https://accounts.google.com", "https://google.com"])
                session_cookie_names = {"SID", "__Secure-3PSID", "HSID", "SSID"}
                has_session = any(c["name"] in session_cookie_names for c in cookies)

                # Secondary check: page title shouldn't contain "Sign in"
                title = await page.title()
                on_signin_page = "sign in" in title.lower()

                healthy = has_session and not on_signin_page
                email = ps.get("signed_in_email") or cfg.get("email") or None

                return {
                    "healthy":    healthy,
                    "checked_at": checked_at,
                    "email":      email,
                    "error":      None if healthy else (
                        "Google session has expired. Re-authenticate via Admin → Bot Config."
                    ),
                    "auth_mode":  "profile",
                }
            finally:
                await ctx.close()

    except Exception as exc:
        log.warning("Bot health check failed: %s", exc)
        return {
            "healthy":    False,
            "checked_at": checked_at,
            "email":      None,
            "error":      f"Health check error: {exc}",
            "auth_mode":  "profile",
        }


def run_health_check() -> dict:
    """Synchronous wrapper — runs the async check in a new event loop."""
    result = asyncio.run(_check_async())
    _save(result)
    _maybe_send_alert(result)
    log.info(
        "Bot health check: healthy=%s email=%s error=%s",
        result.get("healthy"), result.get("email"), result.get("error"),
    )
    return result


# ── Alert ─────────────────────────────────────────────────────────────────────

def _maybe_send_alert(result: dict) -> None:
    global _last_alert_sent
    if result.get("healthy") is True:
        _last_alert_sent = None  # reset so next failure triggers a fresh alert
        return
    if result.get("healthy") is None:
        return  # skipped/stub — not an actionable failure

    # Send alert only on the first detection of the unhealthy state
    error = result.get("error", "Unknown error")
    if _last_alert_sent == error:
        return
    _last_alert_sent = error

    try:
        import services.admin as svc
        from services.email import send_bot_auth_alert
        admin_emails = list(svc.configured_admin_emails())
        if admin_emails:
            send_bot_auth_alert(admin_emails, result.get("email"), error)
    except Exception as exc:
        log.warning("Failed to send bot auth alert: %s", exc)


# ── Background loop ───────────────────────────────────────────────────────────

def _loop() -> None:
    # Initial delay so the server has time to fully start before the first check
    time.sleep(60)
    while True:
        try:
            run_health_check()
        except Exception:
            log.exception("Bot health check loop error")
        time.sleep(HEALTH_CHECK_INTERVAL)


def start_health_check_loop() -> None:
    t = threading.Thread(target=_loop, name="bot-health-check", daemon=True)
    t.start()
    log.info("Bot profile health check loop started (interval=%ds)", HEALTH_CHECK_INTERVAL)
