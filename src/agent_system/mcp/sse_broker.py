from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


class SSEBroker:
    """Simple in-process SSE broker using aiohttp.

    Endpoints:
    - GET /status/stream -> SSE stream
    - POST /status/publish -> publish an event (JSON body)
    """

    def __init__(self):
        self._clients: list[asyncio.Queue] = []

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        queue: asyncio.Queue = asyncio.Queue()
        self._clients.append(queue)
        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
        resp = web.StreamResponse(status=200, headers=headers)
        await resp.prepare(request)

        logger.info("SSE client connected: %s", request.remote)

        try:
            while True:
                ev = await queue.get()
                # Ensure stable JSON shape
                data = json.dumps(ev, ensure_ascii=False)
                chunk = f"data: {data}\n\n"
                await resp.write(chunk.encode("utf-8"))
                try:
                    await resp.drain()
                except Exception:
                    # client probably disconnected
                    break
        except asyncio.CancelledError:
            pass
        finally:
            try:
                self._clients.remove(queue)
            except ValueError:
                pass
            logger.info("SSE client disconnected: %s", request.remote)

        return resp

    async def handle_publish(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        await self.publish(payload)
        return web.json_response({"result": "ok"})

    async def publish(self, payload: dict[str, Any]) -> None:
        # copy list to avoid mutation during iteration
        clients = list(self._clients)
        for q in clients:
            try:
                await q.put(payload)
            except Exception:
                # ignore individual failures
                logger.debug("failed to put event into client queue")


def make_app(broker: SSEBroker | None = None) -> web.Application:
    broker = broker or SSEBroker()
    app = web.Application()
    app.add_routes([
        web.get("/status/stream", broker.handle_stream),
        web.post("/status/publish", broker.handle_publish),
    ])
    # attach broker for external access
    app["_sse_broker"] = broker
    return app


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    app = make_app()
    logger.info("Starting SSE broker on http://%s:%s", host, port)
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    ns = p.parse_args()
    run(ns.host, ns.port)
