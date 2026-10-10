"""Tests for the OpenAI Responses client (native item round-tripping).

Core invariant: the model's output items are stored VERBATIM in a
reasoning_details block and reproduced exactly on the next request -- no
reconstruction, no bridging loss (the cause of the encrypted-reasoning 400s
on the Chat Completions route).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.llm.capabilities import ModelCapabilities
from agent_system.llm.models import (
    AudioContent,
    ChatMessage,
    ImageContent,
    ImageSource,
    TextContent,
)
from plugins.llm_openai_compat.openai_responses_client import (
    RESPONSES_ITEMS_FORMAT,
    OpenAIResponsesClient,
)


def _client(**kw):
    defaults = dict(
        model="openai/gpt-5.6-terra",
        api_key="sk-or-test",
        base_url="https://openrouter.ai/api/v1",
        thinking_level="max",
        service_tier="flex",
        provider_routing={"order": ["openai"], "allow_fallbacks": False},
    )
    defaults.update(kw)
    return OpenAIResponsesClient(**defaults)


def _multimodal_client(**kw):
    """Client for a model that can actually take image+audio input
    (config shorthand ``capabilities.multimodal: true``, e.g. the
    or-gemini-flash profiles)."""
    kw.setdefault("model", "google/gemini-3.5-flash-lite")
    kw.setdefault("capabilities", ModelCapabilities(
        image_input=True, audio_input=True, video_input=True))
    return _client(**kw)


def _drive_non_streaming(body: dict, **client_kw) -> list[dict]:
    """``chat_tools_streaming`` over a model that does NOT stream.

    The TRANSPORT is swapped, not the method under test: the request loop,
    ``_format_response`` and the final-chunk assembly all really run.
    """
    import asyncio

    import httpx

    client = _client(**client_kw)
    client.supports_streaming = lambda: False

    async def _fake_post(_client, _url, _payload):
        return httpx.Response(200, json=body)

    client._post = _fake_post

    async def _collect():
        return [chunk async for chunk in client.chat_tools_streaming([], [])]

    return asyncio.run(_collect())


#: A tool schema with everything Gemini's function declarations reject.
NASTY_TOOL = [{"type": "function", "function": {
    "name": "f", "description": "d",
    "parameters": {"type": "object", "title": "T", "additionalProperties": False,
                   "properties": {"x": {"type": "string", "default": "a", "format": "id"},
                                  "y": {"oneOf": [{"type": "string"}]}}}}}]


SAMPLE_OUTPUT = [
    {"type": "reasoning", "id": "rs_abc", "status": "completed",
     "encrypted_content": "BLOB", "format": "openai-responses-api",
     "summary": [{"type": "summary_text", "text": "thinking about it"}]},
    {"type": "function_call", "id": "fc_1", "call_id": "call_1",
     "name": "get_value", "arguments": '{"name": "alpha"}'},
    {"type": "function_call", "id": "fc_2", "call_id": "call_2",
     "name": "get_value", "arguments": '{"name": "beta"}'},
]


class TestExtractVerbatimItems:
    """The message validator merges consecutive assistant messages by
    concatenating reasoning_details — a merged message carries TWO
    responses_items blocks. Returning only the first dropped the second
    turn's items (reasoning, text AND function_calls); a tool result
    answering a dropped call is a 400."""

    def test_collects_items_from_all_matching_blocks_in_order(self):
        c = _client()
        msg = {"role": "assistant", "reasoning_details": [
            {"format": RESPONSES_ITEMS_FORMAT, "model": c.model,
             "items": [{"type": "reasoning", "id": "rs_1"}]},
            {"format": RESPONSES_ITEMS_FORMAT, "model": c.model,
             "items": [{"type": "function_call", "id": "fc_2",
                        "call_id": "call_2", "name": "f", "arguments": "{}"}]},
        ]}
        items = c._extract_verbatim_items(msg)
        assert [i["id"] for i in items] == ["rs_1", "fc_2"]

    def test_a_foreign_block_makes_the_whole_message_foreign(self):
        """A merged message replayed in part lost the foreign half's calls and kept their outputs:
        rebuilt from content/tool_calls instead."""
        c = _client()
        msg = {"role": "assistant", "reasoning_details": [
            {"format": RESPONSES_ITEMS_FORMAT, "model": "other/model",
             "items": [{"type": "reasoning", "id": "rs_foreign"}]},
            {"format": RESPONSES_ITEMS_FORMAT, "model": c.model,
             "items": [{"type": "reasoning", "id": "rs_ours"}]},
        ]}
        assert c._extract_verbatim_items(msg) is None

    def test_no_matching_block_returns_none(self):
        c = _client()
        assert c._extract_verbatim_items(
            {"role": "assistant", "reasoning_details": []}) is None


def _own_turn(model: str, rs_id: str, call_id: str) -> dict:
    """An assistant turn as THIS client stored it: verbatim items + tool_calls."""
    return {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": "f", "arguments": "{}"}}],
        "reasoning_details": [{
            "type": "reasoning.responses_items", "format": RESPONSES_ITEMS_FORMAT,
            "index": 0, "model": model,
            "items": [{"type": "reasoning", "id": rs_id, "encrypted_content": "BLOB"},
                      {"type": "function_call", "id": "fc_" + call_id,
                       "call_id": call_id, "name": "f", "arguments": "{}"}]}],
    }


def _history(model: str) -> list:
    return [
        {"role": "user", "content": "los"},
        _own_turn(model, "rs_1", "call_1"),
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        _own_turn(model, "rs_2", "call_2"),
        {"role": "tool", "tool_call_id": "call_2", "content": "ok"},
    ]


class TestReasoningDetailsMode:
    """The declared round-trip mode decides HOW MANY own turns repeat their
    items verbatim. A turn that may not is rebuilt from content/tool_calls,
    like foreign history."""

    @staticmethod
    def _sent(mode=None):
        c = _client(reasoning_details_mode=mode) if mode else _client()
        items = c._messages_to_input(_history(c.model))
        return ([i["id"] for i in items if i.get("type") == "reasoning"],
                [i["call_id"] for i in items if i.get("type") == "function_call"])

    def test_default_replays_every_own_turn(self):
        """This route's default is keep_all: the encrypted chain must have no
        gap, and the cached prefix stays byte-identical."""
        assert self._sent() == (["rs_1", "rs_2"], ["call_1", "call_2"])

    def test_keep_all_is_the_same_declared(self):
        assert self._sent("keep_all") == (["rs_1", "rs_2"], ["call_1", "call_2"])

    def test_keep_last_drops_the_earlier_reasoning_but_not_the_call(self):
        """The older turn loses its reasoning items but keeps its
        function_call -- otherwise the matching tool result would dangle
        (HTTP 400)."""
        assert self._sent("keep_last") == (["rs_2"], ["call_1", "call_2"])

    def test_strip_sends_no_reasoning_at_all(self):
        assert self._sent("strip") == ([], ["call_1", "call_2"])

    def test_an_undeclared_mode_fails_at_construction(self):
        with pytest.raises(ValueError, match="reasoning_details_mode"):
            _client(reasoning_details_mode="keep_first")


class TestFormatResponse:
    def test_tool_calls_and_verbatim_block(self):
        c = _client()
        result = c._format_response({"output": SAMPLE_OUTPUT, "status": "completed",
                                     "usage": {"input_tokens": 100, "output_tokens": 20,
                                               "total_tokens": 120}})
        a = result["assistant"]
        assert [t["id"] for t in a["tool_calls"]] == ["call_1", "call_2"]
        assert a["tool_calls"][0]["function"]["name"] == "get_value"
        # The verbatim block carries ALL output items unchanged
        blocks = a["reasoning_details"]
        assert len(blocks) == 1 and blocks[0]["format"] == RESPONSES_ITEMS_FORMAT
        assert blocks[0]["items"] == SAMPLE_OUTPUT
        # The thinking is NOT copied onto the message: the verbatim items above
        # already carry it, and keeping both stored every thought twice.
        from agent_system.utils.reasoning_artifacts import thinking_text
        assert "reasoning_content" not in a
        assert thinking_text(a) == "thinking about it"
        assert result["usage"]["prompt_tokens"] == 100
        assert result["usage"]["completion_tokens"] == 20

    def test_message_content_extracted(self):
        c = _client()
        out = [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Hallo "},
            {"type": "output_text", "text": "Welt"}]}]
        a = c._format_response({"output": out})["assistant"]
        assert a["content"] == "Hallo Welt"

    def test_body_error_surfaces_as_upstream_error(self):
        c = _client()
        a = c._format_response({"error": {"message": "boom", "code": 502}})["assistant"]
        assert a["error"]["type"] == "upstream_error_502"

    def test_usage_details_mapped(self):
        mapped = OpenAIResponsesClient._map_usage({
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 8},
            "output_tokens_details": {"reasoning_tokens": 3}})
        assert mapped["prompt_tokens_details"]["cached_tokens"] == 8
        assert mapped["completion_tokens_details"]["reasoning_tokens"] == 3

    def test_usage_openrouter_cost_passthrough(self):
        """session_costs.py prefers OpenRouter's billed-cost field --
        _map_usage must pass unknown extra fields through without losing
        the mapped chat keys."""
        mapped = OpenAIResponsesClient._map_usage({
            "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
            "cost": 0.0123, "cost_details": {"upstream_inference_cost": 0.01},
            "is_byok": False})
        assert mapped["prompt_tokens"] == 100
        assert mapped["completion_tokens"] == 20
        assert mapped["cost"] == 0.0123
        assert mapped["cost_details"] == {"upstream_inference_cost": 0.01}


class TestMessagesToInput:
    def test_verbatim_round_trip(self):
        """format_response -> ChatMessage -> _messages_to_input reproduces the
        output items exactly (the core invariant)."""
        c = _client()
        assistant = c._format_response({"output": SAMPLE_OUTPUT})["assistant"]
        msg = ChatMessage(**assistant)
        items = c._messages_to_input([
            ChatMessage(role="user", content="hi"), msg,
            ChatMessage(role="tool", content="42", tool_call_id="call_1"),
            ChatMessage(role="tool", content="43", tool_call_id="call_2"),
        ])
        assert items[0] == {"type": "message", "role": "user", "content": "hi"}
        assert items[1:4] == SAMPLE_OUTPUT                      # verbatim!
        assert items[4] == {"type": "function_call_output", "call_id": "call_1", "output": "42"}
        assert items[5] == {"type": "function_call_output", "call_id": "call_2", "output": "43"}

    def test_verbatim_block_suppresses_reconstruction(self):
        """A message with a verbatim block must NOT additionally serialize
        content/tool_calls (they would duplicate the contained items)."""
        c = _client()
        assistant = c._format_response({"output": SAMPLE_OUTPUT})["assistant"]
        assistant["content"] = "sichtbarer text"
        items = c._messages_to_input([ChatMessage(**assistant)])
        assert items == SAMPLE_OUTPUT

    def test_verbatim_replay_takes_arguments_from_tool_calls(self):
        """The block keeps the model's RAW arguments, tool_calls the copy
        history_safe_tool_calls repaired. The raw string used to be replayed:
        every later request came back invalid_prompt until a fallback model
        rebuilt the turn from tool_calls (server, 2026-09-10)."""
        c = _client()
        broken = '{"doc": "synopsis", "content": "abc"'
        repaired = '{"doc": "synopsis", "content": "abc"}'
        output = [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "X"},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1",
             "name": "f", "arguments": broken},
        ]
        assistant = c._format_response({"output": output})["assistant"]
        assistant["tool_calls"][0]["function"]["arguments"] = repaired
        items = c._messages_to_input([ChatMessage(**assistant)])
        assert [i["type"] for i in items] == ["reasoning", "function_call"]  # still verbatim
        assert items[1]["arguments"] == repaired
        assert output[1]["arguments"] == broken  # stored block untouched

    def test_foreign_history_reconstructed_without_artifacts(self):
        """Chat-route sessions (openai-responses-v1 blocks) and Gemini blocks
        are ignored -- the chain starts fresh, calls/content stay."""
        c = _client()
        msg = ChatMessage(
            role="assistant", content="txt",
            tool_calls=[{"id": "call_x", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}],
            reasoning_details=[
                {"type": "reasoning.encrypted", "format": "openai-responses-v1",
                 "id": "rs_old", "data": "X", "index": 0},
                {"type": "reasoning.encrypted", "format": "google-gemini-v1",
                 "data": "sig", "index": 0},
            ])
        items = c._messages_to_input([msg])
        types = [i["type"] for i in items]
        assert types == ["message", "function_call"]
        assert items[1]["call_id"] == "call_x"
        assert not any("rs_old" in str(i) for i in items)

    def test_system_and_multimodal_content_parts(self):
        # Vision-capable model (all openai_responses profiles run
        # capabilities.multimodal: true) — without it the guard drops the image.
        c = _multimodal_client()
        chat_parts = [
            {"type": "text", "text": "beschreibe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]
        items = c._messages_to_input([
            ChatMessage(role="system", content="sys"),
            {"role": "user", "content": chat_parts},
        ])
        assert items[0] == {"type": "message", "role": "system", "content": "sys"}
        parts = items[1]["content"]
        assert parts[0] == {"type": "input_text", "text": "beschreibe"}
        assert parts[1] == {"type": "input_image", "image_url": "data:image/png;base64,AAA"}

    def test_tool_dict_output_serialized(self):
        c = _client()
        items = c._messages_to_input([
            {"role": "tool", "tool_call_id": "c1", "content": {"ok": True}}])
        assert items[0]["output"] == '{"ok": true}'


class TestMediaParts:
    """Media must arrive as Responses media items — NEVER as a serialized blob.

    Live find 2026-07-25 (audio-Phase-4b): ChatMessage content carries the
    PYDANTIC models (AudioContent/TextContent), which are not dicts, so every
    part fell into ``str(part)`` — the full 1.9 MB base64 went out as
    ``input_text`` ("input token count exceeds ... 1048576") and the comparator
    judged text against a stringified blob instead of listening to the clip.
    """

    def _parts(self, client, *content):
        items = client._messages_to_input([ChatMessage(role="user", content=list(content))])
        return items[0]["content"]

    def test_audio_base64_source_becomes_input_audio(self):
        c = _multimodal_client()
        parts = self._parts(
            c,
            TextContent(text="ORIGINAL: hallo"),
            AudioContent(
                source=ImageSource(type="base64", media_type="audio/mpeg", data="QUJD"),
                media_type="audio/mpeg", name="seg1.mp3"),
        )
        assert parts[0] == {"type": "input_text", "text": "ORIGINAL: hallo"}
        assert parts[1] == {"type": "input_audio",
                            "input_audio": {"data": "QUJD", "format": "mp3"}}
        assert c.dropped_media_parts == 0

    def test_audio_data_url_becomes_input_audio(self):
        c = _multimodal_client()
        parts = self._parts(c, {"type": "audio", "audio_url": "data:audio/wav;base64,QUJD"})
        assert parts[0] == {"type": "input_audio",
                            "input_audio": {"data": "QUJD", "format": "wav"}}

    def test_audio_payload_never_serialized_as_text(self):
        """Regression guard for the exact failure: the base64 must not appear
        inside any input_text part."""
        import json as _json
        c = _multimodal_client()
        blob = "A" * 5000
        parts = self._parts(c, AudioContent(
            source=ImageSource(type="base64", media_type="audio/mpeg", data=blob),
            media_type="audio/mpeg"))
        assert parts[0]["type"] == "input_audio"
        texts = _json.dumps([p for p in parts if p["type"] == "input_text"])
        assert blob[:100] not in texts

    def test_image_base64_source_becomes_data_url(self):
        c = _multimodal_client()
        parts = self._parts(c, ImageContent(
            type="image",
            source=ImageSource(type="base64", media_type="image/png", data="AAA"),
            name="cover.png"))
        assert parts[0] == {"type": "input_image", "image_url": "data:image/png;base64,AAA"}

    def test_image_url_shapes_become_input_image(self):
        c = _multimodal_client()
        parts = self._parts(
            c,
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
            {"type": "image_url", "image_url": "data:image/jpeg;base64,BBB"},
        )
        assert parts[0] == {"type": "input_image", "image_url": "https://x/y.png"}
        assert parts[1] == {"type": "input_image", "image_url": "data:image/jpeg;base64,BBB"}

    def test_unknown_part_still_degrades_to_truncated_text(self):
        c = _multimodal_client()
        parts = self._parts(c, {"type": "sensor_reading", "payload": "Z" * 5000})
        assert parts[0]["type"] == "input_text"
        assert len(parts[0]["text"]) == 2000

    def test_capability_guard_drops_audio_for_text_only_model(self, caplog):
        """No audio_input capability => audio is DROPPED with a visible note and
        a WARNING naming the model — not embedded in any form."""
        import logging
        c = _client(model="openai/gpt-5.6-terra",
                    capabilities=ModelCapabilities(image_input=True))
        with caplog.at_level(logging.WARNING):
            parts = self._parts(c, AudioContent(
                source=ImageSource(type="base64", media_type="audio/mpeg", data="SECRET"),
                media_type="audio/mpeg", name="seg1.mp3"))
        assert parts[0]["type"] == "input_text"
        assert "audio input omitted" in parts[0]["text"]
        assert "SECRET" not in parts[0]["text"]
        assert c.dropped_media_parts == 1
        assert any("audio" in r.message and "openai/gpt-5.6-terra" in r.message
                   for r in caplog.records)

    def test_capability_guard_drops_image_without_vision(self, caplog):
        import logging
        c = _client(capabilities=ModelCapabilities(image_input=False))
        with caplog.at_level(logging.WARNING):
            parts = self._parts(c, {"type": "image_url",
                                    "image_url": "data:image/png;base64,SECRET"})
        assert parts[0]["type"] == "input_text"
        assert "image input omitted" in parts[0]["text"]
        assert "SECRET" not in parts[0]["text"]
        assert c.dropped_media_parts == 1

    def test_no_capabilities_object_drops_media(self):
        """capabilities=None (unconfigured model) must be treated as 'cannot',
        not as 'unknown, send anyway'."""
        c = _client()
        parts = self._parts(c, AudioContent(
            source=ImageSource(type="base64", media_type="audio/mpeg", data="X"),
            media_type="audio/mpeg"))
        assert parts[0]["type"] == "input_text"
        assert c.dropped_media_parts == 1

    def test_video_dropped_with_note(self):
        c = _multimodal_client()
        parts = self._parts(c, {"type": "video", "video_url": "data:video/mp4;base64,X"})
        assert parts[0]["type"] == "input_text"
        assert "video input omitted" in parts[0]["text"]

    def test_unusable_media_dropped_not_serialized(self):
        c = _multimodal_client()
        parts = self._parts(c, {"type": "audio", "name": "broken.flac"})
        assert parts[0]["type"] == "input_text"
        assert "audio input omitted" in parts[0]["text"]
        assert c.dropped_media_parts == 1

    def test_text_file_part_becomes_readable_text(self):
        c = _multimodal_client()
        parts = self._parts(c, {"type": "text_file", "name": "a.md", "content": "hi"})
        assert parts[0] == {"type": "input_text", "text": "[File: a.md]\nhi"}


class TestToolsAndPayload:
    def test_tools_converted_to_flat_format(self):
        tools = [{"type": "function", "function": {
            "name": "get_value", "description": "d", "parameters": {"type": "object"}}}]
        conv = _client()._convert_tools(tools)
        assert conv == [{"type": "function", "name": "get_value",
                         "description": "d", "parameters": {"type": "object"}}]

    def test_a_flat_tool_without_a_type_is_tagged(self):
        """The "already flat" branch took the caller at its word and passed a
        tool on without a discriminator. The Responses API requires
        ``type`` -- found when the SDK validated the same payload in typed
        form and rejected it with ``union_tag_invalid``."""
        conv = _client()._convert_tools(
            [{"name": "f", "description": "d", "parameters": {"type": "object"}}])
        assert conv == [{"type": "function", "name": "f", "description": "d",
                         "parameters": {"type": "object"}}]

    def test_an_explicit_type_survives(self):
        """Control: the guard only sets a type where none is present -- a
        server tool (``web_search`` & co.) must not turn into a function."""
        conv = _client()._convert_tools([{"type": "web_search", "name": "s"}])
        assert conv[0]["type"] == "web_search"

    def test_the_declared_dialect_sanitizes_the_schemas(self):
        """``tool_schema_dialect: gemini_function_declarations`` strips the
        JSON Schema keywords that function declarations reject
        (additionalProperties, default, format, oneOf, title) -- as on the
        chat route."""
        import json as _json
        gem = _client(model="google/gemini-3.5-flash-lite",
                      tool_schema_dialect="gemini_function_declarations"
                      )._convert_tools(NASTY_TOOL)
        blob = _json.dumps(gem)
        for kw in ('"title"', '"default"', '"oneOf"', '"additionalProperties"', '"format"'):
            assert kw not in blob, f"{kw} not sanitized"

    def test_without_the_dialect_the_schema_travels_as_it_is(self):
        """Default = json_schema: the endpoint gets the schema untouched."""
        import json as _json
        gpt = _client(model="openai/gpt-5.6-terra")._convert_tools(NASTY_TOOL)
        assert '"oneOf"' in _json.dumps(gpt)

    def test_the_model_name_no_longer_decides(self):
        """The guard against sniffing model names, in both directions.

        The catalogue lists the same family as ``google/gemini-3.1-pro-preview``
        AND as the gateway alias ``~google/gemini-flash-latest``. Prefix
        detection sanitized only the first spelling -- the alias went out
        unfiltered. Now only the declared key decides."""
        import json as _json
        alias = _client(model="~google/gemini-flash-latest",
                        tool_schema_dialect="gemini_function_declarations"
                        )._convert_tools(NASTY_TOOL)
        assert '"oneOf"' not in _json.dumps(alias)
        named = _client(model="google/gemini-3.1-pro-preview")._convert_tools(NASTY_TOOL)
        assert '"oneOf"' in _json.dumps(named)

    def test_an_undeclared_dialect_fails_at_construction(self):
        with pytest.raises(ValueError, match="tool_schema_dialect"):
            _client(tool_schema_dialect="gemini")

    def test_payload_carries_config(self):
        c = _client(max_tokens=16384, parallel_tool_calls=True)
        p = c._build_payload([ChatMessage(role="user", content="hi")],
                             [{"type": "function", "function": {"name": "f", "parameters": {}}}])
        assert p["store"] is False
        assert p["reasoning"] == {"effort": "max"}
        assert p["max_output_tokens"] == 16384
        assert p["service_tier"] == "flex"
        assert p["provider"] == {"order": ["openai"], "allow_fallbacks": False}
        assert p["parallel_tool_calls"] is True
        assert p["tool_choice"] == "auto"

    def test_parallel_tool_calls_none_leaves_the_field_out(self):
        """None means "leave the field out" -- not False. bool(None) would
        have sent parallel_tool_calls=false to models that should not see the
        field at all, switching off parallel tool calling."""
        p = _client(parallel_tool_calls=None)._build_payload(
            [ChatMessage(role="user", content="hi")],
            [{"type": "function", "function": {"name": "f", "parameters": {}}}])
        assert "parallel_tool_calls" not in p
        assert p["tool_choice"] == "auto"  # the rest of the tool block stays

    def test_parallel_tool_calls_false_is_still_sent(self):
        p = _client(parallel_tool_calls=False)._build_payload(
            [ChatMessage(role="user", content="hi")],
            [{"type": "function", "function": {"name": "f", "parameters": {}}}])
        assert p["parallel_tool_calls"] is False

    def test_payload_carries_safety_settings(self):
        """Previously guarded by no test — and unlike the httpx route, this
        client sends the thresholds UNCONDITIONALLY once set (httpx only when
        _is_gemini_via_openrouter). Since `turbo` moved to
        gemini-3.5-flash-lite it hangs on exactly this line: if the BLOCK_NONE
        thresholds fall out of the payload, the provider filters with its own
        defaults and fiction prose gets silently mangled."""
        c = _client(safety_settings={"HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_NONE",
                                     "HARM_CATEGORY_HARASSMENT": "BLOCK_ONLY_HIGH"})
        p = c._build_payload([ChatMessage(role="user", content="hi")], None)
        assert p["safety_settings"] == [
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
        ]

    def test_payload_omits_safety_settings_when_unset(self):
        p = _client(safety_settings=None)._build_payload(
            [ChatMessage(role="user", content="hi")], None)
        assert "safety_settings" not in p

    def test_payload_omits_unset_options(self):
        c = _client(thinking_level=None, service_tier=None,
                    provider_routing=None, max_tokens=None)
        p = c._build_payload([ChatMessage(role="user", content="hi")], None)
        for absent in ("reasoning", "max_output_tokens", "service_tier", "provider",
                       "tools", "tool_choice", "parallel_tool_calls",
                       "prompt_cache_key"):
            assert absent not in p

    def test_payload_carries_prompt_cache_key(self):
        # GPT-5.6+: without a prompt_cache_key, no reliable cache matching
        # (measured: byte-identical prefix, cached_tokens=0).
        c = _client(prompt_cache_key="scene_planner")
        p = c._build_payload([ChatMessage(role="user", content="hi")], None)
        assert p["prompt_cache_key"] == "scene_planner"

    def test_payload_splits_cache_breakpoint_sentinel(self):
        # GPT-5.6 caches mid-prompt divergence only with explicit breakpoints
        # (experiments 2026-07-21): sentinel in the task -> input_text parts,
        # all but the last marked with prompt_cache_breakpoint.
        from agent_system.llm.cache_key import CACHE_BP_SENTINEL
        c = _client(prompt_cache_key="auto")
        task = "stabiler teil" + CACHE_BP_SENTINEL + "variabler teil"
        p = c._build_payload([ChatMessage(role="user", content=task)], None)
        parts = p["input"][0]["content"]
        assert [x["text"] for x in parts] == ["stabiler teil", "variabler teil"]
        assert parts[0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert "prompt_cache_breakpoint" not in parts[1]
        # The sentinel must no longer appear anywhere in the payload
        import json as _json
        assert "CACHE_BREAKPOINT" not in _json.dumps(p)

    def test_payload_prompt_cache_key_auto_hashes_prefix(self):
        # "auto" = prefix hash (cache_key.py): same prompt -> same key,
        # early-diverging prompt (different book) -> different key.
        c = _client(prompt_cache_key="auto")
        p1 = c._build_payload([ChatMessage(role="user", content="Buch A")], None)
        p2 = c._build_payload([ChatMessage(role="user", content="Buch A")], None)
        p3 = c._build_payload([ChatMessage(role="user", content="Buch B")], None)
        assert p1["prompt_cache_key"].startswith("auto-")
        assert p1["prompt_cache_key"] == p2["prompt_cache_key"]
        assert p1["prompt_cache_key"] != p3["prompt_cache_key"]


class TestRegistryDispatch:
    def test_provider_dispatch(self):
        from agent_system.config.models import LLMModelConfig
        from agent_system.llm import registry
        # conftest replaces registry.build_client with a fake; the original
        # is kept as _orig_build_client (fallback: unpatched directly).
        build = getattr(registry, "_orig_build_client", registry.build_client)
        c = build(LLMModelConfig(
            provider="openai_responses", model="openai/gpt-5.6-terra",
            api_key="sk-or-test", base_url="https://openrouter.ai/api/v1",
            thinking_level="max", service_tier="flex",
            provider_routing={"order": ["openai"], "allow_fallbacks": False}))
        assert isinstance(c, OpenAIResponsesClient)
        assert c.thinking_level == "max"
        assert c.provider_routing["order"] == ["openai"]


class TestRdOrphanedHandling:
    def test_orphaned_message_not_replayed_verbatim(self):
        """Review finding (major): after a history mutation,
        invalidate_reasoning_artifacts flags the last assistant message with
        rd_orphaned and KEEPS its block -- a verbatim replay would then be a
        partial chain (predecessor stripped) -> verify 400. Orphaned messages
        must be rebuilt from content/tool_calls."""
        c = _client()
        assistant = c._format_response({"output": SAMPLE_OUTPUT})["assistant"]
        msg = ChatMessage(**assistant)
        msg.rd_orphaned = True
        items = c._messages_to_input([msg])
        types = [i["type"] for i in items]
        assert "reasoning" not in types            # no verbatim replay
        assert types == ["function_call", "function_call"]
        assert [i["call_id"] for i in items] == ["call_1", "call_2"]

    def test_invalidation_integration(self):
        """End to end with the real invalidation infrastructure: the older
        message is stripped, the last one flagged -> the input contains NO
        reasoning items."""
        from agent_system.utils.reasoning_artifacts import invalidate_reasoning_artifacts
        c = _client()
        a1 = ChatMessage(**c._format_response({"output": SAMPLE_OUTPUT})["assistant"])
        a2 = ChatMessage(**c._format_response({"output": SAMPLE_OUTPUT})["assistant"])
        msgs = [ChatMessage(role="user", content="hi"), a1,
                ChatMessage(role="tool", content="r", tool_call_id="call_1"), a2]
        invalidate_reasoning_artifacts(msgs)
        items = c._messages_to_input(msgs)
        assert all(i["type"] != "reasoning" for i in items)


class TestHookPayloadKeys:
    def test_success_notification_uses_response_data_key(self):
        """Review finding (major): hook_integration reads info['response_data'];
        the key 'response' would fill the message_debugger with NULL bodies.
        Source invariant: the success path sends response_data."""
        src = (Path(__file__).parents[1] /
               "openai_responses_client.py").read_text(encoding="utf-8")
        assert '"response_data": response_data' in src
        assert '"response": response_data' not in src


class TestVerbatimReplayIsModelBound:
    """Every model on this route writes the same format tag, but the encrypted
    payload only verifies against the model that produced it. So the block
    records its model and the replay checks it — otherwise a mid-run switch
    (escalation, fallback, continued session) replays foreign items and the
    gateway rejects the turn."""

    def test_the_producing_model_is_recorded(self):
        assistant = _client(model="A")._format_response({"output": SAMPLE_OUTPUT})["assistant"]
        assert assistant["reasoning_details"][0]["model"] == "A"

    def test_the_same_model_replays_verbatim(self):
        c = _client(model="A")
        msg = ChatMessage(**c._format_response({"output": SAMPLE_OUTPUT})["assistant"])
        assert [i["type"] for i in c._messages_to_input([msg])][0] == "reasoning"

    def test_another_model_does_not(self):
        producer = _client(model="A")
        msg = ChatMessage(**producer._format_response({"output": SAMPLE_OUTPUT})["assistant"])
        items = _client(model="B")._messages_to_input([msg])
        assert all(i["type"] != "reasoning" for i in items), \
            "foreign reasoning replayed — this is the payload the gateway rejects"
        assert [i["call_id"] for i in items] == ["call_1", "call_2"], \
            "the turn itself must survive; only the artifacts are foreign"

    def test_a_block_without_a_model_counts_as_foreign(self):
        """Legacy sessions (written before the model was recorded) restart the
        chain instead of gambling on the payload matching."""
        c = _client(model="A")
        msg = ChatMessage(**c._format_response({"output": SAMPLE_OUTPUT})["assistant"])
        msg.reasoning_details[0].pop("model")
        assert all(i["type"] != "reasoning" for i in c._messages_to_input([msg]))


def _iter_all_parts(payload):
    """Yield every content part dict across all input items of a payload."""
    for item in payload.get("input", []):
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, list):
            yield from (p for p in content if isinstance(p, dict))


class TestClaudeCacheControl:
    """Claude on the Responses API: one top-level cache_control, nothing on
    the parts. The GPT breakpoint path stays untouched.

    Measured 2026-09-22 against OpenRouter (claude-haiku-4.5, the same ~15.6k
    token request twice): cache_control on an input_text part, on the message
    item, on a tool or with a ttl -> the second call read 0 cached tokens;
    top-level -> 15,628. These tests pin the payload; the cache itself is
    only visible live.
    """

    CONVERSATION = [
        ChatMessage(role="system", content="SYS"),
        ChatMessage(role="user", content="Q1"),
        ChatMessage(role="assistant", content="A1"),
        ChatMessage(role="user", content="Q2"),
    ]
    TOOLS = [{"type": "function", "function": {"name": "f", "parameters": {}}}]

    def _claude(self, mode):
        return _client(model="~anthropic/claude-sonnet-latest",
                       prompt_cache_marker_style="anthropic", prompt_cache_mode=mode)

    def test_a_conversation_is_cached_at_the_top_only(self):
        p = self._claude("multi_turn")._build_payload(self.CONVERSATION, self.TOOLS)
        assert p["cache_control"] == {"type": "ephemeral"}
        assert all("cache_control" not in part and "prompt_cache_breakpoint" not in part
                   for part in _iter_all_parts(p))
        assert all("cache_control" not in t for t in p["tools"])

    def test_a_tool_loop_counts_as_a_conversation(self):
        """A tool loop replays function_call items that carry no role; the
        history is read from the ChatMessages, so auto still caches."""
        msgs = [ChatMessage(role="system", content="SYS"), ChatMessage(role="user", content="Q"),
                ChatMessage(role="assistant", content="",
                            tool_calls=[{"id": "c1", "type": "function",
                                         "function": {"name": "f", "arguments": "{}"}}]),
                ChatMessage(role="tool", content="R", tool_call_id="c1")]
        p = self._claude("auto")._build_payload(msgs, self.TOOLS)
        assert p["cache_control"] == {"type": "ephemeral"}

    def test_auto_without_history_and_one_shot_stay_uncached(self):
        """The top-level marker caches the whole prompt; a single call would
        pay the write surcharge for nothing."""
        first = self.CONVERSATION[:2]
        assert "cache_control" not in self._claude("auto")._build_payload(first, None)
        assert "cache_control" not in self._claude("one_shot")._build_payload(self.CONVERSATION, None)

    def test_a_non_claude_model_is_left_alone(self):
        c = _client(prompt_cache_mode="multi_turn")   # openai/gpt-5.6-terra
        p = c._build_payload(self.CONVERSATION, None)
        assert "cache_control" not in p
        assert all("cache_control" not in part for part in _iter_all_parts(p))

    def test_gpt_default_uses_breakpoints_not_cache_control(self):
        c = _client(prompt_cache_key="auto", prompt_cache_mode="task_sequence")
        task = "STATIC\n<<<CACHE_BREAKPOINT>>>\nAPPEND\n<<<CACHE_BREAKPOINT>>>\nVOLATILE"
        p = c._build_payload([ChatMessage(role="user", content=task)], None)
        parts = list(_iter_all_parts(p))
        assert any("prompt_cache_breakpoint" in part for part in parts)
        assert all("cache_control" not in part for part in parts)

    def test_claude_strips_the_breakpoint_sentinels(self):
        task = "A\n<<<CACHE_BREAKPOINT>>>\nB"
        for mode in ("multi_turn", "one_shot"):
            p = self._claude(mode)._build_payload([ChatMessage(role="user", content=task)], None)
            texts = [part.get("text", "") for part in _iter_all_parts(p)]
            texts += [i["content"] for i in p["input"] if isinstance(i.get("content"), str)]
            assert texts and all("<<<CACHE_BREAKPOINT>>>" not in t for t in texts)


class TestTruncationReachesTheCaller:
    """A cut-off answer must be distinguishable from a complete one.

    Measured 2026-09-01 in production: this client detected
    ``status=incomplete``, logged a warning and returned the truncated
    answer as a normal result. The agent server's content-filter guard
    and its truncation guard both key on ``finish_reason``, so for every
    Responses model both were dead. Two real cases that night: a
    content_filter cut and a max_output_tokens cut of a writing agent.
    """

    def test_max_output_tokens_surfaces_as_length(self):
        c = _client()
        result = c._format_response({
            "output": SAMPLE_OUTPUT,
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        })
        # "length" is the chat-completions vocabulary the server's guards
        # already speak — not the Responses API's own wording.
        assert result["finish_reason"] == "length"

    def test_content_filter_surfaces(self):
        c = _client()
        result = c._format_response({
            "output": SAMPLE_OUTPUT,
            "status": "incomplete",
            "incomplete_details": {"reason": "content_filter"},
        })
        assert result["finish_reason"] == "content_filter"

    def test_unknown_reason_is_passed_through_not_dropped(self):
        """A reason we have no mapping for must stay visible."""
        c = _client()
        result = c._format_response({
            "output": SAMPLE_OUTPUT,
            "status": "incomplete",
            "incomplete_details": {"reason": "some_future_reason"},
        })
        assert result["finish_reason"] == "some_future_reason"

    def test_complete_response_carries_no_finish_reason(self):
        """The normal path must stay untouched — a spurious finish_reason
        would feed the server's guards on every single healthy answer."""
        c = _client()
        result = c._format_response({"output": SAMPLE_OUTPUT, "status": "completed"})
        assert "finish_reason" not in result

    def test_incomplete_without_details_still_surfaces(self):
        """``reason`` is Optional in the SDK type and upstream is known to
        send the details empty. The status alone already says the answer is
        cut off, so keying on the details would drop exactly those cases
        back into silence."""
        c = _client()
        result = c._format_response({"output": SAMPLE_OUTPUT, "status": "incomplete"})
        assert result["finish_reason"] == "length"

    def test_incomplete_with_null_reason_still_surfaces(self):
        c = _client()
        result = c._format_response({
            "output": SAMPLE_OUTPUT,
            "status": "incomplete",
            "incomplete_details": {"reason": None},
        })
        # "length" and not "content_filter": the conservative reading warns
        # without switching the fallback profile for an hour.
        assert result["finish_reason"] == "length"

    def test_truncated_answer_still_keeps_its_content(self):
        """Surfacing the truncation must not cost the partial output — the
        caller decides what to do with it, this client does not discard."""
        c = _client()
        result = c._format_response({
            "output": SAMPLE_OUTPUT,
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        })
        assert result["assistant"]["tool_calls"], "partial output was dropped"

    def test_streaming_chunk_carries_the_truncation(self):
        """The streaming assembler reads finish_reason off the CHUNK.

        Setting it only on the result would leave the guards dead on this
        path — the mutation that removes the chunk line stays green
        without this test. Driven through the real loop (the transport is
        swapped, not the method under test), so a wrapper that stops passing
        the truncation on fails here.
        """
        chunks = _drive_non_streaming({
            "output": SAMPLE_OUTPUT,
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        })
        assert len(chunks) == 1, "fixture produced no final chunk"
        assert chunks[0]["finish_reason"] == "length"

    def test_streaming_chunk_stays_clean_when_complete(self):
        chunks = _drive_non_streaming({"output": SAMPLE_OUTPUT, "status": "completed"})
        assert "finish_reason" not in chunks[0]
        assert chunks[0]["assistant"]["tool_calls"], "a complete answer lost its tool calls"


class _FakeStream:
    """A scripted event stream behind the client's ``_stream`` seam."""

    def __init__(self, lines, status_code: int = 200, text: str = "",
                 content_type: str = "text/event-stream",
                 byte_chunks: list | None = None):
        self._lines = list(lines)
        # Hand-cut network chunks, for tests about the framing itself.
        self._byte_chunks = byte_chunks
        self.status_code = status_code
        # The real gateway answers a stream request with this content type;
        # anything else means there is no stream to read line by line.
        self.headers: dict = {"content-type": content_type}
        self.text = text
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        """Raw bytes — one line per chunk, or the test's own cuts. The reader
        does its own framing, exactly as it must against the real socket."""
        if self._byte_chunks is not None:
            for chunk in self._byte_chunks:
                yield chunk
            return
        for line in self._lines:
            yield (line + "\n").encode("utf-8")

    async def aiter_lines(self):
        """What httpx hands a reader that asks for LINES: its decoder splits
        like ``str.splitlines()`` — also at U+2028, U+2029 and U+0085, which
        SSE does not treat as line ends and JSON allows raw inside a string.

        Kept faithful on purpose. Going back to ``aiter_lines()`` must fail a
        test here rather than a book in production.
        """
        for line in self._lines:
            for piece in line.splitlines():
                yield piece

    async def aread(self):
        return self.text.encode()

    def json(self):
        import json

        return json.loads(self.text)


def _sse(*events) -> list[str]:
    import json

    # ensure_ascii=False like the real gateway: it sends UTF-8 on the wire and
    # does not escape non-ASCII, which is what makes the framing matter.
    return [f"data: {json.dumps(event, ensure_ascii=False)}" for event in events] + \
        ["data: [DONE]"]


#: What the gateway sends last. Measured 2026-09-12: the object inside
#: ``response.completed`` has the same shape as the non-streaming body.
_COMPLETED = {
    "type": "response.completed",
    "response": {
        "status": "completed",
        "output": SAMPLE_OUTPUT,
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        "openrouter_metadata": {
            "endpoints": {"available": [{"provider": "DeepInfra", "selected": True}]}},
    },
}


#: How a run that reaches the output cap ends. Measured 2026-09-12 against the
#: live gateway with a 16-token cap: ``response.incomplete``, carrying the
#: reason, the partial output and the usage — an ordinary body, not a broken
#: stream.
_INCOMPLETE = {
    "type": "response.incomplete",
    "response": {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": SAMPLE_OUTPUT,
        "usage": {"input_tokens": 10, "output_tokens": 16, "total_tokens": 26},
    },
}


def _stream_chunks(lines, stream_kw: dict | None = None, **client_kw) -> list[dict]:
    import asyncio

    client = _client(**client_kw)
    client._stream = lambda *a, **kw: _FakeStream(lines, **(stream_kw or {}))

    async def _collect():
        return [chunk async for chunk in client.chat_tools_streaming([], [])]

    return asyncio.run(_collect())


class TestTheEventStream:
    """What the client makes of the gateway's server-sent events."""

    def test_thinking_arrives_as_its_own_chunk_before_the_answer(self):
        chunks = _stream_chunks(_sse(
            {"type": "response.reasoning_text.delta", "delta": "wei"},
            {"type": "response.reasoning_text.delta", "delta": "l"},
            {"type": "response.output_text.delta", "delta": "Der "},
            {"type": "response.output_text.delta", "delta": "Himmel"},
            _COMPLETED,
        ))
        kinds = [chunk["type"] for chunk in chunks]
        assert kinds == ["thinking_delta", "thinking_delta",
                         "content_delta", "content_delta", "final"]
        assert [c["delta"] for c in chunks[:2]] == ["wei", "l"]
        # The agent server forwards accumulated text, not just the delta.
        assert chunks[3]["accumulated"] == "Der Himmel"

    def test_the_final_chunk_carries_what_the_non_streaming_path_returns(self):
        chunks = _stream_chunks(_sse(_COMPLETED))
        final = chunks[-1]
        assert final["type"] == "final"
        assistant = final["assistant"]
        # Verbatim replay block, backend pin and usage all come off the same
        # object — that is why the stream is read into _format_response.
        assert assistant["reasoning_details"][0]["items"] == SAMPLE_OUTPUT
        assert assistant["served_by"] == "DeepInfra"
        assert assistant["tool_calls"], "tool calls lost on the streaming path"
        assert final["usage"]["prompt_tokens"] == 10

    def test_tool_call_arguments_are_forwarded_while_they_arrive(self):
        chunks = _stream_chunks(_sse(
            {"type": "response.function_call_arguments.delta",
             "output_index": 1, "delta": '{"city":'},
            {"type": "response.function_call_arguments.delta",
             "output_index": 1, "delta": ' "Hamburg"}'},
            _COMPLETED,
        ))
        deltas = [c for c in chunks if c["type"] == "tool_call_delta"]
        assert [d["delta"]["function"]["arguments"] for d in deltas] == \
            ['{"city":', ' "Hamburg"}']
        assert deltas[0]["index"] == 1

    def test_a_capped_run_ends_the_stream_and_keeps_its_answer(self):
        """``response.completed`` is not the only way a run ends.

        Measured 2026-09-12: a request capped at 16 output tokens ends with
        ``response.incomplete``. Accepting only ``completed`` turned that into
        a missing body — so a DETERMINISTIC cap was regenerated until the
        retries ran out and then raised, while the truncation guard, which
        exists for exactly this case, never saw a finish_reason.
        """
        chunks = _stream_chunks(_sse(
            {"type": "response.output_text.delta", "delta": "Das Meer"},
            _INCOMPLETE,
        ), max_retries=0)
        final = chunks[-1]
        assert final["type"] == "final"
        assert final["finish_reason"] == "length"
        assert final["assistant"]["tool_calls"], "the partial answer was dropped"

    def test_a_failed_run_ends_the_stream_with_its_error(self):
        """The third terminal event. Its error sits inside the response
        object, so it reaches the caller as an upstream error instead of
        looking like a stream that broke off."""
        chunks = _stream_chunks(_sse({
            "type": "response.failed",
            "response": {"status": "failed",
                         "error": {"code": "invalid_request",
                                   "message": "bad tool schema"}},
        }), max_retries=0)
        assert chunks[-1]["assistant"]["error"]["message"] == "bad tool schema"

    def test_a_json_body_is_not_read_as_a_stream(self):
        """The gateway proxies upstream errors as a plain JSON body — also
        when the request asked for a stream.

        Read line by line, such a body has no ``data:`` lines at all: every
        line is skipped, nothing terminal arrives, and a deterministic error
        looks like a broken stream. The body-error handling (rate-limit
        retry, reasoning-artifact healing, the error the caller can act on)
        would never run.
        """
        import json

        body = json.dumps({"error": {"code": "invalid_request",
                                     "message": "no endpoints found"}})
        chunks = _stream_chunks([], stream_kw={"text": body,
                                               "content_type": "application/json"},
                                max_retries=0)
        assert chunks[-1]["assistant"]["error"]["message"] == "no endpoints found"

    def test_a_stream_that_stops_early_is_handed_over_flagged_not_raised(self):
        """A dropped connection is not worth an exception.

        Retries come first; this is what is left when they are spent. Raising
        instead would switch the agent server's fallback profile persistently
        and end a run that has no fallback chain — its own comment calls that
        far too heavy for what is usually a network hiccup, and the sibling
        client hands the partial answer over under exactly this flag.
        """
        chunks = _stream_chunks(
            _sse({"type": "response.output_text.delta", "delta": "halb"})[:-1],
            max_retries=0)
        final = chunks[-1]
        assert final["type"] == "final"
        assert final["finish_reason"] == "incomplete_stream"
        assert final["assistant"]["content"] == "halb"

    def test_an_event_split_across_two_packets_is_reassembled(self):
        """The network cuts where it likes, not at line ends."""
        line = _sse(_COMPLETED)[0] + "\n"
        raw = line.encode("utf-8")
        cut = len(raw) // 2
        chunks = _stream_chunks([], stream_kw={"byte_chunks": [raw[:cut], raw[cut:]]})
        assert chunks[-1]["assistant"]["tool_calls"], "the split event was lost"

    def test_a_line_separator_in_the_prose_does_not_cut_the_event(self):
        """U+2028 is a line end to ``str.splitlines()`` but not to SSE, and
        JSON carries it raw inside a string. Read line-wise, one of them in
        the answer slices the event in half — the delta vanishes, or the whole
        body does."""
        chunks = _stream_chunks(_sse(
            {"type": "response.output_text.delta", "delta": "erst dann"},
            _COMPLETED,
        ))
        deltas = [c for c in chunks if c["type"] == "content_delta"]
        assert [d["delta"] for d in deltas] == ["erst dann"]

    def test_the_openai_family_thinking_channel_is_read_too(self):
        """Those models never emit raw reasoning, only a summary of it — under
        an event name of its own. Missing it leaves them with no live view of
        their thinking at all."""
        chunks = _stream_chunks(_sse(
            {"type": "response.reasoning_summary_text.delta", "delta": "Ich prüfe"},
            _COMPLETED,
        ))
        assert chunks[0]["type"] == "thinking_delta"
        assert chunks[0]["delta"] == "Ich prüfe"

    def test_only_data_lines_are_events(self):
        """The gateway holds a silent call open with ': ' lines — that is why
        no read timeout ever fires on a 40-minute call.

        The smuggled line is the point: a reader without the ``data:`` check
        slices five characters off every line and believes whatever is left,
        so a line that merely LOOKS like a field would become a token.
        """
        import json

        smuggled = json.dumps({"type": "response.output_text.delta", "delta": "X"})
        lines = [": ", "", ": OPENROUTER PROCESSING",
                 f"evt: {smuggled}",
                 f"data: {json.dumps(_COMPLETED)}", "data: [DONE]"]
        chunks = _stream_chunks(lines)
        assert [c["type"] for c in chunks] == ["final"]


class _PacedStream(_FakeStream):
    """Keep-alive comments every ``gap`` seconds, each line after the next one. After the lines, by ``ending``:
    keep-alives forever (a silent upstream as the gateway holds it open), the read timeout httpx raises when not
    even those arrive ("quiet"), or a dropped connection ("drop")."""

    def __init__(self, lines, gap: float, ending: str = "keep-alives"):
        super().__init__(lines)
        self._gap = gap
        self._ending = ending

    async def aiter_bytes(self):
        import asyncio

        import httpx

        for line in self._lines:
            await asyncio.sleep(self._gap)
            yield b": OPENROUTER PROCESSING\n\n"
            yield (line + "\n").encode("utf-8")
        if self._ending == "quiet":
            raise httpx.ReadTimeout("")  # what httpcore raises for a silent socket: no message
        if self._ending == "drop":
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        while True:
            await asyncio.sleep(self._gap)
            yield b": OPENROUTER PROCESSING\n\n"


_THINKING_EVENT = {"type": "response.reasoning_text.delta", "delta": "hm"}
#: all hidden thinking sends: measured on 16.09.2026, 74 of these pairs over 38k tokens of hidden reasoning
_BOOKKEEPING_EVENT = {"type": "response.output_item.added", "item": {"type": "reasoning"}}


class TestASilentUpstream:
    """The gateway sends keep-alive comments about every half second; they reset the socket's read timeout, so only
    events count as progress, and the declared ``stream_silence_timeout`` bounds a stream that sends nothing else."""

    LIMIT = 1.0
    GAP = 0.1
    opened: list

    def _run(self, *attempts, silence=LIMIT, ending="keep-alives", outer=20):
        """One scripted stream per attempt. Only the limit a test names is short."""
        import asyncio

        from plugins.llm_openai_compat.httpx_client import HTTPXTimeoutConfig

        client = _client(max_retries=len(attempts) - 1, retry_backoff=0, stream_silence_timeout=silence,
                         timeout_config=HTTPXTimeoutConfig(connect=30, read=30, write=30, pool=30))
        self.opened = []
        streams = iter(attempts)

        def open_stream(*args, **kwargs):
            self.opened.append(1)
            return _PacedStream(next(streams), self.GAP, ending)

        client._stream = open_stream

        async def collect():
            return [chunk async for chunk in client.chat_tools_streaming([], [])]

        # a regression hangs; the outer limit turns that into a failure
        return asyncio.run(asyncio.wait_for(collect(), outer))

    @staticmethod
    def _text(chunks) -> str:
        return "".join(c["delta"] for c in chunks if c["type"] == "content_delta")

    def test_only_keep_alives_end_in_a_timeout_after_every_attempt(self):
        import httpx

        with pytest.raises(httpx.ReadTimeout, match="only keep-alives"):
            self._run([], [])
        assert len(self.opened) == 2

    def test_without_a_declared_limit_keep_alives_hold_the_call(self):
        """None is the endpoint's own bound (DeepSeek closes its queue after 10 minutes), not ours."""
        import asyncio

        with pytest.raises(asyncio.TimeoutError):
            self._run([], silence=None, outer=3 * self.LIMIT)
        assert len(self.opened) == 1

    def test_a_silent_attempt_is_retried_with_a_fresh_clock(self):
        chunks = self._run([], _sse({"type": "response.output_text.delta", "delta": "da"}, _COMPLETED))
        assert chunks[-1]["type"] == "final" and self._text(chunks) == "da"
        assert len(self.opened) == 2

    @pytest.mark.parametrize("event", [_THINKING_EVENT, _BOOKKEEPING_EVENT], ids=["thinking", "bookkeeping"])
    def test_a_stream_of_one_kind_of_event_outlasts_the_limit(self, event):
        """The limit is the silence between events, not the length of the call, and every event counts: this one
        sends only that kind of event for one and a half times the limit before the answer comes."""
        chunks = self._run(_sse(*[event] * 15, {"type": "response.output_text.delta", "delta": "da"}, _COMPLETED))
        assert chunks[-1]["type"] == "final" and self._text(chunks) == "da"
        assert len(self.opened) == 1

    @pytest.mark.parametrize("ending", ["keep-alives", "quiet", "drop"])
    def test_a_finished_run_is_kept_however_the_stream_ends(self, ending):
        """Paid for and complete: however the stream ends after the terminal event, the answer must not be thrown
        away and generated again."""
        chunks = self._run(_sse({"type": "response.output_text.delta", "delta": "fertig"}, _COMPLETED)[:-1],
                           ending=ending)
        assert chunks[-1]["type"] == "final"
        assert "finish_reason" not in chunks[-1], "a complete run was flagged as cut off"
        assert chunks[-1]["assistant"]["tool_calls"], "the finished answer was dropped"
        assert len(self.opened) == 1


class TestWhoStreams:
    def test_capabilities_decide(self):
        assert _client().supports_streaming() is True
        assert _client(capabilities={"streaming": False}).supports_streaming() is False
        assert _client(capabilities=ModelCapabilities(streaming=False)).supports_streaming() is False

    def test_a_transport_swapping_subclass_does_not_stream(self):
        """The reader talks to httpx directly, so a subclass that swaps the
        transport must keep the non-streaming path — otherwise its transport is
        silently bypassed."""
        class _SdkLike(OpenAIResponsesClient):
            _STREAMS_SSE = False

        assert _SdkLike(model="m", api_key="k").supports_streaming() is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
