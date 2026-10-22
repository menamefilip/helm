"""
Server-Sent Events broadcaster for real-time push notifications.

Why SSE over WebSocket: one-way server→client push is all we need
(run events, needs_attention, new messages). SSE is simpler (HTTP-based,
auto-reconnect, works through proxies).

Sync + async: the broadcaster holds plain lists of asyncio.Queues.
Sync code calls broadcast_sync() which uses asyncio.run_coroutine_threadsafe.
Async code (SSE endpoint) awaits queue.get().
"""

import asyncio
import json
import logging
import threading
from typing import Any

logger = logging.getLogger("helm.sse")


class SSEBroadcaster:
    """Manages SSE client connections and broadcasts events.

    Thread-safe: sync code calls broadcast_sync(), async code uses broadcast().
    """

    def __init__(self) -> None:
        self._queues: dict[int, asyncio.Queue] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def register_sync(self) -> tuple[int, asyncio.Queue]:
        """Register a new client (sync version for setup)."""
        with self._lock:
            self._counter += 1
            client_id = self._counter
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._queues[client_id] = queue
        return client_id, queue

    def unregister_sync(self, client_id: int) -> None:
        """Remove a client (sync version)."""
        self._queues.pop(client_id, None)

    async def broadcast(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Broadcast to all clients (async version)."""
        payload = {"event": event, "data": data or {}}
        dead: list[int] = []
        for cid, queue in list(self._queues.items()):
            try:
                queue.put_nowait(payload)
            except (asyncio.QueueFull, Exception):
                dead.append(cid)
        for cid in dead:
            self._queues.pop(cid, None)

    def broadcast_sync(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Broadcast to all clients (sync version, callable from sync code).

        Uses the running event loop if available, otherwise drops the message.
        """
        payload = {"event": event, "data": data or {}}
        loop = self._get_loop()
        if loop is None:
            # No running loop — just push directly (best effort)
            self._push_direct(payload)
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self.broadcast(event, data), loop)
            # Don't wait for result — fire and forget
        except RuntimeError:
            self._push_direct(payload)

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        """Get the running event loop, if any."""
        try:
            loop = asyncio.get_running_loop()
            return loop
        except RuntimeError:
            return None

    def _push_direct(self, payload: dict) -> None:
        """Push to queues directly (no asyncio). Used as fallback."""
        dead: list[int] = []
        for cid, queue in list(self._queues.items()):
            try:
                queue.put_nowait(payload)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self._queues.pop(cid, None)


# Global singleton
sse_broadcaster = SSEBroadcaster()
