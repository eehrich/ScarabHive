"""Unit tests for the speech_gen tool server plugin."""
from __future__ import annotations

import asyncio
import os
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_system.config.models import (
    AgentSystemConfig,
    LLMSystemConfig,
    TTSModelConfig,
    TTSProfile,
    ToolServerConfig,
)
from agent_system.llm.tts import TTSClient, TTSResult, TTSVoice
from agent_system.core.cancellation import CancellationToken
from plugins.file_checkpoints.targets import is_untracked
from plugins.speech_gen.server import (
    DEFAULT_MAX_REFERENCE_MB,
    DEFAULT_MAX_TEXT_CHARS,
    SpeechGenServer,
    _has_windows_ads,
    _is_windows_reserved_name,
    _save_wav_atomic,
)


def _pcm_bytes(seconds: float = 0.1, rate: int = 24000, width: int = 2, channels: int = 1) -> bytes:
    return b"\x00\x01" * int(seconds * rate * channels)


def _make_server(tmp_path: Path, *, use_nested_config: bool = False, **extra) -> SpeechGenServer:
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    sys_cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            tts_models={
                "gm": TTSModelConfig(provider="gemini_tts", model="gemini-3.8-flash-tts", voice="Algenib"),
                "el": TTSModelConfig(provider="elevenlabs", model="eleven_v4"),
            },
            tts_profiles={
                "gemini-tts": TTSProfile(model_ref="gm", description="Gemini Flash TTS"),
                "elevenlabs": TTSProfile(model_ref="el", description="ElevenLabs v4"),
            },
        )
    )
    opts = {"output_directories": [str(out_dir)], "input_directories": [str(out_dir)], **extra}
    if use_nested_config:
        srv_cfg = ToolServerConfig(type="speech_gen", enabled=True, config=opts)
    else:
        srv_cfg = ToolServerConfig(type="speech_gen", enabled=True, **opts)
    return SpeechGenServer("speech_gen", sys_cfg, srv_cfg)


class _SingleAndMultiClient(TTSClient):
    supports_voice_cloning = False
    supports_style_prompt = True

    def __init__(self, result: TTSResult | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._result = result or TTSResult(
            audio_data=_pcm_bytes(0.25),
            sample_rate=24000,
            sample_width=2,
            channels=1,
            model="gemini-3.8-flash-tts",
            voice_name="Algenib",
        )

    async def synthesize(self, text, *, voice=None, language=None, system_instruction=None, seed=None):
        self.calls.append(("single", {
            "text": text, "voice": voice, "system_instruction": system_instruction, "seed": seed,
        }))
        return self._result

    async def synthesize_multi_speaker(self, text, *, speakers, language=None):
        self.calls.append(("multi", {"text": text, "speakers": speakers}))
        return self._result


class _CloningNoStyleClient(TTSClient):
    supports_voice_cloning = True
    supports_style_prompt = False

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def check_voice(self, voice: TTSVoice | None) -> None:
        super().check_voice(voice)
        if voice is None or (not voice.name and voice.reference_audio is None):
            raise ValueError("voice or reference_audio required")

    async def synthesize(self, text, *, voice=None, language=None, system_instruction=None, seed=None):
        self.calls.append({
            "text": text, "voice": voice, "system_instruction": system_instruction, "seed": seed,
        })
        return TTSResult(
            audio_data=_pcm_bytes(0.1),
            sample_rate=24000,
            sample_width=2,
            channels=1,
            model="eleven_v4",
            voice_name=voice.name if voice else "",
        )


def test_schema_reflects_profiles_default_cloning_and_config_guards(tmp_path):
    no_default = _make_server(tmp_path, profiles=["gemini-tts"])
    tools = {t["function"]["name"]: t["function"] for t in no_default.get_tools()}
    assert set(tools) == {"speech_gen_synthesize", "speech_gen_list_profiles"}
    params = tools["speech_gen_synthesize"]["parameters"]
    assert params["properties"]["profile"]["enum"] == ["gemini-tts"]
    assert "profile" in params["required"]
    assert "reference_audio" not in params["properties"]
    assert "reference_text" not in params["properties"]

    with_cloning = _make_server(
        tmp_path, use_nested_config=True,
        profiles=["elevenlabs"], default_profile="elevenlabs", allow_voice_cloning=True,
    )
    params2 = {
        t["function"]["name"]: t["function"] for t in with_cloning.get_tools()
    }["speech_gen_synthesize"]["parameters"]
    assert "profile" not in params2["required"]
    assert "reference_audio" in params2["properties"]
    assert "reference_text" in params2["properties"]

    # Blank allowlist fails closed; invalid numeric settings fall back to defaults.
    assert _make_server(tmp_path, profiles=["   "])._visible_profiles() == {}
    bad_nums = _make_server(tmp_path, max_text_chars=True, max_reference_size_mb="inf")
    assert bad_nums.max_text_chars == DEFAULT_MAX_TEXT_CHARS
    assert bad_nums.max_reference_size_mb == DEFAULT_MAX_REFERENCE_MB


async def test_list_profiles_returns_allowed_metadata(tmp_path):
    server = _make_server(
        tmp_path, profiles=["gemini-tts"], default_profile="gemini-tts", max_text_chars=2500,
    )
    res = await server.call_with_status("speech_gen_list_profiles", {})
    assert res["status"] == "success"
    assert res["default_profile"] == "gemini-tts"
    assert res["allow_voice_cloning"] is False
    assert res["max_text_chars"] == 2500
    assert len(res["profiles"]) == 1
    assert res["profiles"][0]["provider"] == "gemini_tts"
    assert res["profiles"][0]["default_voice"] == "Algenib"


async def test_synthesize_writes_valid_wav_and_attaches_preview_only_when_asked(tmp_path):
    server = _make_server(tmp_path, default_profile="gemini-tts")
    client = _SingleAndMultiClient()
    out_wav = tmp_path / "out" / "hello.wav"

    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=client):
        res = await server.call_with_status(
            "speech_gen_synthesize",
            {
                "text": "Hallo Welt!",
                "output_path": str(out_wav),
                "voice": "Puck",
                "system_instruction": "Warm and calm",
                "seed": 42,
            },
        )

    assert res["status"] == "success", res
    assert out_wav.is_file()
    assert res["mime_type"] == "audio/wav"
    assert res["duration_seconds"] == 0.25
    assert "_multimodal_content" not in res  # default attach_preview=False
    with wave.open(str(out_wav), "rb") as wf:
        assert (wf.getframerate(), wf.getsampwidth(), wf.getnchannels()) == (24000, 2, 1)

    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=client):
        res_preview = await server.call_with_status(
            "speech_gen_synthesize",
            {"text": "Preview", "output_path": str(out_wav), "attach_preview": True},
        )
    assert res_preview["_multimodal_content"][0]["type"] == "audio"
    assert res_preview["_multimodal_content"][0]["mime_type"] == "audio/wav"

    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=client), \
            patch("plugins.speech_gen.server.attach_problem", return_value="hello.wav: over 25 MB"):
        res_big = await server.call_with_status(
            "speech_gen_synthesize",
            {"text": "Big", "output_path": str(out_wav), "attach_preview": True},
        )
    assert "_multimodal_content" not in res_big and res_big["not_attached"] == "hello.wav: over 25 MB"


async def test_synthesize_multi_speaker_and_rejections(tmp_path):
    server = _make_server(tmp_path, default_profile="gemini-tts")
    multi_client = _SingleAndMultiClient()
    out_wav = tmp_path / "out" / "dialog.wav"

    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=multi_client):
        res = await server.call_with_status(
            "speech_gen_synthesize",
            {
                "text": "Joe: Hi!\nJane: Hello!",
                "output_path": str(out_wav),
                "speakers": [{"name": "Joe", "voice": "Kore"}, {"name": "Jane", "voice": "Puck"}],
            },
        )
    assert res["status"] == "success" and multi_client.calls[0][0] == "multi"

    # Client without multi-speaker is rejected before calling synthesize_multi_speaker
    single_only = _CloningNoStyleClient()
    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=single_only):
        unsupported = await server.call_with_status(
            "speech_gen_synthesize",
            {
                "text": "Joe: Hi!",
                "output_path": str(out_wav),
                "speakers": [{"name": "Joe", "voice": "Kore"}],
            },
        )
    assert unsupported["status"] == "error" and "multi-speaker" in unsupported["error"]

    # Validation errors on speakers: duplicate names, combining with voice or system_instruction
    for bad in (
        {"speakers": [{"name": "Joe", "voice": "Kore"}, {"name": "Joe", "voice": "Puck"}]},
        {"speakers": [{"name": "Joe", "voice": "Kore"}], "voice": "Puck"},
        {"speakers": [{"name": "Joe", "voice": "Kore"}], "system_instruction": "fast"},
    ):
        err = await server.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(out_wav), **bad},
        )
        assert err["status"] == "error", bad


async def test_voice_cloning_gate_clone_label_and_style_warning(tmp_path):
    ref = tmp_path / "out" / "narrator_sample.wav"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_bytes(b"RIFF" + b"\x00" * 64)
    out_wav = tmp_path / "out" / "cloned.wav"

    # Disabled by default at runtime
    locked = _make_server(tmp_path, default_profile="elevenlabs", allow_voice_cloning=False)
    refused = await locked.call_with_status(
        "speech_gen_synthesize",
        {"text": "Hi", "output_path": str(out_wav), "reference_audio": str(ref)},
    )
    assert refused["status"] == "error" and "disabled" in refused["error"]

    # Enabled: uses stem as clone label when voice is omitted, warns on ignored style/seed
    unlocked = _make_server(tmp_path, default_profile="elevenlabs", allow_voice_cloning=True)
    client = _CloningNoStyleClient()
    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=client):
        res = await unlocked.call_with_status(
            "speech_gen_synthesize",
            {
                "text": "Hi",
                "output_path": str(out_wav),
                "reference_audio": str(ref),
                "reference_text": "sample transcript",
                "system_instruction": "whisper",
                "seed": 5,
            },
        )
    assert res["status"] == "success", res
    assert client.calls[0]["voice"].name == "narrator_sample"
    assert client.calls[0]["voice"].reference_text == "sample transcript"
    assert res["warnings"] and "system_instruction" in res["warnings"][0] and "seed" in res["warnings"][0]


async def test_validation_guards_run_before_client_creation(tmp_path):
    server = _make_server(tmp_path, default_profile="gemini-tts", max_text_chars=10)
    factory_mock = AsyncMock()
    with patch("plugins.speech_gen.server.create_tts_from_profile", factory_mock):
        too_long = await server.call_with_status(
            "speech_gen_synthesize",
            {"text": "x" * 11, "output_path": str(tmp_path / "out" / "a.wav")},
        )
        assert too_long["status"] == "error" and "limit" in too_long["error"]

        wrong_ext = await server.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(tmp_path / "out" / "a.mp3")},
        )
        assert wrong_ext["status"] == "error" and ".wav" in wrong_ext["error"]

        outside = await server.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(tmp_path / "escape.wav")},
        )
        assert outside["status"] == "error" and "outside" in outside["error"].lower()

        dir_out = await server.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(tmp_path / "out")},
        )
        assert dir_out["status"] == "error" and "directory" in dir_out["error"].lower()

        blank_voice = await server.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(tmp_path / "out" / "a.wav"), "voice": "   "},
        )
        assert blank_voice["status"] == "error" and "voice" in blank_voice["error"]

        orphan_ref_text = await server.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(tmp_path / "out" / "a.wav"), "reference_text": "orphan"},
        )
        assert orphan_ref_text["status"] == "error" and "reference_text" in orphan_ref_text["error"]

    factory_mock.assert_not_called()


async def test_pre_cancelled_token_propagates_cancelled_error(tmp_path):
    server = _make_server(tmp_path, default_profile="gemini-tts")
    factory_mock = AsyncMock()
    with patch("plugins.speech_gen.server.create_tts_from_profile", factory_mock):
        with pytest.raises(asyncio.CancelledError):
            await server.call_with_status(
                "speech_gen_synthesize",
                {
                    "text": "hello",
                    "output_path": str(tmp_path / "out" / "c.wav"),
                    "_cancellation_token": SimpleNamespace(is_cancelled=True),
                },
            )
    factory_mock.assert_not_called()


async def test_midflight_and_pre_write_cancellation_leave_no_file(tmp_path):
    server = _make_server(tmp_path, default_profile="gemini-tts")
    out_wav = tmp_path / "out" / "cancelled.wav"

    started = asyncio.Event()

    class _SlowClient(_SingleAndMultiClient):
        async def synthesize(self, text, *, voice=None, language=None, system_instruction=None, seed=None):
            started.set()
            await asyncio.sleep(30)
            return self._result

    token = CancellationToken("req-midflight")
    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=_SlowClient()):
        task = asyncio.create_task(
            server.call_with_status(
                "speech_gen_synthesize",
                {
                    "text": "hello",
                    "output_path": str(out_wav),
                    "_cancellation_token": token,
                },
            )
        )
        await started.wait()
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not out_wav.exists()

    # Cancel right after synthesize returns, before disk write runs
    token2 = CancellationToken("req-prewrite")

    class _CancelOnReturnClient(_SingleAndMultiClient):
        async def synthesize(self, text, *, voice=None, language=None, system_instruction=None, seed=None):
            token2.cancel()
            return self._result

    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=_CancelOnReturnClient()):
        with pytest.raises(asyncio.CancelledError):
            await server.call_with_status(
                "speech_gen_synthesize",
                {
                    "text": "hello",
                    "output_path": str(out_wav),
                    "_cancellation_token": token2,
                },
            )
    assert not out_wav.exists()


async def test_blank_directories_fail_closed_and_windows_path_guards(tmp_path):
    for bad_dirs in ("", [""], ["   "], 123):
        srv = _make_server(
            tmp_path,
            default_profile="gemini-tts",
            output_directories=bad_dirs,
            input_directories=bad_dirs,
        )
        assert srv.output_sandbox.roots == ()
        assert srv.input_sandbox.roots == ()
        denied = await srv.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(Path.cwd() / "escape.wav")},
        )
        assert denied["status"] == "error"

    if os.name == "nt":
        assert _is_windows_reserved_name(tmp_path / "out" / "CON .wav") is True
        assert _has_windows_ads(tmp_path / "out" / "foo:bar.wav") is True
        srv = _make_server(tmp_path, default_profile="gemini-tts")
        factory_mock = AsyncMock()
        with patch("plugins.speech_gen.server.create_tts_from_profile", factory_mock):
            res_con = await srv.call_with_status(
                "speech_gen_synthesize",
                {"text": "hi", "output_path": str(tmp_path / "out" / "CON .wav")},
            )
            res_ads = await srv.call_with_status(
                "speech_gen_synthesize",
                {"text": "hi", "output_path": str(tmp_path / "out" / "foo:bar.wav")},
            )
        assert res_con["status"] == "error" and "reserved" in res_con["error"]
        assert res_ads["status"] == "error" and ":" in res_ads["error"]
        factory_mock.assert_not_called()


async def test_profile_validation_and_provider_errors(tmp_path):
    out_wav = tmp_path / "out" / "p.wav"

    # Falsy invalid profiles must fail validation instead of falling back to default_profile
    srv = _make_server(tmp_path, default_profile="gemini-tts")
    factory_mock = AsyncMock()
    with patch("plugins.speech_gen.server.create_tts_from_profile", factory_mock):
        for bad_profile in ("", "   ", False, 0, []):
            err = await srv.call_with_status(
                "speech_gen_synthesize",
                {"text": "hi", "output_path": str(out_wav), "profile": bad_profile},
            )
            assert err["status"] == "error" and "profile" in err["error"], bad_profile
    factory_mock.assert_not_called()

    # Disallowed default_profile behaves as no default (reports "profile is required")
    hidden_default = _make_server(tmp_path, profiles=["gemini-tts"], default_profile="elevenlabs")
    missing = await hidden_default.call_with_status(
        "speech_gen_synthesize", {"text": "hi", "output_path": str(out_wav)},
    )
    assert missing["status"] == "error" and "profile is required" in missing["error"]

    disallowed = await hidden_default.call_with_status(
        "speech_gen_synthesize",
        {"text": "hi", "output_path": str(out_wav), "profile": "elevenlabs"},
    )
    assert disallowed["status"] == "error" and "not allowed" in disallowed["error"]

    # Empty visible profiles
    empty_srv = _make_server(tmp_path, profiles=["   "])
    no_prof = await empty_srv.call_with_status(
        "speech_gen_synthesize", {"text": "hi", "output_path": str(out_wav)},
    )
    assert no_prof["status"] == "error" and "No TTS profiles" in no_prof["error"]

    # Factory exception & provider synthesis exception
    with patch("plugins.speech_gen.server.create_tts_from_profile", side_effect=RuntimeError("no key")):
        init_err = await srv.call_with_status(
            "speech_gen_synthesize", {"text": "hi", "output_path": str(out_wav)},
        )
    assert init_err["status"] == "error" and "Failed to initialize" in init_err["error"]

    class _BoomClient(_SingleAndMultiClient):
        async def synthesize(self, text, *, voice=None, language=None, system_instruction=None, seed=None):
            raise RuntimeError("upstream 503")

    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=_BoomClient()):
        api_err = await srv.call_with_status(
            "speech_gen_synthesize", {"text": "hi", "output_path": str(out_wav)},
        )
    assert api_err["status"] == "error" and "upstream 503" in api_err["error"]

    # Multi-speaker bounds & seed / reference_audio combinations
    unlocked = _make_server(tmp_path, default_profile="gemini-tts", allow_voice_cloning=True)
    for bad in (
        {"speakers": []},
        {"speakers": [
            {"name": "A", "voice": "Kore"},
            {"name": "B", "voice": "Puck"},
            {"name": "C", "voice": "Aoede"},
        ]},
        {"speakers": [{"name": "A", "voice": "Kore"}], "seed": 7},
        {"speakers": [{"name": "A", "voice": "Kore"}], "reference_audio": str(tmp_path / "out" / "r.wav")},
    ):
        err = await unlocked.call_with_status(
            "speech_gen_synthesize", {"text": "hi", "output_path": str(out_wav), **bad},
        )
        assert err["status"] == "error", bad


async def test_voice_cloning_rejection_sandbox_and_check_voice(tmp_path):
    in_dir = tmp_path / "refs"
    in_dir.mkdir()
    out_wav = tmp_path / "out" / "cloned.wav"
    valid_ref = in_dir / "voice.wav"
    valid_ref.write_bytes(b"RIFF" + b"\x00" * 32)
    empty_ref = in_dir / "empty.wav"
    empty_ref.write_bytes(b"")
    big_ref = in_dir / "big.wav"
    big_ref.write_bytes(b"\x00" * 2048)
    outside_ref = tmp_path / "outside.wav"
    outside_ref.write_bytes(b"RIFF" + b"\x00" * 32)

    srv = _make_server(
        tmp_path,
        default_profile="elevenlabs",
        input_directories=[str(in_dir)],
        allow_voice_cloning=True,
        max_reference_size_mb=0.001,  # ~1048 bytes
    )

    # Provider without voice cloning support
    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=_SingleAndMultiClient()):
        no_clone = await srv.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(out_wav), "reference_audio": str(valid_ref)},
        )
    assert no_clone["status"] == "error" and "does not support voice cloning" in no_clone["error"]

    client = _CloningNoStyleClient()
    with patch("plugins.speech_gen.server.create_tts_from_profile", return_value=client):
        # Outside separate input_directories sandbox
        esc = await srv.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(out_wav), "reference_audio": str(outside_ref)},
        )
        assert esc["status"] == "error" and "outside" in esc["error"].lower()

        # Missing file
        missing = await srv.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(out_wav), "reference_audio": str(in_dir / "missing.wav")},
        )
        assert missing["status"] == "error" and "not found" in missing["error"]

        # Empty file
        empty = await srv.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(out_wav), "reference_audio": str(empty_ref)},
        )
        assert empty["status"] == "error" and "empty" in empty["error"]

        # Over size limit
        over = await srv.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(out_wav), "reference_audio": str(big_ref)},
        )
        assert over["status"] == "error" and "limit" in over["error"]

        # check_voice ValueError when neither voice nor reference_audio is supplied
        no_voice = await srv.call_with_status(
            "speech_gen_synthesize",
            {"text": "hi", "output_path": str(out_wav)},
        )
        assert no_voice["status"] == "error" and "voice or reference_audio required" in no_voice["error"]

        # Explicit voice label overrides filename stem when cloning
        ok = await srv.call_with_status(
            "speech_gen_synthesize",
            {
                "text": "hi",
                "output_path": str(out_wav),
                "voice": "NarratorLabel",
                "reference_audio": str(valid_ref),
            },
        )
        assert ok["status"] == "success" and client.calls[-1]["voice"].name == "NarratorLabel"


def test_save_wav_atomic_validation_nested_dirs_and_cleanup(tmp_path):
    target = tmp_path / "nested" / "dir" / "speech.wav"
    good = TTSResult(audio_data=_pcm_bytes(0.05), sample_rate=24000, sample_width=2, channels=1, model="m")
    saved, nbytes, dur = _save_wav_atomic(good, target)
    assert saved == target and target.is_file() and nbytes == target.stat().st_size and dur == pytest.approx(0.05)

    for bad in (
        TTSResult(audio_data=b"", sample_rate=24000, sample_width=2, channels=1, model="m"),
        TTSResult(audio_data=b"\x00\x01", sample_rate=0, sample_width=2, channels=1, model="m"),
        TTSResult(audio_data=b"\x00\x01", sample_rate=True, sample_width=2, channels=1, model="m"),
        TTSResult(audio_data=b"\x00\x01", sample_rate=24000, sample_width=2, channels=True, model="m"),
        TTSResult(audio_data=b"\x00\x01", sample_rate=24000, sample_width=5, channels=1, model="m"),
        TTSResult(audio_data=b"\x00\x01", sample_rate=24000, sample_width=True, channels=1, model="m"),
        TTSResult(audio_data=b"\x00\x01\x02", sample_rate=24000, sample_width=2, channels=1, model="m"),
    ):
        with pytest.raises(ValueError):
            _save_wav_atomic(bad, target)

    # Atomic replace failure cleans up .tmp and leaves existing target intact
    original = target.read_bytes()
    with patch("plugins.speech_gen.server.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            _save_wav_atomic(good, target)
    assert target.read_bytes() == original
    assert list(target.parent.glob("*.tmp")) == []


def test_file_checkpoints_untracked_registration():
    assert is_untracked("speech_gen", "synthesize") is True
    assert is_untracked("speech_gen", "list_profiles") is False
