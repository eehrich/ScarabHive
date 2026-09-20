"""The config model and the dialect module must offer the same words.

Both sides list the allowed values: ``LLMModelConfig`` as a ``Literal`` (a typo fails config load)
and ``model_dialects`` as the tuple every client validates against (a typo fails the factory call).
Nothing kept the two in step. A value added only to the Literal is accepted by config and then
refused at construction; a value added only to the tuple is refused before any client sees it --
in both cases the operator reads a documented key and gets an error about something else.
"""
from __future__ import annotations

from typing import get_args, get_type_hints

import pytest

from agent_system.config.models import LLMModelConfig
from plugins.llm_common import model_dialects

MIRRORED = {
    "tool_schema_dialect": model_dialects.TOOL_SCHEMA_DIALECTS,
    "assistant_reasoning_field": model_dialects.ASSISTANT_REASONING_FIELDS,
    "reasoning_details_mode": model_dialects.REASONING_DETAILS_MODES,
    "thinking_request_shape": model_dialects.THINKING_REQUEST_SHAPES,
}


def literal_values(field: str) -> tuple:
    """The words the config model accepts for ``field`` -- Optional[Literal[...]] unwrapped."""
    annotation = get_type_hints(LLMModelConfig)[field]
    for argument in get_args(annotation):
        values = get_args(argument)
        if values:
            return values
    raise AssertionError(f"{field} is no longer an Optional[Literal[...]]: {annotation}")


@pytest.mark.parametrize("field", sorted(MIRRORED))
def test_config_and_clients_allow_exactly_the_same_values(field):
    assert set(literal_values(field)) == set(MIRRORED[field])
