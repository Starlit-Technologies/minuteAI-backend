import asyncio
import base64
import os
import shutil
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
    get_session,
    list_startable_sessions,
    update_session,
)
from models.bot_account import get_bot_account
from services.meeting_bots.platforms import detect_platform
from services.processing_pipeline import create_processing_job_from_media

try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None


DEBUG_DIR = Path(__file__).parent.parent.parent / "storage" / "meeting_bot_debug"
CAPTURE_DIR = Path(__file__).parent.parent.parent / "storage" / "meeting_bot_captures"
PROFILE_DIR = Path(
    os.environ.get(
        "MEETING_BOT_PROFILE_DIR",
        str(Path(__file__).parent.parent.parent / "storage" / "meeting_bot_recorder_profile"),
    )
)
RUNTIME_PROFILE_ROOT = Path(
    os.environ.get(
        "MEETING_BOT_RUNTIME_PROFILE_ROOT",
        str(Path(__file__).parent.parent.parent / "storage" / "meeting_bot_runtime_profiles"),
    )
)
MEETING_BOT_MODE = os.environ.get("MEETING_BOT_MODE", "playwright").strip().lower()
MEETING_BOT_RECORDER_NAME = os.environ.get("MEETING_BOT_RECORDER_NAME", "MinuteAI Recorder").strip() or "MinuteAI Recorder"
MEETING_BOT_RECORD_SECONDS = int(os.environ.get("MEETING_BOT_RECORD_SECONDS", "1800"))
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

GOOGLE_MEET_BLOCK_MESSAGES = [
    "You can't join this video call",
    "You can't join this call",
]

# ── Audio capture via WebRTC interception ─────────────────────────────────────
# Injected into every page before content loads. Patches RTCPeerConnection to
# capture all incoming audio tracks into a MediaRecorder. Python drains chunks
# periodically via page.evaluate('window.__maDrain()').

_AUDIO_CAPTURE_SCRIPT = r"""
(function() {
    if (window.__ma_installed) return;
    window.__ma_installed = true;

    const _AC = window.AudioContext || window.webkitAudioContext;
    if (!_AC) return;

    const ctx  = new _AC();
    const dest = ctx.createMediaStreamDestination();
    let   recorder = null;
    const chunks   = [];   // base64-encoded WebM blobs

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

    // Patch RTCPeerConnection — must happen before any meeting JS runs.
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

    // Drain: Python calls this periodically to collect encoded chunks.
    window.__maDrain = function() { return chunks.splice(0); };

    // Stop: called before final drain so the last in-flight chunk is flushed.
    window.__maStop = function() {
        if (recorder && recorder.state === 'recording') {
            try { recorder.requestData(); } catch(_) {}
            setTimeout(function() {
                try { recorder.stop(); } catch(_) {}
            }, 600);
        }
    };

    window.__maActive   = function() { return recorder !== null && recorder.state === 'recording'; };
    window.__maChunks   = function() { return chunks.length; };
})();
"""


@dataclass
class BotRunContext:
    session_id: str
    meeting_url: str
    bot_name: str
    platform: str
    profile_dir: Optional[Path] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(session_id: str, message: str):
    ts = datetime.utcnow().strftime("%H:%M:%S")
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
    update_session(
        session_id,
        status="failed",
        ended_at=datetime.utcnow(),
        failure_reason=message,
        **extra,
    )


def _current_status(session_id: str) -> Optional[str]:
    session = get_session(session_id)
    return session.status if session else None


def _capture_has_audio_stream(media_path: str) -> bool:
    cmd = [FFMPEG, "-i", media_path, "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return "Audio:" in (result.stderr or "")


def _runtime_profile_ignore(dirpath: str, names: list[str]) -> set[str]:
    volatile = {
        "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
        "GrShaderCache", "GraphiteDawnCache", "ShaderCache", "Safe Browsing",
        "Safe Browsing Network", "Service Worker", "Shared Dictionary",
        "Segmentation Platform", "Session Storage", "Sessions",
        "AutofillAiModelCache", "WebrtcVideoStats", "blob_storage",
        "LOCK", "lockfile", "DevToolsActivePort", "CrashpadMetrics-active.pma",
        "chrome_debug.log", "LOG", "LOG.old",
    }
    return {n for n in names if n in volatile or n.startswith("Singleton")}


def _prepare_runtime_profile(session_id: str, platform: str, profile_dir: Optional[Path] = None) -> Optional[Path]:
    if platform != "google_meet":
        return None

    source = profile_dir or PROFILE_DIR
    if not source.exists():
        if profile_dir and PROFILE_DIR.exists():
            source = PROFILE_DIR
        else:
            raise RuntimeError(
                f"Recorder profile not found at '{source}'. "
                "Sign the bot account into Google first (see bootstrap script)."
            )

    RUNTIME_PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    import tempfile
    runtime_dir = Path(tempfile.mkdtemp(prefix=f"{session_id}_", dir=str(RUNTIME_PROFILE_ROOT)))
    try:
        shutil.rmtree(runtime_dir)
        shutil.copytree(source, runtime_dir, ignore=_runtime_profile_ignore)
    except shutil.Error as exc:
        locked = any(
            "Cookies" in str(i) and ("WinError 32" in str(i) or "being used" in str(i))
            for i in getattr(exc, "args", [])
        )
        with suppress(Exception):
            shutil.rmtree(runtime_dir, ignore_errors=True)
        if locked:
            raise RuntimeError(
                f"Profile at '{source}' is open in another browser. Close it and retry."
            ) from exc
        raise
    except Exception:
        with suppress(Exception):
            shutil.rmtree(runtime_dir, ignore_errors=True)
        raise
    return runtime_dir


def _cleanup_runtime_profile(runtime_dir: Optional[Path]):
    if runtime_dir:
        with suppress(Exception):
            shutil.rmtree(runtime_dir, ignore_errors=True)


# ── Audio helpers ─────────────────────────────────────────────────────────────

async def _drain_audio_chunks(page, chunks_store: list, session_id: str):
    """Background task: drain base64 audio chunks from the page every 10 s."""
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


# ── Google Meet helpers ───────────────────────────────────────────────────────

async def _take_debug_screenshot(page, session_id: str, label: str) -> str:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"{session_id}_{label}.png"
    with suppress(Exception):
        await page.screenshot(path=str(path), full_page=True)
    return str(path)


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

    for selector in [
        'input[aria-label="Your name"]',
        'input[placeholder="Your name"]',
        'input[aria-label*="name"]',
        'input[type="text"]',
    ]:
        with suppress(Exception):
            field = page.locator(selector).first
            if await field.is_visible(timeout=1500):
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


async def _has_google_session(page) -> bool:
    for sel in ['a[aria-label*="Google Account"]', 'button[aria-label*="Google Account"]',
                'img[alt*="Google Account"]', '[data-email]']:
        with suppress(Exception):
            if await page.locator(sel).first.is_visible(timeout=1200):
                return True
    for text in ["Sign in", "Choose an account"]:
        with suppress(Exception):
            if await page.get_by_text(text).is_visible(timeout=800):
                return False
    return False


async def _ensure_google_profile_ready(context, session_id: str, profile_dir: Optional[Path] = None):
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://meet.google.com/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    if await _has_google_session(page):
        return page
    screenshot_path = await _take_debug_screenshot(page, session_id, "google_login_required")
    raise RuntimeError(
        "Recorder profile is not signed into Google. "
        f"Bootstrap the recorder account at '{profile_dir or PROFILE_DIR}'. "
        f"Debug screenshot: {screenshot_path}"
    )


# ── Zoom helpers ──────────────────────────────────────────────────────────────

async def _prepare_zoom(page):
    await page.goto(page.url, wait_until="domcontentloaded", timeout=60000)
    for text in ["Join from Your Browser", "join from your browser"]:
        with suppress(Exception):
            await page.get_by_text(text).click(timeout=3000)
            break


async def _join_zoom(page, bot_name: str, session_id: str):
    for selector in ['input[aria-label="Your Name"]', 'input[name="inputname"]', 'input[type="text"]']:
        with suppress(Exception):
            field = page.locator(selector).first
            if await field.is_visible(timeout=1500):
                await field.fill(bot_name)
                break

    for label in ["Join", "Join Meeting"]:
        with suppress(Exception):
            await page.get_by_role("button", name=label).click(timeout=2500)
            _status(session_id, "joining")
            return
    raise RuntimeError("Could not find the Zoom join button.")


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
                    "The meeting may require signed-in participants only."
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
        ended = ["You have left the meeting", "This meeting has ended", "Join Meeting"]

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
                return True
    return False


# ── Main Playwright session ───────────────────────────────────────────────────

async def _run_playwright_session(ctx: BotRunContext):
    session = get_session(ctx.session_id)
    if not session:
        return

    if async_playwright is None:
        _failure(ctx.session_id, "Playwright is not installed. Run: pip install playwright && playwright install chromium")
        return

    _status(ctx.session_id, "preparing", started_at=datetime.utcnow(), failure_reason=None)
    _log(ctx.session_id, f"Preparing browser session for {ctx.platform} — {ctx.meeting_url}")

    runtime_profile_dir: Optional[Path] = None
    audio_chunks: list[str] = []
    drain_task: Optional[asyncio.Task] = None

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        runtime_profile_dir = _prepare_runtime_profile(ctx.session_id, ctx.platform, ctx.profile_dir)

        # Chromium launch flags — tuned for headless meeting capture
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

        async with async_playwright() as pw:
            context = None
            browser  = None
            page     = None

            try:
                if ctx.platform == "google_meet":
                    context = await pw.chromium.launch_persistent_context(
                        user_data_dir=str(runtime_profile_dir),
                        headless=True,
                        viewport={"width": 1440, "height": 900},
                        permissions=["camera", "microphone"],
                        ignore_https_errors=True,
                        args=chromium_args,
                    )
                else:
                    browser = await pw.chromium.launch(
                        headless=True,
                        args=chromium_args,
                    )
                    context = await browser.new_context(
                        viewport={"width": 1440, "height": 900},
                        permissions=["camera", "microphone"],
                        ignore_https_errors=True,
                    )

                # Inject audio capture script into every page/frame before content loads
                await context.add_init_script(_AUDIO_CAPTURE_SCRIPT)
                _log(ctx.session_id, "Audio capture script registered")

                if ctx.platform == "google_meet":
                    page = await _ensure_google_profile_ready(context, ctx.session_id, ctx.profile_dir)
                else:
                    page = context.pages[0] if context.pages else await context.new_page()

                await page.goto(ctx.meeting_url, wait_until="domcontentloaded", timeout=60000)
                await _take_debug_screenshot(page, ctx.session_id, "landing")

                if ctx.platform == "google_meet":
                    await _prepare_google_meet(page, ctx.meeting_url, ctx.bot_name)
                    await _take_debug_screenshot(page, ctx.session_id, "prejoin")
                    await _join_google_meet(page, ctx.session_id)
                elif ctx.platform == "zoom":
                    await _prepare_zoom(page)
                    await _take_debug_screenshot(page, ctx.session_id, "prejoin")
                    await _join_zoom(page, ctx.bot_name, ctx.session_id)
                else:
                    raise RuntimeError(f"Unsupported platform: {ctx.platform}")

                joined = await _wait_for_admission(page, ctx.session_id, ctx.platform)
                screenshot_path = await _take_debug_screenshot(page, ctx.session_id, "postjoin")

                if not joined:
                    _failure(
                        ctx.session_id,
                        f"Bot could not confirm admission to the meeting. "
                        f"Debug screenshot: {screenshot_path}",
                        recording_path=screenshot_path,
                    )
                    return

                _log(ctx.session_id, "Joined meeting — audio capture active")

                # Start background audio drain
                drain_task = asyncio.create_task(
                    _drain_audio_chunks(page, audio_chunks, ctx.session_id)
                )

                # Wait for meeting to end (or timeout / user cancel)
                await _wait_for_recording_completion(page, ctx.session_id, ctx.platform)

            except Exception as exc:
                screenshot_path = None
                with suppress(Exception):
                    screenshot_path = await _take_debug_screenshot(page, ctx.session_id, "error")
                _failure(
                    ctx.session_id,
                    f"Browser automation error: {exc}",
                    recording_path=screenshot_path,
                )

            finally:
                # Stop drain task
                if drain_task and not drain_task.done():
                    drain_task.cancel()
                    with suppress(Exception):
                        await drain_task

                # Ask recorder to flush its last chunk, then wait for it
                with suppress(Exception):
                    await page.evaluate("if (window.__maStop) window.__maStop()")
                with suppress(Exception):
                    await page.wait_for_timeout(1500)
                # Final drain
                with suppress(Exception):
                    final = await page.evaluate("window.__maDrain ? window.__maDrain() : []")
                    audio_chunks.extend(final)
                    _log(ctx.session_id, f"Final drain: {len(final)} chunk(s), total {len(audio_chunks)}")

                # Leave and close browser
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
        current_status = _current_status(ctx.session_id)
        if current_status in {"failed", "cancelled"}:
            return

        audio_path = str(CAPTURE_DIR / f"{ctx.session_id}.webm")
        audio_ok = _save_audio_chunks(audio_chunks, audio_path)

        if not audio_ok:
            _failure(
                ctx.session_id,
                "Audio capture produced no data. The meeting may have had no audio, "
                "or the browser was blocked from processing WebRTC streams. "
                "Try running with MEETING_BOT_MODE=playwright and ensuring the bot was admitted.",
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
    finally:
        _cleanup_runtime_profile(runtime_profile_dir)


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

    bot_account = get_bot_account(session.bot_account_id) if session.bot_account_id else None
    ctx = BotRunContext(
        session_id=session.id,
        meeting_url=session.meeting_url,
        bot_name=session.bot_name or MEETING_BOT_RECORDER_NAME,
        platform=platform,
        profile_dir=Path(bot_account.profile_dir) if bot_account else None,
    )
    asyncio.run(_run_playwright_session(ctx))


def start_meeting_bot_session(session_id: str):
    worker = threading.Thread(target=_run_session, args=(session_id,), daemon=True)
    worker.start()


def start_pending_meeting_bot_sessions():
    for session in list_startable_sessions():
        start_meeting_bot_session(session.id)
