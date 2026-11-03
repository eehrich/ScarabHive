"""Gemini takes an object as functionResponse.response, and a batch with an image builds.

A tool answering `[...]` or `42` went in as is and Gemini refused every later
turn with a 400 (both the HTTP client and the batch). A dumped ImageContent
carries `image_url: None` beside `source`, and the batch converter crashed on
`None.startswith`; source (base64) images were never sent at all.
"""
import base64
from unittest.mock import patch

import pytest

from plugins.llm_gemini.gemini_utils import function_response_body


@pytest.mark.parametrize("content,expected", [
    ('{"a": 1}', {"a": 1}),
    ('[{"id": 1}]', {"result": [{"id": 1}]}),
    ("42", {"result": 42}),
    ('"ok"', {"result": "ok"}),
    ("[Tool:x ref:y]", {"result": "[Tool:x ref:y]"}),
    ([{"type": "text", "text": "hi"}], {"result": "hi"}),
    (None, {}),
])
def test_function_response_body_is_always_an_object(content, expected):
    assert function_response_body(content) == expected


def _client():
    from plugins.llm_gemini.gemini_batch import GeminiBatchClient

    with patch("google.genai.Client"):
        return GeminiBatchClient(api_key="test-key")


def test_batch_tool_result_list_is_wrapped():
    contents = _client()._convert_messages_to_contents([
        {"role": "tool", "name": "t", "tool_call_id": "c", "content": "[1, 2]"}])
    assert contents[0].parts[0].function_response.response == {"result": [1, 2]}


def test_batch_source_image_is_sent_and_does_not_crash():
    png = base64.b64encode(b"\x89PNG fake").decode()
    contents = _client()._convert_messages_to_contents([{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "image_url": None,
         "source": {"type": "base64", "media_type": "image/png", "data": png}},
    ]}])
    blob = contents[0].parts[1].inline_data
    assert blob.mime_type == "image/png" and blob.data == b"\x89PNG fake"


def test_batch_data_url_image_still_works():
    png = base64.b64encode(b"img").decode()
    contents = _client()._convert_messages_to_contents([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{png}"}}]}])
    assert contents[0].parts[0].inline_data.mime_type == "image/jpeg"


def test_batch_parallel_tool_responses_share_one_block():
    contents = _client()._convert_messages_to_contents([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "text", "text": "calling"}], "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "g", "arguments": "{}"}}]},
        {"role": "tool", "name": "f", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "name": "g", "tool_call_id": "b", "content": "2"},
    ])
    assert [c.role for c in contents].count("tool") == 1
    tool_block = contents[-1]
    assert [p.function_response.name for p in tool_block.parts] == ["f", "g"]
    assert contents[1].parts[0].text == "calling"
