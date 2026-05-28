"""
WebSocket endpoint for live meeting transcription via Deepgram.

/ws/live-transcript — streams tab audio from the Chrome extension,
returns real-time transcript updates, and on stop creates a MinuteAI
processing job from the accumulated transcript.

Client → Server:
  - Binary frames: PCM16 48 kHz mono audio
  - Text: '{"action":"stop","title":"...","highlights":[...]}'

Server → Client:
  {"type":"connected"}
  {"type":"partial",  "text":"...", "ts":12.3}
  {"type":"final",    "text":"...", "ts":12.3}
  {"type":"job_created", "job_id":"..."}
  {"type":"error",    "message":"..."}
"""

import asyncio
import io
import json
import logging
import os
import tempfile
import time
import uuid
import wave
from typing import Optional

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from middleware.auth import verify_token_string
from models.job import Job, store
from services.pipeline.processing_pipeline import start_pipeline
from services.pipeline.transcription_deepgram import (
    _build_url,
    parse_deepgram_message,
    is_configured as deepgram_configured,
)
from services.ai.wake_word import WakeWordDetector
from services.live.control_panel_bus import (
    register as cp_register,
    unregister as cp_unregister,
    publish as cp_publish,
    append_history as cp_append,
    get_history as cp_history,
)

log = logging.getLogger(__name__)
router = APIRouter()

SAMPLE_RATE = 48_000
WHISPER_CHUNK_SECONDS = 15  # fallback: transcribe every N seconds


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pcm16_to_wav(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _create_job(user_id: Optional[str], title: str, transcript: str,
                workspace_id: Optional[str] = None) -> str:
    job_id = str(uuid.uuid4())
    store.create(Job(
        id=job_id,
        status="transcribing",   # run_pipeline skips transcription when transcript is set
        user_id=user_id,
        workspace_id=workspace_id,
        transcript=transcript,
        job_config={"meeting_title": title, "template": "standard", "output_language": "en"},
    ))
    start_pipeline(job_id)
    return job_id


# ── Deepgram session ─────────────────────────────────────────────────────────

class LiveTranscriptSession:
    """Bridges client audio → Deepgram → transcript updates → client."""

    def __init__(
        self,
        client_ws: WebSocket,
        user_id: Optional[str],
        title: str,
        language: str,
        workspace_id: Optional[str],
        session_id: Optional[str] = None,
        bot_name: str = "Steph",
    ):
        self.client_ws    = client_ws
        self.user_id      = user_id
        self.title        = title
        self.language     = language
        self.workspace_id = workspace_id
        self.session_id   = session_id or str(uuid.uuid4())
        self.bot_name     = bot_name

        self._parts: list[str] = []       # accumulated final transcript
        self._start_ts: float  = 0.0
        self._stopped          = False

        self._detector = WakeWordDetector(bot_name=bot_name)
        self._detector.on_command(self._handle_command)

    # ── Outbound helpers ──────────────────────────────────────────────────────

    async def _send(self, payload: dict) -> None:
        try:
            await self.client_ws.send_text(json.dumps(payload))
        except Exception:
            pass

    def _elapsed(self) -> float:
        return time.monotonic() - self._start_ts

    # ── Wake word handler ─────────────────────────────────────────────────────

    async def _handle_command(self, command: str) -> None:
        """Called by WakeWordDetector when a command is ready."""
        log.info("Wake word command: %s", command[:80])
        cp_publish(self.session_id, {"type": "steph_wake", "command": command})
        await self._send({"type": "steph_wake", "command": command})

        live_transcript = " ".join(self._parts[-60:])
        history = cp_history(self.session_id)

        try:
            from services.ai.steph_chat import get_reply
            loop = asyncio.get_event_loop()
            reply = await loop.run_in_executor(
                None,
                lambda: get_reply(
                    message=command,
                    user_id=self.user_id or "",
                    workspace_id=self.workspace_id,
                    history=history,
                    live_transcript=live_transcript,
                )
            )
        except Exception as exc:
            log.error("Steph reply error in wake handler: %s", exc)
            reply = "Sorry, I couldn't process that. Please try again."

        cp_append(self.session_id, "user", command)
        cp_append(self.session_id, "assistant", reply)
        event = {"type": "steph_response", "command": command, "reply": reply}
        cp_publish(self.session_id, event)
        await self._send(event)

    # ── Main run (Deepgram path) ──────────────────────────────────────────────

    async def run(self) -> None:
        self._start_ts = time.monotonic()

        if self.user_id:
            cp_register(self.user_id, self.session_id)

        try:
            lang = None if self.language == "auto" else self.language
            dg_url, api_key = _build_url(lang, diarize=True)
        except Exception as exc:
            await self._send({"type": "error", "message": f"Deepgram config error: {exc}"})
            if self.user_id:
                cp_unregister(self.user_id, self.session_id)
            return

        headers = {"Authorization": f"Token {api_key}"}

        try:
            async with websockets.connect(dg_url, additional_headers=headers) as dg_ws:
                await self._send({"type": "connected", "session_id": self.session_id})
                log.info("Live transcript session connected to Deepgram")

                async def _recv_deepgram():
                    try:
                        async for raw in dg_ws:
                            if isinstance(raw, bytes):
                                continue
                            result = parse_deepgram_message(raw)
                            if result is None:
                                continue
                            ts = round(self._elapsed(), 1)
                            if not result.is_final:
                                await self._send({"type": "partial", "text": result.text, "ts": ts})
                            else:
                                text = result.reliable_text or result.text
                                if text and text.strip():
                                    self._parts.append(text.strip())
                                    payload: dict = {"type": "final", "text": text.strip(), "ts": ts}
                                    if result.speaker is not None:
                                        payload["speaker"] = result.speaker
                                    await self._send(payload)
                                    await self._detector.process(text.strip())
                    except Exception as exc:
                        log.debug("Deepgram recv closed: %s", exc)

                async def _forward_client():
                    try:
                        while not self._stopped:
                            msg = await self.client_ws.receive()
                            if "bytes" in msg and msg["bytes"]:
                                await dg_ws.send(msg["bytes"])
                            elif "text" in msg and msg["text"]:
                                data = json.loads(msg["text"])
                                if data.get("action") == "stop":
                                    self._stopped = True
                                    await dg_ws.send(json.dumps({"type": "CloseStream"}))
                                    await self._finalize(
                                        title=data.get("title", self.title),
                                        highlights=data.get("highlights", []),
                                        notes=data.get("notes", ""),
                                    )
                                    break
                    except (WebSocketDisconnect, Exception) as exc:
                        log.debug("Client disconnected: %s", exc)
                        self._stopped = True

                await asyncio.gather(_recv_deepgram(), _forward_client())

        except Exception as exc:
            log.error("Live transcript WS error: %s", exc)
            await self._send({"type": "error", "message": str(exc)})
        finally:
            self._detector.cancel()
            if self.user_id:
                cp_unregister(self.user_id, self.session_id)

    # ── Finalise ──────────────────────────────────────────────────────────────

    async def _finalize(self, title: str, highlights: list, notes: str = "") -> None:
        full_transcript = " ".join(self._parts).strip()
        if not full_transcript:
            await self._send({"type": "error", "message": "No transcript captured."})
            return
        if notes.strip():
            full_transcript += f"\n\n--- Notes ---\n{notes.strip()}"
        try:
            job_id = _create_job(self.user_id, title, full_transcript, self.workspace_id)
            await self._send({"type": "job_created", "job_id": job_id})
        except Exception as exc:
            await self._send({"type": "error", "message": f"Failed to create job: {exc}"})


# ── Whisper fallback session ──────────────────────────────────────────────────

class WhisperChunkSession:
    """Buffer PCM16 audio, transcribe with Whisper every N seconds (no Deepgram)."""

    def __init__(
        self,
        client_ws: WebSocket,
        user_id: Optional[str],
        title: str,
        language: str,
        workspace_id: Optional[str],
    ):
        self.client_ws    = client_ws
        self.user_id      = user_id
        self.title        = title
        self.language     = language
        self.workspace_id = workspace_id

        self._audio_buf: bytearray = bytearray()
        self._parts: list[str]     = []
        self._start_ts: float      = 0.0
        self._stopped              = False
        self._lock                 = asyncio.Lock()

    async def _send(self, payload: dict) -> None:
        try:
            await self.client_ws.send_text(json.dumps(payload))
        except Exception:
            pass

    def _elapsed(self) -> float:
        return time.monotonic() - self._start_ts

    async def run(self) -> None:
        self._start_ts = time.monotonic()
        await self._send({"type": "connected"})
        log.info("Live transcript session started (Whisper fallback)")

        # Start background transcription ticker
        ticker_task = asyncio.create_task(self._ticker())

        try:
            while not self._stopped:
                msg = await self.client_ws.receive()
                if "bytes" in msg and msg["bytes"]:
                    async with self._lock:
                        self._audio_buf.extend(msg["bytes"])
                elif "text" in msg and msg["text"]:
                    data = json.loads(msg["text"])
                    if data.get("action") == "stop":
                        self._stopped = True
                        ticker_task.cancel()
                        await self._transcribe_buffer(is_final=True)
                        await self._finalize(
                            title=data.get("title", self.title),
                            highlights=data.get("highlights", []),
                            notes=data.get("notes", ""),
                        )
                        break
        except (WebSocketDisconnect, Exception) as exc:
            log.debug("Client disconnected (Whisper): %s", exc)
            self._stopped = True
            ticker_task.cancel()

    async def _ticker(self) -> None:
        chunk_bytes = SAMPLE_RATE * 2 * WHISPER_CHUNK_SECONDS  # 15s of PCM16
        try:
            while not self._stopped:
                await asyncio.sleep(WHISPER_CHUNK_SECONDS)
                async with self._lock:
                    if len(self._audio_buf) >= chunk_bytes:
                        await self._transcribe_buffer(is_final=False)
        except asyncio.CancelledError:
            pass

    async def _transcribe_buffer(self, is_final: bool) -> None:
        async with self._lock:
            if not self._audio_buf:
                return
            pcm = bytes(self._audio_buf)
            self._audio_buf.clear()

        loop = asyncio.get_event_loop()
        ts   = round(self._elapsed(), 1)

        def _run_whisper():
            from services.pipeline.transcription import transcribe as whisper_transcribe
            wav_bytes = _pcm16_to_wav(pcm)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_bytes)
                tmp_path = f.name
            try:
                lang = None if self.language == "auto" else self.language
                return whisper_transcribe(tmp_path, language=lang)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        try:
            text = await loop.run_in_executor(None, _run_whisper)
            if text and text.strip():
                self._parts.append(text.strip())
                await self._send({"type": "final", "text": text.strip(), "ts": ts})
        except Exception as exc:
            log.warning("Whisper chunk error: %s", exc)

    async def _finalize(self, title: str, highlights: list, notes: str = "") -> None:
        full_transcript = " ".join(self._parts).strip()
        if not full_transcript:
            await self._send({"type": "error", "message": "No transcript captured."})
            return
        if notes.strip():
            full_transcript += f"\n\n--- Notes ---\n{notes.strip()}"
        try:
            job_id = _create_job(self.user_id, title, full_transcript, self.workspace_id)
            await self._send({"type": "job_created", "job_id": job_id})
        except Exception as exc:
            await self._send({"type": "error", "message": f"Failed to create job: {exc}"})


# ── Route ─────────────────────────────────────────────────────────────────────

@router.websocket("/ws/live-transcript")
async def live_transcript_ws(
    websocket:    WebSocket,
    token:        str = Query(...),
    title:        str = Query("Meeting"),
    language:     str = Query("auto"),
    workspace_id: Optional[str] = Query(None),
    session_id:   Optional[str] = Query(None),
    bot_name:     str = Query("Steph"),
):
    await websocket.accept()

    # Authenticate
    user_id: Optional[str] = None
    try:
        payload = verify_token_string(token)
        user_id = payload.get("sub")
    except ValueError as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close()
        return

    # Route to the right backend
    if deepgram_configured():
        session = LiveTranscriptSession(
            websocket, user_id, title, language, workspace_id,
            session_id=session_id, bot_name=bot_name,
        )
    else:
        session = WhisperChunkSession(websocket, user_id, title, language, workspace_id)

    await session.run()
