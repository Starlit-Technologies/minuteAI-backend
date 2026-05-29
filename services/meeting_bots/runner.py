import asyncio
import base64
import os
import queue as _stdlib_queue
import re
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

import imageio_ffmpeg

from models.meeting_bot import (
    append_session_log,
    get_session,
    list_startable_sessions,
    update_session,
)
from services.meeting_bots.platforms import detect_platform
from services.pipeline.processing_pipeline import create_processing_job_from_media

try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None


DEBUG_DIR   = Path(__file__).parent.parent.parent / "storage" / "meeting_bot_debug"
CAPTURE_DIR = Path(__file__).parent.parent.parent / "storage" / "meeting_bot_captures"

# ── Live translate audio registry ─────────────────────────────────────────────
# Maps bot session_id → asyncio.Queue[bytes | None] when a client is listening.
# None is the sentinel that ends the stream.
_live_audio_queues: dict[str, asyncio.Queue] = {}

# ── Bot live listener audio registry ──────────────────────────────────────────
# Separate fan-out target for BotLiveListener (always-on transcription + wake word).
_bot_listener_queues: dict[str, asyncio.Queue] = {}

# ── Stop-signal set ───────────────────────────────────────────────────────────
# The /end API route adds a session_id here so the recording loop wakes up
# immediately instead of waiting for the next 1-second DB poll.
_stop_requested: set[str] = set()

# Track sessions that have an active thread so we never double-launch.
_running_sessions: set[str] = set()
_running_lock = threading.Lock()


def signal_stop(session_id: str) -> None:
    """Called by the /end route to make the recording loop exit immediately."""
    _stop_requested.add(session_id)


def register_bot_audio_listener(session_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _live_audio_queues[session_id] = q
    return q


def unregister_bot_audio_listener(session_id: str) -> None:
    q = _live_audio_queues.pop(session_id, None)
    if q:
        try:
            q.put_nowait(None)  # wake up any waiting consumer
        except Exception:
            pass


def register_bot_listener_queue(session_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _bot_listener_queues[session_id] = q
    return q


def unregister_bot_listener_queue(session_id: str) -> None:
    q = _bot_listener_queues.pop(session_id, None)
    if q:
        try:
            q.put_nowait(None)
        except Exception:
            pass


# ── TTS speak-in-meeting registry ─────────────────────────────────────────────
# Maps session_id → thread-safe queue of base64 MP3 payloads to play through
# the bot's virtual mic inside the Google Meet/Zoom call (public mode only).
_tts_queues: dict[str, _stdlib_queue.Queue] = {}


def register_tts_queue(session_id: str) -> "_stdlib_queue.Queue[str]":
    q: _stdlib_queue.Queue = _stdlib_queue.Queue(maxsize=20)
    _tts_queues[session_id] = q
    return q


def unregister_tts_queue(session_id: str) -> None:
    _tts_queues.pop(session_id, None)


def queue_tts(session_id: str, b64_audio: str) -> bool:
    """Called by the control-panel WS handler to speak audio in the meeting."""
    q = _tts_queues.get(session_id)
    if not q:
        return False
    try:
        q.put_nowait(b64_audio)
        return True
    except _stdlib_queue.Full:
        return False


GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

MEETING_BOT_MODE            = os.environ.get("MEETING_BOT_MODE", "playwright").strip().lower()
MEETING_BOT_RECORDER_NAME   = os.environ.get("MEETING_BOT_RECORDER_NAME", "MinuteAI Recorder").strip() or "MinuteAI Recorder"
MEETING_BOT_RECORD_SECONDS  = int(os.environ.get("MEETING_BOT_RECORD_SECONDS", "1800"))

# Env-var fallbacks — overridden at runtime by bot_config.json (set via admin panel).
MEETING_BOT_PROFILE_DIR = os.environ.get("MEETING_BOT_PROFILE_DIR", "").strip()
BOT_EMAIL               = os.environ.get("BOT_EMAIL", "").strip()
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

GOOGLE_MEET_BLOCK_MESSAGES = [
    "You can't join this video call",
    "You can't join this call",
    "This call is restricted",
]

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ── Anti-detection init script ────────────────────────────────────────────────
# Runs before any page content so Cloudflare and meeting platforms see a
# normal browser rather than headless automation.

_ANTIDETECT_SCRIPT = """
(function() {
    // Hide webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Realistic language / plugin lists
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', {
        get: () => Object.assign([
            { name: 'Chrome PDF Plugin',  filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer',  filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client',      filename: 'internal-nacl-plugin' }
        ], { length: 3 })
    });

    // chrome runtime stub (absent in headless)
    if (!window.chrome) {
        window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
    }

    // Permissions API stub so sites see a real browser
    const _query = window.Permissions && window.Permissions.prototype.query;
    if (_query) {
        window.Permissions.prototype.query = function(params) {
            if (params && params.name === 'notifications') {
                return Promise.resolve({ state: 'denied', onchange: null });
            }
            return _query.apply(this, arguments);
        };
    }

    // Spoof media device list — Chromium's fake devices expose "Fake Audio Input Device"
    // which Zoom's bot detection flags. Return realistic laptop device names instead.
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
        navigator.mediaDevices.enumerateDevices = function() {
            return Promise.resolve([
                { kind: 'audioinput',  label: 'Built-in Microphone',          deviceId: 'default',  groupId: 'built-in' },
                { kind: 'audiooutput', label: 'Built-in Speakers',             deviceId: 'default',  groupId: 'built-in' },
                { kind: 'videoinput',  label: 'FaceTime HD Camera (Built-in)', deviceId: 'facetime', groupId: 'camera'   },
            ]);
        };
    }
})();
"""

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

    // ── PCM16 capture for real-time live translate ──────────────────────────
    // Separate AudioContext at 16kHz so the browser resamples automatically.
    const pcmCtx    = new _AC({ sampleRate: 16000 });
    const pcmChunks = [];
    let   pcmActive = false;

    function _f32ToPcm16b64(f32) {
        const i16 = new Int16Array(f32.length);
        for (let i = 0; i < f32.length; i++) {
            const s = Math.max(-1, Math.min(1, f32[i]));
            i16[i] = s < 0 ? s * 32768 : s * 32767;
        }
        const bytes = new Uint8Array(i16.buffer);
        let bin = '';
        for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
        return btoa(bin);
    }

    window.__maPcmStart = function() {
        pcmActive = true;
        pcmCtx.resume().catch(function(){});
    };
    window.__maPcmStop  = function() { pcmActive = false; };
    window.__maPcmDrain = function() { return pcmChunks.splice(0); };

    const pcmConnected = new WeakSet();
    function connectTrackPcm(track) {
        if (track.kind !== 'audio' || pcmConnected.has(track)) return;
        pcmConnected.add(track);
        try {
            const src  = pcmCtx.createMediaStreamSource(new MediaStream([track]));
            const proc = pcmCtx.createScriptProcessor(4096, 1, 1);
            proc.onaudioprocess = function(e) {
                if (!pcmActive) return;
                pcmChunks.push(_f32ToPcm16b64(e.inputBuffer.getChannelData(0)));
            };
            src.connect(proc);
            proc.connect(pcmCtx.destination);
        } catch(_) {}
    }
    // ─────────────────────────────────────────────────────────────────────────

    const connected = new WeakSet();
    function connectTrack(track) {
        if (track.kind !== 'audio' || connected.has(track)) return;
        connected.add(track);
        try {
            const src = ctx.createMediaStreamSource(new MediaStream([track]));
            src.connect(dest);
            ensureRecorder();
        } catch(_) {}
        connectTrackPcm(track);
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

# ── TTS virtual-mic injection ─────────────────────────────────────────────────
# Patches getUserMedia to replace the real/fake mic track with an AudioContext
# MediaStreamDestination we fully control.  __stephSpeak(b64mp3) plays audio
# through that destination so all meeting participants hear Steph's voice.

_TTS_MIC_SCRIPT = """
(function() {
    if (window.__stephMicInstalled) return;
    window.__stephMicInstalled = true;

    const _AC = window.AudioContext || window.webkitAudioContext;
    if (!_AC) return;

    let _ctx = null, _dest = null;

    function _setup() {
        if (_dest) return;
        _ctx  = new _AC({ sampleRate: 48000 });
        _dest = _ctx.createMediaStreamDestination();
        // Ensure context starts running even without a user gesture
        [0, 200, 1000, 3000].forEach(function(ms) {
            setTimeout(function() { _ctx.state === 'suspended' && _ctx.resume().catch(function(){}); }, ms);
        });
    }

    // Intercept getUserMedia: swap any audio track for our virtual mic track
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        var _origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        navigator.mediaDevices.getUserMedia = function(constraints) {
            _setup();
            return _origGUM(constraints).then(function(stream) {
                if (constraints && constraints.audio) {
                    var virt = _dest.stream.getAudioTracks()[0];
                    if (virt) {
                        stream.getAudioTracks().forEach(function(t) {
                            stream.removeTrack(t);
                            try { t.stop(); } catch(_) {}
                        });
                        // Clone so each caller gets an independent track reference
                        stream.addTrack(virt.clone());
                    }
                }
                return stream;
            });
        };
    }

    // Play base64 MP3 through the virtual mic so the room hears Steph
    window.__stephSpeak = function(b64mp3) {
        _setup();
        return _ctx.resume().catch(function(){}).then(function() {
            var bytes = Uint8Array.from(atob(b64mp3), function(c) { return c.charCodeAt(0); });
            return _ctx.decodeAudioData(bytes.buffer);
        }).then(function(audioBuffer) {
            var src = _ctx.createBufferSource();
            src.buffer = audioBuffer;
            src.connect(_dest);
            src.start();
        }).catch(function(e) { console.warn('Steph speak error:', e); });
    };
})();
"""


@dataclass
class BotRunContext:
    session_id: str
    user_id: str
    meeting_url: str
    bot_name: str
    platform: str
    source: str = ""       # "user_calendar", "manual", etc.
    user_name: str = ""    # display name of the user who booked the bot


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_user_display_name(user_id: str) -> str:
    """Return the user's display name, or empty string if not found."""
    try:
        from models.user import User
        from db.base import SessionLocal
        with SessionLocal() as db:
            user = db.get(User, user_id)
            return (user.name or "").strip() if user else ""
    except Exception:
        return ""


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


def _flag_auth_failure(reason: str) -> None:
    """Write an unhealthy health status immediately when sign-in fails mid-session."""
    try:
        from datetime import datetime as _dt
        from services.meeting_bots.health_check import _save, _maybe_send_alert
        result = {
            "healthy":    False,
            "checked_at": _dt.utcnow().isoformat(),
            "email":      None,
            "error":      reason,
        }
        _save(result)
        _maybe_send_alert(result)
    except Exception:
        pass


def _current_status(session_id: str) -> Optional[str]:
    s = get_session(session_id)
    return s.status if s else None


def _capture_has_audio_stream(media_path: str) -> bool:
    try:
        result = subprocess.run(
            [FFMPEG, "-i", media_path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        return "Audio:" in (result.stderr or "")
    except subprocess.TimeoutExpired:
        return False


# ── Audio helpers ─────────────────────────────────────────────────────────────

async def _drain_audio_chunks(page, chunks_store: list, session_id: str):
    while True:
        await asyncio.sleep(10)
        try:
            new_total = 0
            # Drain from main frame and all child frames — Zoom creates WebRTC
            # connections inside iframes so their audio lives in the iframe's context.
            for frame in [page] + list(page.frames):
                try:
                    new = await frame.evaluate("window.__maDrain ? window.__maDrain() : []")
                    if new:
                        chunks_store.extend(new)
                        new_total += len(new)
                except Exception:
                    pass
            if new_total:
                _log(session_id, f"Drained {new_total} audio chunk(s) ({len(chunks_store)} total)")
        except Exception as exc:
            _log(session_id, f"Audio drain error: {exc}")
            break


async def _drain_pcm_for_live_translate(page, session_id: str):
    """
    Fast drain loop (250ms) for PCM audio chunks.  Activates the browser-side
    ScriptProcessor when any consumer is registered (live-translate client OR
    the always-on BotLiveListener).  Fans out to both queues when present.
    """
    pcm_was_active = False
    while True:
        await asyncio.sleep(0.25)
        q_translate = _live_audio_queues.get(session_id)
        q_listener  = _bot_listener_queues.get(session_id)
        has_consumer = q_translate is not None or q_listener is not None
        try:
            if not has_consumer:
                if pcm_was_active:
                    await page.evaluate("window.__maPcmStop && window.__maPcmStop()")
                    pcm_was_active = False
                continue

            if not pcm_was_active:
                await page.evaluate("window.__maPcmStart && window.__maPcmStart()")
                pcm_was_active = True

            chunks = await page.evaluate("window.__maPcmDrain ? window.__maPcmDrain() : []")
            for b64 in (chunks or []):
                try:
                    pcm = base64.b64decode(b64)
                    if q_translate is not None:
                        q_translate.put_nowait(pcm)
                    if q_listener is not None:
                        q_listener.put_nowait(pcm)
                except Exception:
                    pass
        except Exception:
            pass


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
    # "Got it" dismisses the "Sign in with your Google account" tooltip
    for label in ["Got it", "Continue without microphone", "Continue without camera", "Dismiss"]:
        with suppress(Exception):
            await page.get_by_role("button", name=label, exact=True).click(timeout=1500)
            await page.wait_for_timeout(400)
    # Text-based fallback in case role detection misses it
    with suppress(Exception):
        await page.locator("button", has_text="Got it").first.click(timeout=1000)
        await page.wait_for_timeout(400)


async def _dismiss_google_account_picker(page, session_id: str):
    """
    Dismiss 'Choose an account' or 'Continue as X' prompts that appear when the
    persistent profile has multiple Google accounts or the URL contains ?authuser=N.
    """
    # "Choose an account" list — click the first account entry
    for selector in [
        "li[data-identifier]",
        "div[data-authuser]",
        "div[jsname='ik5nac']",  # account row in newer chooser UI
    ]:
        with suppress(Exception):
            el = page.locator(selector).first
            if await el.is_visible(timeout=1500):
                _log(session_id, f"Account picker detected ({selector}) — clicking first account")
                await el.click(timeout=2000)
                await page.wait_for_timeout(2500)
                return

    # Single-account "Continue as [name]" button
    for label in ["Continue", "Use this account", "Confirm"]:
        with suppress(Exception):
            btn = page.get_by_role("button", name=label, exact=False).first
            if await btn.is_visible(timeout=1000):
                _log(session_id, f"Account chooser: clicking '{label}'")
                await btn.click(timeout=2000)
                await page.wait_for_timeout(2000)
                return


async def _maybe_turn_off_google_meet_av(page):
    for label in [
        "Turn off microphone", "Turn off camera", "Mute microphone",
        "Turn off your microphone", "Turn off your camera",
    ]:
        with suppress(Exception):
            await page.get_by_role("button", name=label).click(timeout=1500)


# JS used as the primary fill strategy: bypasses Playwright visibility/interactability
# checks and works regardless of attribute differences in headless mode.
_FILL_NAME_JS = """
(name) => {
    const candidates = [
        'input[jsname="YPqjbf"]',
        'input[aria-label="Your name"]',
        'input[placeholder="Your name"]',
        'input[aria-label*="name" i]',
        'input[placeholder*="name" i]',
        'input[type="text"]',
        'input:not([type])',
    ];
    const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    for (const sel of candidates) {
        const els = document.querySelectorAll(sel);
        for (const el of els) {
            if (el && el.getBoundingClientRect().width > 0) {
                setter.call(el, name);
                el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
            el.focus();
            return sel;
            }
        }
    }
    // Last resort: any visible input by bounding rect
    for (const el of document.querySelectorAll('input')) {
        const r = el.getBoundingClientRect();
        if (r.width > 50 && r.height > 10) {
            setter.call(el, name);
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
            el.focus();
            return 'input[rect-fallback]';
        }
    }
    return null;
}
"""

# Scans ALL buttons/role=button elements for any "join"-related text and clicks the first match.
# Used as the last-resort fallback when Playwright locators miss the button.
_CLICK_JOIN_BUTTON_JS = """
() => {
    const exact = /^(ask to join|join now|request to join|join meeting|join)$/i;
    const loose = /join/i;
    for (const pass of [exact, loose]) {
        for (const el of document.querySelectorAll('button, [role="button"]')) {
            const text  = (el.textContent || '').replace(/\\s+/g, ' ').trim();
            const label = (el.getAttribute('aria-label') || '').trim();
            if (pass.test(text) || pass.test(label)) {
                const r = el.getBoundingClientRect();
                if (r.width > 20 && r.height > 10) {
                    el.click();
                    return text || label;
                }
            }
        }
    }
    return null;
}
"""


async def _sign_in_google_meet_as_bot(page, refresh_token: str, session_id: str) -> bool:
    """
    Sign the bot account into a fresh browser context.

    Strategy (in order):
    1. **Stored session cookies** — injected directly via Playwright's
       ``context.add_cookies()``.  This is the most reliable method on
       production because Google deprecated the MergeSession/OAuthLogin
       endpoints that used to convert an access token into browser cookies.
    2. **MergeSession / OAuthLogin** — legacy fallback kept in case
       it still works for some account configurations.
    3. Returns False (guest mode) if nothing succeeds.
    """
    import requests as _req
    from models.bot_config import load_session_cookies

    # ── Step 1: Try stored session cookies ────────────────────────────────
    stored_cookies = load_session_cookies()
    if stored_cookies:
        try:
            # Playwright expects each cookie to have url OR (domain + path).
            # Normalise the cookie dicts so Playwright accepts them.
            normalised = []
            for c in stored_cookies:
                entry = {
                    "name":     c["name"],
                    "value":    c["value"],
                    "domain":   c.get("domain", ".google.com"),
                    "path":     c.get("path", "/"),
                    "httpOnly": c.get("httpOnly", False),
                    "secure":   c.get("secure", True),
                }
                if c.get("expires", -1) > 0:
                    entry["expires"] = c["expires"]
                if c.get("sameSite"):
                    site = c["sameSite"]
                    # Playwright expects title case: Strict, Lax, None
                    if site.lower() in {"strict", "lax", "none"}:
                        entry["sameSite"] = site.capitalize() if site.lower() != "none" else "None"
                normalised.append(entry)

            await page.context.add_cookies(normalised)
            _log(session_id, f"Injected {len(normalised)} stored session cookies")

            # Navigate to Google to verify the cookies actually work
            await page.goto("https://myaccount.google.com", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)

            cookies = await page.context.cookies(
                urls=["https://accounts.google.com", "https://google.com"]
            )
            has_session = any(c["name"] in {"SID", "__Secure-3PSID", "HSID", "SSID"} for c in cookies)
            on_signin = "signin" in page.url.lower() or "sign in" in (await page.title()).lower()

            if has_session and not on_signin:
                _log(session_id, "Bot sign-in confirmed via stored session cookies")
                return True
            else:
                _log(session_id, "Stored cookies injected but sign-in not confirmed — cookies may be expired")
        except Exception as exc:
            _log(session_id, f"Stored cookie injection failed: {exc}")

    # ── Step 2: Try OAuth token → MergeSession/OAuthLogin ────────────────
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        _log(session_id, "Bot OAuth sign-in skipped — no OAuth client credentials configured")
        return False

    try:
        resp = _req.post("https://oauth2.googleapis.com/token", data={
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        }, timeout=15)
        if not resp.ok:
            _log(session_id, f"Bot OAuth token refresh failed: HTTP {resp.status_code}")
            _flag_auth_failure("Bot OAuth refresh token is invalid or revoked. Re-connect in Admin → Bot Config → Connect Google.")
            return False
        access_token = resp.json().get("access_token")
        if not access_token:
            _log(session_id, "Bot OAuth token refresh returned no access_token")
            return False
    except Exception as exc:
        _log(session_id, f"Bot OAuth token refresh error: {exc}")
        return False

    async def _is_signed_in_now() -> bool:
        try:
            cookies = await page.context.cookies(
                urls=["https://accounts.google.com", "https://google.com"]
            )
            return any(c["name"] in {"SID", "__Secure-3PSID", "HSID", "SSID"} for c in cookies)
        except Exception:
            return False

    # ── Step 2: try legacy sign-in endpoints (may still work for some accounts) ──
    # Note: MergeSession/OAuthLogin are deprecated by Google. They are tried as a
    # best-effort fallback. The recommended path is stored session cookies (Step 1).
    candidates = [
        (
            "OAuthLogin",
            f"https://accounts.google.com/OAuthLogin"
            f"?source=ogb&issuedTo={GOOGLE_CLIENT_ID}&token={access_token}"
        ),
        (
            "MergeSession",
            f"https://accounts.google.com/MergeSession"
            f"?service=mail&continue=https://myaccount.google.com"
            f"&token={access_token}&source=googletalkplugin"
        ),
    ]

    for method, sign_in_url in candidates:
        try:
            await page.goto(sign_in_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2500)
            if await _is_signed_in_now():
                _log(session_id, f"Bot OAuth sign-in confirmed via {method}")
                return True
            await page.goto("https://myaccount.google.com", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1500)
            if "myaccount.google.com" in page.url and "signin" not in page.url:
                _log(session_id, f"Bot OAuth sign-in confirmed via {method} (myaccount)")
                return True
            _log(session_id, f"[{method}] bot sign-in not confirmed — url: {page.url!r}")
        except Exception as exc:
            _log(session_id, f"[{method}] bot sign-in attempt raised: {exc}")

    _log(
        session_id,
        "Bot OAuth sign-in failed — MergeSession/OAuthLogin are deprecated by Google. "
        "To fix: Admin → Bot Config → Export Cookies (locally) then Import Cookies on the server, "
        "OR use the persistent browser profile setup. Joining as guest."
    )
    _flag_auth_failure(
        "Bot Google sign-in failed. Google deprecated MergeSession/OAuthLogin. "
        "Fix: export session cookies from Admin → Bot Config on the local machine and import them on the server."
    )
    return False


async def _sign_in_google_meet(page, user_id: str, session_id: str) -> bool:
    """
    Converts the user's stored Google OAuth access token into a browser session
    so Meet sees an authenticated user. Tries MergeSession first, falls back to
    OAuthLogin, then verifies the result by navigating to myaccount.google.com.
    Returns True only when sign-in is actually confirmed.
    """
    import requests as _req
    from models.connected_account import get_connected_account

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        _log(session_id, "Google sign-in skipped — no OAuth client credentials configured")
        return False

    account = get_connected_account(user_id, "google")
    if not account or not account.refresh_token:
        _log(session_id, "Google sign-in skipped — no connected Google account for this user")
        return False

    # Refresh to get a fresh access token
    try:
        resp = _req.post("https://oauth2.googleapis.com/token", data={
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": account.refresh_token,
            "grant_type":    "refresh_token",
        }, timeout=15)
        if not resp.ok:
            _log(session_id, f"Google token refresh failed: HTTP {resp.status_code}")
            return False
        access_token = resp.json().get("access_token")
        if not access_token:
            _log(session_id, "Google token refresh returned no access_token")
            return False
    except Exception as exc:
        _log(session_id, f"Google token refresh error: {exc}")
        return False

    # Try multiple sign-in endpoints; stop at the first one that verifies
    candidates = [
        (
            "MergeSession",
            f"https://accounts.google.com/MergeSession"
            f"?service=mail&continue=https://myaccount.google.com"
            f"&token={access_token}&source=googletalkplugin"
        ),
        (
            "OAuthLogin",
            f"https://accounts.google.com/OAuthLogin"
            f"?source=ogb&issuedTo={GOOGLE_CLIENT_ID}&token={access_token}"
        ),
    ]

    async def _is_signed_in_now() -> bool:
        """Check for actual Google session cookies — more reliable than URL inspection."""
        try:
            cookies = await page.context.cookies(urls=["https://accounts.google.com", "https://google.com"])
            return any(c["name"] in {"SID", "__Secure-3PSID", "HSID", "SSID"} for c in cookies)
        except Exception:
            return False

    for method, sign_in_url in candidates:
        try:
            await page.goto(sign_in_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)
            _log(session_id, f"[{method}] landed — title: {await page.title()!r} url: {page.url!r}")

            # Primary verification: check for Google session cookies
            if await _is_signed_in_now():
                _log(session_id, f"Google sign-in confirmed via {method} (cookie check)")
                return True

            # Fallback verification: navigate to myaccount and check URL
            await page.goto("https://myaccount.google.com", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1500)
            final_url = page.url
            _log(session_id, f"[{method}] myaccount URL: {final_url!r}")

            if "myaccount.google.com" in final_url and "signin" not in final_url:
                _log(session_id, f"Google sign-in confirmed via {method} (myaccount check)")
                return True

            _log(session_id, f"[{method}] sign-in not confirmed")
        except Exception as exc:
            _log(session_id, f"[{method}] attempt raised: {exc}")

    _log(session_id, "All Google sign-in methods failed — joining as guest")
    _flag_auth_failure("All Google sign-in methods failed — profile session may have expired.")
    return False


async def _prepare_google_meet(page, meeting_url: str, bot_name: str, session_id: str, is_signed_in: bool = False, sign_in_ok: bool = False):
    await page.goto(meeting_url, wait_until="domcontentloaded", timeout=60000)
    # Signed-in lobbies take a bit longer to render the join button
    await page.wait_for_timeout(6000 if is_signed_in else 5000)
    _log(session_id, f"Google Meet page loaded — title: {await page.title()} url: {page.url}")

    # Handle account picker before anything else (appears with persistent profiles / ?authuser=N URLs)
    await _dismiss_google_account_picker(page, session_id)
    await _dismiss_google_meet_prejoin(page)
    await _maybe_turn_off_google_meet_av(page)
    await page.wait_for_timeout(1500)

    # Check for access block before spending more time
    blocked = await _detect_google_meet_block(page)
    if blocked:
        if sign_in_ok:
            hint = (
                "The bot signed in with your Google account but was still blocked — "
                "this meeting is restricted to specific accounts (e.g. a different org). "
                "Ask the meeting host to allow external participants."
            )
        else:
            hint = (
                "The bot could not sign in with your Google account and was blocked as a guest. "
                "Fix: go to Settings → Meeting Bot and re-run the bot setup, signing in with "
                "the Google account that has access to these meetings."
            )
        raise RuntimeError(f"Google Meet blocked the bot: '{blocked}'. {hint}")

    # Always attempt name fill — if the profile is genuinely signed in there will be no
    # input field and this silently no-ops. If the profile session expired and the page
    # fell back to guest mode the name MUST be filled or the join button stays disabled.

    # Primary: JavaScript fill (works regardless of Playwright visibility detection)
    with suppress(Exception):
        matched = await page.evaluate(_FILL_NAME_JS, bot_name)
        if matched:
            await page.wait_for_timeout(1500)
            _log(session_id, f"Filled guest name via JS ({matched})")
            return

    # Playwright fallback
    for selector in [
        'input[jsname="YPqjbf"]',
        'input[aria-label="Your name"]',
        'input[placeholder="Your name"]',
        'input[aria-label*="name" i]',
        'input[placeholder*="name" i]',
        'input[type="text"]',
    ]:
        with suppress(Exception):
            loc = page.locator(selector)
            count = await loc.count()
            for i in range(count):
                field = loc.nth(i)
                if await field.is_visible(timeout=500):
                    await field.triple_click()
                    await field.fill(bot_name)
                    await field.dispatch_event("input")
                    await field.dispatch_event("change")
                    await field.press("Tab")
                    await page.wait_for_timeout(1500)
                    _log(session_id, f"Filled guest name using selector: {selector}")
                    return

    if is_signed_in:
        _log(session_id, "No name input found — profile is signed in, proceeding to join button")
    else:
        _log(session_id, "Warning: could not find name input")


async def _join_google_meet(page, session_id: str):
    # Re-check for block — could have appeared after the page settled
    blocked = await _detect_google_meet_block(page)
    if blocked:
        raise RuntimeError(
            f"Google Meet blocked the bot: '{blocked}'. "
            "The meeting is restricted. Ask the host to invite the bot account or allow external participants."
        )

    # Wait up to 15 s for any known join button to appear in the DOM before trying to click.
    # This is critical for signed-in flow where the lobby renders the button after the
    # camera/mic preview loads — fixed sleeps miss it when the network is slow.
    _JOIN_BUTTON_CSS = (
        "button[jsname='w5gBed'], "   # "Ask to join"  (current Meet lobby)
        "button[jsname='d8E3Dd'], "   # "Join now"     (older variant)
        "button[jsname='V5AQZE'], "   # legacy join button
        "button[aria-label*='join' i]"
    )
    with suppress(Exception):
        await page.wait_for_selector(_JOIN_BUTTON_CSS, state="visible", timeout=15000)
        _log(session_id, "Join button visible in DOM")

    await page.wait_for_timeout(800)  # brief settle after button appears

    async def _try_click(btn) -> bool:
        """Click btn if visible; use JS-click if disabled."""
        if not await btn.is_visible(timeout=2000):
            return False
        enabled = await btn.is_enabled(timeout=500)
        if enabled:
            await btn.click(timeout=3000)
        else:
            await btn.evaluate("el => el.click()")
        _status(session_id, "joining")
        return True

    # 1. Role-based (most reliable when text is stable)
    for label in ["Ask to join", "Join now", "Request to join", "Join meeting", "Join"]:
        with suppress(Exception):
            btn = page.get_by_role("button", name=label, exact=False).first
            if await _try_click(btn):
                return

    # 2. jsname selectors
    for jsname in ["w5gBed", "d8E3Dd", "V5AQZE"]:
        with suppress(Exception):
            btn = page.locator(f"button[jsname='{jsname}']").first
            if await _try_click(btn):
                return

    # 3. Walk all buttons whose text contains "join"
    with suppress(Exception):
        btns = page.locator("button").filter(has_text=re.compile(r"join", re.IGNORECASE))
        count = await btns.count()
        for i in range(count):
            btn = btns.nth(i)
            if await _try_click(btn):
                return

    # 4. JS full-page scan — catches buttons that Playwright locators miss in headless mode
    with suppress(Exception):
        clicked_text = await page.evaluate(_CLICK_JOIN_BUTTON_JS)
        if clicked_text:
            _log(session_id, f"Joined via JS scan (button text: '{clicked_text}')")
            _status(session_id, "joining")
            return

    title = await page.title()
    raise RuntimeError(f"Could not find the Google Meet join button. Page title: '{title}' url: {page.url}")


async def _detect_google_meet_block(page) -> Optional[str]:
    for msg in GOOGLE_MEET_BLOCK_MESSAGES:
        with suppress(Exception):
            if await page.get_by_text(msg).is_visible(timeout=1000):
                return msg
    return None


# ── Zoom helpers ──────────────────────────────────────────────────────────────

_ZOOM_FILL_NAME_JS = """
(name) => {
    const candidates = [
        '#input-for-name',
        '#inputname',
        'input[name="inputname"]',
        'input[aria-label="Your Name"]',
        'input[placeholder="Your Name"]',
        'input[placeholder*="name" i]',
        'input[type="text"]',
    ];
    const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    for (const sel of candidates) {
        const el = document.querySelector(sel);
        if (el) {
            setter.call(el, name);
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
            el.focus();
            return sel;
        }
    }
    return null;
}
"""


def _zoom_is_in_meeting_url(page) -> bool:
    """True if the URL has moved past the /join page into the actual meeting."""
    return "/wc/" in page.url and "/join" not in page.url


def _zoom_web_client_url(meeting_url: str) -> Optional[str]:
    """Convert Zoom launcher links (/j/{id}) to the web-client join URL."""
    parsed = urlparse(meeting_url)
    if "zoom.us" not in parsed.netloc:
        return None

    match = re.search(r"/j/(\d+)", parsed.path)
    if not match:
        return None

    web_path = f"/wc/join/{match.group(1)}"
    return urlunparse(parsed._replace(path=web_path, params="", fragment=""))


async def _zoom_wait_past_interstitial(page, session_id: str, timeout_ms: int = 25000) -> bool:
    """Return True when Zoom has moved past the Cloudflare/launcher interstitial."""
    try:
        await page.wait_for_function(
            "() => document.title !== 'Just a moment...'",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        _log(session_id, f"Zoom interstitial still active after {timeout_ms // 1000}s")
        return False


async def _zoom_open_web_client(page, meeting_url: str, session_id: str) -> bool:
    web_client_url = _zoom_web_client_url(meeting_url)
    if not web_client_url or web_client_url == page.url:
        return False

    _log(session_id, f"Opening Zoom web client directly - {web_client_url}")
    await page.goto(web_client_url, wait_until="domcontentloaded", timeout=60000)
    await _zoom_wait_past_interstitial(page, session_id, timeout_ms=25000)
    await page.wait_for_timeout(2000)
    return True


async def _zoom_click_join_from_browser(page, session_id: str):
    """Click the 'Join from browser' button on Zoom's launcher page."""
    # Both URL formats mean the web client is already open — skip launcher.
    # New format: app.zoom.us/wc/join/{id}
    # Old format (after regional redirect): app.zoom.us/wc/{id}/join
    if "/wc/join/" in page.url or re.search(r"/wc/\d+/join", page.url):
        _log(session_id, "Zoom web client already open - skipping launcher join")
        return

    _log(session_id, f"Zoom launcher — title: {await page.title()} url: {page.url}")

    # Wait for the launcher to render before searching.
    # "Join from browser" is always the secondary (outline) button; the primary is the app button.
    with suppress(Exception):
        await page.wait_for_selector(
            "button.zoom-button--secondary, button:has-text('Join from browser'), a#btnBrowserJoin",
            timeout=12000,
        )

    selectors = [
        # From live HTML: secondary outline button = "Join from browser"
        "button.zoom-button--secondary",
        "button:has-text('Join from browser')",
        "button:has-text('Join from Browser')",
        "button:has-text('Join from Your Browser')",
        ".zoom-button--secondary:has-text('Join')",
        # Legacy anchor-based UI
        "a#btnBrowserJoin",
        "a#btnWebClient",
        "a[href*='wc/join']",
        "a:has-text('Join from')",
        "[data-testid='join-from-browser']",
    ]
    for attempt in range(3):
        for selector in selectors:
            with suppress(Exception):
                elem = page.locator(selector).first
                if await elem.is_visible(timeout=2000):
                    _log(session_id, f"Clicking browser join: {selector}")
                    await elem.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(3000)
                    _log(session_id, f"After browser join click — url: {page.url}")
                    return
        _log(session_id, f"Browser join not found (attempt {attempt + 1}/3), waiting...")
        await page.wait_for_timeout(3000)


async def _zoom_fill_name(page, bot_name: str, session_id: str):
    # Use the frame-aware finder so we don't miss inputs inside child frames
    frame, field = await _zoom_find_name_field(page)
    if field:
        with suppress(Exception):
            await field.triple_click()
            await field.fill(bot_name)
            await field.dispatch_event("input")
            await field.dispatch_event("change")
            await field.dispatch_event("keyup")
            _log(session_id, "Filled Zoom name via frame-aware locator")
            return

    # JS fallback (main frame only)
    with suppress(Exception):
        matched = await page.evaluate(_ZOOM_FILL_NAME_JS, bot_name)
        if matched:
            await page.wait_for_timeout(600)
            _log(session_id, f"Filled Zoom name via JS ({matched})")
            return

    _log(session_id, f"Warning: name input not found — url: {page.url}")


async def _zoom_click_join_button(page, session_id: str):
    # Poll up to 12 s — wait for button to become enabled after name fill.
    # From live HTML: button.preview-join-button gets zm-btn--disabled removed once name is entered.
    for attempt in range(12):
        if _zoom_is_in_meeting_url(page):
            _log(session_id, "Zoom URL moved to meeting — already joined")
            _status(session_id, "joining")
            return

        # Enabled preview-join-button (no disabled class)
        with suppress(Exception):
            btn = page.locator("button.preview-join-button:not(.zm-btn--disabled)").first
            if await btn.is_visible(timeout=500) and await btn.is_enabled(timeout=300):
                await btn.click()
                _status(session_id, "joining")
                return

        # Any visible enabled join button
        for selector in ["#joinBtn", "button#joinBtn", 'button[data-testid="join-btn"]', "button.btn-join"]:
            with suppress(Exception):
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=400) and await btn.is_enabled(timeout=200):
                    await btn.click()
                    _status(session_id, "joining")
                    return

        # Also search child frames for the join button
        for frame in [page] + list(page.frames):
            for label in ["Join", "Join Meeting"]:
                with suppress(Exception):
                    btn = frame.get_by_role("button", name=label, exact=True).first
                    if await btn.is_visible(timeout=400) and await btn.is_enabled(timeout=200):
                        await btn.click()
                        _status(session_id, "joining")
                        return

        await page.wait_for_timeout(1000)

    title = await page.title()
    raise RuntimeError(f"Could not find the Zoom join button. Page title: '{title}' url: {page.url}")


async def _zoom_find_name_field(page):
    """Return (frame, locator) for the name input, searching all frames.
    Uses get_by_label first (most semantic), then CSS selectors."""
    all_frames = [page] + list(page.frames)
    for frame in all_frames:
        # get_by_label is the most reliable — matches the <label for="input-for-name">Your Name</label>
        with suppress(Exception):
            loc = frame.get_by_label("Your Name", exact=False)
            if await loc.first.is_visible(timeout=300):
                return frame, loc.first
        for sel in ["#input-for-name", "#inputname", 'input[type="text"]']:
            with suppress(Exception):
                loc = frame.locator(sel).first
                if await loc.is_visible(timeout=300):
                    return frame, loc
    return None, None


async def _zoom_wait_for_name_form(page, session_id: str, timeout_seconds: int = 45) -> bool:
    """Wait until the name input is visible, including inputs inside child frames."""
    _log(session_id, "Waiting for Zoom name form to render...")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        frame, field = await _zoom_find_name_field(page)
        if frame and field:
            _log(session_id, "Zoom name form found")
            return True
        if _zoom_is_in_meeting_url(page):
            return False
        await page.wait_for_timeout(1000)

    _log(session_id, "Zoom name form did not appear within timeout")
    return False


def _fetch_zoom_zak(_unused_user_id: str = "") -> Optional[str]:
    """Return a Zoom Access Key for the bot account configured in the admin panel.
    Uses the bot's stored Zoom refresh token to get a fresh access token, then
    fetches the ZAK so the bot joins meetings as an authenticated Zoom account
    (bypassing reCAPTCHA on the web client)."""
    import base64
    import requests as _req
    from models.bot_config import load_bot_config, save_bot_config

    cfg = load_bot_config()
    refresh_token = cfg.get("zoom_bot_refresh_token", "")
    if not refresh_token:
        return None

    zoom_client_id     = os.environ.get("ZOOM_OAUTH_CLIENT_ID", "")
    zoom_client_secret = os.environ.get("ZOOM_OAUTH_CLIENT_SECRET", "")
    if not zoom_client_id or not zoom_client_secret:
        return None

    creds = base64.b64encode(f"{zoom_client_id}:{zoom_client_secret}".encode()).decode()

    # Exchange refresh token for a fresh access token
    r = _req.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"Authorization": f"Basic {creds}"},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    data = r.json()
    access_token = data["access_token"]
    if data.get("refresh_token"):
        save_bot_config({"zoom_bot_refresh_token": data["refresh_token"]})

    # Fetch the ZAK
    r = _req.get(
        "https://api.zoom.us/v2/users/me/token",
        params={"type": "zak"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if r.status_code == 200:
        return r.json().get("token")
    return None


async def _prepare_and_join_zoom(page, meeting_url: str, bot_name: str, session_id: str,
                                  user_id: str = ""):
    initial_url = _zoom_web_client_url(meeting_url) or meeting_url
    _log(session_id, f"Zoom join start — original: {meeting_url}")
    _log(session_id, f"Zoom join start — web client: {initial_url}")

    # Inject ZAK token so the bot joins as the authenticated Zoom user instead
    # of an anonymous guest — this bypasses reCAPTCHA on the web client form.
    zak = None
    with suppress(Exception):
        zak = await asyncio.get_event_loop().run_in_executor(None, _fetch_zoom_zak)
    if zak:
        sep = "&" if "?" in initial_url else "?"
        initial_url = f"{initial_url}{sep}zak={zak}"
        _log(session_id, "Zoom join — ZAK token injected (authenticated join)")
    else:
        _log(session_id, "Zoom join — no ZAK available, joining as guest")

    await page.goto(initial_url, wait_until="domcontentloaded", timeout=60000)
    _log(session_id, f"After goto — title: '{await page.title()}' url: {page.url}")

    past_interstitial = await _zoom_wait_past_interstitial(page, session_id, timeout_ms=25000)
    if not past_interstitial and initial_url == meeting_url:
        await _zoom_open_web_client(page, meeting_url, session_id)
    await page.wait_for_timeout(2000)
    _log(session_id, f"Before browser-join click — title: '{await page.title()}' url: {page.url}")

    await _zoom_click_join_from_browser(page, session_id)
    _log(session_id, f"After browser-join — title: '{await page.title()}' url: {page.url}")

    if await page.title() == "Just a moment...":
        await _zoom_open_web_client(page, meeting_url, session_id)

    # Wait for the name form (the "Joining Meeting..." spinner is just a
    # transition — the form appears after it clears).
    name_form_visible = await _zoom_wait_for_name_form(page, session_id, timeout_seconds=30)
    _log(session_id, f"Name form visible: {name_form_visible} — url: {page.url}")

    if not name_form_visible:
        if _zoom_is_in_meeting_url(page):
            _log(session_id, "Zoom URL already in meeting — skipping name/join steps")
            _status(session_id, "joining")
            return
        _log(session_id, "Zoom name form not found — attempting join anyway")

    await _zoom_fill_name(page, bot_name, session_id)
    await page.wait_for_timeout(800)
    _log(session_id, f"After name fill — url: {page.url}")
    await _zoom_click_join_button(page, session_id)
    _log(session_id, f"After join button click — url: {page.url}")

    # Log page state so we can tell if the join was accepted (waiting room)
    # or rejected (error / still on join form).
    await page.wait_for_timeout(2000)
    with suppress(Exception):
        body_text = await page.evaluate("() => document.body.innerText.slice(0, 300)")
        _log(session_id, f"Page state after join: {body_text!r}")


# ── Shared wait/detection ─────────────────────────────────────────────────────

async def _wait_for_admission(page, session_id: str, platform: str,
                              sign_in_attempted: bool = False, sign_in_ok: bool = False) -> bool:
    if _current_status(session_id) not in {"cancelled", "failed"}:
        _status(session_id, "waiting_for_host")
    if platform != "google_meet":
        _log(session_id, f"Waiting for Zoom admission — current URL: {page.url}")
    # Zoom needs a longer window: the "Joining Meeting..." spinner can run for
    # 30–60 s before the in-meeting UI appears on the headless browser.
    deadline_seconds = 90 if platform == "google_meet" else 120

    _zoom_diag_counter = 0
    for _ in range(deadline_seconds // 3):
        if _current_status(session_id) in {"cancelled", "processing"}:
            return False

        if platform == "google_meet":
            blocked = await _detect_google_meet_block(page)
            if blocked:
                if sign_in_attempted and not sign_in_ok:
                    hint = (
                        "Your connected Google account session could not be established. "
                        "Try reconnecting it in Settings → Integrations, then start the bot again."
                    )
                elif not sign_in_attempted:
                    hint = (
                        "Connect your Google account in Settings → Integrations so the bot can join as you."
                    )
                else:
                    # signed in successfully but still blocked — meeting may restrict to org accounts
                    hint = (
                        "Even with your Google account, this meeting restricts access to specific accounts. "
                        "Ask the host to allow external participants, or share the meeting link with guest access."
                    )
                raise RuntimeError(
                    f"Google Meet blocked the bot: {blocked}. "
                    f"The meeting is restricted to signed-in accounts. {hint}"
                )
            signals = [
                page.get_by_role("button", name="Leave call"),
                page.get_by_role("button", name="Leave meeting"),
                page.get_by_role("button", name="End call"),
                page.get_by_text("You joined"),
            ]
        else:
            # Primary check: URL moves from /join → /participant when Zoom admits the bot.
            # This is the most reliable signal — it's impossible to false-positive from
            # the waiting room page.
            if _zoom_is_in_meeting_url(page):
                _log(session_id, f"Zoom URL indicates in-meeting: {page.url}")
                _status(session_id, "recording")
                return True

            # Secondary: toolbar buttons that only exist once inside the meeting.
            # Deliberately exclude page.get_by_text("Chat") / get_by_text("Participants")
            # as those also appear on the Zoom waiting-room page (false positives).
            signals = [
                page.get_by_role("button", name="Mute"),
                page.get_by_role("button", name="Unmute"),
                page.get_by_role("button", name="Stop Video"),
                page.get_by_role("button", name="Start Video"),
                page.get_by_role("button", name="Leave"),
            ]

        for locator in signals:
            with suppress(Exception):
                if await locator.is_visible(timeout=1200):
                    _status(session_id, "recording")
                    return True

        # Log page state every ~15 s for Zoom so we can see what's on screen.
        if platform != "google_meet":
            _zoom_diag_counter += 1
            if _zoom_diag_counter % 5 == 1:
                with suppress(Exception):
                    body = await page.evaluate("() => document.body.innerText.slice(0, 200)")
                    _log(session_id, f"Zoom page state: {body!r}")

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
        fallback_in_meeting = False
    else:
        # URL is the primary indicator for Zoom — /wc/.../participant = in meeting.
        if _zoom_is_in_meeting_url(page):
            return True
        active = [
            page.get_by_role("button", name="Mute"),
            page.get_by_role("button", name="Unmute"),
            page.get_by_role("button", name="Stop Video"),
            page.get_by_role("button", name="Start Video"),
            page.get_by_role("button", name="Leave"),
        ]
        ended  = ["You have left the meeting", "This meeting has ended", "Join Meeting", "This meeting has been ended by the host"]
        # If we can't determine state, assume still in meeting (avoids premature exit).
        fallback_in_meeting = True

    for loc in active:
        with suppress(Exception):
            if await loc.is_visible(timeout=800):
                return True
    for text in ended:
        with suppress(Exception):
            if await page.get_by_text(text).is_visible(timeout=800):
                return False
    return fallback_in_meeting


async def _wait_for_recording_completion(page, session_id: str, platform: str):
    deadline = asyncio.get_running_loop().time() + MEETING_BOT_RECORD_SECONDS
    # Wait at least 12 s before first presence check — gives MediaRecorder time to
    # fire its first ondataavailable event (start(4000)) and ensures Zoom's toolbar
    # is fully rendered before we try to detect buttons.
    min_record_until = asyncio.get_running_loop().time() + 12
    while asyncio.get_running_loop().time() < deadline:
        # Check in-memory stop signal first (set by /end route immediately).
        if session_id in _stop_requested:
            _stop_requested.discard(session_id)
            return
        if _current_status(session_id) in {"cancelled", "processing"}:
            return
        if asyncio.get_running_loop().time() >= min_record_until:
            if not await _is_still_in_meeting(page, platform):
                _status(session_id, "processing")
                return
        await page.wait_for_timeout(1000)
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


# ── Join announcement chat message ────────────────────────────────────────────

_APP_URL = os.environ.get("APP_URL", "").strip().rstrip("/")


def _session_link(session_id: str) -> str:
    """Return the public (no-auth) live-view URL for the given session.
    The runner receives the session_id; the share_token is looked up here."""
    if not _APP_URL:
        return ""
    try:
        from models.meeting_bot import get_session as _get_session
        s = _get_session(session_id)
        if s and s.share_token:
            return f"{_APP_URL}/live/{s.share_token}"
    except Exception:
        pass
    return f"{_APP_URL}/meeting-bots/{session_id}"


def _build_join_message(user_name: str, session_id: str = "") -> str:
    who  = f"{user_name}'s " if user_name else ""
    link = _session_link(session_id)
    lines = [
        f"👋 {who}MinuteAI Notetaker has joined to record & transcribe this meeting.",
        "This meeting is being recorded. All participants are notified of the recording.",
    ]
    if link:
        lines.append(f"View live transcript & summary: {link}")
    return "\n".join(lines)


def _build_latecomer_message(user_name: str, session_id: str = "") -> str:
    who  = f"{user_name}'s " if user_name else "a "
    link = _session_link(session_id)
    lines = [
        f"📝 Reminder: This meeting is being recorded by {who}MinuteAI Notetaker.",
        "Minutes and transcript will be shared after the call.",
    ]
    if link:
        lines.append(f"View live transcript: {link}")
    return "\n".join(lines)


# How long to wait before re-sending the announcement for latecomers (seconds).
_LATECOMER_ANNOUNCE_DELAY = 180  # 3 minutes


async def _send_google_meet_chat(page, message: str, session_id: str):
    """Open the chat panel and post a message. Silently skips on any error."""
    try:
        # Reveal the auto-hiding toolbar: click centre of page, then move to toolbar area.
        vp = page.viewport_size or {"width": 1280, "height": 720}
        cx, cy = vp["width"] // 2, vp["height"] // 2
        with suppress(Exception):
            await page.mouse.click(cx, cy)
        await page.wait_for_timeout(600)
        for y_frac in (0.85, 0.90, 0.95):
            await page.mouse.move(cx, int(vp["height"] * y_frac))
            await page.wait_for_timeout(300)
        await page.wait_for_timeout(800)

        # Wait for the right-side nav (where chat lives) to render.
        # data-panel-id="2" is the stable identifier for the Chat panel button.
        # Timeout of 60 s matches reality: the latecomer reminder (3 min) works,
        # so the nav is definitely rendered by then — 60 s is a safe upper bound.
        with suppress(Exception):
            await page.wait_for_selector(
                'button[data-panel-id="2"], button[aria-label*="Chat" i]',
                timeout=60000,
            )

        # Open chat panel — try stable CSS selector first, then aria-label variants
        chat_opened = False

        # Most reliable: data-panel-id="2" is Google Meet's chat panel ID
        with suppress(Exception):
            btn = page.locator('button[data-panel-id="2"]').first
            if await btn.is_visible(timeout=3000):
                await btn.click(timeout=2000)
                await page.wait_for_timeout(1500)
                chat_opened = True
                _log(session_id, "Opened chat panel via data-panel-id=2")

        if not chat_opened:
            for label in [
                "Chat with everyone", "In-call messages", "Chat", "Messages",
                "Open chat", "Chat and captions", "People and chat",
            ]:
                with suppress(Exception):
                    btn = page.get_by_role("button", name=label, exact=False).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click(timeout=2000)
                        await page.wait_for_timeout(1500)
                        chat_opened = True
                        _log(session_id, f"Opened chat panel via '{label}' button")
                        break

        # JS fallback: broader scan — aria-label, title, data-tooltip, data-panel-id
        if not chat_opened:
            with suppress(Exception):
                clicked = await page.evaluate("""
                    () => {
                        const re = /chat|messages|in-call/i;
                        const attrs = ['aria-label', 'title', 'data-tooltip', 'data-panel-id'];
                        for (const el of document.querySelectorAll('button, [role="button"]')) {
                            const label = el.getAttribute('aria-label') || '';
                            if (label && re.test(label)) { el.click(); return label.slice(0, 60); }
                        }
                        // Second pass: check all attributes including textContent
                        for (const el of document.querySelectorAll('button, [role="button"]')) {
                            const s = attrs.map(a => el.getAttribute(a) || '').join(' ')
                                      + ' ' + (el.textContent || '');
                            if (re.test(s)) { el.click(); return s.trim().slice(0, 60); }
                        }
                        return null;
                    }
                """)
                if clicked:
                    _log(session_id, f"Opened chat via JS scan ('{clicked}')")
                    await page.wait_for_timeout(1500)
                    chat_opened = True

        if not chat_opened:
            with suppress(Exception):
                labels = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('button, [role="button"]'))
                        .map(el => (el.getAttribute('aria-label') || el.textContent || '').trim())
                        .filter(s => s.length > 0 && s.length < 80)
                """)
                _log(session_id, f"All buttons: {labels}")
            _log(session_id, "Chat panel button not found — skipping announcement")
            return

        # Find the chat input (textarea or contenteditable div)
        chat_input = None
        for selector in [
            'textarea[aria-label*="message" i]',
            'textarea[placeholder*="message" i]',
            'div[contenteditable="true"][aria-label*="message" i]',
            'div[contenteditable="true"][aria-label*="chat" i]',
            'div[contenteditable="true"]',
        ]:
            with suppress(Exception):
                el = page.locator(selector).last
                if await el.is_visible(timeout=2000):
                    chat_input = el
                    break

        if not chat_input:
            _log(session_id, "Chat input not found — skipping announcement")
            return

        await chat_input.click()
        await page.wait_for_timeout(400)

        # Use keyboard.type() — fill() doesn't work on contenteditable divs.
        # Replace \n with Shift+Enter so newlines don't prematurely send the message.
        lines = message.split("\n")
        for i, line in enumerate(lines):
            await page.keyboard.type(line)
            if i < len(lines) - 1:
                await page.keyboard.press("Shift+Enter")

        await page.wait_for_timeout(400)

        # Try an explicit send button first, fall back to Enter
        sent = False
        for send_label in ["Send message", "Send", "Submit"]:
            with suppress(Exception):
                send_btn = page.get_by_role("button", name=send_label, exact=False).first
                if await send_btn.is_visible(timeout=800):
                    await send_btn.click(timeout=2000)
                    sent = True
                    break
        if not sent:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(600)
        _log(session_id, "Chat message sent")
    except Exception as exc:
        _log(session_id, f"Chat message skipped: {exc}")


async def _send_zoom_chat(page, message: str, session_id: str):
    """Open the Zoom web-client chat panel and post a message. Silently skips on any error."""
    try:
        # Reveal the auto-hiding Zoom toolbar by hovering over the bottom of the viewport.
        vp = page.viewport_size or {"width": 1440, "height": 900}
        cx, cy = vp["width"] // 2, vp["height"] // 2
        with suppress(Exception):
            await page.mouse.click(cx, cy)
        await page.wait_for_timeout(600)
        for y_frac in (0.85, 0.90, 0.95):
            await page.mouse.move(cx, int(vp["height"] * y_frac))
            await page.wait_for_timeout(300)
        await page.wait_for_timeout(800)

        # Wait up to 60 s for the chat button to appear (toolbar renders after meeting loads).
        _ZOOM_CHAT_SELECTORS = (
            'button[aria-label*="chat" i]',
            'button[aria-label*="Chat" i]',
            'button.footer-button-base__button--chat',
            '[data-testid="chat-button"]',
            '.footer-chat-button',
        )
        with suppress(Exception):
            await page.wait_for_selector(
                ", ".join(_ZOOM_CHAT_SELECTORS),
                timeout=60000,
            )

        # Open the chat panel — try common button selectors
        chat_opened = False

        # Search across all frames (Zoom web client uses iframes)
        all_frames = [page] + list(page.frames)

        for frame in all_frames:
            for label in ["Chat", "Open Chat", "chat", "Show chat"]:
                with suppress(Exception):
                    btn = frame.get_by_role("button", name=label, exact=False).first
                    if await btn.is_visible(timeout=1500):
                        await btn.click(timeout=2000)
                        await page.wait_for_timeout(1500)
                        chat_opened = True
                        _log(session_id, f"Opened Zoom chat via '{label}' button")
                        break
            if chat_opened:
                break

        # CSS selector fallback for the chat toolbar button
        if not chat_opened:
            for frame in all_frames:
                for selector in _ZOOM_CHAT_SELECTORS + (
                    '.footer-button__chat-icon',
                    'button.footer-button-base__button[aria-label*="Chat"]',
                ):
                    with suppress(Exception):
                        btn = frame.locator(selector).first
                        if await btn.is_visible(timeout=1000):
                            await btn.click(timeout=2000)
                            await page.wait_for_timeout(1500)
                            chat_opened = True
                            _log(session_id, f"Opened Zoom chat via selector: {selector}")
                            break
                if chat_opened:
                    break

        # JS full-page scan — scans aria-label and textContent across all elements
        if not chat_opened:
            with suppress(Exception):
                clicked = await page.evaluate("""
                    () => {
                        const re = /^chat$/i;
                        for (const el of document.querySelectorAll('button, [role="button"]')) {
                            const label = (el.getAttribute('aria-label') || '').trim();
                            const text  = (el.textContent || '').trim();
                            if (re.test(label) || re.test(text)) {
                                el.click();
                                return label || text;
                            }
                        }
                        return null;
                    }
                """)
                if clicked:
                    _log(session_id, f"Opened Zoom chat via JS scan ('{clicked}')")
                    await page.wait_for_timeout(1500)
                    chat_opened = True

        if not chat_opened:
            with suppress(Exception):
                labels = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('button, [role="button"]'))
                        .map(el => (el.getAttribute('aria-label') || el.textContent || '').trim())
                        .filter(s => s.length > 0 && s.length < 80)
                        .slice(0, 20)
                """)
                _log(session_id, f"Zoom toolbar buttons visible: {labels}")
            _log(session_id, "Zoom chat button not found — skipping announcement")
            return

        # The chat panel may load lazily in a new iframe — re-capture frames after a delay.
        # Also wait for any contenteditable/textarea to appear rather than a flat sleep.
        _CHAT_INPUT_SELECTORS = [
            # Quill.js editor — Zoom PWA newChatSwitchEnabled (v7+)
            '.ql-editor[contenteditable="true"]',
            '.ql-editor',
            'div[contenteditable="true"][aria-label*="message" i]',
            'div[contenteditable="true"][aria-label*="chat" i]',
            'div[contenteditable="true"][data-placeholder]',
            'div[contenteditable="true"]',
            'textarea[aria-label*="message" i]',
            'textarea[aria-label*="chat" i]',
            'textarea[placeholder*="message" i]',
            'textarea[placeholder*="Type message" i]',
            'textarea[placeholder*="chat" i]',
            '[data-testid="chat-input"]',
            '[data-testid="message-input"]',
            '.chat-message-input div[contenteditable]',
            '.chat-input__text',
            '.chat-input',
            '#chat-input',
            '.chat-box textarea',
            '.chat-box input',
            '[class*="chatInput"] div[contenteditable]',
            '[class*="chat-input"] div[contenteditable]',
            '[class*="message-input"] div[contenteditable]',
        ]
        _INPUT_COMBINED = ", ".join(_CHAT_INPUT_SELECTORS[:8])

        # Wait up to 10 s for any chat input to appear in the main frame
        with suppress(Exception):
            await page.wait_for_selector(_INPUT_COMBINED, timeout=10000)

        # Re-capture frames (chat panel may have loaded a new iframe after the button click)
        all_frames = [page] + list(page.frames)

        # Log frame URLs to help diagnose cross-origin iframe issues
        frame_urls = [f.url for f in page.frames if f.url and f.url != "about:blank"]
        if len(frame_urls) > 1:
            _log(session_id, f"Zoom frames detected: {frame_urls}")

        chat_input = None
        target_frame = page
        for frame in all_frames:
            for selector in _CHAT_INPUT_SELECTORS:
                with suppress(Exception):
                    el = frame.locator(selector).last
                    if await el.is_visible(timeout=600):
                        chat_input = el
                        target_frame = frame
                        _log(session_id, f"Zoom chat input found: {selector} in {frame.url or 'main'}")
                        break
            if chat_input:
                break

        # JS fallback — scan all visible contenteditable/textarea in the main document
        if not chat_input:
            with suppress(Exception):
                # Log diagnostics: what editable elements exist right now?
                diag = await page.evaluate("""
                    () => {
                        const els = [
                            ...document.querySelectorAll('div[contenteditable]'),
                            ...document.querySelectorAll('textarea'),
                        ];
                        return els.map(el => {
                            const r = el.getBoundingClientRect();
                            return {
                                tag: el.tagName,
                                ce: el.contentEditable,
                                cls: el.className.slice(0, 60),
                                aria: el.getAttribute('aria-label') || '',
                                ph: el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '',
                                vis: r.width > 10 && r.height > 10,
                                w: Math.round(r.width), h: Math.round(r.height),
                            };
                        });
                    }
                """)
                _log(session_id, f"Zoom editable elements: {diag}")

            with suppress(Exception):
                handle = await page.evaluate_handle("""
                    () => {
                        const candidates = [
                            ...document.querySelectorAll('div[contenteditable="true"]'),
                            ...document.querySelectorAll('textarea'),
                            ...document.querySelectorAll('input[type="text"]'),
                        ];
                        for (const el of candidates) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 50 && r.height > 10) return el;
                        }
                        return null;
                    }
                """)
                element = handle.as_element()
                if element:
                    await page.evaluate("el => el.focus()", element)
                    await page.wait_for_timeout(300)
                    await page.keyboard.type(message)
                    await page.wait_for_timeout(400)
                    await page.keyboard.press("Enter")
                    _log(session_id, "Zoom chat message sent via JS element handle")
                    return

        if not chat_input:
            _log(session_id, "Zoom chat input not found — skipping announcement")
            return

        await chat_input.click()
        await page.wait_for_timeout(400)

        # Type the message line by line with Shift+Enter for newlines
        lines = message.split("\n")
        for i, line in enumerate(lines):
            await page.keyboard.type(line)
            if i < len(lines) - 1:
                await page.keyboard.press("Shift+Enter")

        await page.wait_for_timeout(400)

        # Try send button first, fall back to Enter
        sent = False
        for frame in all_frames:
            for send_label in ["Send", "Send Message", "Submit"]:
                with suppress(Exception):
                    send_btn = frame.get_by_role("button", name=send_label, exact=False).first
                    if await send_btn.is_visible(timeout=800):
                        await send_btn.click(timeout=2000)
                        sent = True
                        break
            if sent:
                break

        # CSS fallback for send button
        if not sent:
            for frame in all_frames:
                for selector in [
                    'button[aria-label*="Send" i]',
                    '.chat-btn-send',
                    '[data-testid="send-button"]',
                ]:
                    with suppress(Exception):
                        btn = frame.locator(selector).first
                        if await btn.is_visible(timeout=500):
                            await btn.click(timeout=2000)
                            sent = True
                            break
                if sent:
                    break

        if not sent:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(600)
        _log(session_id, "Zoom chat message sent")
    except Exception as exc:
        _log(session_id, f"Zoom chat message skipped: {exc}")


async def _send_chat_message(page, message: str, session_id: str, platform: str):
    """Platform-agnostic chat sender."""
    if platform == "google_meet":
        await _send_google_meet_chat(page, message, session_id)
    elif platform == "zoom":
        await _send_zoom_chat(page, message, session_id)
    else:
        _log(session_id, f"Chat not supported for platform: {platform}")


async def _schedule_recording_announcements(page, session_id: str, platform: str, user_name: str = ""):
    """
    Send the recording announcement to the meeting chat:
    1. Immediately upon joining — notifies everyone currently in the call.
    2. After a ~3-minute delay — catches latecomers who joined after the bot
       without spamming the chat repeatedly.

    This task runs in the background alongside audio capture. It exits
    gracefully if the session is cancelled/ended or the bot leaves the meeting.
    """
    # ── Initial announcement ──────────────────────────────────────────────
    await _send_chat_message(page, _build_join_message(user_name, session_id), session_id, platform)

    # ── Wait, then send the latecomer reminder ────────────────────────────
    # Sleep in small increments so we can bail out early if the session ends.
    elapsed = 0
    while elapsed < _LATECOMER_ANNOUNCE_DELAY:
        await asyncio.sleep(10)
        elapsed += 10

        # Bail if the session ended while we were waiting
        status = _current_status(session_id)
        if status in {"cancelled", "failed", "processing", "completed"}:
            _log(session_id, "Session ended before latecomer announcement — skipping")
            return

        # Bail if the bot is no longer in the meeting
        try:
            still_in = await _is_still_in_meeting(page, platform)
            if not still_in:
                _log(session_id, "Bot left meeting before latecomer announcement — skipping")
                return
        except Exception:
            pass

    # Re-check one last time before sending
    status = _current_status(session_id)
    if status not in {"recording"}:
        return

    _log(session_id, "Sending latecomer recording reminder")
    await _send_chat_message(page, _build_latecomer_message(user_name, session_id), session_id, platform)


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
    pcm_drain_task: Optional[asyncio.Task] = None
    announce_task: Optional[asyncio.Task] = None
    tts_task: Optional[asyncio.Task] = None
    _listener = None          # BotLiveListener instance
    _live_transcript: str = ""

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    # Load runtime bot config (admin panel may have updated it since startup).
    from models.bot_config import load_bot_config as _load_bot_config
    _bcfg = _load_bot_config()
    _bot_profile_dir = _bcfg.get("profile_dir") or MEETING_BOT_PROFILE_DIR
    _bot_email       = _bcfg.get("email")       or BOT_EMAIL
    _bot_name_cfg    = _bcfg.get("bot_name")    or ctx.bot_name

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
                # Use a persistent Chromium profile (pre-signed-in bot account) when configured.
                # This is the Fireflies-style approach: Meet sees a real Google user, not a guest.
                bot_profile = _bot_profile_dir if _bot_profile_dir and Path(_bot_profile_dir).is_dir() else None

                # Bot OAuth refresh token — the primary production auth method.
                # Admin connects the bot Google account once via Admin → Bot Config → Connect Google.
                _bot_refresh_token = _bcfg.get("bot_refresh_token", "") or ""

                shared_context_kwargs = dict(
                    viewport={"width": 1440, "height": 900},
                    permissions=["camera", "microphone"],
                    ignore_https_errors=True,
                    user_agent=_USER_AGENT,
                    locale="en-US",
                    timezone_id="America/New_York",
                )

                if bot_profile and ctx.platform == "google_meet":
                    # Persistent profile: launch_persistent_context owns the browser.
                    browser = None
                    context = await pw.chromium.launch_persistent_context(
                        user_data_dir=bot_profile,
                        headless=True,
                        args=chromium_args,
                        **shared_context_kwargs,
                    )
                    _log(ctx.session_id, f"Launched with bot profile from {bot_profile}")
                else:
                    browser = await pw.chromium.launch(headless=True, args=chromium_args)
                    context = await browser.new_context(**shared_context_kwargs)

                await context.add_init_script(_ANTIDETECT_SCRIPT)
                await context.add_init_script(_AUDIO_CAPTURE_SCRIPT)
                await context.add_init_script(_TTS_MIC_SCRIPT)
                _log(ctx.session_id, "Anti-detect + audio capture + TTS mic scripts registered")

                page = await context.new_page()
                await _take_debug_screenshot(page, ctx.session_id, "landing")

                sign_in_attempted = False
                sign_in_ok        = False

                if ctx.platform == "google_meet":
                    # For meetings sourced from the user's own calendar, sign in as THAT USER
                    # so the bot can access org/workspace-restricted meetings without needing
                    # a separate invitation for the bot account.
                    use_user_oauth = ctx.source == "user_calendar"

                    if bot_profile and not use_user_oauth:
                        # Persistent profile: already signed in
                        sign_in_attempted = True
                        sign_in_ok        = True
                        _log(ctx.session_id, f"Joining as bot account ({_bot_email or 'bot'}) via persistent profile")
                    elif _bot_refresh_token and not use_user_oauth:
                        # OAuth mode: fresh context, sign in via MergeSession with stored refresh token
                        sign_in_attempted = True
                        sign_in_ok = await _sign_in_google_meet_as_bot(page, _bot_refresh_token, ctx.session_id)
                        if sign_in_ok:
                            _log(ctx.session_id, f"Joining as bot account ({_bot_email or 'bot'}) via OAuth")
                        else:
                            _log(ctx.session_id, "Bot OAuth sign-in failed — joining as guest")
                    else:
                        # User-calendar sessions (or no bot configured): sign in as the meeting owner
                        sign_in_attempted = True
                        sign_in_ok = await _sign_in_google_meet(page, ctx.user_id, ctx.session_id)
                        if sign_in_ok:
                            _log(ctx.session_id, "Signed into Google via user OAuth — joining as meeting owner")
                        else:
                            _log(ctx.session_id, "Joining Google Meet as guest (OAuth sign-in unavailable or failed)")
                    await _prepare_google_meet(page, ctx.meeting_url, ctx.bot_name, ctx.session_id, is_signed_in=sign_in_ok, sign_in_ok=sign_in_ok)
                    await _take_debug_screenshot(page, ctx.session_id, "prejoin")
                    await _join_google_meet(page, ctx.session_id)
                elif ctx.platform == "zoom":
                    await _prepare_and_join_zoom(page, ctx.meeting_url, ctx.bot_name, ctx.session_id, ctx.user_id)
                    await _take_debug_screenshot(page, ctx.session_id, "prejoin")
                else:
                    raise RuntimeError(f"Unsupported platform: {ctx.platform}")

                joined = await _wait_for_admission(
                    page, ctx.session_id, ctx.platform,
                    sign_in_attempted=sign_in_attempted, sign_in_ok=sign_in_ok,
                )
                screenshot_path = await _take_debug_screenshot(page, ctx.session_id, "postjoin")

                if not joined:
                    # Don't overwrite a "cancelled" status with a misleading admission error.
                    if _current_status(ctx.session_id) not in {"cancelled", "failed"}:
                        _failure(
                            ctx.session_id,
                            "Bot was not admitted to the meeting within the timeout. "
                            "The host needs to accept 'MinuteAI Recorder' from the waiting room.",
                            recording_path=screenshot_path,
                        )
                    return

                _log(ctx.session_id, "Admitted — audio capture active")

                # Register TTS queue so the control-panel WS can push audio to the meeting
                _tts_q = register_tts_queue(ctx.session_id)

                async def _drain_tts():
                    while True:
                        try:
                            b64 = _tts_q.get_nowait()
                            with suppress(Exception):
                                await page.evaluate("(b64) => window.__stephSpeak(b64)", b64)
                        except _stdlib_queue.Empty:
                            pass
                        await asyncio.sleep(0.1)

                # Send recording announcements in the background (initial + latecomer reminder)
                announce_task = asyncio.create_task(
                    _schedule_recording_announcements(page, ctx.session_id, ctx.platform, ctx.user_name)
                )

                tts_task       = asyncio.create_task(_drain_tts())
                drain_task     = asyncio.create_task(_drain_audio_chunks(page, audio_chunks, ctx.session_id))
                pcm_drain_task = asyncio.create_task(_drain_pcm_for_live_translate(page, ctx.session_id))

                # Always-on live listener: real-time transcript + wake-word for Steph
                from services.meeting_bots.live_listener import BotLiveListener
                _listener = BotLiveListener(
                    session_id=ctx.session_id,
                    user_id=ctx.user_id,
                    workspace_id=session.workspace_id,
                    bot_name=ctx.bot_name,
                )
                _listener_q = register_bot_listener_queue(ctx.session_id)
                _listener.start(_listener_q)

                await _wait_for_recording_completion(page, ctx.session_id, ctx.platform)

            except Exception as exc:
                screenshot_path = None
                with suppress(Exception):
                    screenshot_path = await _take_debug_screenshot(page, ctx.session_id, "error")
                _failure(ctx.session_id, f"Browser automation error: {exc}", recording_path=screenshot_path)

            finally:
                # asyncio.CancelledError is a BaseException (Python 3.8+), not Exception,
                # so suppress(Exception) would NOT catch it — use explicit try/except.
                if announce_task and not announce_task.done():
                    announce_task.cancel()
                    try:
                        await announce_task
                    except BaseException:
                        pass
                if drain_task and not drain_task.done():
                    drain_task.cancel()
                    try:
                        await drain_task
                    except BaseException:
                        pass
                if pcm_drain_task and not pcm_drain_task.done():
                    pcm_drain_task.cancel()
                    try:
                        await pcm_drain_task
                    except BaseException:
                        pass
                if tts_task and not tts_task.done():
                    tts_task.cancel()
                    try:
                        await tts_task
                    except BaseException:
                        pass
                unregister_tts_queue(ctx.session_id)
                unregister_bot_audio_listener(ctx.session_id)
                unregister_bot_listener_queue(ctx.session_id)

                # Stop the live listener and capture its transcript
                if _listener is not None:
                    try:
                        _live_transcript = await _listener.stop()
                        if _live_transcript:
                            _log(ctx.session_id, f"Live transcript captured: {len(_live_transcript)} chars")
                    except Exception as _le:
                        _log(ctx.session_id, f"Live listener stop error: {_le}")

                # Stop recorder and do final drain across all frames
                all_frames = [page] + list(page.frames) if page else []
                for frame in all_frames:
                    with suppress(Exception):
                        await frame.evaluate("if (window.__maStop) window.__maStop()")
                with suppress(Exception):
                    await page.wait_for_timeout(1500)
                final_total = 0
                for frame in all_frames:
                    with suppress(Exception):
                        final = await frame.evaluate("window.__maDrain ? window.__maDrain() : []")
                        if final:
                            audio_chunks.extend(final)
                            final_total += len(final)
                _log(ctx.session_id, f"Final drain: {final_total} chunk(s), total {len(audio_chunks)}")

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

        try:
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
                attendees=session.attendees or None,
                meeting_title=session.title or None,
                transcript=_live_transcript or None,
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
            _failure(ctx.session_id, f"Post-processing error: {exc}")

    except Exception as exc:
        _failure(ctx.session_id, f"Session failed before launch: {exc}")
    except BaseException as exc:
        # Catch CancelledError and other BaseException subclasses that escape the inner handlers.
        # Always update status so the session is never left stuck at "processing" or "recording".
        with suppress(Exception):
            _failure(ctx.session_id, f"Session terminated unexpectedly: {type(exc).__name__}")
        raise


# ── Job entry point (called by RQ worker or directly by thread fallback) ──────

def _run_session(session_id: str):
    # Track in _running_sessions regardless of RQ vs thread mode so the health
    # check knows not to open the same Chrome profile while we're using it.
    with _running_lock:
        if session_id in _running_sessions:
            return
        _running_sessions.add(session_id)
    try:
        _run_session_inner(session_id)
    finally:
        with _running_lock:
            _running_sessions.discard(session_id)


def _run_session_inner(session_id: str):
    session = get_session(session_id)
    if not session or session.status == "cancelled":
        return

    # With RQ, the job is enqueued_at (scheduled_for - 90s) so by the time
    # this function runs we're already within the early-join window.
    # In thread-fallback mode we still do the busy-wait for compatibility.
    if session.scheduled_for:
        from services.core.queue import is_redis_available
        if not is_redis_available():
            # Thread fallback: busy-wait until 90s before start
            update_session(session_id, status="scheduled")
            _EARLY_JOIN_SECONDS = 90
            while True:
                latest = get_session(session_id)
                if not latest or latest.status == "cancelled":
                    return
                seconds_until = (latest.scheduled_for - datetime.utcnow()).total_seconds()
                if seconds_until <= _EARLY_JOIN_SECONDS:
                    break
                _log(session_id, f"Scheduled — starting in {int(seconds_until)}s")
                time.sleep(min(15, max(1, seconds_until - _EARLY_JOIN_SECONDS)))

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
        user_id=session.user_id,
        meeting_url=session.meeting_url,
        bot_name=session.bot_name or MEETING_BOT_RECORDER_NAME,
        platform=platform,
        source=session.source or "",
        user_name=_get_user_display_name(session.user_id),
    )
    try:
        asyncio.run(_run_playwright_session(ctx))
    except BaseException as exc:
        # Last-resort guard: if something escaped asyncio.run(), ensure the session
        # is never left stuck at "processing" or "recording".
        current = _current_status(ctx.session_id)
        if current in {"processing", "recording", "preparing", "joining", "waiting_for_host"}:
            with suppress(Exception):
                _failure(ctx.session_id, f"Session aborted: {type(exc).__name__}")


def start_meeting_bot_session(session_id: str):
    """
    Enqueue the session to Redis/RQ if available, otherwise fall back to a
    daemon thread (local dev without Redis).

    For scheduled sessions, RQ's enqueue_at fires the job 90s before the
    meeting starts.  For immediate sessions the job runs right away.
    """
    from services.core.queue import enqueue_session, is_redis_available

    session = get_session(session_id)
    if not session or session.status == "cancelled":
        return

    if is_redis_available():
        if session.scheduled_for:
            from datetime import timedelta
            # Fire 90s early so the browser has time to open and sign in
            run_at = session.scheduled_for - timedelta(seconds=90)
            update_session(session_id, status="scheduled")
            enqueue_session(session_id, run_at=run_at)
        else:
            enqueue_session(session_id)
        return

    # ── Thread fallback (no Redis / local dev) ────────────────────────────────
    with _running_lock:
        if session_id in _running_sessions:
            return
        _running_sessions.add(session_id)

    def _wrapped():
        try:
            _run_session_inner(session_id)
        finally:
            with _running_lock:
                _running_sessions.discard(session_id)

    threading.Thread(target=_wrapped, daemon=True).start()


def start_pending_meeting_bot_sessions():
    """Re-enqueue any sessions that were pending when the server last restarted."""
    for session in list_startable_sessions():
        start_meeting_bot_session(session.id)


def _pending_sessions_poll_loop():
    """Safety-net poll every 30s — catches sessions the calendar sync creates
    while the server is running (thread fallback / Redis down)."""
    while True:
        time.sleep(30)
        try:
            start_pending_meeting_bot_sessions()
        except Exception:
            pass


def start_bot_session_poll_loop():
    threading.Thread(target=_pending_sessions_poll_loop, name="bot-session-poller", daemon=True).start()
