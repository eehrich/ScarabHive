"""Tests for the 2026-08-25 thinking/reasoning parameter fixes.

Audit findings these tests pin down:

- OpenRouter treats ``reasoning.effort`` and ``reasoning.max_tokens`` as
  mutually exclusive ("one of the following, not both") — the client used to
  send both when thinking_level AND thinking_budget were configured.
- ``thinking_budget=0`` used to suppress temperature while sending no
  reasoning field at all (diverging falsy/None guards); the temperature
  suppression now uses the exact payload predicate.
- Gemini ``build_thinking_config`` dropped ``thinking_budget=0`` (the retry
  reducer's "disable thinking" value), silently re-enabling the Google
  default budget; foreign effort values (xhigh/max/none) reached the API as
  unknown enum values instead of being clamped.
- Streamed ``reasoning_details``/tool-call fragments arriving only in the
  tail buffer were dropped; the accumulation now lives in shared helpers.
- The config Literal allowed ``ultra`` (no provider knows it) and lacked
  ``xhigh`` (OpenRouter effort enum).
"""

import pytest

from agent_system.config.models import LLMModelConfig
from agent_system.llm.gemini_utils import (
    adjust_thinking_for_retry,
    apply_retry_thinking_config,
    build_thinking_config,
)
from agent_system.llm.httpx_client import HTTPXOpenAIClient
from agent_system.llm.models import ChatMessage


def _client(**kw):
    return HTTPXOpenAIClient(model="m", api_key="k", **kw)


class TestReasoningParamExclusivity:
    def test_effort_only(self):
        assert _client(thinking_level="high")._build_reasoning_param() == {
            "effort": "high"}

    def test_budget_only_maps_to_max_tokens(self):
        assert _client(thinking_budget=2048)._build_reasoning_param() == {
            "max_tokens": 2048}

    def test_effort_wins_over_budget(self):
        # OpenRouter: "one of the following (not both)"
        assert _client(
            thinking_level="low", thinking_budget=2048
        )._build_reasoning_param() == {"effort": "low"}

    def test_nothing_configured_sends_no_field(self):
        assert _client()._build_reasoning_param() is None

    def test_budget_zero_counts_as_unset(self):
        # The same predicate gates the temperature suppression: no reasoning
        # field sent → temperature must survive in the payload.
        assert _client(thinking_budget=0)._build_reasoning_param() is None


class TestGeminiThinkingConfig:
    def test_budget_zero_is_sent_explicitly(self):
        # 0 = disable thinking on Gemini 2.5; dropping it re-enabled the
        # Google default budget and made the retry reduction a no-op.
        cfg = build_thinking_config(include_thoughts=None, thinking_budget=0)
        assert cfg == {"thinkingBudget": 0}

    def test_negative_budget_dropped(self):
        assert build_thinking_config(None, -5) is None

    def test_supported_level_passes_through(self):
        assert build_thinking_config(None, None, "medium") == {
            "thinkingLevel": "medium"}

    @pytest.mark.parametrize("level", ["max", "xhigh"])
    def test_foreign_high_levels_clamped_to_high(self, level):
        assert build_thinking_config(None, None, level) == {
            "thinkingLevel": "high"}

    def test_level_none_clamped_to_minimal(self):
        # Gemini 3 always thinks — "none" cannot disable it, so it clamps
        # DOWN to minimal (clamping up to high would raise thinking).
        assert build_thinking_config(None, None, "none") == {
            "thinkingLevel": "minimal"}


class TestStreamAccumulators:
    def test_reasoning_detail_fragments_append_data(self):
        acc: dict = {}
        HTTPXOpenAIClient._accumulate_reasoning_detail(
            acc, {"index": 0, "type": "reasoning.encrypted", "data": "abc"})
        HTTPXOpenAIClient._accumulate_reasoning_detail(
            acc, {"index": 0, "data": "def"})
        assert acc[0]["data"] == "abcdef"
        assert acc[0]["type"] == "reasoning.encrypted"

    def test_reasoning_detail_indices_stay_separate(self):
        acc: dict = {}
        HTTPXOpenAIClient._accumulate_reasoning_detail(
            acc, {"index": 0, "data": "a"})
        HTTPXOpenAIClient._accumulate_reasoning_detail(
            acc, {"index": 1, "data": "b"})
        assert acc[0]["data"] == "a" and acc[1]["data"] == "b"

    def test_tool_call_arguments_accumulate_across_fragments(self):
        acc: dict = {}
        HTTPXOpenAIClient._accumulate_tool_call_delta(
            acc, {"index": 0, "id": "c1",
                  "function": {"name": "f", "arguments": '{"a"'}})
        idx = HTTPXOpenAIClient._accumulate_tool_call_delta(
            acc, {"index": 0, "function": {"arguments": ': 1}'}})
        assert idx == 0
        assert acc[0] == {
            "id": "c1", "type": "function",
            "function": {"name": "f", "arguments": '{"a": 1}'}}


class TestApplyRetryThinkingConfig:
    """The replace-or-pop retry logic existed as two hand-rolled copies in
    GeminiClient (streaming + non-streaming) and had diverged: the
    non-streaming copy kept the first attempt's thinkingConfig when the
    reduction resolved to None. Now a shared helper."""

    def test_replaces_existing_config_and_maps_wire_format(self):
        payload = {"generationConfig": {
            "thinkingConfig": {"thinkingLevel": "THINKING_LEVEL_HIGH"}}}
        apply_retry_thinking_config(payload, {"thinkingLevel": "low"})
        assert payload["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "THINKING_LEVEL_LOW"}

    def test_none_pops_stale_config(self):
        # Keeping the stale config silently undid the whole retry reduction.
        payload = {"generationConfig": {
            "thinkingConfig": {"thinkingBudget": 8192}, "temperature": 1.0}}
        apply_retry_thinking_config(payload, None)
        assert "thinkingConfig" not in payload["generationConfig"]
        assert payload["generationConfig"]["temperature"] == 1.0

    def test_budget_zero_config_is_written(self):
        # The reducer's disable value must reach the payload.
        payload: dict = {}
        apply_retry_thinking_config(payload, {"thinkingBudget": 0})
        assert payload["generationConfig"]["thinkingConfig"] == {
            "thinkingBudget": 0}


class _StopAtNotify(Exception):
    """Raised from the pre-request hook to capture the payload without any
    HTTP traffic — the hook fires after the payload is fully built."""


async def _captured_payload(client, messages):
    captured = {}

    async def fake_notify(info):
        captured.update(info)
        raise _StopAtNotify()

    client._notify_pre_request = fake_notify
    gen = client.chat_tools_streaming(messages, tools=[])
    with pytest.raises(_StopAtNotify):
        await gen.__anext__()
    return captured["payload"]


class TestTemperatureGuardUsesPayloadPredicate:
    """The guard must suppress temperature exactly when a reasoning field is
    sent. The old guard checked the raw config fields instead — with
    thinking_budget=0 it suppressed temperature although NO reasoning field
    went out."""

    async def test_budget_zero_keeps_temperature_in_payload(self):
        client = _client(temperature=0.2, thinking_budget=0)
        payload = await _captured_payload(
            client, [ChatMessage(role="user", content="hi")])
        assert payload["temperature"] == 0.2
        assert "reasoning" not in payload

    async def test_effort_suppresses_temperature_in_payload(self):
        client = _client(temperature=0.2, thinking_level="high")
        payload = await _captured_payload(
            client, [ChatMessage(role="user", content="hi")])
        assert "temperature" not in payload
        assert payload["reasoning"] == {"effort": "high"}


class TestRetryReducerNoneFloor:
    def test_none_is_not_raised_to_low_on_retry(self):
        # "none" is clamped to minimal by build_thinking_config; raising it
        # to "low" on retry would INCREASE thinking for a no-think model.
        assert adjust_thinking_for_retry(1, None, "none") == (None, "none")

    def test_medium_still_reduces_to_low(self):
        assert adjust_thinking_for_retry(1, None, "medium") == (None, "low")


class TestThinkingLevelLiteral:
    def test_xhigh_accepted(self):
        cfg = LLMModelConfig(
            provider="openai_responses", model="m", thinking_level="xhigh")
        assert cfg.thinking_level == "xhigh"

    def test_ultra_rejected(self):
        # No provider knows "ultra" (OpenRouter enum ends at xhigh/max).
        with pytest.raises(Exception):
            LLMModelConfig(
                provider="openai_responses", model="m", thinking_level="ultra")
