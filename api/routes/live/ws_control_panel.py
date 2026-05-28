"""
WebSocket endpoint for the Host Control Panel.

/ws/control-panel — the web app connects here to receive real-time
  steph_wake / steph_response events from an active live transcript session,
  and to send manual questions to Steph.

Client → Server:
  {"action": "ask", "message": "...", "mode": "public"|"copilot"|"advocate"|"mediator",
   "cross_thread": false}

Server → Client:
  {"type": "connected",      "session_id": "..."}
  {"type": "steph_wake",     "command": "..."}
  {"type": "steph_response", "command": "...", "reply": "...", "audio_b64": "..."|null}
  {"type": "error",          "message": "..."}
"""

import asyncio
import base64
import json
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from middleware.auth import verify_token_string
from services.live.control_panel_bus import (
    subscribe, unsubscribe,
    append_history, get_history,
    publish as cp_publish,
)
from services.meeting_bots.runner import queue_tts

log = logging.getLogger(__name__)
router = APIRouter()

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
_TTS_VOICE = "aura-asteria-en"

_MODE_PROMPTS = {
    "copilot": (
        "You are in Silent Co-Pilot mode. Your response goes ONLY to the host — "
        "not the room. Be brief, direct, and tactical."
    ),
    "advocate": (
        "You are in Devil's Advocate mode. Present the strongest possible counter-argument "
        "or challenge to what is being proposed. Be structured and use data where available."
    ),
    "mediator": (
        "You are in Neutral Mediator mode. Synthesise both sides of any disagreement, "
        "identify the core point of contention, and suggest a resolution framework."
    ),
}


def _tts_b64(text: str) -> str | None:
    """Generate TTS audio via Deepgram Aura and return base64-encoded MP3, or None on failure."""
    if not DEEPGRAM_API_KEY or not text.strip():
        return None
    try:
        import urllib.request
        url = f"https://api.deepgram.com/v1/speak?model={_TTS_VOICE}&encoding=mp3"
        # Cap at 500 chars for control panel — keep latency low
        snippet = text[:500]
        data = json.dumps({"text": snippet}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            audio_bytes = resp.read()
        return base64.b64encode(audio_bytes).decode("utf-8")
    except Exception as exc:
        log.debug("TTS failed: %s", exc)
        return None


async def _ask_steph(
    session_id: str,
    user_id: str,
    workspace_id: str | None,
    message: str,
    mode: str,
    cross_thread: bool = False,
) -> str:
    from services.ai.steph_chat import get_reply
    history = get_history(session_id)
    mode_note = _MODE_PROMPTS.get(mode, "")
    full_message = f"[{mode_note}]\n\n{message}" if mode_note else message

    loop = asyncio.get_event_loop()
    reply = await loop.run_in_executor(
        None,
        lambda: get_reply(
            message=full_message,
            user_id=user_id,
            workspace_id=workspace_id,
            history=history,
            cross_thread=cross_thread,
        )
    )
    append_history(session_id, "user", message)
    append_history(session_id, "assistant", reply)
    return reply


@router.websocket("/ws/control-panel")
async def control_panel_ws(
    websocket:    WebSocket,
    token:        str = Query(...),
    session_id:   str = Query(...),
    workspace_id: str | None = Query(None),
):
    await websocket.accept()

    # Auth
    user_id: str | None = None
    try:
        payload = verify_token_string(token)
        user_id = payload.get("sub")
    except ValueError as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close()
        return

    q = subscribe(session_id)
    await websocket.send_text(json.dumps({"type": "connected", "session_id": session_id}))
    log.info("Control panel connected for session %s (user %s)", session_id, user_id)

    async def _recv_client():
        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                if data.get("action") == "ask":
                    message = (data.get("message") or "").strip()
                    mode = data.get("mode", "public")
                    cross_thread = bool(data.get("cross_thread", False))
                    if not message:
                        continue
                    cp_publish(session_id, {"type": "steph_wake", "command": message, "source": "manual"})
                    try:
                        reply = await _ask_steph(
                            session_id, user_id or "", workspace_id,
                            message, mode, cross_thread,
                        )
                    except Exception as exc:
                        log.error("Control panel ask error: %s", exc)
                        reply = "Sorry, something went wrong. Please try again."

                    # Generate TTS in thread pool so it doesn't block the event loop
                    loop = asyncio.get_event_loop()
                    audio_b64 = await loop.run_in_executor(None, lambda: _tts_b64(reply))

                    # Public mode: route audio through the bot's virtual mic so the
                    # whole room hears Steph.  Private modes: send audio_b64 to the
                    # host's browser only.
                    spoke_in_meeting = False
                    if mode == "public" and audio_b64:
                        spoke_in_meeting = queue_tts(session_id, audio_b64)

                    event = {
                        "type": "steph_response",
                        "command": message,
                        "reply": reply,
                        "mode": mode,
                        # Private modes send audio to the host browser; public mode
                        # plays through the bot so no private echo is needed.
                        "audio_b64": None if spoke_in_meeting else audio_b64,
                        "spoke_in_meeting": spoke_in_meeting,
                    }
                    cp_publish(session_id, event)
        except (WebSocketDisconnect, Exception) as exc:
            log.debug("Control panel client disconnected: %s", exc)

    async def _send_events():
        try:
            while True:
                event = await q.get()
                await websocket.send_text(json.dumps(event))
        except Exception as exc:
            log.debug("Control panel send loop ended: %s", exc)

    try:
        await asyncio.gather(_recv_client(), _send_events())
    finally:
        unsubscribe(session_id, q)
        log.info("Control panel disconnected for session %s", session_id)
