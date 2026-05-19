"""
WebSocket endpoints for real-time audio translation via Deepgram.

/ws/live-translate          — mic audio from the client device
/ws/bot-live-translate/{id} — audio piped from a running meeting bot

Message types sent to client:
  { "type": "partial",   "text": "...", "speaker": 0, "speaker_label": "Speaker 1", "buffering": true }
  { "type": "result",    "source_text": "...", "translated_text": "...", "speaker": 0,
                         "speaker_label": "Speaker 1", "source_language": "fr", "backend": "deepl" }
  { "type": "error",     "message": "..." }
  { "type": "connected", "message": "..." }

Client → Server (mic endpoint only):
  - Binary frames: raw PCM16 16kHz mono audio
  - Text: '{"action":"flush"}' or '{"action":"stop"}'
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

    async def _send_partial(self, text: str, speaker: Optional[int] = None) -> None:
        payload: dict = {"type": "partial", "text": text, "buffering": True}
        if speaker is not None:
            payload["speaker"] = speaker
            payload["speaker_label"] = f"Speaker {speaker + 1}"
        await self._send(payload)

    async def _translate_and_send(
        self, source_text: str, source_lang: str, speaker: Optional[int] = None
    ) -> None:
        if not source_text or not _is_meaningful(source_text):
            return

        effective_source = source_lang if source_lang != "auto" else "en"
        spk_payload: dict = {}
        if speaker is not None:
            spk_payload = {"speaker": speaker, "speaker_label": f"Speaker {speaker + 1}"}

        # Same language → passthrough
        if effective_source == self.target_lang:
            await self._send({
                "type": "result",
                "source_text": source_text,
                "translated_text": source_text,
                "source_language": effective_source,
                "backend": "passthrough",
                **spk_payload,
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
                **spk_payload,
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
                None if self.source_lang == "auto" else self.source_lang,
                diarize=True,
            )
        except Exception as exc:
            await self._send({"type": "error", "message": f"Deepgram config error: {exc}"})
            return

        headers = {"Authorization": f"Token {api_key}"}

        try:
            async with websockets.connect(dg_url, additional_headers=headers) as dg_ws:
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

                            speaker = transcript.speaker

                            if not transcript.text and transcript.speech_final:
                                # UtteranceEnd — force flush
                                text, lang = self._flush()
                                if text:
                                    await self._translate_and_send(text, lang, speaker)
                                continue

                            if not transcript.is_final:
                                # Interim result — send partial to show live words
                                await self._send_partial(
                                    self._sentence_buffer + " " + transcript.text,
                                    speaker,
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
                                        await self._translate_and_send(text, lang, speaker)
                                else:
                                    await self._send_partial(self._sentence_buffer, speaker)

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


# ── Bot live translate ─────────────────────────────────────────────────────────

class BotLiveTranslateSession:
    """
    Live translation fed by the meeting bot's captured audio rather than the
    user's microphone.  Audio arrives as raw PCM16 16kHz mono bytes via an
    in-memory asyncio.Queue supplied by the bot runner.
    """

    def __init__(
        self,
        client_ws: WebSocket,
        audio_queue: asyncio.Queue,
        source_lang: str,
        target_lang: str,
    ):
        self.client_ws = client_ws
        self.audio_queue = audio_queue
        self.source_lang = source_lang
        self.target_lang = target_lang
        # Per-speaker sentence buffers so each speaker gets their own translation
        self._buffers: dict[Optional[int], str] = {}
        self._detected_lang: Optional[str] = None

    async def _send(self, payload: dict) -> None:
        try:
            await self.client_ws.send_text(json.dumps(payload))
        except Exception:
            pass

    def _has_sentence_end(self, text: str) -> bool:
        import re
        return bool(re.search(r'[.!?।۔。！？]\s*$', text))

    async def _translate_and_send(
        self, source_text: str, speaker: Optional[int], source_lang: str
    ) -> None:
        if not source_text or not _is_meaningful(source_text):
            return

        effective_source = source_lang if source_lang != "auto" else "en"
        spk_payload = (
            {"speaker": speaker, "speaker_label": f"Speaker {speaker + 1}"}
            if speaker is not None else {}
        )

        try:
            if effective_source == self.target_lang:
                translated, backend = source_text, "passthrough"
            else:
                source_name = LANGUAGE_NAMES.get(effective_source, effective_source.upper())
                target_name = LANGUAGE_NAMES.get(self.target_lang, self.target_lang.upper())
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
                **spk_payload,
            })
        except Exception as exc:
            await self._send({"type": "error", "message": f"Translation failed: {exc}"})

    async def run(self) -> None:
        try:
            dg_url, api_key = _build_url(
                None if self.source_lang == "auto" else self.source_lang,
                diarize=True,
            )
        except Exception as exc:
            await self._send({"type": "error", "message": f"Deepgram config error: {exc}"})
            return

        headers = {"Authorization": f"Token {api_key}"}

        try:
            async with websockets.connect(dg_url, additional_headers=headers) as dg_ws:
                await self._send({"type": "connected", "message": "Bot audio streaming active"})
                log.info("Bot live translate connected to Deepgram")

                async def _recv_from_deepgram():
                    try:
                        async for raw_msg in dg_ws:
                            if isinstance(raw_msg, bytes):
                                continue

                            transcript = parse_deepgram_message(raw_msg)
                            if transcript is None:
                                continue

                            speaker = transcript.speaker
                            lang = self._detected_lang or self.source_lang

                            if not transcript.text and transcript.speech_final:
                                # Flush all pending speaker buffers
                                for spk, buf in list(self._buffers.items()):
                                    if buf.strip():
                                        await self._translate_and_send(buf.strip(), spk, lang)
                                self._buffers.clear()
                                continue

                            if not transcript.is_final:
                                partial = (self._buffers.get(speaker, "") + " " + transcript.text).strip()
                                spk_payload = (
                                    {"speaker": speaker, "speaker_label": f"Speaker {speaker + 1}"}
                                    if speaker is not None else {}
                                )
                                await self._send({
                                    "type": "partial",
                                    "text": partial,
                                    "buffering": True,
                                    **spk_payload,
                                })
                                continue

                            reliable = transcript.reliable_text
                            if reliable and _is_meaningful(reliable):
                                buf = (self._buffers.get(speaker, "") + " " + reliable).strip()
                                self._buffers[speaker] = buf
                                if self._has_sentence_end(buf) or transcript.speech_final:
                                    await self._translate_and_send(buf, speaker, lang)
                                    self._buffers[speaker] = ""
                                else:
                                    spk_payload = (
                                        {"speaker": speaker, "speaker_label": f"Speaker {speaker + 1}"}
                                        if speaker is not None else {}
                                    )
                                    await self._send({"type": "partial", "text": buf, "buffering": True, **spk_payload})

                    except Exception as exc:
                        log.warning("Bot Deepgram recv error: %s", exc)

                async def _pump_bot_audio():
                    try:
                        while True:
                            try:
                                pcm = await asyncio.wait_for(self.audio_queue.get(), timeout=30.0)
                            except asyncio.TimeoutError:
                                log.warning("Bot audio: no audio received in 30s — bot may have left")
                                break
                            if pcm is None:  # sentinel from unregister
                                break
                            await dg_ws.send(pcm)
                    except Exception as exc:
                        log.warning("Bot audio pump error: %s", exc)
                    finally:
                        try:
                            await dg_ws.send(json.dumps({"type": "CloseStream"}))
                        except Exception:
                            pass

                await asyncio.gather(
                    _recv_from_deepgram(),
                    _pump_bot_audio(),
                    return_exceptions=True,
                )

        except Exception as exc:
            log.error("Bot Deepgram connection failed: %s", exc)
            await self._send({"type": "error", "message": f"Deepgram connection failed: {exc}"})


@router.websocket("/ws/bot-live-translate/{session_id}")
async def bot_live_translate_ws(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(...),
    target_lang: str = Query("en"),
    source_lang: str = Query("auto"),
):
    """
    Live translation of a meeting bot's captured audio.
    The bot runner pipes PCM audio into an in-memory queue; this endpoint
    drains that queue and forwards to Deepgram with diarization enabled.
    """
    try:
        verify_token_string(token)
    except Exception:
        await websocket.close(code=4001)
        return

    if not deepgram_configured():
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "Deepgram is not configured on this server.",
        }))
        await websocket.close(code=4000)
        return

    from services.meeting_bots.runner import register_bot_audio_listener, unregister_bot_audio_listener
    audio_q = register_bot_audio_listener(session_id)

    await websocket.accept()
    try:
        session = BotLiveTranslateSession(
            client_ws=websocket,
            audio_queue=audio_q,
            source_lang=source_lang,
            target_lang=target_lang,
        )
        await session.run()
    finally:
        unregister_bot_audio_listener(session_id)
