"""One WebSocket connection to the OpenAI Realtime API (GA).

The caller reads events itself, one at a time, with a timeout per event: no
background task and no queue. The beta version of this class re-queued every
event it was not waiting for, so its timeouts never fired, and it kept polling
a closed socket forever.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_URL = "wss://api.openai.com/v1/realtime"


class RealtimeError(Exception):
    """The Realtime connection failed, closed, or went silent."""


def realtime_url(base_url: str | None) -> str:
    """The WebSocket endpoint that belongs to an HTTP base URL."""
    if not base_url:
        return DEFAULT_URL
    return "ws" + base_url.rstrip("/").removeprefix("http") + "/realtime"


class RealtimeSession:
    """Usage::

        async with RealtimeSession("gpt-realtime-mini", api_key) as session:
            await session.send_event({"type": "response.create", "response": {...}})
            while (event := await session.next_event(60))["type"] != "response.done":
                ...
    """

    def __init__(self, model: str, api_key: str, base_url: str = DEFAULT_URL,
                 ssl_context: Optional[ssl.SSLContext] = None):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.ssl_context = ssl_context  # None: the default verification
        self.ws: Optional[Any] = None

    async def __aenter__(self) -> "RealtimeSession":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def connect(self, timeout: float = 10.0) -> None:
        """Open the socket and wait for ``session.created``."""
        import websockets

        if not self.api_key or self.api_key == "None":
            raise ValueError("API key is required for Realtime API connection")
        self.ws = await websockets.connect(
            f"{self.base_url}?model={self.model}",
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            ping_interval=20,
            ping_timeout=10,
            **({"ssl": self.ssl_context} if self.ssl_context is not None else {}),
        )
        try:
            event = await self.next_event(timeout)
            if event.get("type") != "session.created":
                raise RealtimeError(f"expected session.created, got {event.get('type')}: {event.get('error', '')}")
        except BaseException:
            # `async with` does not call __aexit__ when __aenter__ raises.
            await self.close()
            raise

    async def send_event(self, event: dict[str, Any]) -> None:
        if self.ws is None:
            raise RealtimeError("not connected")
        await self.ws.send(json.dumps(event))

    async def next_event(self, timeout: float) -> dict[str, Any]:
        """The next server event; RealtimeError when none comes or the socket closed."""
        import websockets

        if self.ws is None:
            raise RealtimeError("not connected")
        try:
            message = await asyncio.wait_for(self.ws.recv(), timeout)
        except asyncio.TimeoutError as e:
            raise RealtimeError(f"no event from the Realtime API within {timeout:g}s") from e
        except websockets.ConnectionClosed as e:
            raise RealtimeError(f"Realtime connection closed: {e}") from e
        return json.loads(message)

    async def close(self) -> None:
        if self.ws is None:
            return
        ws, self.ws = self.ws, None
        try:
            await ws.close()
        except Exception as e:  # noqa: BLE001 -- closing must not mask the real error
            logger.warning("Error closing Realtime WebSocket: %s", e)
