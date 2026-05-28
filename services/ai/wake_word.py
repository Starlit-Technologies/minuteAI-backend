"""Wake word detector for live transcript streams.

Scans final transcript tokens for the bot name. Once detected, buffers
subsequent tokens as a command. Fires after SILENCE_SECONDS of no new tokens.
"""

import asyncio
import re
import logging
from typing import Callable, Awaitable

log = logging.getLogger(__name__)

SILENCE_SECONDS = 3.0
MIN_COMMAND_WORDS = 2


class WakeWordDetector:
    def __init__(self, bot_name: str = "Steph"):
        self.bot_name = bot_name
        self._collecting = False
        self._tokens: list[str] = []
        self._timer: asyncio.Task | None = None
        self._callback: Callable[[str], Awaitable[None]] | None = None

    def on_command(self, callback: Callable[[str], Awaitable[None]]) -> "WakeWordDetector":
        self._callback = callback
        return self

    def _matches(self, text: str) -> bool:
        name = re.escape(self.bot_name)
        return bool(re.search(rf'\b{name}\b', text, flags=re.IGNORECASE))

    def _after_name(self, text: str) -> str:
        """Return text that comes after the bot name (strips leading occurrence)."""
        name = re.escape(self.bot_name)
        cleaned = re.sub(rf'^.*?\b{name}\b[,\.]?\s*', '', text, count=1, flags=re.IGNORECASE)
        return cleaned.strip()

    async def process(self, text: str) -> None:
        """Call with each final transcript token."""
        if not self._collecting:
            if self._matches(text):
                self._collecting = True
                self._tokens = []
                rest = self._after_name(text)
                if rest:
                    self._tokens.append(rest)
                log.debug("Wake word detected. Collecting command…")
                self._reschedule()
        else:
            if self._timer and not self._timer.done():
                self._timer.cancel()
            # Strip the name if it appears again (e.g. repetition)
            rest = self._after_name(text) if self._matches(text) else text.strip()
            if rest:
                self._tokens.append(rest)
            self._reschedule()

    def _reschedule(self) -> None:
        self._timer = asyncio.create_task(self._fire_after_silence())

    async def _fire_after_silence(self) -> None:
        try:
            await asyncio.sleep(SILENCE_SECONDS)
        except asyncio.CancelledError:
            return
        if not self._collecting:
            return
        command = " ".join(self._tokens).strip()
        self._collecting = False
        self._tokens = []
        if len(command.split()) >= MIN_COMMAND_WORDS and self._callback:
            log.debug("Firing wake word command: %s", command[:80])
            try:
                await self._callback(command)
            except Exception as exc:
                log.error("Wake word callback error: %s", exc)

    def cancel(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._collecting = False
        self._tokens = []
