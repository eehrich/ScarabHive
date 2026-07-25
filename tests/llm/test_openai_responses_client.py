"""Tests für den OpenAI-Responses-Client (natives Item-Round-Tripping).

Kern-Invariante: Output-Items des Modells werden VERBATIM in einem
reasoning_details-Block gespeichert und beim nächsten Request exakt
reproduziert — keine Rekonstruktion, kein Bridging-Verlust (die Ursache der
encrypted-reasoning-400s der Chat-Completions-Route).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from agent_system.llm.capabilities import ModelCapabilities
from agent_system.llm.models import (
    AudioContent,
    ChatMessage,
    ImageContent,
    ImageSource,
    TextContent,
)
from agent_system.llm.openai_responses_client import (
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


SAMPLE_OUTPUT = [
    {"type": "reasoning", "id": "rs_abc", "status": "completed",
     "encrypted_content": "BLOB", "format": "openai-responses-api",
     "summary": [{"type": "summary_text", "text": "thinking about it"}]},
    {"type": "function_call", "id": "fc_1", "call_id": "call_1",
     "name": "get_value", "arguments": '{"name": "alpha"}'},
    {"type": "function_call", "id": "fc_2", "call_id": "call_2",
     "name": "get_value", "arguments": '{"name": "beta"}'},
]


class TestFormatResponse:
    def test_tool_calls_and_verbatim_block(self):
        c = _client()
        result = c._format_response({"output": SAMPLE_OUTPUT, "status": "completed",
                                     "usage": {"input_tokens": 100, "output_tokens": 20,
                                               "total_tokens": 120}})
        a = result["assistant"]
        assert [t["id"] for t in a["tool_calls"]] == ["call_1", "call_2"]
        assert a["tool_calls"][0]["function"]["name"] == "get_value"
        # Verbatim-Block trägt ALLE Output-Items unverändert
        blocks = a["reasoning_details"]
        assert len(blocks) == 1 and blocks[0]["format"] == RESPONSES_ITEMS_FORMAT
        assert blocks[0]["items"] == SAMPLE_OUTPUT
        assert a["reasoning_content"] == "thinking about it"
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
        """session_costs.py bevorzugt das billed-cost-Feld von OpenRouter —
        _map_usage muss unbekannte Zusatzfelder durchreichen, ohne die
        gemappten Chat-Keys zu verlieren."""
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
        """format_response → ChatMessage → _messages_to_input reproduziert die
        Output-Items exakt (die Kern-Invariante)."""
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
        """Message mit Verbatim-Block darf content/tool_calls NICHT zusätzlich
        serialisieren (wären Duplikate der enthaltenen Items)."""
        c = _client()
        assistant = c._format_response({"output": SAMPLE_OUTPUT})["assistant"]
        assistant["content"] = "sichtbarer text"
        items = c._messages_to_input([ChatMessage(**assistant)])
        assert items == SAMPLE_OUTPUT

    def test_foreign_history_reconstructed_without_artifacts(self):
        """Chat-Route-Sessions (openai-responses-v1-Blöcke) und Gemini-Blöcke
        werden ignoriert — Kette startet frisch, calls/content bleiben."""
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

    def test_gemini_tool_schemas_sanitized(self):
        """Gemini-Modelle: Function-Declaration-feindliche JSON-Schema-Keywords
        (additionalProperties, default, format, oneOf, title) werden entfernt —
        wie auf der Chat-Route (_sanitize_tools_for_gemini); GPT bleibt roh."""
        import json as _json
        nasty = [{"type": "function", "function": {"name": "f", "description": "d",
                  "parameters": {"type": "object", "title": "T", "additionalProperties": False,
                                 "properties": {"x": {"type": "string", "default": "a",
                                                      "format": "id"},
                                                "y": {"oneOf": [{"type": "string"}]}}}}}]
        gem = _client(model="google/gemini-3.5-flash-lite")._convert_tools(nasty)
        blob = _json.dumps(gem)
        for kw in ('"title"', '"default"', '"oneOf"', '"additionalProperties"', '"format"'):
            assert kw not in blob, f"{kw} nicht sanitized"
        gpt = _client(model="openai/gpt-5.6-terra")._convert_tools(nasty)
        assert '"oneOf"' in _json.dumps(gpt)

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

    def test_payload_omits_unset_options(self):
        c = _client(thinking_level=None, service_tier=None,
                    provider_routing=None, max_tokens=None)
        p = c._build_payload([ChatMessage(role="user", content="hi")], None)
        for absent in ("reasoning", "max_output_tokens", "service_tier", "provider",
                       "tools", "tool_choice", "parallel_tool_calls",
                       "prompt_cache_key"):
            assert absent not in p

    def test_payload_carries_prompt_cache_key(self):
        # GPT-5.6+: ohne prompt_cache_key kein zuverlaessiges Cache-Matching
        # (belegt Testlauf B936: byte-identischer Prefix, cached_tokens=0).
        c = _client(prompt_cache_key="v4_scene_planner")
        p = c._build_payload([ChatMessage(role="user", content="hi")], None)
        assert p["prompt_cache_key"] == "v4_scene_planner"

    def test_payload_splits_cache_breakpoint_sentinel(self):
        # GPT-5.6 cached Mid-Prompt-Divergenz nur mit expliziten Breakpoints
        # (Experimente 2026-07-21): Sentinel im Task -> input_text-Parts,
        # alle bis auf den letzten mit prompt_cache_breakpoint markiert.
        from agent_system.llm.cache_key import CACHE_BP_SENTINEL
        c = _client(prompt_cache_key="auto")
        task = "stabiler teil" + CACHE_BP_SENTINEL + "variabler teil"
        p = c._build_payload([ChatMessage(role="user", content=task)], None)
        parts = p["input"][0]["content"]
        assert [x["text"] for x in parts] == ["stabiler teil", "variabler teil"]
        assert parts[0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert "prompt_cache_breakpoint" not in parts[1]
        # Sentinel darf den Payload nirgends mehr verlassen
        import json as _json
        assert "CACHE_BREAKPOINT" not in _json.dumps(p)

    def test_payload_prompt_cache_key_auto_hashes_prefix(self):
        # "auto" = Praefix-Hash (cache_key.py): gleicher Prompt -> gleicher
        # Key, frueh divergenter Prompt (anderes Buch) -> anderer Key.
        c = _client(prompt_cache_key="auto")
        p1 = c._build_payload([ChatMessage(role="user", content="Buch A")], None)
        p2 = c._build_payload([ChatMessage(role="user", content="Buch A")], None)
        p3 = c._build_payload([ChatMessage(role="user", content="Buch B")], None)
        assert p1["prompt_cache_key"].startswith("auto-")
        assert p1["prompt_cache_key"] == p2["prompt_cache_key"]
        assert p1["prompt_cache_key"] != p3["prompt_cache_key"]


class TestMakeLLMRegistration:
    def test_provider_dispatch(self):
        from agent_system.llm import clients
        # conftest ersetzt make_llm durch einen Fake; das Original liegt in
        # _orig_make_llm (Fallback: unpatched direkt).
        make_llm = getattr(clients, "_orig_make_llm", clients.make_llm)
        c = make_llm("openai_responses", "openai/gpt-5.6-terra", "sk-or-test",
                     base_url="https://openrouter.ai/api/v1",
                     thinking_level="max", service_tier="flex",
                     provider_routing={"order": ["openai"], "allow_fallbacks": False})
        assert isinstance(c, OpenAIResponsesClient)
        assert c.thinking_level == "max"
        assert c.provider_routing["order"] == ["openai"]


class TestRdOrphanedHandling:
    def test_orphaned_message_not_replayed_verbatim(self):
        """Review-Fund (major): Nach History-Mutation flaggt
        invalidate_reasoning_artifacts die letzte assistant-Message mit
        rd_orphaned und BEHÄLT ihren Block — verbatim-Replay wäre dann eine
        partielle Kette (Vorgänger gestrippt) → Verify-400. Orphaned Messages
        müssen aus content/tool_calls rekonstruiert werden."""
        c = _client()
        assistant = c._format_response({"output": SAMPLE_OUTPUT})["assistant"]
        msg = ChatMessage(**assistant)
        msg.rd_orphaned = True
        items = c._messages_to_input([msg])
        types = [i["type"] for i in items]
        assert "reasoning" not in types            # kein verbatim-Replay
        assert types == ["function_call", "function_call"]
        assert [i["call_id"] for i in items] == ["call_1", "call_2"]

    def test_invalidation_integration(self):
        """End-to-End mit der echten Invalidierungs-Infra: ältere Message wird
        gestrippt, letzte geflaggt → Input enthält KEINE reasoning-Items."""
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
        """Review-Fund (major): hook_integration liest info['response_data'];
        der Key 'response' würde den message_debugger mit NULL-Bodies füllen.
        Source-Invariante: Erfolgspfad sendet response_data."""
        src = Path(__file__).parent.parent.parent.joinpath(
            "src/agent_system/llm/openai_responses_client.py").read_text(encoding="utf-8")
        assert '"response_data": response_data' in src
        assert '"response": response_data' not in src


class TestEncrypted400Detection:
    def test_detects_verification_error(self):
        assert OpenAIResponsesClient._is_encrypted_reasoning_400(
            'The encrypted content for item rs_x could not be verified')
        assert OpenAIResponsesClient._is_encrypted_reasoning_400(
            '"code": "invalid_encrypted_content"')
        assert not OpenAIResponsesClient._is_encrypted_reasoning_400(
            'Invalid request: missing field input')


def _iter_all_parts(payload):
    """Yield every content part dict across all input items of a payload."""
    for item in payload.get("input", []):
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, list):
            yield from (p for p in content if isinstance(p, dict))


class TestAnthropicFuturePath:
    """Forward-wiring: no Claude model routes through the Responses API today,
    but if one is configured with prompt_cache_marker_style=anthropic it must
    use cache_control (the shared policy) — NOT the GPT breakpoint path. The GPT
    default path must stay byte-for-byte unchanged."""

    def test_gpt_default_uses_breakpoints_not_cache_control(self):
        """Default (no marker style) = GPT-5.6 breakpoint path, no cache_control."""
        c = _client(prompt_cache_key="auto", prompt_cache_mode="task_sequence")
        task = "STATIC\n<<<CACHE_BREAKPOINT>>>\nAPPEND\n<<<CACHE_BREAKPOINT>>>\nVOLATILE"
        p = c._build_payload([ChatMessage(role="user", content=task)], None)
        parts = list(_iter_all_parts(p))
        assert any("prompt_cache_breakpoint" in part for part in parts)
        assert all("cache_control" not in part for part in parts)

    def test_anthropic_style_uses_cache_control_not_breakpoints(self):
        c = _client(
            model="anthropic/claude-sonnet-4",
            prompt_cache_marker_style="anthropic",
            prompt_cache_mode="multi_turn",
            prompt_cache_key="auto",
        )
        msgs = [
            ChatMessage(role="system", content="SYS"),
            ChatMessage(role="user", content="Q1"),
        ]
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        p = c._build_payload(msgs, tools)
        parts = list(_iter_all_parts(p))
        # cache_control present, NO OpenAI breakpoints
        assert any("cache_control" in part for part in parts)
        assert all("prompt_cache_breakpoint" not in part for part in parts)
        # last tool marked
        assert p["tools"][-1].get("cache_control") == {"type": "ephemeral"}

    def test_anthropic_style_strips_sentinels(self):
        """Anthropic path removes OpenAI breakpoint sentinels (wrong dialect)."""
        c = _client(
            model="anthropic/claude-sonnet-4",
            prompt_cache_marker_style="anthropic",
            prompt_cache_mode="multi_turn",
        )
        task = "A\n<<<CACHE_BREAKPOINT>>>\nB"
        p = c._build_payload([ChatMessage(role="user", content=task)], None)
        for part in _iter_all_parts(p):
            assert "<<<CACHE_BREAKPOINT>>>" not in part.get("text", "")

    def test_anthropic_cap_never_exceeds_four(self):
        c = _client(
            model="anthropic/claude-sonnet-4",
            prompt_cache_marker_style="anthropic",
            prompt_cache_mode="multi_turn",
        )
        msgs = [
            ChatMessage(role="system", content="SYS"),
            ChatMessage(role="user", content="Q1"),
            ChatMessage(role="assistant", content="A1"),
            ChatMessage(role="user", content="Q2"),
        ]
        tools = [{"type": "function", "function": {"name": f"t{i}", "parameters": {}}}
                 for i in range(5)]
        p = c._build_payload(msgs, tools)
        n = sum(1 for part in _iter_all_parts(p) if "cache_control" in part)
        n += sum(1 for t in p.get("tools", []) if "cache_control" in t)
        assert n <= 4


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
