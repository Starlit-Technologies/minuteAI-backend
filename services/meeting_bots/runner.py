import asyncio
import base64
import os
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import imageio_ffmpeg

from models.meeting_bot import (
    append_session_log,
    get_session,
    list_startable_sessions,
    update_session,
)
from services.meeting_bots.platforms import detect_platform
from services.processing_pipeline import create_processing_job_from_media

try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None


DEBUG_DIR   = Path(__file__).parent.parent.parent / "storage" / "meeting_bot_debug"
CAPTURE_DIR = Path(__file__).parent.parent.parent / "storage" / "meeting_bot_captures"

MEETING_BOT_MODE            = os.environ.get("MEETING_BOT_MODE", "playwright").strip().lower()
MEETING_BOT_RECORDER_NAME   = os.environ.get("MEETING_BOT_RECORDER_NAME", "MinuteAI Recorder").strip() or "MinuteAI Recorder"
MEETING_BOT_RECORD_SECONDS  = int(os.environ.get("MEETING_BOT_RECORD_SECONDS", "1800"))
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

GOOGLE_MEET_BLOCK_MESSAGES = [
    "You can't join this video call",
    "You can't join this call",
    "This call is restricted",
]

# ── Audio capture via WebRTC interception ─────────────────────────────────────
# Injected before page content loads. Patches RTCPeerConnection to capture all
# incoming audio tracks. Python drains chunks via page.evaluate('window.__maDrain()').

_AUDIO_CAPTURE_SCRIPT = r"""
(function() {
    if (window.__ma_installed) return;
    window.__ma_installed = true;

    const _AC = window.AudioContext || window.webkitAudioContext;
    if (!_AC) return;

    const ctx  = new _AC();
    const dest = ctx.createMediaStreamDestination();
    let   recorder = null;
    const chunks   = [];

    function tryResume() { ctx.state === 'suspended' && ctx.resume().catch(() => {}); }
    [0, 500, 1500, 3000, 6000].forEach(ms => setTimeout(tryResume, ms));

    function ensureRecorder() {
        if (recorder && recorder.state === 'recording') return;
        const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
        const mime  = types.find(t => { try { return MediaRecorder.isTypeSupported(t); } catch(_) { return false; } }) || '';
        try {
            recorder = new MediaRecorder(dest.stream, mime ? { mimeType: mime } : {});
            recorder.ondataavailable = function(e) {
                if (!e.data || e.data.size === 0) return;
                const reader = new FileReader();
                reader.onloadend = function() {
                    const b64 = reader.result.split(',')[1];
                    if (b64) chunks.push(b64);
                };
                reader.readAsDataURL(e.data);
            };
            recorder.start(4000);
        } catch(_) {}
    }

    const connected = new WeakSet();
    function connectTrack(track) {
        if (track.kind !== 'audio' || connected.has(track)) return;
        connected.add(track);
        try {
            const src = ctx.createMediaStreamSource(new MediaStream([track]));
            src.connect(dest);
            ensureRecorder();
        } catch(_) {}
    }

    const _PC = window.RTCPeerConnection;
    if (_PC) {
        function PatchedPC() {
            const pc = Reflect.construct(_PC, arguments, PatchedPC);
            pc.addEventListener('track', function(e) { connectTrack(e.track); });
            return pc;
        }
        Object.setPrototypeOf(PatchedPC, _PC);
        Object.setPrototypeOf(PatchedPC.prototype, _PC.prototype);
        if (_PC.generateCertificate) PatchedPC.generateCertificate = _PC.generateCertificate.bind(_PC);
        window.RTCPeerConnection = PatchedPC;
    }

    window.__maDrain  = function() { return chunks.splice(0); };
    window.__maStop   = function() {
        if (recorder && recorder.state === 'recording') {
            try { recorder.requestData(); } catch(_) {}
            setTimeout(function() { try { recorder.stop(); } catch(_) {} }, 600);
        }
    };
    window.__maActive = function() { return recorder !== null && recorder.state === 'recording'; };
    window.__maChunks = function() { return chunks.length; };
})();
"""


@dataclass
class BotRunContext:
    session_id: str
    meeting_url: str
    bot_name: str
    platform: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(session_id: str, message: str):
    ts   = datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] {message}"
    print(f"[bot:{session_id[:8]}] {message}")
    try:
        append_session_log(session_id, line)
    except Exception:
        pass


def _status(session_id: str, status: str, **extra):
    update_session(session_id, status=status, **extra)
    _log(session_id, f"Status → {status}")


def _failure(session_id: str, message: str, **extra):
    _log(session_id, f"FAILED: {message}")
    update_session(session_id, status="failed", ended_at=datetime.utcnow(), failure_reason=message, **extra)


def _current_status(session_id: str) -> Optional[str]:
    s = get_session(session_id)
    return s.status if s else None


def _capture_has_audio_stream(media_path: str) -> bool:
    result = subprocess.run([FFMPEG, "-i", media_path, "-f", "null", "-"], capture_output=True, text=True)
    return "Audio:" in (result.stderr or "")


# ── Audio helpers ─────────────────────────────────────────────────────────────

async def _drain_audio_chunks(page, chunks_store: list, session_id: str):
    while True:
        await asyncio.sleep(10)
        try:
            new = await page.evaluate("window.__maDrain ? window.__maDrain() : []")
            if new:
                chunks_store.extend(new)
                _log(session_id, f"Drained {len(new)} audio chunk(s) ({len(chunks_store)} total)")
        except Exception as exc:
            _log(session_id, f"Audio drain error: {exc}")
            break


def _save_audio_chunks(chunks: list[str], path: str) -> bool:
    if not chunks:
        return False
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        for chunk in chunks:
            try:
                f.write(base64.b64decode(chunk))
            except Exception:
                pass
    return os.path.getsize(path) > 1024


# ── Debug ─────────────────────────────────────────────────────────────────────

async def _take_debug_screenshot(page, session_id: str, label: str) -> str:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"{session_id}_{label}.png"
    with suppress(Exception):
        await page.screenshot(path=str(path), full_page=True)
    return str(path)


# ── Google Meet helpers ───────────────────────────────────────────────────────

async def _dismiss_google_meet_prejoin(page):
    for label in ["Continue without microphone", "Continue without camera", "Dismiss", "Got it"]:
        with suppress(Exception):
            await page.get_by_role("button", name=label).click(timeout=1000)


async def _maybe_turn_off_google_meet_av(page):
    for label in [
        "Turn off microphone", "Turn off camera", "Mute microphone",
        "Turn off your microphone", "Turn off your camera",
    ]:
        with suppress(Exception):
            await page.get_by_role("button", name=label).click(timeout=1500)


async def _prepare_google_meet(page, meeting_url: str, bot_name: str):
    await page.goto(meeting_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    await _dismiss_google_meet_prejoin(page)
    await _maybe_turn_off_google_meet_av(page)

    # Fill in guest name — always present when not signed in
    for selector in [
        'input[aria-label="Your name"]',
        'input[placeholder="Your name"]',
        'input[aria-label*="name" i]',
        'input[type="text"]',
    ]:
        with suppress(Exception):
            field = page.locator(selector).first
            if await field.is_visible(timeout=2000):
                await field.click()
                await field.fill(bot_name)
                return


async def _join_google_meet(page, session_id: str):
    for label in ["Ask to join", "Join now", "Request to join", "Join meeting"]:
        with suppress(Exception):
            button = page.get_by_role("button", name=label)
            await button.click(timeout=2000)
            _status(session_id, "joining")
            return
    raise RuntimeError("Could not find the Google Meet join button.")


async def _detect_google_meet_block(page) -> Optional[str]:
    for msg in GOOGLE_MEET_BLOCK_MESSAGES:
        with suppress(Exception):
            if await page.get_by_text(msg).is_visible(timeout=1000):
                return msg
    return None


# ── Zoom helpers ──────────────────────────────────────────────────────────────

async def _zoom_click_join_from_browser(page):
    """Click the 'Join from Your Browser' link on Zoom's launcher page."""
    selectors = [
        "a#btnBrowserJoin",
        "a.join-from-browser",
        "a:has-text('Join from Your Browser')",
        "a:has-text('join from your browser')",
        "[data-testid='join-from-browser']",
    ]
    for selector in selectors:
        with suppress(Exception):
            elem = page.locator(selector).first
            if await elem.is_visible(timeout=3000):
                await elem.click()
                await page.wait_for_timeout(2500)
                return
    # Sometimes appears after a short delay — one more attempt
    await page.wait_for_timeout(3000)
    for selector in selectors:
        with suppress(Exception):
            elem = page.locator(selector).first
            if await elem.is_visible(timeout=2000):
                await elem.click()
                await page.wait_for_timeout(2000)
                return


async def _zoom_fill_name(page, bot_name: str):
    selectors = [
        "#inputname",
        'input[name="inputname"]',
        'input[aria-label="Your Name"]',
        'input[placeholder="Your Name"]',
        'input[placeholder="Enter your name"]',
        'input[type="text"]',
    ]
    for selector in selectors:
        with suppress(Exception):
            field = page.locator(selector).first
            if await field.is_visible(timeout=3000):
                await field.triple_click()
                await field.fill(bot_name)
                return


async def _zoom_click_join_button(page, session_id: str):
    selectors = [
        "#joinBtn",
        "button#joinBtn",
        'button[data-testid="join-btn"]',
        "button.preview-join-button",
        "button:has-text('Join')",
    ]
    for selector in selectors:
        with suppress(Exception):
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                _status(session_id, "joining")
                return
    # Fall back to role-based
    for label in ["Join", "Join Meeting", "Join Audio by Computer"]:
        with suppress(Exception):
            btn = page.get_by_role("button", name=label, exact=False).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                _status(session_id, "joining")
                return
    raise RuntimeError("Could not find the Zoom join button.")


async def _prepare_and_join_zoom(page, meeting_url: str, bot_name: str, session_id: str):
    await page.goto(meeting_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    await _zoom_click_join_from_browser(page)
    await _zoom_fill_name(page, bot_name)
    await page.wait_for_timeout(500)
    await _zoom_click_join_button(page, session_id)


# ── Shared wait/detection ─────────────────────────────────────────────────────

async def _wait_for_admission(page, session_id: str, platform: str) -> bool:
    _status(session_id, "waiting_for_host")
    deadline_seconds = 90 if platform == "google_meet" else 45

    for _ in range(deadline_seconds // 3):
        if _current_status(session_id) in {"cancelled", "processing"}:
            return False

        if platform == "google_meet":
            blocked = await _detect_google_meet_block(page)
            if blocked:
                raise RuntimeError(
                    f"Google Meet blocked the bot: {blocked}. "
                    "The meeting may be restricted to signed-in accounts only."
                )
            signals = [
                page.get_by_role("button", name="Leave call"),
                page.get_by_role("button", name="Leave meeting"),
                page.get_by_role("button", name="End call"),
                page.get_by_text("You joined"),
            ]
        else:
            signals = [
                page.get_by_role("button", name="Leave"),
                page.get_by_text("Mute"),
                page.get_by_text("Participants"),
            ]

        for locator in signals:
            with suppress(Exception):
                if await locator.is_visible(timeout=1200):
                    _status(session_id, "recording")
                    return True

        await page.wait_for_timeout(3000)

    return False


async def _is_still_in_meeting(page, platform: str) -> bool:
    if platform == "google_meet":
        active = [
            page.get_by_role("button", name="Leave call"),
            page.get_by_role("button", name="Leave meeting"),
            page.get_by_role("button", name="End call"),
        ]
        ended = ["You left the meeting", "Call ended", "Return to home screen", "Ready to join?"]
    else:
        active = [page.get_by_role("button", name="Leave"), page.get_by_text("Mute"), page.get_by_text("Participants")]
        ended  = ["You have left the meeting", "This meeting has ended", "Join Meeting"]

    for loc in active:
        with suppress(Exception):
            if await loc.is_visible(timeout=800):
                return True
    for text in ended:
        with suppress(Exception):
            if await page.get_by_text(text).is_visible(timeout=800):
                return False
    return False


async def _wait_for_recording_completion(page, session_id: str, platform: str):
    deadline = asyncio.get_running_loop().time() + MEETING_BOT_RECORD_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if _current_status(session_id) in {"cancelled", "processing"}:
            return
        if not await _is_still_in_meeting(page, platform):
            _status(session_id, "processing")
            return
        await page.wait_for_timeout(5000)
    _status(session_id, "processing")


async def _leave_meeting(page, platform: str):
    labels = ["Leave call", "Leave meeting", "End call"] if platform == "google_meet" else ["Leave", "Leave Meeting", "End Meeting"]
    for label in labels:
        with suppress(Exception):
            button = page.get_by_role("button", name=label)
            if await button.is_visible(timeout=1200):
                await button.click(timeout=2000)
                await page.wait_for_timeout(1500)
                return


# ── Main Playwright session ───────────────────────────────────────────────────

async def _run_playwright_session(ctx: BotRunContext):
    session = get_session(ctx.session_id)
    if not session:
        return

    if async_playwright is None:
        _failure(ctx.session_id, "Playwright is not installed. Run: pip install playwright && playwright install chromium")
        return

    _status(ctx.session_id, "preparing", started_at=datetime.utcnow(), failure_reason=None)
    _log(ctx.session_id, f"Preparing browser for {ctx.platform} — {ctx.meeting_url}")

    audio_chunks: list[str] = []
    drain_task: Optional[asyncio.Task] = None

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    chromium_args = [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-gpu",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-dev-shm-usage",
        "--disable-extensions",
    ]

    try:
        async with async_playwright() as pw:
            browser = None
            context = None
            page    = None

            try:
                browser = await pw.chromium.launch(headless=True, args=chromium_args)
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    permissions=["camera", "microphone"],
                    ignore_https_errors=True,
                )

                await context.add_init_script(_AUDIO_CAPTURE_SCRIPT)
                _log(ctx.session_id, "Audio capture script registered")

                page = await context.new_page()
                await _take_debug_screenshot(page, ctx.session_id, "landing")

                if ctx.platform == "google_meet":
                    await _prepare_google_meet(page, ctx.meeting_url, ctx.bot_name)
                    await _take_debug_screenshot(page, ctx.session_id, "prejoin")
                    await _join_google_meet(page, ctx.session_id)
                elif ctx.platform == "zoom":
                    await _prepare_and_join_zoom(page, ctx.meeting_url, ctx.bot_name, ctx.session_id)
                    await _take_debug_screenshot(page, ctx.session_id, "prejoin")
                else:
                    raise RuntimeError(f"Unsupported platform: {ctx.platform}")

                joined = await _wait_for_admission(page, ctx.session_id, ctx.platform)
                screenshot_path = await _take_debug_screenshot(page, ctx.session_id, "postjoin")

                if not joined:
                    _failure(
                        ctx.session_id,
                        "Bot was not admitted to the meeting within the timeout. "
                        "The host needs to accept 'MinuteAI Recorder' from the waiting room. "
                        f"Debug screenshot: {screenshot_path}",
                        recording_path=screenshot_path,
                    )
                    return

                _log(ctx.session_id, "Admitted — audio capture active")

                drain_task = asyncio.create_task(_drain_audio_chunks(page, audio_chunks, ctx.session_id))
                await _wait_for_recording_completion(page, ctx.session_id, ctx.platform)

            except Exception as exc:
                screenshot_path = None
                with suppress(Exception):
                    screenshot_path = await _take_debug_screenshot(page, ctx.session_id, "error")
                _failure(ctx.session_id, f"Browser automation error: {exc}", recording_path=screenshot_path)

            finally:
                if drain_task and not drain_task.done():
                    drain_task.cancel()
                    with suppress(Exception):
                        await drain_task

                with suppress(Exception):
                    await page.evaluate("if (window.__maStop) window.__maStop()")
                with suppress(Exception):
                    await page.wait_for_timeout(1500)
                with suppress(Exception):
                    final = await page.evaluate("window.__maDrain ? window.__maDrain() : []")
                    audio_chunks.extend(final)
                    _log(ctx.session_id, f"Final drain: {len(final)} chunk(s), total {len(audio_chunks)}")

                if page is not None:
                    with suppress(Exception):
                        await _leave_meeting(page, ctx.platform)
                if context is not None:
                    with suppress(Exception):
                        await context.close()
                if browser is not None:
                    with suppress(Exception):
                        await browser.close()

        # ── Post-session: save audio and create processing job ─────────────────
        if _current_status(ctx.session_id) in {"failed", "cancelled"}:
            return

        audio_path = str(CAPTURE_DIR / f"{ctx.session_id}.webm")
        if not _save_audio_chunks(audio_chunks, audio_path):
            _failure(
                ctx.session_id,
                "Audio capture produced no data. The meeting may have had no audio, "
                "or the browser was blocked from processing WebRTC streams.",
            )
            return

        if not _capture_has_audio_stream(audio_path):
            _failure(
                ctx.session_id,
                "Captured file has no audio stream. The WebRTC intercept may not have fired — "
                "this can happen if the meeting uses a non-standard audio stack.",
                recording_path=audio_path,
            )
            return

        _log(ctx.session_id, f"Audio file ready: {audio_path}")

        processing_job_id = create_processing_job_from_media(
            media_path=audio_path,
            original_filename=f"{ctx.session_id}.webm",
            user_id=session.user_id,
            workspace_id=session.workspace_id,
        )
        update_session(
            ctx.session_id,
            status="completed",
            ended_at=datetime.utcnow(),
            recording_path=audio_path,
            job_id=processing_job_id,
            failure_reason=None,
        )
        _log(ctx.session_id, f"Processing job created: {processing_job_id}")

    except Exception as exc:
        _failure(ctx.session_id, f"Session failed before launch: {exc}")


# ── Thread entry point ────────────────────────────────────────────────────────

def _run_session(session_id: str):
    session = get_session(session_id)
    if not session or session.status == "cancelled":
        return

    if session.scheduled_for:
        while True:
            latest = get_session(session_id)
            if not latest or latest.status == "cancelled":
                return
            seconds_until = (latest.scheduled_for - datetime.utcnow()).total_seconds()
            if seconds_until <= 0:
                break
            _log(session_id, f"Scheduled — waiting {int(seconds_until)}s")
            time.sleep(min(15, max(1, seconds_until)))

    if MEETING_BOT_MODE == "stub":
        update_session(
            session_id,
            status="failed",
            ended_at=datetime.utcnow(),
            failure_reason=(
                "Bot is in stub mode. Set MEETING_BOT_MODE=playwright and install Chromium "
                "(`playwright install chromium`) to enable real browser automation."
            ),
        )
        return

    try:
        platform = detect_platform(session.meeting_url)
    except Exception as exc:
        _failure(session_id, str(exc))
        return

    ctx = BotRunContext(
        session_id=session.id,
        meeting_url=session.meeting_url,
        bot_name=session.bot_name or MEETING_BOT_RECORDER_NAME,
        platform=platform,
    )
    asyncio.run(_run_playwright_session(ctx))


def start_meeting_bot_session(session_id: str):
    threading.Thread(target=_run_session, args=(session_id,), daemon=True).start()


def start_pending_meeting_bot_sessions():
    for session in list_startable_sessions():
        start_meeting_bot_session(session.id)
