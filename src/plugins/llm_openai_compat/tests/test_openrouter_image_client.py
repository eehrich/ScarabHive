"""Unit tests for OpenRouterImageClient (/chat/completions image generation)."""
from __future__ import annotations

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image

from agent_system.config.models import ImageModelConfig
from agent_system.llm.image import ImageInput
from plugins.llm_openai_compat.openrouter_image_client import (
    MAX_RAW_B64_CHARS,
    OpenRouterImageClient,
    _decode_data_uri,
    build_openrouter_image,
)


def _png_bytes(size=(10, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 80, 120)).save(buf, format="PNG")
    return buf.getvalue()


def _data_uri(data: bytes, mime: str = "image/png", wrap: bool = False) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    if wrap:
        b64 = "\n".join(b64[i : i + 20] for i in range(0, len(b64), 20))
    return f"data:{mime};base64,{b64}"


async def test_openrouter_image_generate_parses_wrapped_message_images_and_cost():
    out_png = _png_bytes((20, 10))
    ref_png = _png_bytes((4, 4))
    captured: dict = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Generated artwork.",
                        "images": [
                            {"type": "image_url", "image_url": {"url": _data_uri(out_png, wrap=True)}},
                        ],
                    },
                }],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 1120,
                    "total_tokens": 1170,
                    "cost": 0.034,
                },
            },
        )

    client = OpenRouterImageClient(model="google/gemini-nano-banana-2.1", api_key="or-key")
    req_hook = AsyncMock()
    resp_hook = AsyncMock()
    with patch.object(httpx.AsyncClient, "post", fake_post), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_request", req_hook), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_response", resp_hook):
        res = await client.generate(
            "Retro sunset",
            reference_images=[ImageInput(data=ref_png, label="sketch.png")],
            aspect_ratio="16:9",
            image_size="1K",
            seed=99,
        )

    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer or-key"
    assert captured["body"]["modalities"] == ["image", "text"]
    assert captured["body"]["image_config"] == {"aspect_ratio": "16:9", "image_size": "1K"}
    assert captured["body"]["seed"] == 99
    msg_content = captured["body"]["messages"][0]["content"]
    assert isinstance(msg_content, list) and msg_content[0]["type"] == "image_url"

    assert len(res.images) == 1
    assert res.images[0].data == out_png
    assert (res.images[0].width, res.images[0].height) == (20, 10)
    assert res.text == "Generated artwork."
    assert res.usage["cost"] == 0.034
    assert resp_hook.call_args.kwargs["usage"]["cost"] == 0.034
    assert base64.b64encode(ref_png).decode("ascii") not in json.dumps(req_hook.call_args.kwargs["payload"])


async def test_openrouter_image_parses_content_list_fallback():
    out_png = _png_bytes((6, 6))

    async def fake_post(self, url, headers=None, json=None):
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Here it is"},
                {"type": "image_url", "image_url": {"url": _data_uri(out_png)}},
            ]}}]},
        )

    client = OpenRouterImageClient(model="google/gemini-nano-banana-2.1", api_key="or-key")
    with patch.object(httpx.AsyncClient, "post", fake_post), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_request", AsyncMock()), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_response", AsyncMock()):
        res = await client.generate("fallback")
    assert len(res.images) == 1 and res.images[0].data == out_png and res.text == "Here it is"


@pytest.mark.parametrize("bad_url, expected", [
    ("https://evil.example/ssrf.png", "unsupported image URL"),
    ("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=", "unsupported image URL"),
    ("data:image/png;base64,PHN2Zz48L3N2Zz4=", "Unrecognised image container"),
])
async def test_openrouter_image_refuses_external_urls_svg_and_spoofed_mime(bad_url, expected):
    async def fake_post(self, url, headers=None, json=None):
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "images": [{"image_url": {"url": bad_url}}]},
                }],
            },
        )

    client = OpenRouterImageClient(model="google/gemini-nano-banana-2.1", api_key="or-key", max_retries=0)
    with patch.object(httpx.AsyncClient, "post", fake_post), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_request", AsyncMock()), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_response", AsyncMock()):
        with pytest.raises(RuntimeError, match=expected):
            await client.generate("test")


def test_openrouter_decode_checks_size_before_decoding():
    with pytest.raises(ValueError, match="exceeds"):
        _decode_data_uri("data:image/png;base64," + "A" * (MAX_RAW_B64_CHARS + 1))


async def test_openrouter_retries_429_with_retry_after_and_never_retries_read_timeout():
    out_png = _png_bytes((4, 4))
    attempts = 0

    async def fake_post_429(self, url, headers=None, json=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, text="rate limit", headers={"Retry-After": "3"})
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant",
                    "images": [{"image_url": {"url": _data_uri(out_png)}}],
                }}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost": 0.01},
            },
        )

    sleep_mock = AsyncMock()
    resp_hook = AsyncMock()
    client = OpenRouterImageClient(model="google/gemini-nano-banana-2.1", api_key="or-key", max_retries=2)
    with patch.object(httpx.AsyncClient, "post", fake_post_429), \
            patch("plugins.llm_openai_compat.openrouter_image_client._sleep_cancel", sleep_mock), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_request", AsyncMock()), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_response", resp_hook):
        res = await client.generate("ok")

    assert attempts == 2 and len(res.images) == 1
    sleep_mock.assert_awaited_once_with(3.0, None)
    assert resp_hook.call_args_list[0].kwargs.get("usage") is None
    assert resp_hook.call_args_list[1].kwargs["usage"]["cost"] == 0.01

    timeout_attempts = 0

    async def fake_post_timeout(self, url, headers=None, json=None):
        nonlocal timeout_attempts
        timeout_attempts += 1
        raise httpx.ReadTimeout("")

    with patch.object(httpx.AsyncClient, "post", fake_post_timeout), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_request", AsyncMock()), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_response", AsyncMock()):
        with pytest.raises(RuntimeError, match="ReadTimeout"):
            await client.generate("slow")
    assert timeout_attempts == 1


async def test_openrouter_handles_200_error_body_and_pre_cancel():
    async def fake_post(self, url, headers=None, json=None):
        return httpx.Response(200, json={"error": {"message": "provider down"}})

    client = OpenRouterImageClient(model="google/gemini-nano-banana-2.1", api_key="or-key", max_retries=0)
    with patch.object(httpx.AsyncClient, "post", fake_post), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_request", AsyncMock()), \
            patch("plugins.llm_openai_compat.openrouter_image_client.notify_image_response", AsyncMock()):
        with pytest.raises(RuntimeError, match="provider down"):
            await client.generate("err")

    post_mock = AsyncMock()
    with patch.object(httpx.AsyncClient, "post", post_mock):
        with pytest.raises(asyncio.CancelledError):
            await client.generate("cancel", cancellation_token=SimpleNamespace(is_cancelled=True))
    post_mock.assert_not_called()


def test_openrouter_image_builder_uses_openrouter_key_and_enforces_host(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    cfg = ImageModelConfig(provider="openrouter_image", model="google/gemini-nano-banana-2.1")
    client = build_openrouter_image(cfg, ssl_verify=True)
    assert client.api_key == "sk-or-test"
    assert client.base_url == "https://openrouter.ai/api/v1"
    assert client._ssl_verify is True

    foreign = ImageModelConfig(
        provider="openrouter_image",
        model="google/gemini-nano-banana-2.1",
        base_url="https://other.example/v1",
    )
    with pytest.raises(ValueError, match="no environment variable is configured"):
        build_openrouter_image(foreign)
