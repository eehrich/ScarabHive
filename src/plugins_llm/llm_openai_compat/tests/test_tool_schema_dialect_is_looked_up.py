"""A tool-schema dialect is an entry in model_dialects, not a branch in every client.

Both OpenAI-shaped routes used to compare the declared dialect against the Gemini constant, so a
third dialect meant editing two clients that must not know any model family. They now look the
sanitiser up by the declared name -- these tests register a dialect the clients have never heard of
and prove both of them apply it.

The two routes do NOT agree on a tool without parameters, and that difference is pinned here on
purpose: the chat route drops the field, the Responses route sends an empty object. Both have been
sent that way for as long as the routes exist; unifying them would be a payload change, not a
refactor.
"""
from __future__ import annotations

import json

import pytest

from plugins_llm.llm_common import model_dialects
from plugins_llm.llm_openai_compat.httpx_client import HTTPXOpenAIClient
from plugins_llm.llm_openai_compat.openai_responses_client import OpenAIResponsesClient

OPENROUTER = "https://openrouter.ai/api/v1"

TOOLS = [
    {"type": "function", "function": {
        "name": "read_catalogue", "description": "Read it.",
        "parameters": {"type": "object", "title": "Args", "additionalProperties": False,
                       "properties": {"section": {"type": "string", "default": "all"}}},
    }},
    {"type": "function", "function": {
        "name": "ping", "description": "Takes nothing.", "parameters": {},
    }},
]


def chat_tools(dialect: str | None) -> list:
    """What the Chat Completions route puts into the payload."""
    payload: dict = {}
    client = HTTPXOpenAIClient(model="some/model", api_key="test-key", base_url=OPENROUTER,
                              tool_schema_dialect=dialect)
    client._apply_tool_fields(payload, json.loads(json.dumps(TOOLS)), [])
    return payload["tools"]


def responses_tools(dialect: str | None) -> list:
    """What the Responses route sends for the same tools."""
    client = OpenAIResponsesClient(model="some/model", api_key="test-key", base_url=OPENROUTER,
                                   tool_schema_dialect=dialect)
    return client._convert_tools(json.loads(json.dumps(TOOLS)))


@pytest.fixture
def shouting_dialect(monkeypatch) -> str:
    """A dialect that exists only inside this test -- no client may know its name."""
    def shout(schema: dict) -> dict:
        return {"SHOUTED": sorted(schema)}

    monkeypatch.setitem(model_dialects.TOOL_SCHEMA_SANITIZERS, "shouting_declarations", shout)
    monkeypatch.setattr(model_dialects, "TOOL_SCHEMA_DIALECTS",
                        tuple(model_dialects.TOOL_SCHEMA_SANITIZERS))
    return "shouting_declarations"


def test_the_chat_route_applies_the_sanitiser_the_dialect_names(shouting_dialect):
    tools = chat_tools(shouting_dialect)

    assert tools[0]["function"]["parameters"] == {
        "SHOUTED": ["additionalProperties", "properties", "title", "type"]}


def test_the_responses_route_applies_the_same_sanitiser(shouting_dialect):
    tools = responses_tools(shouting_dialect)

    assert tools[0]["parameters"] == {
        "SHOUTED": ["additionalProperties", "properties", "title", "type"]}


@pytest.mark.parametrize("dialect", [None, "json_schema"])
def test_a_plain_endpoint_gets_the_schema_it_was_handed(dialect):
    """No declared dialect means no sanitiser at all -- not the default of some other endpoint."""
    assert chat_tools(dialect)[0]["function"]["parameters"] == TOOLS[0]["function"]["parameters"]
    assert responses_tools(dialect)[0]["parameters"] == TOOLS[0]["function"]["parameters"]


def test_the_two_routes_keep_their_own_answer_for_a_tool_without_parameters(shouting_dialect):
    """Measured divergence, left as it is: the chat route omits the field, the Responses route sends {}."""
    assert "parameters" not in chat_tools(shouting_dialect)[1]["function"]
    assert responses_tools(shouting_dialect)[1]["parameters"] == {}


@pytest.mark.parametrize("field, value", [
    ("tool_schema_dialect", "gemini"),
    ("assistant_reasoning_field", "reasoning"),
    ("prompt_cache_marker_style", "claude"),
])
def test_an_undeclared_value_dies_at_construction_and_names_the_model(field, value):
    """One owner for key names, values AND the error text -- a typo must not buy a silent default."""
    with pytest.raises(ValueError) as error:
        HTTPXOpenAIClient(model="book/launcher", api_key="test-key", base_url=OPENROUTER,
                          **{field: value})

    assert field in str(error.value)
    assert "book/launcher" in str(error.value), "the message must name the model entry that carries the typo"
