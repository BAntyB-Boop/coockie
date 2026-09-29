"""Per-client bounded send queues so slow WebSocket clients never block automation."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable


class ClientChannel:
    def __init__(self, send: Callable[[Any], Awaitable[None]], maxsize: int = 1) -> None:
        self.send = send
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.sent = 0
        self.dropped = 0
        self.closed = False
        self.view = "annotated"
        self.min_interval = 0.0
        self.last_sent_t = 0.0
        self.last_frame_id = -1

    def offer(self, item: Any) -> None:
        """Non-blocking enqueue; drops the oldest item when full (latest-only for maxsize=1)."""
        if self.closed:
            return
        while True:
            try:
                self.queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass

    async def pump(self) -> None:
        try:
            while not self.closed:
                item = await self.queue.get()
                await self.send(item)
                self.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self.closed = True


class Hub:
    def __init__(self) -> None:
        self.channels: set[ClientChannel] = set()

    def add(self, ch: ClientChannel) -> None:
        self.channels.add(ch)

    def remove(self, ch: ClientChannel) -> None:
        ch.closed = True
        self.channels.discard(ch)

    def __len__(self) -> int:
        return len(self.channels)

    def broadcast(self, item: Any) -> None:
        for ch in list(self.channels):
            ch.offer(item)
