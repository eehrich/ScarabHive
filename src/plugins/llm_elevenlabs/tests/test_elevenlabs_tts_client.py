"""Tests for the ElevenLabs TTS client: wire shape, clone once, a lost clone made again."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from agent_system.llm.tts import TTSVoice
from plugins.llm_elevenlabs.elevenlabs_tts_client import ElevenLabsTTSClient

SAMPLE = b"RIFF" + b"\x00" * 64


def _client(tmp_path, **kwargs) -> ElevenLabsTTSClient:
    defaults = dict(model="eleven_v4", api_key="xi-test", max_retries=0,
                    clone_cache=tmp_path / "clones.json")
    defaults.update(kwargs)
    return ElevenLabsTTSClient(**defaults)


def _response(status=200, content=b"\x00\x00" * 100, body=None):
    request = httpx.Request("POST", "https://api.elevenlabs.io")
    if body is not None:
        return httpx.Response(status, json=body, request=request)
    return httpx.Response(status, content=content, request=request)


class _Server:
    """Fake ElevenLabs: hands out voice ids for clones, knows which ids exist."""

    def __init__(self, known=()):
        self.calls: list[tuple[str, dict]] = []
        self.known = set(known)
        self.made = 0
        self.upload_seconds = 0.0
        self.speak_seconds = 0.0

    async def post(self, client, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/v1/voices/add"):
            await asyncio.sleep(self.upload_seconds)     # room for a second run to look meanwhile
            self.made += 1
            voice_id = f"clone{self.made}"
            self.known.add(voice_id)
            return _response(body={"voice_id": voice_id})
        voice_id = url.split("/v1/text-to-speech/")[1].split("?")[0]
        await asyncio.sleep(self.speak_seconds)
        return _response() if voice_id in self.known else _response(404, b'{"detail":"voice_not_found"}')

    def patch(self):
        server = self

        async def fake_post(self, url, **kwargs):
            return await server.post(self, url, **kwargs)
        return patch.object(httpx.AsyncClient, "post", fake_post)


@pytest.mark.asyncio
async def test_wire_shape_voice_id_pcm_model_and_key(tmp_path):
    server = _Server(known={"abc"})
    with server.patch():
        result = await _client(tmp_path).synthesize("Hallo /kvɔʁk/", voice=TTSVoice(name="abc"))
    url, kwargs = server.calls[0]
    assert url == "https://api.elevenlabs.io/v1/text-to-speech/abc?output_format=pcm_24000"
    assert kwargs["json"] == {"text": "Hallo /kvɔʁk/", "model_id": "eleven_v4"}
    assert kwargs["headers"] == {"xi-api-key": "xi-test"}
    assert (result.sample_rate, result.channels, result.sample_width) == (24000, 1, 2)
    assert result.audio_data == b"\x00\x00" * 100


@pytest.mark.asyncio
async def test_a_clone_is_uploaded_once_across_clients(tmp_path):
    server = _Server()
    voice = TTSVoice(name="clone:alex", reference_audio=SAMPLE)
    with server.patch():
        await _client(tmp_path).synthesize("Eins.", voice=voice)
        await _client(tmp_path).synthesize("Zwei.", voice=voice)   # a new client per scene
    adds = [u for u, _ in server.calls if u.endswith("/v1/voices/add")]
    spoken = [u for u, _ in server.calls if "/text-to-speech/" in u]
    assert len(adds) == 1
    upload = next(k for u, k in server.calls if u.endswith("/v1/voices/add"))
    assert upload["files"]["files"] == ("reference.wav", SAMPLE, "audio/wav")
    assert all("/text-to-speech/clone1?" in u for u in spoken) and len(spoken) == 2
    assert list(json.loads((tmp_path / "clones.json").read_text()).values()) == ["clone1"]


@pytest.mark.asyncio
async def test_a_clone_gone_from_the_account_is_made_again(tmp_path):
    server = _Server()
    voice = TTSVoice(name="clone:alex", reference_audio=SAMPLE)
    with server.patch():
        await _client(tmp_path).synthesize("Eins.", voice=voice)
        server.known.discard("clone1")                               # deleted by hand
        result = await _client(tmp_path).synthesize("Zwei.", voice=voice)
    assert server.made == 2
    assert result.metadata["voice_id"] == "clone2"
    assert list(json.loads((tmp_path / "clones.json").read_text()).values()) == ["clone2"]


@pytest.mark.asyncio
async def test_a_missing_preset_voice_is_an_error_not_a_reclone(tmp_path):
    server = _Server()
    with server.patch(), pytest.raises(httpx.HTTPStatusError):
        await _client(tmp_path).synthesize("Eins.", voice=TTSVoice(name="nope"))
    assert server.made == 0


def test_no_voice_at_all_fails_before_any_request(tmp_path):
    with pytest.raises(ValueError, match="voice id"):
        _client(tmp_path).check_voice(None)


def test_only_v4_takes_inline_ipa(tmp_path):
    assert _client(tmp_path).supports_inline_ipa is True
    assert _client(tmp_path, model="eleven_multilingual_v2").supports_inline_ipa is False


@pytest.mark.asyncio
async def test_a_404_that_is_not_voice_not_found_never_clones_again(tmp_path):
    server = _Server()
    voice = TTSVoice(name="clone:alex", reference_audio=SAMPLE)
    with server.patch():
        await _client(tmp_path).synthesize("Eins.", voice=voice)

    async def broken_path(client, url, **kwargs):           # e.g. a proxy answering 404
        server.calls.append((url, kwargs))
        if url.endswith("/v1/voices/add"):
            server.made += 1
            return _response(body={"voice_id": f"clone{server.made}"})
        return _response(404, b"Not Found")
    server.post = broken_path
    with server.patch(), pytest.raises(httpx.HTTPStatusError):
        await _client(tmp_path).synthesize("Zwei.", voice=voice)
    assert server.made == 1


@pytest.mark.asyncio
async def test_another_account_gets_its_own_clone(tmp_path):
    server = _Server()
    voice = TTSVoice(name="clone:alex", reference_audio=SAMPLE)
    with server.patch():
        await _client(tmp_path).synthesize("Eins.", voice=voice)
        await _client(tmp_path, api_key="xi-other").synthesize("Zwei.", voice=voice)
        await _client(tmp_path).synthesize("Drei.", voice=voice)
    assert server.made == 2
    assert sorted(json.loads((tmp_path / "clones.json").read_text()).values()) == ["clone1", "clone2"]


@pytest.mark.asyncio
async def test_two_runs_cloning_the_same_new_voice_make_one_clone(tmp_path):
    server = _Server()
    server.upload_seconds = 0.2
    voice = TTSVoice(name="clone:alex", reference_audio=SAMPLE)
    with server.patch():
        results = await asyncio.gather(_client(tmp_path).synthesize("Eins.", voice=voice),
                                       _client(tmp_path).synthesize("Zwei.", voice=voice))
    assert server.made == 1
    assert [r.metadata["voice_id"] for r in results] == ["clone1", "clone1"]


@pytest.mark.asyncio
async def test_two_runs_finding_the_clone_gone_make_one_new_clone(tmp_path):
    server = _Server()
    voice = TTSVoice(name="clone:alex", reference_audio=SAMPLE)
    with server.patch():
        await _client(tmp_path).synthesize("Eins.", voice=voice)
        server.known.discard("clone1")                               # deleted by hand
        server.speak_seconds = 0.2                                   # both runs meet the 404
        results = await asyncio.gather(_client(tmp_path).synthesize("Zwei.", voice=voice),
                                       _client(tmp_path).synthesize("Drei.", voice=voice))
    assert server.made == 2
    assert [r.metadata["voice_id"] for r in results] == ["clone2", "clone2"]
    assert list(json.loads((tmp_path / "clones.json").read_text()).values()) == ["clone2"]
