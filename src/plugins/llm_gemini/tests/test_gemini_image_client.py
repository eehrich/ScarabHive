"""Unit tests for GeminiImageClient (native Gemini :generateContent image generation)."""
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
from plugins.llm_gemini.gemini_image_client import (
    GeminiImageClient,
    MAX_RAW_B64_CHARS,
    _decode_b64_image,
    build_gemini_image,
)


def _png_bytes(size=(8, 6)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def test_gemini_image_generate_sends_image_config_refs_and_extracts_usage():
    out_png = _png_bytes((16, 12))
    ref_png = _png_bytes((4, 4))
    captured: dict = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return httpx.Response(
            200,
            json={
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {"text": "thinking step", "thought": True},
                            {"text": "Here is the scene."},
                            {"inlineData": {"mimeType": "image/png", "data": _b64(out_png)}},
                        ]
                    },
                }],
                "usageMetadata": {
                    "promptTokenCount": 120,
                    "candidatesTokenCount": 1120,
                    "thoughtsTokenCount": 80,
                    "totalTokenCount": 1320,
                },
            },
        )

    client = GeminiImageClient(
        model="gemini-nano-banana-2.1",
        api_key="test-key",
        default_aspect_ratio="4:3",
        default_image_size="1K",
    )
    req_hook = AsyncMock()
    resp_hook = AsyncMock()
    with patch.object(httpx.AsyncClient, "post", fake_post), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_request", req_hook), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_response", resp_hook):
        result = await client.generate(
            "Cyberpunk castle",
            reference_images=[ImageInput(data=ref_png, label="ref.png")],
            aspect_ratio="16:9",
            image_size="2K",
            seed=7,
        )

    assert captured["url"].endswith("/models/gemini-nano-banana-2.1:generateContent")
    assert captured["headers"]["x-goog-api-key"] == "test-key"
    gen_cfg = captured["body"]["generationConfig"]
    assert gen_cfg["responseModalities"] == ["TEXT", "IMAGE"]
    assert gen_cfg["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
    assert gen_cfg["seed"] == 7
    parts = captured["body"]["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/png"
    assert parts[1] == {"text": "Cyberpunk castle"}

    assert len(result.images) == 1
    assert result.images[0].data == out_png
    assert (result.images[0].width, result.images[0].height) == (16, 12)
    assert result.text == "Here is the scene."
    assert result.usage["prompt_tokens"] == 120
    assert result.usage["completion_tokens"] == 1200
    assert result.usage["completion_tokens_details"] == {"reasoning_tokens": 80}

    hook_payload = req_hook.call_args.kwargs["payload"]
    assert _b64(ref_png) not in json.dumps(hook_payload)
    assert hook_payload["reference_images"] == [{"mime_type": "image/png", "bytes": len(ref_png), "label": "ref.png"}]
    assert resp_hook.call_args.kwargs["usage"] == result.usage


async def test_gemini_image_applies_default_aspect_ratio_and_size():
    out_png = _png_bytes((4, 4))
    captured: dict = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["body"] = json
        return httpx.Response(
            200,
            json={"candidates": [{"finishReason": "STOP", "content": {
                "parts": [{"inlineData": {"mimeType": "image/png", "data": _b64(out_png)}}],
            }}]},
        )

    client = GeminiImageClient(
        model="gemini-nano-banana-2.1", api_key="k",
        default_aspect_ratio="4:3", default_image_size="2K",
    )
    with patch.object(httpx.AsyncClient, "post", fake_post), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_request", AsyncMock()), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_response", AsyncMock()):
        await client.generate("castle")

    assert captured["body"]["generationConfig"]["imageConfig"] == {"aspectRatio": "4:3", "imageSize": "2K"}


async def test_gemini_image_retries_503_honouring_retry_after_without_double_billing():
    out_png = _png_bytes((4, 4))
    attempts = 0

    async def fake_post(self, url, headers=None, json=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="overloaded", headers={"Retry-After": "4"})
        return httpx.Response(
            200,
            json={
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": _b64(out_png)}}]},
                }],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 100, "totalTokenCount": 110},
            },
        )

    client = GeminiImageClient(model="gemini-nano-banana-2.1", api_key="k", max_retries=2)
    sleep_mock = AsyncMock()
    resp_hook = AsyncMock()
    with patch.object(httpx.AsyncClient, "post", fake_post), \
            patch("plugins.llm_gemini.gemini_image_client._sleep_cancel", sleep_mock), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_request", AsyncMock()), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_response", resp_hook):
        res = await client.generate("a tree")

    assert attempts == 2 and len(res.images) == 1
    sleep_mock.assert_awaited_once_with(4.0, None)
    assert resp_hook.call_count == 2
    assert resp_hook.call_args_list[0].kwargs.get("usage") is None
    assert resp_hook.call_args_list[1].kwargs["usage"]["total_tokens"] == 110


async def test_gemini_image_does_not_retry_read_timeout_and_logs_nonempty_error():
    attempts = 0

    async def fake_post(self, url, headers=None, json=None):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("")  # empty message: must still log a non-empty error string

    client = GeminiImageClient(model="gemini-nano-banana-2.1", api_key="k", max_retries=3)
    resp_hook = AsyncMock()
    with patch.object(httpx.AsyncClient, "post", fake_post), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_request", AsyncMock()), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_response", resp_hook):
        with pytest.raises(RuntimeError, match="ReadTimeout"):
            await client.generate("slow")

    assert attempts == 1
    assert resp_hook.call_count == 1
    assert resp_hook.call_args.kwargs["error"] == "ReadTimeout"


@pytest.mark.parametrize("resp_obj, expected", [
    (httpx.Response(302, headers={"Location": "https://other.example"}), "HTTP 302"),
    (httpx.Response(200, json=["not", "an", "object"]), "expected JSON object"),
])
async def test_gemini_image_handles_redirect_and_nondict_json_with_paired_hook(resp_obj, expected):
    async def fake_post(self, url, headers=None, json=None):
        return resp_obj

    client = GeminiImageClient(model="gemini-nano-banana-2.1", api_key="k", max_retries=0)
    resp_hook = AsyncMock()
    with patch.object(httpx.AsyncClient, "post", fake_post), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_request", AsyncMock()), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_response", resp_hook):
        with pytest.raises(RuntimeError, match=expected):
            await client.generate("test")

    assert resp_hook.call_count == 1 and expected in resp_hook.call_args.kwargs["error"]


async def test_gemini_image_pre_cancelled_token_sends_no_request():
    post_mock = AsyncMock()
    client = GeminiImageClient(model="gemini-nano-banana-2.1", api_key="k")
    with patch.object(httpx.AsyncClient, "post", post_mock):
        with pytest.raises(asyncio.CancelledError):
            await client.generate("test", cancellation_token=SimpleNamespace(is_cancelled=True))
    post_mock.assert_not_called()


async def test_gemini_image_reports_safety_block():
    async def fake_post(self, url, headers=None, json=None):
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    client = GeminiImageClient(model="gemini-nano-banana-2.1", api_key="k", max_retries=0)
    with patch.object(httpx.AsyncClient, "post", fake_post), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_request", AsyncMock()), \
            patch("plugins.llm_gemini.gemini_image_client.notify_image_response", AsyncMock()):
        with pytest.raises(RuntimeError, match="prompt blocked: SAFETY"):
            await client.generate("blocked")


def test_gemini_image_decode_checks_size_before_decoding_and_handles_line_wraps():
    png = _png_bytes((4, 4))
    wrapped = "\n".join(_b64(png)[i : i + 16] for i in range(0, len(_b64(png)), 16))
    decoded = _decode_b64_image(wrapped)
    assert decoded.data == png and decoded.mime_type == "image/png"

    with pytest.raises(ValueError, match="exceeds"):
        _decode_b64_image("A" * (MAX_RAW_B64_CHARS + 1))


@pytest.mark.parametrize("bad_url", [
    "https://other-host.example/v1beta",
    "http://generativelanguage.googleapis.com/v1beta",
    "https://storage.googleapis.com/bucket",
    "https://user:topsecret@other-host.example/v1beta",
])
def test_gemini_image_key_follows_endpoint_strictly(monkeypatch, bad_url):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-google-key")
    cfg = ImageModelConfig(provider="gemini_image", model="gemini-nano-banana-2.1", base_url=bad_url)
    with pytest.raises(ValueError) as exc_info:
        build_gemini_image(cfg)
    assert "not the official HTTPS Google Gemini endpoint" in str(exc_info.value)
    assert "topsecret" not in str(exc_info.value)


def test_gemini_image_whitespace_base_url_uses_default_google_endpoint(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-google-key")
    cfg = ImageModelConfig(provider="gemini_image", model="gemini-nano-banana-2.1", base_url="   ")
    built = build_gemini_image(cfg, ssl_verify=False)
    assert built.api_key == "secret-google-key"
    assert built.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert built._ssl_verify is False
