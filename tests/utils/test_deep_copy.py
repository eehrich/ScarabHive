"""utils/deep_copy.py: the copy a hook gets of the run's context is copy.deepcopy's, only faster -- the same values,
nothing shared with the original, what the original shares shared in the copy, the model's bookkeeping kept."""
from __future__ import annotations

import copy
from collections import OrderedDict

from pydantic import BaseModel, ConfigDict, PrivateAttr

from agent_system.llm.models import ChatMessage
from agent_system.utils.deep_copy import deep_copy


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""
    tags: list[str] = []
    _seen: dict = PrivateAttr(default_factory=dict)


class _OwnCopy(BaseModel):
    value: int = 0

    def __deepcopy__(self, memo=None):
        return _OwnCopy(value=-1)


def _history() -> list[ChatMessage]:
    call = {"id": "c1", "type": "function", "function": {"name": "file_ops_read_file", "arguments": '{"path": "a"}'}}
    return [ChatMessage(role="user", content="find the save"),
            ChatMessage(role="assistant", content="", tool_calls=[call], step=1),
            ChatMessage(role="tool", content='{"status": "success", "content": "x"}', tool_call_id="c1"),
            ChatMessage(role="user", content=[{"type": "text", "text": "and this"}])]


def _independent(original, copied) -> None:
    """No container or model of the copy is one of the original's."""
    def ids(value, out):
        if isinstance(value, (dict, list, BaseModel)):
            out.add(id(value))
            items = (value.__dict__.values() if isinstance(value, BaseModel)
                     else value.values() if isinstance(value, dict) else value)
            for item in items:
                ids(item, out)
        return out
    assert not ids(original, set()) & ids(copied, set()), "the copy shares a container with the original"


def test_a_history_is_copied_as_deepcopy_copies_it():
    history = _history()
    copied = deep_copy(history)
    assert copied == copy.deepcopy(history)
    assert [m.model_fields_set for m in copied] == [m.model_fields_set for m in history]
    assert [m.model_dump(exclude_unset=True) for m in copied] == [m.model_dump(exclude_unset=True) for m in history]
    _independent(history, copied)
    copied[1].tool_calls[0]["function"]["name"] = "changed"
    copied[3].content[0].text = "changed"
    assert history[1].tool_calls[0]["function"]["name"] == "file_ops_read_file" and history[3].content[0].text == "and this"


def test_what_the_original_shares_the_copy_shares_and_a_cycle_stays_one():
    part = {"k": [1, 2]}
    shared = {"a": part, "b": part, "list": [part, part]}
    copied = deep_copy(shared)
    assert copied["a"] is copied["b"] is copied["list"][0] is copied["list"][1] and copied["a"] is not part
    loop: dict = {"name": "x"}
    loop["self"] = loop
    looped = deep_copy(loop)
    assert looped["self"] is looped and looped is not loop
    # across a model and the dict around it, as deepcopy keeps it
    message = ChatMessage(role="user", content="x")
    held = {"one": message, "two": [message]}
    copied = deep_copy(held)
    assert copied["one"] is copied["two"][0] and copied["one"] is not message


def test_a_models_extra_fields_set_and_private_state_are_copied():
    model = _Loose(name="n", tags=["t"], loose={"deep": [1]})
    model._seen["k"] = ["v"]
    copied = deep_copy(model)
    reference = copy.deepcopy(model)
    assert copied == reference and copied.model_fields_set == reference.model_fields_set == {"name", "tags", "loose"}
    assert copied.__pydantic_extra__ == {"loose": {"deep": [1]}} and copied.__pydantic_extra__ is not model.__pydantic_extra__
    assert copied.loose["deep"] is not model.loose["deep"]
    assert copied._seen == {"k": ["v"]} and copied._seen is not model._seen and copied._seen["k"] is not model._seen["k"]


def test_what_it_does_not_know_goes_to_deepcopy():
    ordered = OrderedDict(a=[1])
    data = {"ordered": ordered, "tuple": ([1], "s"), "set": {1, 2}, "own": _OwnCopy(value=5), "raw": bytearray(b"ab")}
    copied = deep_copy(data)
    assert type(copied["ordered"]) is OrderedDict and copied["ordered"] == ordered and copied["ordered"]["a"] is not ordered["a"]
    assert copied["tuple"] == ([1], "s") and copied["tuple"][0] is not data["tuple"][0]
    assert copied["set"] == {1, 2} and copied["set"] is not data["set"]
    assert copied["own"].value == -1, "a model's own __deepcopy__ was not asked"
    assert copied["raw"] == data["raw"] and copied["raw"] is not data["raw"]


def test_atomic_values_are_the_originals_as_deepcopy_keeps_them():
    for value in ("text", 3, 2.5, True, None, b"x", 1j):
        assert deep_copy(value) is value
