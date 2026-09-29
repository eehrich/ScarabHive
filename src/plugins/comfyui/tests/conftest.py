"""No test reaches a real ComfyUI.

The server's first tool call syncs the job tracker with the queue of its configured host -- 127.0.0.1:8188 in most
tests, where a real ComfyUI may be running (it does on the render host). Every connection the client would open is
refused at aiohttp's connector instead; a test that stands in for ``aiohttp.ClientSession`` never gets that far.
"""
import aiohttp
import aiohttp.connector
import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def refused(self, req, *args, **kwargs):
        raise aiohttp.ClientConnectorError(req.connection_key, OSError(111, f"tests do not connect to {req.url.host}"))

    monkeypatch.setattr(aiohttp.connector.TCPConnector, "_create_connection", refused)
