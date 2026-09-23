"""The turn, rendered as it happens, in one message that keeps being edited.

The web UI streams a turn as server-sent events (app/streaming.py): `status`
while tools run, `card` as each returns, `delta` for the synthesis. Telegram
has no stream, but it has `editMessageText`, and the status lines are already
written as sentences a person can read -- "Running mobula token details",
"Writing the due-diligence verdict". So the bot posts one placeholder and
edits it as the work moves.

This is not decoration. A deep dive is sixty to seventy seconds; a chat with
no output for a minute looks broken, and the user sends the question again --
which costs another turn and another set of provider calls.

Nothing about the turn changes: `streaming.attach` is the same public hook the
SSE endpoint uses, and a turn nobody is watching emits into a queue nobody
reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time

from app import streaming
from app.settings import settings
from app.telegram import client

logger = logging.getLogger(__name__)

# Shown until the first tool reports, so the first edit is never the first
# sign of life -- the placeholder itself is.
OPENING = "🔎 Working on it…"
_MAX_TRAIL = 3


class Progress:
    """One live message for one turn."""

    def __init__(self, chat_id: int | str, thread_id: int | None = None) -> None:
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.message_id: int | None = None
        self._lines: list[str] = []
        self._last_edit = 0.0
        self._queue: asyncio.Queue = asyncio.Queue()
        self._token = None
        self._pump: asyncio.Task | None = None

    async def __aenter__(self) -> "Progress":
        sent = await client.send_message(self.chat_id, OPENING, thread_id=self.thread_id)
        self.message_id = (sent or {}).get("message_id")
        self._token = streaming.attach(self._queue)
        self._pump = asyncio.create_task(self._consume())
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._token is not None:
            streaming.detach(self._token)
            self._token = None
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                continue
            if item.get("event") != "status":
                # `card` and `delta` are for a client that can render partial
                # output in place. Telegram cannot, and replacing the progress
                # message with half an answer would leave the user reading a
                # sentence that is about to be overwritten.
                continue
            text = str(item.get("text") or "").strip()
            if text:
                await self._push(text)

    async def _push(self, line: str) -> None:
        if self._lines and self._lines[-1] == line:
            return
        self._lines.append(line)
        now = time.monotonic()
        if now - self._last_edit < settings.telegram_progress_interval_seconds:
            # Dropped, not queued: the next status line is a better thing to
            # show than a backlog of stale ones replayed a second apart.
            return
        self._last_edit = now
        await self._render()

    async def _render(self) -> None:
        if self.message_id is None:
            return
        trail = self._lines[-_MAX_TRAIL:]
        body = "\n".join(
            f"✓ <s>{html.escape(line)}</s>" if index < len(trail) - 1 else f"⏳ {html.escape(line)}"
            for index, line in enumerate(trail)
        )
        await client.edit_message_text(self.chat_id, self.message_id, body or OPENING)

    async def finish(self, text: str, *, reply_markup: dict | None = None) -> bool:
        """Replace the progress message with the answer.

        Editing rather than deleting-and-sending keeps the answer in the place
        the user is already looking, and costs one API call instead of two.
        Returns False when there was no message to edit or Telegram refused
        it, and the caller sends the answer as a new message instead.
        """
        if self.message_id is None:
            return False
        result = await client.edit_message_text(self.chat_id, self.message_id, text, reply_markup=reply_markup)
        return result is not None

    async def fail(self, text: str) -> None:
        if self.message_id is None:
            await client.send_message(self.chat_id, text, thread_id=self.thread_id)
            return
        await client.edit_message_text(self.chat_id, self.message_id, text)
