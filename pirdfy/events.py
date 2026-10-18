"""Thread-safe publish/subscribe bridge from worker threads to async SSE clients."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, max_queue: int = 100):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._max_queue = max_queue

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Callable from any thread. Slow clients drop events instead of blocking workers."""
        loop = self._loop
        if loop is None or not self._subscribers or loop.is_closed():
            return
        payload = json.dumps({"ts": time.time(), **(data or {})}, default=str)
        message = f"event: {event}\ndata: {payload}\n\n"
        with contextlib.suppress(RuntimeError):  # loop shutting down
            loop.call_soon_threadsafe(self._fanout, message)

    def _fanout(self, message: str) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                log.debug("SSE subscriber queue full; dropping event")
