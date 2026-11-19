"""api/json_answer.py and GET /api/sessions/{id}: the bytes are FastAPI's own -- encoded directly where the data is
JSON already, through jsonable_encoder where it is not -- and the route is get_session's, parameters and name."""
from __future__ import annotations

import inspect
import json
import math
from datetime import datetime, timezone
from enum import Enum, IntEnum
from types import SimpleNamespace

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_system.api import json_answer as json_answer_module
from agent_system.api import session_endpoints
from agent_system.api.json_answer import json_answer
from agent_system.services.session_manager import SessionManager


class _Colour(str, Enum):
    RED = "red"


class _Level(IntEnum):
    HIGH = 3


class _Point(BaseModel):
    x: int = 1


def _fastapi_bytes(content) -> bytes:
    """What FastAPI sends for an endpoint returning ``content``: jsonable_encoder, then JSONResponse."""
    return JSONResponse(jsonable_encoder(content)).body


JSON_ALREADY = [
    {"session_id": "s1", "messages": [{"role": "user", "content": "Grüße – 你好   \"quoted\" \\ end"},
                                      {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "args": {}}]}],
     "metadata": {"n": 3, "f": 0.1, "big": 10 ** 20, "ok": True, "none": None, "empty": [], "nested": {"a": [1, [2]]}}},
    [1, "two", 3.5, None, False],
    {1: "int key", False: "bool key", None: "none key", 2.5: "float key"},
    {"tuple": (1, 2), "colour": _Colour.RED, "level": _Level.HIGH},
    "just text",
    0,
]


@pytest.mark.parametrize("content", JSON_ALREADY)
def test_json_already_is_encoded_directly_into_fastapis_bytes(content, monkeypatch):
    expected = _fastapi_bytes(content)
    monkeypatch.setattr(json_answer_module, "jsonable_encoder",
                        lambda *a, **k: pytest.fail("JSON already went through jsonable_encoder"))
    answer = json_answer(content)
    assert answer.body == expected
    assert answer.media_type == "application/json" and answer.status_code == 200
    assert answer.headers["content-type"] == JSONResponse(content).headers["content-type"]


@pytest.mark.parametrize("content", [
    {"when": datetime(2026, 11, 19, 4, 30, tzinfo=timezone.utc)},
    {"model": _Point(x=5)},
    {"raw": b"bytes"},
    {"set": {1}},
])
def test_what_json_dumps_does_not_take_goes_the_way_it_went(content):
    assert json_answer(content).body == _fastapi_bytes(content)


def test_a_key_starting_with_sa_stays_which_jsonable_encoder_dropped():
    content = {"_saved": 1, "nested": [{"_sample": 2}], "ok": 3}
    assert json.loads(_fastapi_bytes(content)) == {"nested": [{}], "ok": 3}
    assert json.loads(json_answer(content).body) == content


def test_a_nan_fails_as_it_failed():
    with pytest.raises(ValueError):
        _fastapi_bytes({"x": math.nan})
    with pytest.raises(ValueError):
        json_answer({"x": math.nan})


def _route():
    return next(r for r in session_endpoints.session_router.routes
                if getattr(r, "path", "") == "/api/sessions/{session_id}" and "GET" in r.methods)


def test_the_route_is_get_sessions_parameters_name_and_description():
    route = _route()
    assert route.name == "get_session"
    assert inspect.signature(route.endpoint) == inspect.signature(session_endpoints.get_session)
    assert [p.name for p in route.dependant.query_params] == ["descendants"]
    assert route.description == inspect.cleandoc(session_endpoints.get_session.__doc__)


async def test_the_route_sends_get_sessions_record(tmp_path):
    manager = SessionManager(storage_path=str(tmp_path))
    await manager.create_session(user_id="ada", title="Grüße – 你好", agent_name="chat_agent", session_id="s1")
    arguments = dict(current_user=SimpleNamespace(username="ada", role="user"), session_manager=manager,
                     default_agent=None, tool_registry=None)
    record = await session_endpoints.get_session("s1", **arguments)
    answer = await _route().endpoint("s1", **arguments)
    assert answer.body == _fastapi_bytes(record) and json.loads(answer.body)["title"] == "Grüße – 你好"
