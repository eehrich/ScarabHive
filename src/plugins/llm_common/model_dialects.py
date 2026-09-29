"""Per-model dialect keys, resolved once for every client that speaks them.

A client must not know model names. What an endpoint accepts is declared per
model in ``LLMModelConfig`` (``tool_schema_dialect``, ``reasoning_details_mode``,
``assistant_reasoning_field``, ``thinking_request_shape``,
``prompt_cache_marker_style``) and handed to the client as a keyword argument;
these helpers turn the declared string into behaviour and reject an unknown one
at CONSTRUCTION — a typo must kill the factory call, not produce a request that
silently does the wrong thing.

This module owns the key names, the allowed values and the error text, so a new
value is added in ONE place and every route sees the same word list.

Kept deliberately small: the routes share the vocabulary and the validation,
not a base class.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from agent_system.llm.cache_key import (
    MARKER_STYLE_ANTHROPIC,
    MARKER_STYLE_NONE,
    MARKER_STYLE_OPENAI,
)

from plugins.llm_common.schema_sanitize import sanitize_schema_for_gemini

#: ``tool_schema_dialect``: what the endpoint accepts as a tool's parameter
#: schema, and how a schema reaches that shape. ``None`` means "send it as it
#: is"; every other dialect names the sanitiser its endpoint needs. A route
#: looks the entry up instead of comparing against a dialect constant, so a
#: new dialect is one line HERE and no branch in any client.
DIALECT_JSON_SCHEMA = "json_schema"
DIALECT_GEMINI_FUNCTION_DECLARATIONS = "gemini_function_declarations"
TOOL_SCHEMA_SANITIZERS: dict[str, Optional[Callable[[dict], dict]]] = {
    DIALECT_JSON_SCHEMA: None,
    DIALECT_GEMINI_FUNCTION_DECLARATIONS: sanitize_schema_for_gemini,
}
TOOL_SCHEMA_DIALECTS = tuple(TOOL_SCHEMA_SANITIZERS)

#: ``reasoning_details_mode``: how many of the stored reasoning blocks travel
#: back to the model on the next turn.
REASONING_DETAILS_MODES = ("keep_last", "keep_all", "strip")

#: ``thinking_request_shape``: how the native Anthropic API wants thinking
#: asked for. The wrong shape is an HTTP 400, not a degraded answer.
THINKING_REQUEST_SHAPES = ("budget", "adaptive")

#: ``assistant_reasoning_field``: the non-standard field an assistant message
#: carries its thinking in, next to ``reasoning_details``.
ASSISTANT_REASONING_FIELDS = ("omit", "reasoning_content")

#: ``prompt_cache_marker_style``: which cache marker the endpoint reads.
MARKER_STYLES = (MARKER_STYLE_OPENAI, MARKER_STYLE_ANTHROPIC, MARKER_STYLE_NONE)


def declared_choice(value: Optional[str], *, default: Optional[str],
                    allowed: Sequence[str], field: str, model: str) -> Optional[str]:
    """One declared config value, validated once at construction.

    Unset falls back to *default*; an unknown value raises. A typo must not
    silently buy the default behaviour of a different provider dialect.
    """
    if value is None:
        return default
    if value not in allowed:
        raise ValueError(
            f"{field}={value!r} is not a declared value for model {model!r} — "
            f"expected one of {list(allowed)}")
    return value


def tool_schema_sanitizer(dialect: str) -> Optional[Callable[[dict], dict]]:
    """The sanitiser a declared dialect needs for a tool's parameter schema.

    ``None`` for a dialect that takes JSON Schema as it is. The dialect was
    validated at construction, so an unknown one here is a bug, not config.
    """
    return TOOL_SCHEMA_SANITIZERS[dialect]


def resolve_tool_schema_dialect(value: Optional[str], *, model: str,
                                default: str = DIALECT_JSON_SCHEMA) -> str:
    """Declared tool-schema dialect, or the plain-endpoint default."""
    return declared_choice(value, default=default, allowed=TOOL_SCHEMA_DIALECTS,
                           field="tool_schema_dialect", model=model)


def resolve_reasoning_details_mode(value: Optional[str], *, model: str,
                                   default: str) -> str:
    """Declared reasoning round-trip mode, or the route's own default.

    ``default`` has no house-wide value on purpose: on the chat route an
    assistant turn carries a thought signature that is only valid for the
    current turn (``keep_last``), while the routes that replay verbatim item
    chains (Responses, native Anthropic) would break the chain by dropping
    older turns (``keep_all``). Each client passes the mode it has always used,
    so a model entry without the key keeps behaving as it did.
    """
    return declared_choice(value, default=default, allowed=REASONING_DETAILS_MODES,
                           field="reasoning_details_mode", model=model)


def resolve_thinking_request_shape(value: Optional[str], *, model: str,
                                   default: str = "budget") -> str:
    """Declared thinking request shape, or the long-standing budget form."""
    return declared_choice(value, default=default, allowed=THINKING_REQUEST_SHAPES,
                           field="thinking_request_shape", model=model)


#: Every key that declares how an endpoint behaves. A factory names what it WIRES;
#: whatever is left over is warned about. Stated that way round, a key added here and
#: forgotten in a factory is loud, not silent.
DIALECT_KEYS = ("tool_schema_dialect", "assistant_reasoning_field",
                "reasoning_details_mode", "thinking_request_shape", "stream_silence_timeout",
                "provider_affinity_minutes")


def warn_unwired(config: Any, *, provider: str, wired: Sequence[str], logger: Any) -> None:
    """A declared key this route cannot honour says so, instead of being accepted and doing nothing.

    The same rule the clients follow for a typo, one layer up: an operator who
    reads the key's documentation and sets it must learn from the log that this
    provider is the wrong home for it -- silence would look like it worked.
    """
    for field in DIALECT_KEYS:
        if field in wired:
            continue
        if getattr(config, field, None) is not None:
            logger.warning(
                "%s is not wired for provider=%s and will be ignored (model=%s).",
                field, provider, getattr(config, "model", "?"))


def _role(message: Any) -> Any:
    return message.get("role") if isinstance(message, dict) else getattr(message, "role", None)


def reasoning_replay_flags(messages: Sequence[Any], mode: str) -> list[bool]:
    """Per message: may its stored reasoning travel back to the model?

    True only for assistant messages, and only as far as *mode* allows:
    ``keep_all`` every one of them, ``keep_last`` the most recent one,
    ``strip`` none. Positional, so the caller keeps its own message objects —
    the three routes store reasoning under three different carriers
    (``reasoning_details``, verbatim Responses items, Anthropic thinking
    blocks) and only the policy is shared.
    """
    flags = [_role(m) == "assistant" for m in messages]
    if mode == "keep_all":
        return flags
    if mode == "strip":
        return [False] * len(flags)
    # keep_last
    last = max((i for i, is_assistant in enumerate(flags) if is_assistant),
               default=-1)
    return [i == last for i in range(len(flags))]
