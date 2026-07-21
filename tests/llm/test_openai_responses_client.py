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

from agent_system.llm.models import ChatMessage
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
        c = _client()
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


class TestToolsAndPayload:
    def test_tools_converted_to_flat_format(self):
        tools = [{"type": "function", "function": {
            "name": "get_value", "description": "d", "parameters": {"type": "object"}}}]
        conv = OpenAIResponsesClient._convert_tools(tools)
        assert conv == [{"type": "function", "name": "get_value",
                         "description": "d", "parameters": {"type": "object"}}]

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
