"""The shared helpers that turn a failure into an answer: which failure becomes which answer is the whole contract."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from agent_system.api.app_context import errors_as_200
from agent_system.api.session_http import session_errors_as_http
from agent_system.llm.models import ChatMessage, task_text
from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError


async def test_errors_as_200_answers_the_failure_with_its_text():
    @errors_as_200("list tools")
    async def route():
        raise ValueError("no registry")

    assert await route() == {"error": "Failed to list tools: no registry"}


async def test_errors_as_200_unprefixed_answers_the_bare_text():
    @errors_as_200("list tools", prefixed=False)
    async def route():
        raise ValueError("no registry")

    assert await route() == {"error": "no registry"}


async def test_errors_as_200_leaves_an_answer_alone():
    @errors_as_200("list tools")
    async def route():
        return {"tools": []}

    assert await route() == {"tools": []}


@pytest.mark.parametrize("failure,code", [
    (SessionNotFoundError("gone"), 404),
    (SessionPermissionError("not yours"), 403),
    (HTTPException(status_code=418, detail="teapot"), 418),
])
def test_a_session_failure_answers_by_its_kind(failure, code):
    with pytest.raises(HTTPException) as answer:
        with session_errors_as_http("s1"):
            raise failure
    assert answer.value.status_code == code


def test_anything_else_passes_through_unless_it_names_what_failed():
    with pytest.raises(ValueError):
        with session_errors_as_http("s1"):
            raise ValueError("corrupt")

    with pytest.raises(HTTPException) as answer:
        with session_errors_as_http("s1", failed_to="load"):
            raise ValueError("corrupt")
    assert (answer.value.status_code, answer.value.detail) == (500, "corrupt")


def test_the_not_found_and_forbidden_texts_are_the_callers():
    with pytest.raises(HTTPException) as answer:
        with session_errors_as_http("s1", not_found="No such chat"):
            raise SessionNotFoundError("gone")
    assert answer.value.detail == "No such chat"

    with pytest.raises(HTTPException) as answer:
        with session_errors_as_http("s1", forbidden="Not yours"):
            raise SessionPermissionError("gone")
    assert answer.value.detail == "Not yours"


def test_task_text_joins_the_text_parts_and_names_input_without_any():
    class Part:
        def __init__(self, kind, text=""):
            self.type, self.text = kind, text

    with_text = ChatMessage.model_construct(role="user", content=[Part("text", "a"), Part("image"), Part("text", "b")])
    without = ChatMessage.model_construct(role="user", content=[Part("image")])

    assert task_text(with_text) == "a b"
    assert task_text(without) == "[multimodal input]"
    assert task_text("plain") == "plain"
