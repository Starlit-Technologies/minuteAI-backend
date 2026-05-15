"""
WebSocket endpoint for real-time audio translation via Deepgram.

Flow:
  1. Client connects: ws://backend/ws/live-translate?token=JWT&target_lang=en&source_lang=fr
  2. Client streams raw PCM audio frames (16kHz, 16-bit, mono)
  3. Server forwards frames to Deepgram's streaming API
  4. Deepgram returns word-by-word transcription with confidence scores
  5. Server buffers until sentence boundary, then translates (DeepL → Claude)
  6. Server pushes JSON result back to the client

Message types sent to client:
  { "type": "partial",   "text": "Bonjour tout",    "buffering": true }
  { "type": "result",    "source_text": "...",  "translated_text": "...", "backend": "deepl" }
  { "type": "error",     "message": "..." }
  { "type": "connected", "message": "Deepgram streaming active" }

Client → Server:
  - Binary frames: raw PCM audio
  - Text frame: '{"action":"flush"}' to force translate the current buffer
  - Text frame: '{"action":"stop"}'  to end the session cleanly
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Optional

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from middleware.auth import verify_token_string
from services.transcription_deepgram import (
    _build_url,
    parse_deepgram_message,
    is_configured as deepgram_configured,
    CONFIDENCE_THRESHOLD,
)
from services.translation import translate_text
from api.routes.translate import LANGUAGE_NAMES, _is_meaningful

log = logging.getLogger(__name__)
router = APIRouter()


class LiveTranslateSession:
    """Manages one client's live translation session."""

    def __init__(
        self,
        client_ws: WebSocket,
        source_lang: str,
        target_lang: str,
    ):
        self.client_ws = client_ws
        self.source_lang = source_lang  # may be "auto"
        self.target_lang = target_lang
        self._sentence_buffer = ""
        self._detected_lang: Optional[str] = None

    # ── Buffer helpers ────────────────────────────────────────────────────────

    def _append(self, text: str, detected_lang: Optional[str] = None) -> None:
        self._sentence_buffer = (self._sentence_buffer + " " + text).strip()
        if detected_lang:
            self._detected_lang = detected_lang

    def _flush(self) -> tuple[str, str]:
        text = self._sentence_buffer.strip()
        lang = self._detected_lang or self.source_lang
        self._sentence_buffer = ""
        return text, lang

    def _has_sentence_end(self) -> bool:
        import re
        return bool(re.search(r'[.!?।۔。！？]\s*$', self._sentence_buffer))

    # ── Outbound helpers ──────────────────────────────────────────────────────

    async def _send(self, payload: dict) -> None:
        try:
            await self.client_ws.send_text(json.dumps(payload))
        except Exception:
            pass

    async def _send_partial(self, text: str) -> None:
        await self._send({"type": "partial", "text": text, "buffering": True})

    async def _translate_and_send(self, source_text: str, source_lang: str) -> None:
        if not source_text or not _is_meaningful(source_text):
            return

        effective_source = source_lang if source_lang != "auto" else "en"

        # Same language → passthrough
        if effective_source == self.target_lang:
            await self._send({
                "type": "result",
                "source_text": source_text,
                "translated_text": source_text,
                "source_language": effective_source,
                "backend": "passthrough",
            })
            return

        source_name = LANGUAGE_NAMES.get(effective_source, effective_source.upper())
        target_name = LANGUAGE_NAMES.get(self.target_lang, self.target_lang.upper())

        try:
            translated, backend = translate_text(
                source_text,
                source_lang=effective_source,
                target_lang=self.target_lang,
                source_lang_name=source_name,
                target_lang_name=target_name,
            )
            await self._send({
                "type": "result",
                "source_text": source_text,
                "translated_text": translated,
                "source_language": effective_source,
                "backend": backend,
            })
        except Exception as exc:
            await self._send({"type": "error", "message": f"Translation failed: {exc}"})

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Connect to Deepgram and bridge audio from the client WebSocket.
        Runs until the client disconnects or an error occurs.
        """
        try:
            dg_url, api_key = _build_url(
                None if self.source_lang == "auto" else self.source_lang
            )
        except Exception as exc:
            await self._send({"type": "error", "message": f"Deepgram config error: {exc}"})
            return

        headers = {"Authorization": f"Token {api_key}"}

        try:
            async with websockets.connect(dg_url, extra_headers=headers) as dg_ws:
                await self._send({"type": "connected", "message": "Deepgram streaming active"})
                log.info("Live translate session connected to Deepgram")

                # Task 1: Receive messages from Deepgram and push to client
                async def _recv_from_deepgram():
                    try:
                        async for raw_msg in dg_ws:
                            if isinstance(raw_msg, bytes):
                                continue  # Deepgram only sends text JSON

                            transcript = parse_deepgram_message(raw_msg)
                            if transcript is None:
                                continue

                            if not transcript.text and transcript.speech_final:
                                # UtteranceEnd — force flush
                                text, lang = self._flush()
                                if text:
                                    await self._translate_and_send(text, lang)
                                continue

                            if not transcript.is_final:
                                # Interim result — send partial to show live words
                                await self._send_partial(
                                    self._sentence_buffer + " " + transcript.text
                                )
                                continue

                            # Final result — add to sentence buffer
                            reliable = transcript.reliable_text
                            if reliable and _is_meaningful(reliable):
                                self._append(reliable)
                                # Translate if sentence is complete
                                if self._has_sentence_end() or transcript.speech_final:
                                    text, lang = self._flush()
                                    if text:
                                        await self._translate_and_send(text, lang)
                                else:
                                    await self._send_partial(self._sentence_buffer)

                    except Exception as exc:
                        log.warning("Deepgram recv error: %s", exc)

                # Task 2: Forward audio from client to Deepgram
                async def _forward_from_client():
                    try:
                        while True:
                            data = await self.client_ws.receive()

                            if "bytes" in data and data["bytes"]:
                                # Raw PCM audio frame → forward to Deepgram
                                await dg_ws.send(data["bytes"])

                            elif "text" in data and data["text"]:
                                try:
                                    msg = json.loads(data["text"])
                                    action = msg.get("action", "")
                                    if action == "flush":
                                        text, lang = self._flush()
                                        if text:
                                            await self._translate_and_send(text, lang)
                                    elif action == "stop":
                                        # Flush remaining buffer then close
                                        text, lang = self._flush()
                                        if text:
                                            await self._translate_and_send(text, lang)
                                        await dg_ws.send(json.dumps({"type": "CloseStream"}))
                                        return
                                except json.JSONDecodeError:
                                    pass

                    except WebSocketDisconnect:
                        pass
                    except Exception as exc:
                        log.warning("Client forward error: %s", exc)
                    finally:
                        # Tell Deepgram we're done sending audio
                        try:
                            await dg_ws.send(json.dumps({"type": "CloseStream"}))
                        except Exception:
                            pass

                # Run both tasks concurrently
                await asyncio.gather(
                    _recv_from_deepgram(),
                    _forward_from_client(),
                    return_exceptions=True,
                )

        except Exception as exc:
            log.error("Deepgram WebSocket connection failed: %s", exc)
            await self._send({"type": "error", "message": f"Deepgram connection failed: {exc}"})


@router.websocket("/ws/live-translate")
async def live_translate_ws(
    websocket: WebSocket,
    token: str = Query(...),
    target_lang: str = Query("en"),
    source_lang: str = Query("auto"),
):
    """
    WebSocket endpoint for real-time audio translation.

    Query params:
      token       – JWT access token (passed as query param since WS headers
                    are not easily set in Expo)
      target_lang – target language code (default: "en")
      source_lang – source language code or "auto" (default: "auto")
    """
    # Validate token before accepting
    try:
        verify_token_string(token)
    except Exception:
        await websocket.close(code=4001)
        return

    if not deepgram_configured():
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "Deepgram is not configured on this server. Set DEEPGRAM_API_KEY.",
        }))
        await websocket.close(code=4000)
        return

    await websocket.accept()

    session = LiveTranslateSession(
        client_ws=websocket,
        source_lang=source_lang,
        target_lang=target_lang,
    )
    await session.run()
