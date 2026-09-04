"""Tests for GeminiTTSClient (mocked google.genai SDK).

Moved from tests/llm/test_llm_tts.py when the client moved into this
plugin (2026-08-26).
"""
from unittest.mock import MagicMock, patch

import pytest

from agent_system.llm.tts import TTSResult, TTSSpeaker, TTSVoice
from plugins_llm.llm_gemini.gemini_tts_client import (
    GEMINI_TTS_VOICES,
    GeminiTTSClient,
)


# ---------------------------------------------------------------------------
# GeminiTTSClient tests (mocked SDK)
# ---------------------------------------------------------------------------

def _make_mock_response(audio_bytes: bytes):
    """Create a mock Gemini generate_content response with inline audio data."""
    inline_data = MagicMock()
    inline_data.data = audio_bytes

    part = MagicMock()
    part.inline_data = inline_data

    content = MagicMock()
    content.parts = [part]

    candidate = MagicMock()
    candidate.content = content

    response = MagicMock()
    response.candidates = [candidate]
    return response


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff pauses instead of really waiting them out.

    Retry tests used to sleep for real (2s + 4s + a pointless 8s after the
    LAST failure). Recording also makes the pauses assertable — a backoff
    that stops backing off is otherwise invisible.
    """
    import asyncio

    class Recorder:
        def __init__(self):
            self.calls = []

        async def __call__(self, delay):
            self.calls.append(delay)

    recorder = Recorder()
    monkeypatch.setattr(asyncio, "sleep", recorder)
    return recorder


@pytest.fixture
def mock_genai():
    """Patch google.genai.Client and return mocks."""
    with patch("plugins_llm.llm_gemini.gemini_tts_client.GeminiTTSClient.__init__", return_value=None):
        client = GeminiTTSClient.__new__(GeminiTTSClient)
        # Manually set attributes that __init__ would set
        client.model = "gemini-2.5-flash-preview-tts"
        client.api_key = "test-key"
        client.request_timeout = 300
        client.max_retries = 3
        client.default_voice = "Kore"
        client._client = MagicMock()
        yield client


class TestGeminiTTSClient:
    """Unit tests for GeminiTTSClient with mocked google.genai SDK."""

    @pytest.mark.asyncio
    async def test_synthesize_single_speaker(self, mock_genai):
        pcm = b"\x00\x00" * 24000  # 1 second of silence
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(pcm)
        )

        result = await mock_genai.synthesize("Hello world!", voice=TTSVoice(name="Puck"))

        assert isinstance(result, TTSResult)
        assert result.audio_data == pcm
        assert result.sample_rate == 24000
        assert result.sample_width == 2
        assert result.channels == 1
        assert result.voice_name == "Puck"
        assert result.model == "gemini-2.5-flash-preview-tts"
        assert result.duration_seconds == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_synthesize_default_voice(self, mock_genai):
        pcm = b"\x00\x00" * 100
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(pcm)
        )

        result = await mock_genai.synthesize("Test default voice")
        assert result.voice_name == "Kore"  # Default voice

    @pytest.mark.asyncio
    async def test_synthesize_multi_speaker(self, mock_genai):
        pcm = b"\x00\x00" * 24000
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(pcm)
        )

        speakers = [
            TTSSpeaker(name="Joe", voice=TTSVoice(name="Kore")),
            TTSSpeaker(name="Jane", voice=TTSVoice(name="Puck")),
        ]

        result = await mock_genai.synthesize_multi_speaker(
            "Joe: Hello!\nJane: Hi there!",
            speakers=speakers,
        )

        assert isinstance(result, TTSResult)
        assert result.audio_data == pcm
        assert "Joe=Kore" in result.voice_name
        assert "Jane=Puck" in result.voice_name

    @pytest.mark.asyncio
    async def test_multi_speaker_max_two(self, mock_genai):
        speakers = [
            TTSSpeaker(name="A", voice=TTSVoice(name="Kore")),
            TTSSpeaker(name="B", voice=TTSVoice(name="Puck")),
            TTSSpeaker(name="C", voice=TTSVoice(name="Zephyr")),
        ]
        with pytest.raises(ValueError, match="at most 2 speakers"):
            await mock_genai.synthesize_multi_speaker("text", speakers=speakers)

    @pytest.mark.asyncio
    async def test_multi_speaker_empty(self, mock_genai):
        with pytest.raises(ValueError, match="At least one speaker"):
            await mock_genai.synthesize_multi_speaker("text", speakers=[])

    @pytest.mark.asyncio
    async def test_empty_response_raises(self, mock_genai):
        """Empty candidates should raise RuntimeError."""
        response = MagicMock()
        response.candidates = []
        mock_genai._client.models.generate_content = MagicMock(return_value=response)

        with pytest.raises(RuntimeError, match="empty response"):
            await mock_genai.synthesize("Hello")

    @pytest.mark.asyncio
    async def test_no_inline_data_raises(self, mock_genai):
        """Missing inline_data should raise RuntimeError."""
        part = MagicMock()
        part.inline_data = None
        content = MagicMock()
        content.parts = [part]
        candidate = MagicMock()
        candidate.content = content
        response = MagicMock()
        response.candidates = [candidate]

        mock_genai._client.models.generate_content = MagicMock(return_value=response)

        with pytest.raises(RuntimeError, match="no inline_data"):
            await mock_genai.synthesize("Hello")

    @pytest.mark.asyncio
    async def test_zero_length_audio_raises(self, mock_genai):
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(b"")
        )
        with pytest.raises(RuntimeError, match="zero-length"):
            await mock_genai.synthesize("Hello")

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, mock_genai, no_sleep):
        """Should retry on ServerError and succeed on second attempt."""
        from google.genai.errors import ServerError

        pcm = b"\x00\x00" * 100
        mock_genai._client.models.generate_content = MagicMock(
            side_effect=[
                ServerError(500, {"error": {"message": "internal"}}, None),
                _make_mock_response(pcm),
            ]
        )
        mock_genai.max_retries = 2

        result = await mock_genai.synthesize("Test retry")
        assert result.audio_data == pcm

    @pytest.mark.asyncio
    async def test_transport_errors_are_retried_too(self, mock_genai, no_sleep):
        """A network blip must not kill the whole scene.

        The SDK does not wrap httpx transport failures in APIError, so they
        used to fall into the catch-all and abort immediately — discarding
        every segment already synthesized, with no resume.
        """
        import httpx

        pcm = b"\x00\x00" * 100
        mock_genai._client.models.generate_content = MagicMock(
            side_effect=[httpx.ConnectError("connection reset"),
                         _make_mock_response(pcm)]
        )
        mock_genai.max_retries = 2

        result = await mock_genai.synthesize("Test transport retry")
        assert result.audio_data == pcm

    @pytest.mark.asyncio
    async def test_retry_exhausted(self, mock_genai, no_sleep):
        """Should raise after exhausting all retries."""
        from google.genai.errors import ServerError

        mock_genai._client.models.generate_content = MagicMock(
            side_effect=ServerError(503, {"error": {"message": "unavailable"}}, None)
        )
        mock_genai.max_retries = 2

        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            await mock_genai.synthesize("Test fail")
        # retries + 1: the config value counts RETRIES, like everywhere else.
        assert mock_genai._client.models.generate_content.call_count == 3

    @pytest.mark.asyncio
    async def test_max_retries_zero_still_makes_one_call(self, mock_genai):
        """`max_retries: 0` means "no retries", not "no attempt".

        `range(max_retries)` made zero a client that never called the API at
        all and failed every synthesis — while the same value on the speech
        provider means one attempt.
        """
        pcm = b"\x00\x00" * 100
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(pcm))
        mock_genai.max_retries = 0

        result = await mock_genai.synthesize("Single attempt")
        assert result.audio_data == pcm
        assert mock_genai._client.models.generate_content.call_count == 1

    @pytest.mark.asyncio
    async def test_no_backoff_sleep_after_the_last_attempt(self, mock_genai, no_sleep):
        """The final failure used to still wait its full backoff (up to 30s)
        before raising — pure delay in front of an exception the caller is
        getting anyway."""
        from google.genai.errors import ServerError

        mock_genai._client.models.generate_content = MagicMock(
            side_effect=ServerError(503, {"error": {"message": "unavailable"}}, None))
        mock_genai.max_retries = 2

        with pytest.raises(RuntimeError):
            await mock_genai.synthesize("Test fail")
        # Count, not the backoff constants: the claim is "no pause after the
        # last attempt" (3 attempts → 2 pauses). Pinning [2, 4] would turn a
        # legitimate backoff change into a red test.
        assert len(no_sleep.calls) == 2, (
            f"expected a pause between attempts only, got {no_sleep.calls}")

    @pytest.mark.asyncio
    async def test_non_retryable_error_immediate(self, mock_genai):
        """Non-retryable exceptions should propagate immediately."""
        mock_genai._client.models.generate_content = MagicMock(
            side_effect=ValueError("bad input")
        )
        with pytest.raises(ValueError, match="bad input"):
            await mock_genai.synthesize("Test")



# ---------------------------------------------------------------------------
# Voice list test
# ---------------------------------------------------------------------------

class TestGeminiVoices:
    def test_voice_list_has_30_entries(self):
        assert len(GEMINI_TTS_VOICES) == 30

    def test_common_voices_present(self):
        for name in ["Kore", "Puck", "Zephyr", "Charon", "Fenrir"]:
            assert name in GEMINI_TTS_VOICES


class TestBuildFactory:
    """build_gemini_tts must forward the model entry's default voice —
    review finding: TTSModelConfig.voice used to be silently ignored here."""

    def test_the_model_entry_reaches_the_client(self):
        """Everything the entry configures, in one place.

        The client tests below run against a fixture that patches __init__
        away and sets these by hand, so they measure the fixture — dropping
        `model=cfg.model` or `max_retries=cfg.max_retries` from the factory
        stayed green everywhere (measured). This is the only test that sees
        the real constructor arguments.
        """
        from unittest.mock import patch as _patch
        from agent_system.config.models import TTSModelConfig
        from plugins_llm.llm_gemini.gemini_tts_client import build_gemini_tts

        with _patch("google.genai.Client"):
            client = build_gemini_tts(TTSModelConfig(
                provider="gemini_tts", model="gemini-3.1-flash-tts-preview",
                api_key="test-key", voice="Algenib", max_retries=1,
                request_timeout=90))
        assert client.default_voice == "Algenib"
        assert client.model == "gemini-3.1-flash-tts-preview"
        assert client.max_retries == 1
        assert client.request_timeout == 90

    def test_without_entry_voice_the_client_default_applies(self):
        from unittest.mock import patch as _patch
        from agent_system.config.models import TTSModelConfig
        from plugins_llm.llm_gemini.gemini_tts_client import build_gemini_tts

        with _patch("google.genai.Client"):
            client = build_gemini_tts(TTSModelConfig(
                provider="gemini_tts", model="gemini-3.1-flash-tts-preview",
                api_key="test-key"))
        assert client.default_voice == "Kore"

    def test_the_configured_timeout_reaches_the_sdk(self):
        """The client stored request_timeout and logged it, but never passed
        it to the SDK — which then hands httpx timeout=None, i.e. NO timeout.
        A hanging connection stalled the scene forever."""
        from unittest.mock import patch as _patch
        from agent_system.config.models import TTSModelConfig
        from plugins_llm.llm_gemini.gemini_tts_client import build_gemini_tts

        with _patch("google.genai.Client") as sdk:
            build_gemini_tts(TTSModelConfig(
                provider="gemini_tts", model="m", api_key="test-key",
                request_timeout=90))
        http_options = sdk.call_args.kwargs["http_options"]
        # HttpOptions.timeout is in milliseconds.
        assert http_options.timeout == 90_000


class TestTheRequestItself:
    """What the SDK is actually asked for.

    Every other test here asserts on the RESULT, which the client builds
    from its own local variables — so dropping the speech config or the
    voice from the request would leave them all green.
    """

    @pytest.mark.asyncio
    async def test_voice_and_audio_modality_are_in_the_request(self, mock_genai):
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(b"\x00\x00" * 100))

        await mock_genai.synthesize("Hallo", voice=TTSVoice(name="Puck"))

        kwargs = mock_genai._client.models.generate_content.call_args.kwargs
        assert kwargs["model"] == "gemini-2.5-flash-preview-tts"
        assert kwargs["contents"] == "Hallo"
        config = kwargs["config"]
        assert config.response_modalities == ["AUDIO"]
        voice_cfg = config.speech_config.voice_config.prebuilt_voice_config
        assert voice_cfg.voice_name == "Puck"

    @pytest.mark.asyncio
    async def test_system_instruction_is_prefixed_to_the_text(self, mock_genai):
        """Gemini TTS 500s on a system_instruction in the config, so the
        client prepends it to the prompt instead. If that prefixing silently
        stopped, narration style would vanish with no other symptom."""
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(b"\x00\x00" * 100))

        await mock_genai.synthesize("Der Wald lag still.",
                                    system_instruction="Stil: ruhig")

        contents = mock_genai._client.models.generate_content.call_args.kwargs["contents"]
        assert contents.startswith("Stil: ruhig")
        assert "Der Wald lag still." in contents

    @pytest.mark.asyncio
    async def test_the_debugger_sees_the_call(self, mock_genai):
        """Hook dispatch is the thing that went silently dead once already:
        after the move into this plugin its imports resolved to the wrong
        package, every dispatch raised, and the failure only existed at DEBUG
        level. Nothing measured it — this does."""
        from agent_system.hooks import HookType

        seen = []

        class _Recorder:
            async def execute_hooks(self, hook_type, context):
                seen.append((hook_type, context.llm_provider, context.llm_model,
                             context.llm_request_payload))

        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(b"\x00\x00" * 100))
        with patch("agent_system.hooks.get_hook_registry", return_value=_Recorder()):
            await mock_genai.synthesize("Hallo", voice=TTSVoice(name="Puck"))

        kinds = [k for k, _, _, _ in seen]
        assert HookType.PRE_LLM_REQUEST in kinds, "TTS request invisible to hooks"
        assert HookType.POST_LLM_RESPONSE in kinds, "TTS response invisible to hooks"
        assert all(p == "gemini_tts" for _, p, _, _ in seen)
        pre_payload = next(p for k, _, _, p in seen
                           if k is HookType.PRE_LLM_REQUEST)
        assert pre_payload["voice"] == "Puck"

    @pytest.mark.asyncio
    async def test_a_broken_hook_dispatch_is_loud(self, mock_genai, caplog):
        """The failure mode that hid the bug: dispatch dies, TTS keeps
        working, and the only trace is a DEBUG line nobody has enabled."""
        import logging

        from agent_system.llm import tts as tts_module

        tts_module._hook_failures_reported.clear()
        mock_genai._client.models.generate_content = MagicMock(
            return_value=_make_mock_response(b"\x00\x00" * 100))
        with patch("agent_system.hooks.get_hook_registry",
                   side_effect=RuntimeError("registry exploded")):
            with caplog.at_level(logging.WARNING):
                result = await mock_genai.synthesize("Hallo")

        assert result.audio_data, "a dead hook must not break the TTS call"
        warnings = [r for r in caplog.records
                    if "hooks are NOT being dispatched" in r.getMessage()]
        assert warnings, "a dead hook dispatch stayed below WARNING"


class TestNoCloning:
    """Gemini cannot clone. A reference on the voice must fail the CALL --
    through synthesize()/synthesize_multi_speaker(), before any request --
    not quietly turn into a preset voice for a whole book."""

    @pytest.mark.asyncio
    async def test_synthesize_refuses_a_reference_before_any_request(self, mock_genai):
        mock_genai._client.models.generate_content = MagicMock()
        with pytest.raises(ValueError, match="cannot clone"):
            await mock_genai.synthesize(
                "Hallo", voice=TTSVoice(name="clone:egon", reference_audio=b"RIFF"))
        mock_genai._client.models.generate_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_multi_speaker_refuses_a_reference_on_any_speaker(self, mock_genai):
        mock_genai._client.models.generate_content = MagicMock()
        speakers = [TTSSpeaker(name="A", voice=TTSVoice(name="Kore")),
                    TTSSpeaker(name="B", voice=TTSVoice(name="c", reference_audio=b"RIFF"))]
        with pytest.raises(ValueError, match="cannot clone"):
            await mock_genai.synthesize_multi_speaker(
                "A: hi\nB: ho", speakers=speakers)
        mock_genai._client.models.generate_content.assert_not_called()

    def test_capability_flag_says_no(self):
        assert GeminiTTSClient.supports_voice_cloning is False


def test_gemini_carries_the_style_prompt():
    """Gemini has system_instruction/language/seed — the flag must not
    accidentally turn the narrator direction off for the pipeline."""
    assert GeminiTTSClient.supports_style_prompt is True


@pytest.mark.asyncio
async def test_an_empty_reference_is_still_a_reference(mock_genai):
    """b"" is falsy: a truthiness check would let it through and send the
    clone label 'clone:egon' to Gemini as a PREBUILT voice name."""
    mock_genai._client.models.generate_content = MagicMock()
    with pytest.raises(ValueError, match="cannot clone"):
        await mock_genai.synthesize(
            "Hallo", voice=TTSVoice(name="clone:egon", reference_audio=b""))
    mock_genai._client.models.generate_content.assert_not_called()
