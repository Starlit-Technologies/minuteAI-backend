"""
BotLiveListener — always-on Deepgram stream for meeting-bot sessions.

Runs alongside _drain_pcm_for_live_translate, consuming PCM audio from the
bot's virtual mic capture.  Accumulates a real-time transcript (so post-call
Whisper/AssemblyAI transcription is skipped) and handles wake-word detection
so Steph can respond live through the bot's virtual mic.
"""

import asyncio
import base64
import json
import logging
import os
import urllib.request
from typing import Optional

import websockets

from services.ai.wake_word import WakeWordDetector
from services.pipeline.transcription_deepgram import (
    _build_url,
    parse_deepgram_message,
    is_configured as deepgram_configured,
)

log = logging.getLogger(__name__)

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
_TTS_VOICE = "aura-asteria-en"


def _tts_b64(text: str) -> str | None:
    """Generate TTS audio via Deepgram Aura and return base64-encoded MP3."""
    if not DEEPGRAM_API_KEY or not text.strip():
        return None
    try:
        url = f"https://api.deepgram.com/v1/speak?model={_TTS_VOICE}&encoding=mp3"
        data = json.dumps({"text": text[:500]}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return base64.b64encode(resp.read()).decode("utf-8")
    except Exception as exc:
        log.debug("TTS failed: %s", exc)
        return None


class BotLiveListener:
    """
    Streams bot PCM audio through Deepgram for live transcription and wake-word
    detection.  Returns the accumulated transcript on stop() so the post-call
    Whisper/AssemblyAI step is skipped.
    """

    def __init__(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        bot_name: str = "Steph",
    ):
        self.session_id   = session_id
        self.user_id      = user_id
        self.workspace_id = workspace_id
        self.bot_name     = bot_name

        self._parts: list[str] = []
        self._task: Optional[asyncio.Task] = None

    # ── Wake-word handler ──────────────────────────────────────────────────────

    async def _handle_command(self, command: str) -> None:
        log.info("BotLiveListener wake word [%s]: %s", self.session_id, command[:80])

        from services.live.control_panel_bus import (
            publish as cp_publish,
            append_history as cp_append,
            get_history as cp_history,
        )

        cp_publish(self.session_id, {"type": "steph_wake", "command": command})

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
                ),
            )
        except Exception as exc:
            log.error("Steph reply error in bot listener: %s", exc)
            reply = "Sorry, I couldn't process that."

        cp_append(self.session_id, "user", command)
        cp_append(self.session_id, "assistant", reply)
        cp_publish(self.session_id, {"type": "steph_response", "command": command, "reply": reply})

        # Speak reply through the bot's virtual mic (public — whole room hears Steph)
        loop = asyncio.get_event_loop()
        audio_b64 = await loop.run_in_executor(None, lambda: _tts_b64(reply))
        if audio_b64:
            from services.meeting_bots.runner import queue_tts
            queue_tts(self.session_id, audio_b64)

    # ── Deepgram streaming loop ────────────────────────────────────────────────

    async def _run(self, audio_queue: asyncio.Queue) -> None:
        if not deepgram_configured():
            log.warning("BotLiveListener: Deepgram not configured — skipping live transcript")
            return

        detector = WakeWordDetector(bot_name=self.bot_name)
        detector.on_command(self._handle_command)

        try:
            dg_url, api_key = _build_url(None, diarize=True)
        except Exception as exc:
            log.error("BotLiveListener: Deepgram config error: %s", exc)
            return

        try:
            async with websockets.connect(
                dg_url, additional_headers={"Authorization": f"Token {api_key}"}
            ) as dg_ws:
                log.info("BotLiveListener connected to Deepgram for session %s", self.session_id)

                async def _recv():
                    try:
                        async for raw in dg_ws:
                            if isinstance(raw, bytes):
                                continue
                            result = parse_deepgram_message(raw)
                            if result is None:
                                continue
                            if result.is_final:
                                text = (result.reliable_text or result.text or "").strip()
                                if text:
                                    self._parts.append(text)
                                    await detector.process(text)
                    except Exception as exc:
                        log.debug("BotLiveListener Deepgram recv closed: %s", exc)

                async def _pump():
                    try:
                        while True:
                            try:
                                pcm = await asyncio.wait_for(audio_queue.get(), timeout=30.0)
                            except asyncio.TimeoutError:
                                log.warning("BotLiveListener: no audio for 30s — stopping pump")
                                break
                            if pcm is None:
                                break
                            await dg_ws.send(pcm)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.debug("BotLiveListener pump error: %s", exc)
                    finally:
                        try:
                            await dg_ws.send(json.dumps({"type": "CloseStream"}))
                        except Exception:
                            pass

                await asyncio.gather(_recv(), _pump(), return_exceptions=True)
                detector.cancel()

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("BotLiveListener Deepgram connection error: %s", exc)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self, audio_queue: asyncio.Queue) -> None:
        self._task = asyncio.create_task(self._run(audio_queue))

    async def stop(self) -> str:
        """Cancel the streaming task and return the accumulated transcript."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
        return " ".join(self._parts)
