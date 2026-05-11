import asyncio
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import imageio_ffmpeg

from models.meeting_bot import get_session, list_startable_sessions, update_session
from models.bot_account import get_bot_account
from services.meeting_bots.platforms import detect_platform
from services.processing_pipeline import create_processing_job_from_media

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover - handled at runtime
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
MEETING_BOT_CAPTURE_MODE = os.environ.get("MEETING_BOT_CAPTURE_MODE", "playwright_video").strip().lower()
MEETING_BOT_CAPTURE_COMMAND_TEMPLATE = os.environ.get("MEETING_BOT_CAPTURE_COMMAND_TEMPLATE", "").strip()
MEETING_BOT_RECORD_SECONDS = int(os.environ.get("MEETING_BOT_RECORD_SECONDS", "1800"))
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
GOOGLE_MEET_BLOCK_MESSAGES = [
    "You can't join this video call",
    "You can’t join this video call",
    "You can't join this call",
    "You can’t join this call",
]


@dataclass
class BotRunContext:
    session_id: str
    meeting_url: str
    bot_name: str
    platform: str
    profile_dir: Optional[Path] = None


def _status(session_id: str, status: str, **extra):
    payload = {"status": status, **extra}
    update_session(session_id, **payload)


def _failure(session_id: str, message: str, **extra):
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


def _runtime_profile_ignore(dirpath: str, names: list[str]) -> set[str]:
    volatile_dirs = {
        "Cache",
        "Code Cache",
        "GPUCache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
        "GrShaderCache",
        "GraphiteDawnCache",
        "ShaderCache",
        "Safe Browsing",
        "Safe Browsing Network",
        "Service Worker",
        "Shared Dictionary",
        "Segmentation Platform",
        "Session Storage",
        "Sessions",
        "AutofillAiModelCache",
        "WebrtcVideoStats",
        "blob_storage",
    }
    volatile_files = {
        "LOCK",
        "lockfile",
        "DevToolsActivePort",
        "CrashpadMetrics-active.pma",
        "chrome_debug.log",
        "LOG",
        "LOG.old",
    }
    ignored = set()
    for name in names:
        if name in volatile_dirs or name in volatile_files or name.startswith("Singleton"):
            ignored.add(name)
    return ignored


def _prepare_runtime_profile(session_id: str, platform: str, profile_dir: Optional[Path] = None) -> Optional[Path]:
    if platform != "google_meet":
        return None

    source_profile_dir = profile_dir or PROFILE_DIR
    if not source_profile_dir.exists():
        if profile_dir and PROFILE_DIR.exists():
            source_profile_dir = PROFILE_DIR
        else:
            raise RuntimeError(
                f"The recorder profile directory does not exist at '{source_profile_dir}'. "
                "Sign this bot account into Google or bootstrap the shared recorder profile before using bot joins."
            )

    RUNTIME_PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    runtime_dir = Path(tempfile.mkdtemp(prefix=f"{session_id}_", dir=str(RUNTIME_PROFILE_ROOT)))

    try:
        shutil.rmtree(runtime_dir)
        shutil.copytree(
            source_profile_dir,
            runtime_dir,
            ignore=_runtime_profile_ignore,
        )
    except shutil.Error as exc:
        locked_cookie = any(
            "Cookies" in str(item) and ("WinError 32" in str(item) or "being used by another process" in str(item))
            for item in getattr(exc, "args", [])
        )
        if locked_cookie:
            with suppress(Exception):
                shutil.rmtree(runtime_dir, ignore_errors=True)
            raise RuntimeError(
                f"The recorder profile at '{source_profile_dir}' is still open in another Chromium/Playwright process. "
                "Close the bootstrap browser window, press Enter in the bootstrap script so it finishes, then retry the bot session."
            ) from exc
        with suppress(Exception):
            shutil.rmtree(runtime_dir, ignore_errors=True)
        raise
    except Exception:
        with suppress(Exception):
            shutil.rmtree(runtime_dir, ignore_errors=True)
        raise

    return runtime_dir


def _cleanup_runtime_profile(runtime_dir: Optional[Path]):
    if not runtime_dir:
        return
    with suppress(Exception):
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _capture_mode_failure() -> str:
    return (
        f"Meeting bot capture mode '{MEETING_BOT_CAPTURE_MODE}' is not implemented yet. "
        "Configure a real audio-capable recorder backend before sending sessions into transcription."
    )


def _capture_has_audio_stream(media_path: str) -> bool:
    cmd = [FFMPEG, "-i", media_path, "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr or ""
    return "Audio:" in stderr


def _validate_capture_media(media_path: str) -> Optional[str]:
    if MEETING_BOT_CAPTURE_MODE not in {"playwright_video", "external_ffmpeg"}:
        return _capture_mode_failure()
    if not _capture_has_audio_stream(media_path):
        return (
            "Recorder capture failed: the saved media was video-only and did not contain a usable audio stream. "
            "Configure MEETING_BOT_CAPTURE_MODE=external_ffmpeg with an audio-capable capture command."
        )
    return None


def _external_capture_output_path(session_id: str) -> Path:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    return CAPTURE_DIR / f"{session_id}_external_capture.mkv"


def _start_external_capture(session_id: str) -> tuple[Optional[subprocess.Popen], Optional[str]]:
    if MEETING_BOT_CAPTURE_MODE != "external_ffmpeg":
        return None, None
    if not MEETING_BOT_CAPTURE_COMMAND_TEMPLATE:
        raise RuntimeError(
            "MEETING_BOT_CAPTURE_MODE=external_ffmpeg requires MEETING_BOT_CAPTURE_COMMAND_TEMPLATE."
        )

    output_path = _external_capture_output_path(session_id)
    command = MEETING_BOT_CAPTURE_COMMAND_TEMPLATE.format(
        output=str(output_path),
        session_id=session_id,
        record_seconds=MEETING_BOT_RECORD_SECONDS,
    )
    process = subprocess.Popen(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return process, str(output_path)


def _stop_external_capture(process: Optional[subprocess.Popen]):
    if not process:
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def _dismiss_google_meet_prejoin(page):
    dismiss_labels = [
        "Continue without microphone",
        "Continue without camera",
        "Dismiss",
        "Got it",
    ]
    for label in dismiss_labels:
        with suppress(Exception):
            await page.get_by_role("button", name=label).click(timeout=1000)


async def _maybe_turn_off_google_meet_av(page):
    toggle_labels = [
        "Turn off microphone",
        "Turn off camera",
        "Mute microphone",
        "Turn off your microphone",
        "Turn off your camera",
    ]
    for label in toggle_labels:
        with suppress(Exception):
            await page.get_by_role("button", name=label).click(timeout=1500)


async def _prepare_google_meet(page, meeting_url: str, bot_name: str):
    await page.goto(meeting_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    await _dismiss_google_meet_prejoin(page)
    await _maybe_turn_off_google_meet_av(page)

    name_selectors = [
        'input[aria-label="Your name"]',
        'input[placeholder="Your name"]',
        'input[aria-label*="name"]',
        'input[type="text"]',
    ]
    for selector in name_selectors:
        with suppress(Exception):
            field = page.locator(selector).first
            if await field.is_visible(timeout=1500):
                await field.click()
                await field.fill(bot_name)
                return


async def _join_google_meet(page, session_id: str):
    join_labels = ["Ask to join", "Join now", "Request to join", "Join meeting"]
    for label in join_labels:
        with suppress(Exception):
            button = page.get_by_role("button", name=label)
            await button.click(timeout=2000)
            _status(session_id, "joining")
            return
    raise RuntimeError("Could not find the Google Meet join button.")


async def _detect_google_meet_block(page) -> Optional[str]:
    for message in GOOGLE_MEET_BLOCK_MESSAGES:
        with suppress(Exception):
            if await page.get_by_text(message).is_visible(timeout=1000):
                return message
    return None


async def _has_google_session(page) -> bool:
    signed_in_selectors = [
        'a[aria-label*="Google Account"]',
        'button[aria-label*="Google Account"]',
        'img[alt*="Google Account"]',
        '[data-email]',
    ]
    for selector in signed_in_selectors:
        with suppress(Exception):
            if await page.locator(selector).first.is_visible(timeout=1200):
                return True

    sign_in_texts = ["Sign in", "Choose an account"]
    for text in sign_in_texts:
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
        "The shared recorder profile is not signed into Google. "
        f"Sign the recorder account into the profile at '{profile_dir or PROFILE_DIR}' and retry. "
        f"Debug screenshot saved to {screenshot_path}."
    )


async def _prepare_zoom(page):
    await page.goto(page.url, wait_until="domcontentloaded", timeout=60000)
    launch_web_text = [
        "Join from Your Browser",
        "join from your browser",
    ]
    for text in launch_web_text:
        with suppress(Exception):
            await page.get_by_text(text).click(timeout=3000)
            break


async def _join_zoom(page, bot_name: str, session_id: str):
    name_inputs = [
        'input[aria-label="Your Name"]',
        'input[name="inputname"]',
        'input[type="text"]',
    ]
    for selector in name_inputs:
        with suppress(Exception):
            field = page.locator(selector).first
            if await field.is_visible(timeout=1500):
                await field.fill(bot_name)
                break

    join_labels = ["Join", "Join Meeting"]
    for label in join_labels:
        with suppress(Exception):
            await page.get_by_role("button", name=label).click(timeout=2500)
            _status(session_id, "joining")
            return
    raise RuntimeError("Could not find the Zoom join button.")


async def _wait_for_admission(page, session_id: str, platform: str):
    _status(session_id, "waiting_for_host")
    deadline_seconds = 90 if platform == "google_meet" else 45

    for _ in range(deadline_seconds // 3):
        if _current_status(session_id) in {"cancelled", "processing"}:
            return False

        if platform == "google_meet":
            blocked_message = await _detect_google_meet_block(page)
            if blocked_message:
                raise RuntimeError(
                    f"Google Meet blocked the bot from joining: {blocked_message}. "
                    "This usually means the meeting only allows invited or signed-in participants."
                )

        if platform == "google_meet":
            known_joined_signals = [
                page.get_by_role("button", name="Leave call"),
                page.get_by_role("button", name="Leave meeting"),
                page.get_by_role("button", name="End call"),
                page.get_by_text("You joined"),
            ]
        else:
            known_joined_signals = [
                page.get_by_role("button", name="Leave"),
                page.get_by_text("Mute"),
                page.get_by_text("Participants"),
            ]

        for locator in known_joined_signals:
            with suppress(Exception):
                if await locator.is_visible(timeout=1200):
                    _status(session_id, "recording")
                    return True

        await page.wait_for_timeout(3000)

    return False


async def _leave_meeting(page, platform: str):
    if platform == "google_meet":
        leave_labels = ["Leave call", "Leave meeting", "End call"]
    else:
        leave_labels = ["Leave", "Leave Meeting", "End Meeting"]

    for label in leave_labels:
        with suppress(Exception):
            button = page.get_by_role("button", name=label)
            if await button.is_visible(timeout=1200):
                await button.click(timeout=2000)
                await page.wait_for_timeout(1500)
                return True

    return False


async def _is_still_in_meeting(page, platform: str) -> bool:
    if platform == "google_meet":
        active_locators = [
            page.get_by_role("button", name="Leave call"),
            page.get_by_role("button", name="Leave meeting"),
            page.get_by_role("button", name="End call"),
        ]
        ended_texts = [
            "You left the meeting",
            "Call ended",
            "Return to home screen",
            "Ready to join?",
        ]
    else:
        active_locators = [
            page.get_by_role("button", name="Leave"),
            page.get_by_text("Mute"),
            page.get_by_text("Participants"),
        ]
        ended_texts = [
            "You have left the meeting",
            "This meeting has ended",
            "Join Meeting",
        ]

    for locator in active_locators:
        with suppress(Exception):
            if await locator.is_visible(timeout=800):
                return True

    for text in ended_texts:
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


async def _take_debug_screenshot(page, session_id: str, label: str) -> str:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"{session_id}_{label}.png"
    await page.screenshot(path=str(path), full_page=True)
    return str(path)


async def _run_playwright_session(ctx: BotRunContext):
    session = get_session(ctx.session_id)
    if not session:
        return

    if async_playwright is None:
        _failure(ctx.session_id, "Playwright is not installed in the backend environment.")
        return

    _status(ctx.session_id, "preparing", started_at=datetime.utcnow(), failure_reason=None)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    runtime_profile_dir: Optional[Path] = None

    try:
        runtime_profile_dir = _prepare_runtime_profile(ctx.session_id, ctx.platform, ctx.profile_dir)

        async with async_playwright() as playwright:
            context = None
            browser = None
            page = None
            external_capture_process = None
            external_capture_path = None
            try:
                if ctx.platform == "google_meet":
                    context = await playwright.chromium.launch_persistent_context(
                        user_data_dir=str(runtime_profile_dir),
                        headless=True,
                        viewport={"width": 1440, "height": 1024},
                        permissions=["camera", "microphone"],
                        ignore_https_errors=True,
                        record_video_dir=str(CAPTURE_DIR),
                        record_video_size={"width": 1280, "height": 720},
                        args=[
                            "--use-fake-ui-for-media-stream",
                            "--use-fake-device-for-media-stream",
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                        ],
                    )
                    page = await _ensure_google_profile_ready(context, ctx.session_id, ctx.profile_dir)
                else:
                    browser = await playwright.chromium.launch(
                        headless=True,
                        args=[
                            "--use-fake-ui-for-media-stream",
                            "--use-fake-device-for-media-stream",
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                        ],
                    )
                    context = await browser.new_context(
                        viewport={"width": 1440, "height": 1024},
                        permissions=["camera", "microphone"],
                        ignore_https_errors=True,
                        record_video_dir=str(CAPTURE_DIR),
                        record_video_size={"width": 1280, "height": 720},
                    )
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
                    raise RuntimeError(f"Unsupported platform '{ctx.platform}'.")

                joined = await _wait_for_admission(page, ctx.session_id, ctx.platform)
                screenshot_path = await _take_debug_screenshot(page, ctx.session_id, "postjoin")

                if not joined:
                    _failure(
                        ctx.session_id,
                        f"The bot could not confirm admission to the {ctx.platform.replace('_', ' ')} meeting. "
                        f"Check the saved debug screenshot at {screenshot_path}.",
                        recording_path=screenshot_path,
                    )
                    return

                external_capture_process, external_capture_path = _start_external_capture(ctx.session_id)
                await _wait_for_recording_completion(page, ctx.session_id, ctx.platform)
            except Exception as exc:
                screenshot_path: Optional[str] = None
                with suppress(Exception):
                    screenshot_path = await _take_debug_screenshot(page, ctx.session_id, "error")
                _failure(
                    ctx.session_id,
                    f"Meeting bot failed during browser automation: {exc}",
                    recording_path=screenshot_path,
                )
            finally:
                video_path: Optional[str] = None
                video = None
                _stop_external_capture(external_capture_process)
                if context is not None and page is not None:
                    with suppress(Exception):
                        await _leave_meeting(page, ctx.platform)
                    with suppress(Exception):
                        video = page.video
                elif page is not None:
                    with suppress(Exception):
                        video = page.video
                if context is not None:
                    with suppress(Exception):
                        await context.close()
                if browser is not None:
                    with suppress(Exception):
                        await browser.close()
                if video is not None:
                    with suppress(Exception):
                        video_path = await video.path()
                if external_capture_path and os.path.exists(external_capture_path):
                    video_path = external_capture_path

        if video_path and os.path.exists(video_path):
            try:
                capture_error = _validate_capture_media(video_path)
                if capture_error:
                    _failure(
                        ctx.session_id,
                        capture_error,
                        recording_path=video_path,
                    )
                    return

                processing_job_id = create_processing_job_from_media(
                    media_path=video_path,
                    original_filename=f"{ctx.session_id}.webm",
                    user_id=session.user_id,
                    workspace_id=session.workspace_id,
                )
                update_session(
                    ctx.session_id,
                    status="completed",
                    ended_at=datetime.utcnow(),
                    recording_path=video_path,
                    job_id=processing_job_id,
                    failure_reason=None,
                )
                return
            except Exception as exc:
                _failure(
                    ctx.session_id,
                    f"The bot captured a recording but could not create a processing job: {exc}",
                    recording_path=video_path,
                )
                return

        _failure(
            ctx.session_id,
            "The bot reached the recording stage, but no browser recording file was produced.",
        )
    except Exception as exc:
        _failure(
            ctx.session_id,
            f"Meeting bot failed before or during launch: {exc}",
        )
    finally:
        _cleanup_runtime_profile(runtime_profile_dir)


def _run_session(session_id: str):
    session = get_session(session_id)
    if not session:
        return

    if session.status == "cancelled":
        return

    if session.scheduled_for:
        while True:
            latest = get_session(session_id)
            if not latest or latest.status == "cancelled":
                return

            seconds_until_start = (latest.scheduled_for - datetime.utcnow()).total_seconds()
            if seconds_until_start <= 0:
                break

            time.sleep(min(15, max(1, seconds_until_start)))

    if MEETING_BOT_MODE == "stub":
        update_session(
            session_id,
            status="failed",
            ended_at=datetime.utcnow(),
            failure_reason=(
                "Meeting bot runner is set to stub mode. Set MEETING_BOT_MODE=playwright after "
                "installing Chromium to use the real browser automation path."
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
