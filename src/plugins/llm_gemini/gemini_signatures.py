"""Gemini thought signatures on the native routes (REST, SDK, batch): where they are kept and how they go back.

Google's rules (thought-signatures guide):

- In one step, only the FIRST functionCall part carries the signature; the
  later parallel calls carry none. It goes back in the part it came in.
- The signature of a text part (the answer, or an empty last part when
  streaming) goes back on that text part. Leaving it out costs the model its
  previous reasoning, not a 400.
- Validation is enforced for the current turn: the steps since the last user
  message. A step there without a signature (another model made it) takes the
  dummy ``skip_thought_signature_validator`` on its first call. Older steps
  need none.
- A signature is bound to the model that made it: after a model switch the
  stored ones stay home (``reasoning_model``, as on the other routes).

A function call keeps its signature in ``extra_content.google.thought_signature``
(base64), as on the OpenAI-compatible endpoint. A text signature has no field of
its own on the message; it is kept as a ``reasoning.encrypted`` block in
``reasoning_details`` -- the block OpenRouter uses for the same payload -- so
the resets, the invalidation and the per-request filter for another model's
reasoning (agent_system.utils.reasoning_artifacts) treat it like any other.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any, List, Optional

from agent_system.utils.reasoning_artifacts import produced_by_another_model

#: Google's documented dummy for a step that has no signature of its own.
BYPASS = "skip_thought_signature_validator"

#: The block a text signature is kept in.
TEXT_SIGNATURE_FORMAT = "google-gemini-v1"


def _get(msg: Any, key: str) -> Any:
    return msg.get(key) if isinstance(msg, dict) else getattr(msg, key, None)


def signature_b64(part: Any) -> Optional[str]:
    """The signature of an SDK part (bytes) or a REST part (base64 string), as base64."""
    if isinstance(part, dict):
        value = part.get("thoughtSignature")
        return value if isinstance(value, str) and value else None
    value = getattr(part, "thought_signature", None)
    if isinstance(value, (bytes, bytearray)) and value:
        return base64.b64encode(value).decode("ascii")
    return value if isinstance(value, str) and value else None


def signature_bytes(value: str) -> bytes:
    """A stored signature as the SDK takes it.

    Strict base64 only: the lenient decoder drops what is not base64 and made
    garbage of the dummy, which is no base64 (``_``) and goes out as its text.
    """
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return value.encode("utf-8")


def call_signature(tool_call: Any) -> Optional[str]:
    """The signature stored on one OpenAI-style tool call."""
    if not isinstance(tool_call, dict):
        return None
    google = (tool_call.get("extra_content") or {}).get("google") or {}
    value = google.get("thought_signature") or tool_call.get("thought_signature")
    if isinstance(value, (bytes, bytearray)):
        value = base64.b64encode(value).decode("ascii")
    return value if isinstance(value, str) and value else None


def signed_call(tool_call: dict, signature: Optional[str]) -> dict:
    """*tool_call* carrying *signature* (only the part that came with one)."""
    if signature:
        tool_call["extra_content"] = {"google": {"thought_signature": signature}}
    return tool_call


def text_signature_details(signature: Optional[str]) -> Optional[List[dict]]:
    """The ``reasoning_details`` that keep a text part's signature, or None."""
    if not signature:
        return None
    return [{"type": "reasoning.encrypted", "format": TEXT_SIGNATURE_FORMAT, "data": signature}]


def text_signature(msg: Any, model: Optional[str] = None) -> Optional[str]:
    """The text-part signature kept on an assistant message, unless *model* did not make it.

    OpenRouter writes its tool-call signatures in the same block shape, with
    the call's ``id``; those are no text signature.
    """
    if produced_by_another_model(msg, model):
        return None
    for block in _get(msg, "reasoning_details") or []:
        if (isinstance(block, dict) and block.get("format") == TEXT_SIGNATURE_FORMAT
                and block.get("type") == "reasoning.encrypted" and block.get("data")
                and not block.get("id")):
            return block["data"]
    return None


def current_turn_start(messages: List[Any]) -> int:
    """Index of the first message of the current turn: after the last user message.

    A developer note rides a user turn on this route, so it starts one too.
    """
    for i in range(len(messages) - 1, -1, -1):
        if _get(messages[i], "role") in ("user", "developer"):
            return i + 1
    return 0


def replay_signatures(tool_calls: List[Any], in_current_turn: bool,
                      foreign: bool = False) -> List[Optional[str]]:
    """The signature each functionCall part of one step goes back with.

    The stored ones where they came; a call that repeats the call before it
    carries that signature only because older sessions copied the first one
    onto every parallel call, and goes back without it. A *foreign* step
    (another model made it) keeps none of its own. A current-turn step with
    none at all takes the dummy on its first call.
    """
    stored = [None if foreign else call_signature(tc) for tc in tool_calls]
    out: List[Optional[str]] = []
    for i, sig in enumerate(stored):
        out.append(None if i and sig and sig == stored[i - 1] else sig)
    if in_current_turn and out and not any(out):
        out[0] = BYPASS
    return out
