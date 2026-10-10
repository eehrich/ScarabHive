"""Layered Compaction Strategy - Progressive context compression.

This module implements a multi-stage compaction strategy that applies
increasingly aggressive compression techniques based on token budget.

Layers (in order of application):
1. **Reversible Compaction** - Operations that can be fully undone:
   - Store tool outputs with references
   - Replace large audio/image inline data with references
   - Deduplicate media by hash (keep newest, compact older duplicates)

2. **Semi-Reversible Compaction** - Operations partially recoverable:
   - Archive old messages with summaries
   - Truncate very old tool results
   
3. **Irreversible Compaction** - Last resort operations:
   - Drop old messages entirely
   - Compress remaining summaries

The strategy is token-budget aware and tries to reach target token count
with minimum information loss.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import time
import weakref
from collections import defaultdict
from dataclasses import dataclass, field, fields
from fnmatch import fnmatchcase
from typing import Any, Awaitable, Callable, Iterator, NamedTuple

from agent_system.llm.message_roles import is_input, opens_a_turn
from agent_system.paths import resolve_data_path
from agent_system.utils.multimodal_tool_content import extract_inline_media
from agent_system.utils.reasoning_artifacts import invalidate_reasoning_artifacts
from agent_system.llm.token_utils import (
    estimate_content_tokens,
    estimate_media_tokens,
    estimate_token_count,
    inline_payload,
)

from .archival_memory import ArchivalMemory
from .core_memory import CoreMemory
from .media_store import MediaStore
from .tool_result_store import ToolResultStore

logger = logging.getLogger(__name__)


@dataclass
class CompactionConfig:
    """Configuration for layered compaction."""
    
    # Token thresholds for each layer
    layer1_threshold: int = 80000  # Start reversible compaction
    layer2_threshold: int = 100000  # Start semi-reversible compaction
    layer3_threshold: int = 120000  # Start irreversible compaction
    
    # Target tokens after compaction
    target_tokens: int = 60000
    
    # Byte size limit (Gemini has 100MB limit, use 90MB as safe threshold)
    # If request_bytes exceeds this, force compaction regardless of token count
    max_request_bytes: int = 90 * 1024 * 1024  # 90 MB
    target_request_bytes: int = 70 * 1024 * 1024  # 70 MB target after compaction
    
    # Tool result settings
    tool_result_min_size: int = 500  # Min tokens to store externally
    tool_result_keep_last: int = 3   # Keep last N tool results inline (unless too large)
    tool_result_max_inline_size: int = 5000  # Max tokens before auto-archive (even if in last N)
    # A NEW tool result larger than this share of the model's context window is
    # stored on arrival, below every threshold (Pre-Layer T). 0 disables it.
    tool_result_max_window_share: float = 0.25
    # A NEW tool result of this many tokens is stored on arrival AND a cheap
    # model writes what it says into the placeholder -- for a hand-over to an
    # expensive agent, which pays for every token of a long answer it was handed.
    # The full text stays in the store and is read back with the read tool.
    # 0 disables it -- that count is the switch. The profile is a NAME from
    # config/llm.yaml, never a model: which model condenses text is the
    # operator's to configure and to change when a cheaper one appears.
    tool_result_summary_from: int = 0
    tool_result_summary_profile: str = "summarizer"
    # Which tools' results a summary may replace, as fnmatch patterns over the
    # tool name ("*_manage_sub_agent"). Empty = every tool, which is what a
    # hand-over agent wants; an agent that also READS files with the same hook
    # names the hand-over tools here, so its file reads arrive whole.
    tool_result_summary_tools: list[str] = field(default_factory=list)

    # Message archival settings
    archive_after_turns: int = 10    # Archive messages older than N turns
    keep_system_messages: bool = True  # Never archive system messages
    
    # Irreversible settings
    drop_after_turns: int = 50       # Drop messages older than N turns
    max_summary_tokens: int = 100    # Max tokens for archived summaries
    
    # Message limit - drops oldest messages if exceeded (Pre-Layer P)
    # Counts ALL messages including tool calls/results, not just user messages
    # Set to 0 to disable
    max_messages: int = 0            # 0 = disabled, e.g., 200 = keep max 200 messages

    # The hysteresis of Pre-Layer P: past max_messages, a prune keeps this
    # many (200 / 100 = over 200, drop to 100). Every prune rewrites the front
    # of the conversation and breaks the prompt cache behind it, and the next
    # one comes about max_messages - prune_to new messages later
    # (min_tokens_between_compactions can hold it longer) — so the deeper the
    # cut, the rarer the break. Equal to max_messages prunes whenever the list
    # is over the limit. 0, or a value above max_messages (a per-agent
    # max_messages below the inherited value), prunes to half the limit.
    max_messages_prune_to: int = 0

    # Hysteresis for the layers that rewrite messages (P, 1, 2, 3): once one
    # ran, the next run waits until the context has grown by this many tokens.
    # Every run breaks the provider prompt cache from the first changed message
    # on. The watermarks (a layer threshold down to target_tokens) are the
    # hysteresis whenever a compaction gets below the threshold; this covers the
    # case where it cannot, which otherwise compacts on every call. Tokens, not
    # time or turns: the same agent is three times slower on another provider,
    # and a turn is a user message nobody injected — a whole agent run is one.
    min_tokens_between_compactions: int = 20000

    # Media deduplication settings
    deduplicate_media: bool = True  # Auto-compact older duplicate media (by file hash)
    
    # Media removal settings (compact media after certain events)
    compact_media_after_user_message: bool = False  # Compact all media when a new user message arrives
    compact_media_after_final_response: bool = False  # Compact all media when agent sends final response
    
    # Always compact media setting - runs regardless of token count
    # Removes ALL media items except those in the last N messages that contain media
    # Set to 0 to disable, >0 to enable and keep last N media-containing messages
    always_compact_media_keep_last: int = 0  # 0 = disabled, 5 = keep last 5 messages with media

    # How far BELOW keep_last that pass evicts — a distance, unlike Pre-Layer
    # P's absolute max_messages_prune_to. Nothing goes while at most keep_last
    # messages carry media;
    # past that only the newest max(1, keep_last - headroom) keep theirs.
    # Without it each new media message evicts the oldest kept one on the next
    # call: an old message rewritten and the prompt cache broken from there,
    # every call. With it `headroom` new media messages arrive without a break
    # after each one (while keep_last - headroom >= 1), and keep_last
    # stays the most the context carries -- except for media the model has not
    # seen yet, which always stays for one call. 0 = evict down to keep_last.
    always_compact_media_headroom: int = 0

    # Media store settings (for storing inline base64 before compaction)
    store_media_before_compaction: bool = True  # Save inline media to disk before removing
    media_store_ttl_seconds: int = 86400 * 7  # 7 days TTL for stored media
    media_store_max_files: int = 500  # Max files per session


#: Config keys the plugin consumes that are NOT CompactionConfig fields.
#: Anything a config file sets must be one or the other — see
#: ``unknown_config_keys``, which is what turns a typo into a log line instead
#: of silence.
PLUGIN_LEVEL_KEYS = frozenset({
    "session_ttl_seconds",
    "max_tracked_sessions",
    "enable_semantic_search",
    "core_memory_max_tokens",
    "storage_path",
    "session_data_ttl_days",
})


def _coerce(value: Any, type_name: str, field_name: str) -> Any:
    """YAML already yields ints and bools; this only catches the odd string.

    A value that cannot be coerced is passed through UNCHANGED rather than
    dropped: a wrong type is the operator's to see, and silently substituting
    a default here would be the same disappearing act this module exists to
    stop.

    The exception is ``list[str]``, which is ITERATED at its use site: passing
    a number through raises there and costs the whole compaction. A value of
    that type is therefore dropped instead -- with a warning, and to a pattern
    that matches nothing rather than to the empty list, which the use site
    reads as "everything". An absent value (``None``) is not that case: it is
    the key left blank, and it yields the empty list the default already is.
    """
    try:
        if type_name == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if type_name == "int":
            return int(value)
        if type_name == "float":
            return float(value)
        if type_name == "list[str]":
            # A single pattern written as a plain string is the likely slip,
            # and iterating a str would match its CHARACTERS.
            if isinstance(value, str):
                return [p.strip() for p in value.split(",") if p.strip()]
            if isinstance(value, (list, tuple)):
                return [str(v) for v in value]
            if value is None:
                return []
            # Everything else is dropped rather than passed through, against
            # this function's own rule: a list is ITERATED at the use site, so
            # a number there raises inside the hook and the whole compaction is
            # lost -- silently, until the context outgrows the provider. A
            # mapping would be worse: it iterates its keys and filters by them
            # without a word.
            #
            # It is dropped to a pattern that matches NOTHING, not to the empty
            # list: empty means "every tool" at the use site, so an unreadable
            # filter would turn into the most permissive one there is. The
            # operator who writes this key writes it to keep results away from
            # another provider; a typo must not hand them over.
            logger.warning(
                "[ContextEngineer] config '%s' = %r is not a list of strings; "
                "nothing is summarized until it is one", field_name, value)
            return [MATCHES_NO_TOOL]
    except (TypeError, ValueError):
        logger.warning(
            "[ContextEngineer] config '%s' = %r is not a %s; using it as-is",
            field_name, value, type_name)
    return value


def compaction_config_from(values: dict[str, Any]) -> CompactionConfig:
    """Build the config by FIELD NAME. The dataclass IS the schema.

    Every setting used to be hand-written in three separate lists (read it in
    server.py, copy it onto the hook, name it again when constructing this
    object) and a key missing from any of them was dropped without a word.
    Measured on the shipped config: 5 of 25 settings never arrived — among
    them a request-size guard an operator had deliberately lowered to 29 MB
    to stay under a provider cap, running at 90 MB.
    """
    typed = {f.name: f.type for f in fields(CompactionConfig)}
    return CompactionConfig(**{
        name: _coerce(values[name], typed[name], name)
        for name in typed if name in values
    })


def unknown_config_keys(values: Any) -> list[str]:
    """Keys that reach neither the compaction config nor the plugin itself.

    This is the half a generic mapping cannot do on its own: mapping by field
    name makes a correctly-named key arrive, but a MISSPELLED one still lands
    nowhere. Shipped example: `semantic_search`, where the code reads
    `enable_semantic_search` — set to true for months, off the whole time.
    """
    known = {f.name for f in fields(CompactionConfig)} | PLUGIN_LEVEL_KEYS
    return sorted(set(values) - known)


@dataclass
class CompactionResult:
    """Result of a compaction operation."""
    
    original_tokens: int
    final_tokens: int
    tokens_saved: int
    
    # What was done
    tool_results_stored: int = 0
    messages_archived: int = 0
    messages_dropped: int = 0
    media_deduplicated: int = 0  # Duplicate media compacted
    media_compacted_after_event: int = 0  # Media compacted due to user/final message trigger
    media_always_compacted: int = 0  # Media compacted by always_compact_media_keep_last
    media_bytes_saved: int = 0  # Bytes saved by media compaction (for byte-limit compaction)
    messages_pruned: int = 0  # Messages pruned by max_messages limit (Pre-Layer P)
    
    # Layer applied (int for L1/L2/L3, str "M" for media, "B" for byte-limit, "P" for prune)
    layers_applied: list[int | str] = field(default_factory=list)
    
    # Modified messages
    modified_messages: list[dict[str, Any]] = field(default_factory=list)

    # Message estimate taken right before the first layer that rewrites
    # messages (see _take_baseline), and the media savings booked until then.
    estimated_before: int | None = None
    saved_before_baseline: int = 0

    # What the list looked like when compact() started (see _message_shape):
    # _finalize compares against it to find the first message a pass changed.
    shapes_before: list[tuple] | None = field(default=None, repr=False)

    @property
    def reduction_percent(self) -> float:
        """Calculate reduction percentage."""
        if self.original_tokens == 0:
            return 0.0
        return ((self.original_tokens - self.final_tokens) / self.original_tokens) * 100


#: Marker of the JSON placeholder that replaces an archived message in place.
ARCHIVED_REF_TYPE = "archived_ref"

#: Key the retrieval tools put in their own answers so compaction can recognise
#: them. Deliberately NOT a tool-name match: the tool names are built from the
#: server name in plugins.yaml, so renaming the server there would silently
#: switch the exemption off and bring the retrieval loop back. The answer
#: identifying itself survives any renaming.
RETRIEVAL_MARKER = "retrieval_result"


#: Where a wrapped answer keeps its prose: a sub-agent result
#: ({"instance_id","status","result"}), an untrusted wrapper ({"untrusted":
#: true, "content": …}, which the tools put INSIDE "result" or "data"), a file
#: read, a plain answer.
_PROSE_FIELDS = ("result", "content", "text", "output", "answer", "data")
#: How deep a wrapper may be nested before its prose stops counting as prose.
_PROSE_DEPTH = 3
#: What an unreadable filter falls back to: the empty pattern, which matches no
#: NAMED tool, while a nameless result is turned away by the use site's own
#: check. The empty LIST cannot serve here -- it means "every tool" and would
#: turn a typo into the most permissive filter there is.
MATCHES_NO_TOOL = ""
#: What the summarizing model is shown, what may come back, how short that has
#: to be to be a summary at all, and how much of the wrapper the prose must be
#: (below that the other fields carry their own facts -- a build result's
#: status and exit code next to its log -- and a summary of the prose alone
#: would drop them).
SUMMARY_INPUT_CHARS = 60_000
SUMMARY_CHARS = 1_200
SUMMARY_MAX_SHARE = 0.5
SUMMARY_WRAPPER_SHARE = 0.8
#: All the summaries of one round together, well short of the hook's budget,
#: and the least that is worth starting a call with.
SUMMARY_ROUND_S = 20.0
SUMMARY_MIN_CALL_S = 1.0


def _prose_of(content: str) -> str | None:
    """The text worth summarizing in a tool result, or None when there is none.

    A structured result is read back whole; a summary of it would be a second,
    lossy shape of the same data. A wrapper around prose -- what a sub-agent
    hands its caller -- is worth exactly its prose.
    """
    stripped = content.strip()
    if not stripped.startswith(("{", "[")):
        return content
    try:
        data = json.loads(stripped)
    except ValueError:
        return content            # looked like JSON, is not: prose after all
    prose = _wrapped_prose(data, _PROSE_DEPTH)
    if prose is None or len(prose) < len(content) * SUMMARY_WRAPPER_SHARE:
        # Not a wrapper around prose but a result that HAS prose in it: a build
        # result's log sits next to its status, error and exit code, and a
        # summary written from the log alone would drop them.
        return None
    return prose


def _wrapped_prose(data: Any, depth: int) -> str | None:
    """The prose a wrapper holds. The tools nest them: a sub-agent's answer
    arrives as {"result": {"untrusted": true, "content": "…"}}."""
    if not isinstance(data, dict) or depth <= 0:
        return None
    for key in _PROSE_FIELDS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
        inner = _wrapped_prose(value, depth - 1)
        if inner is not None:
            return inner
    return None


def _is_retrieval_result(message: dict[str, Any]) -> bool:
    """Whether a TOOL message is an answer from the context-retrieval tools.

    Their output is content the agent just pulled OUT of storage; putting it
    straight back is a loop with no exit (see the call site).

    The role check is load-bearing, not decoration: this predicate now also
    decides what is exempt from archiving, and a user who pastes a retrieval
    answer back into the chat to ask about it writes a REAL message that merely
    looks like one. Without the check that message would be deleted and never
    stored.
    """
    if message.get("role") != "tool":
        return False
    content = message.get("content")
    if not isinstance(content, str) or RETRIEVAL_MARKER not in content:
        return False
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get(RETRIEVAL_MARKER) is True


#: Marker of the placeholder Layer 1 leaves where a tool result was externalised.
TOOL_RESULT_REF_TYPE = "tool_result_ref"

#: Heading of the restoration-block section that explains those placeholders.
#: The placeholder itself names no way back, so the hook shows the section as
#: soon as the first one exists (hooks.py, restoration block).
TOOL_RESULTS_SECTION = "## Tool Results"

#: Marker of the single breadcrumb Pre-Layer P leaves after removing messages.
#: Without it a bulk removal is invisible to the agent, which is the "no
#: silent drift" invariant applied to the context: a self-healing step nobody
#: can see is indistinguishable from one that never happened.
PRUNE_NOTICE_TYPE = "pruned_notice"

#: Placeholder types whose body already lives in a store. Dropping one costs
#: its address only — the content stays reachable through the retrieval tools.
_PLACEHOLDER_TYPES = (ARCHIVED_REF_TYPE, TOOL_RESULT_REF_TYPE, PRUNE_NOTICE_TYPE)

#: The role each placeholder is WRITTEN on. The agent can see these JSON blobs
#: in its own context and reproduce one in an answer; on shape alone such an
#: echo counts as a placeholder, gets ranked as free to drop AND excluded from
#: archiving — deleted with no copy anywhere. The role is what separates the
#: real thing from a quotation of it.
#:
#: ``archived_ref`` is absent on purpose: Layer 2 preserves the ORIGINAL role of
#: whatever it archived, so every role is legitimate there and nothing can tell
#: an echo apart. That residue is accepted — an echoed archive pointer is an
#: address whose target is still in the archive, so the loss is the quotation.
_PLACEHOLDER_ROLES = {
    TOOL_RESULT_REF_TYPE: ("tool",),
    PRUNE_NOTICE_TYPE: ("system",),
}

#: Media item types that can appear in a message's ``content`` list.
_MEDIA_TYPES = ("image", "image_url", "audio", "video")

#: What a hint CALLS each of them. Not ``type.title()``: that spells
#: ``image_url`` as "Image_Url", a word the model has never seen, and it raises
#: outright on an item whose ``type`` is not a string — which
#: ``multimodal_content`` does not promise.
_MEDIA_SUBJECTS = {"image": "Image", "image_url": "Image",
                   "audio": "Audio", "video": "Video"}

#: The session every caller lands in that brought no session id of its own.
#: Named rather than spelled out four times, because it is the one value whose
#: appearance means "someone forgot" and not "someone chose".
_SHARED_SESSION_ID = "default"


def _message_shape(msg: dict[str, Any]) -> tuple:
    """The objects a pass replaces when it rewrites a message.

    References, compared by identity: the passes swap a message dict, reassign
    its content, or replace/delete items of its content and media lists — all
    visible here without serializing anything, which matters because compact()
    runs on every LLM call. Holding the references also keeps a freed object's
    id from being reused by its replacement.
    """
    if not isinstance(msg, dict):
        return (msg, None, None, None, None)
    content = msg.get("content")
    media = msg.get("multimodal_content")
    return (msg, content, tuple(content) if isinstance(content, list) else None,
            media, tuple(media) if isinstance(media, list) else None)


def _first_changed_index(before: list[tuple], messages: list[dict[str, Any]]) -> int | None:
    """Position of the first message that differs from its shape in ``before``."""
    for i, (old, msg) in enumerate(zip(before, messages)):
        new = _message_shape(msg)
        if old[0] is not new[0] or old[1] is not new[1] or old[3] is not new[3]:
            return i
        for old_items, new_items in ((old[2], new[2]), (old[4], new[4])):
            if (old_items is None) != (new_items is None):
                return i
            if old_items is not None and (
                    len(old_items) != len(new_items)
                    or any(a is not b for a, b in zip(old_items, new_items))):
                return i
    if len(before) != len(messages):
        return min(len(before), len(messages))
    return None


class _MediaPick(NamedTuple):
    """One media item chosen for eviction, plus the words for its hint.

    Everything that distinguishes the eviction reasons lives in ``subject`` and
    ``reason``; the mechanics below them are identical for all of them.
    """

    msg_idx: int
    item_idx: int
    item: dict[str, Any]
    in_mm: bool
    subject: str
    reason: str


#: How many messages are embedded in one go. Above this, a prune stores its rows
#: inside the request and the embedding follows in the background, chunk by chunk
#: (see _archive_pruned). Measured: ~17 ms per message of embedding against
#: 0.02 ms for the row itself, and the largest of 1000 production prunes was 11
#: messages — so the background path only ever trips on a runaway loop. It used
#: to SKIP the index there, which left the archive half indexed: a similarity
#: search then answers over half of it without saying so.
_SEMANTIC_INDEX_MAX_BATCH = 200

#: One background embedding at a time, per event loop -- see the comment in
#: ``_index_in_background`` for why there is a bound at all. Per loop and not
#: per module, because an asyncio primitive binds to the loop it first WAITS on
#: and raises in any other: uncontended, `acquire` never reaches that check, so
#: a module-level semaphore would work on every loop until two batches overlap
#: and then raise inside a task, where the failure is one log line. The API has
#: one loop per process, but agent-cli, chat and every test build their own.
#: Weak keys alone do not let a loop go: a semaphore that ever had a waiter
#: holds the loop it bound to, so its entry holds its own key. ``_index_slot``
#: drops the closed ones.
_index_slots: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _index_slot() -> asyncio.Semaphore:
    """The one-at-a-time slot of the running loop."""
    for closed in [loop for loop in _index_slots if loop.is_closed()]:
        del _index_slots[closed]
    loop = asyncio.get_running_loop()
    slot = _index_slots.get(loop)
    if slot is None:
        slot = _index_slots[loop] = asyncio.Semaphore(1)
    return slot


def _ref_type(message: dict[str, Any]) -> str | None:
    """The placeholder type of a message, or None if it carries real content.

    Cheap substring test before the JSON parse: this runs over every message on
    every compaction, and the vast majority are ordinary text.
    """
    content = message.get("content")
    if not isinstance(content, str):
        return None
    # Deliberately NOT windowed to the first N characters, matching
    # is_compaction_system_message in the agent core: a window couples this to
    # JSON key order, and one `sort_keys=True` in a producer would push "type"
    # past it. This predicate decides protection, ranking and archive exclusion
    # — losing it silently would stack breadcrumbs and mis-rank candidates.
    if not any(t in content for t in _PLACEHOLDER_TYPES):
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    ref_type = data.get("type")
    if ref_type not in _PLACEHOLDER_TYPES:
        return None
    allowed = _PLACEHOLDER_ROLES.get(ref_type)
    if allowed is not None and message.get("role") not in allowed:
        return None  # an echo of a placeholder, not a placeholder
    return ref_type


def _is_prune_notice(message: dict[str, Any]) -> bool:
    """The breadcrumb. Role is already part of ``_ref_type``'s answer."""
    return _ref_type(message) == PRUNE_NOTICE_TYPE


def _is_archive_pointer(message: dict[str, Any]) -> bool:
    """Whether a message is ALREADY the placeholder left by a previous archival."""
    return _ref_type(message) == ARCHIVED_REF_TYPE


def _arrival_indices(messages: list[dict[str, Any]]) -> range:
    """Positions after the last assistant message: the round no request carried yet.

    Rewriting only these leaves the prompt-cache prefix byte-identical, and no
    model-written assistant turn sits at or after them, so no reasoning artifact
    is touched. An injected assistant message does not end the round: tool_preload
    appends its calls as assistant/tool pairs in the same pass, none of them sent.
    """
    last = max((i for i, msg in enumerate(messages)
                if msg.get("role") == "assistant" and not msg.get("injected_by")),
               default=-1)
    return range(last + 1, len(messages))


def _opens_turn(msg: dict[str, Any]) -> bool:
    """A message nobody injected that opened a request: a person, a pipeline
    calling the agent, or the wake of a woken run (a `developer` message,
    cli_utils/agent_runner.wake_message — the run speaking, but the head of its
    turn all the same). The marked messages the agent loop and the hooks add
    (step budget note, loop intervention, follow-ups, debate posts) belong to
    the request before them — counted as turns, every one of them aged that
    request, and Layers 2 and 3 archived or dropped the task still being worked
    on. On the role alone a woken run started no turn at all: its own task was
    outside the protected set, and a session woken again and again without
    anybody typing kept the age of every message frozen at the last human turn."""
    return opens_a_turn(msg)


def _request_user_indices(messages: list[dict[str, Any]]) -> set[int]:
    """The messages the current request stands on, by three different claims:
    the last input (the API needs one), the last one a PERSON wrote, and the
    head of the current turn.

    They used to be two, because two of them were the same message. The agent
    loop adds marked user messages after a person's (step budget note, loop
    intervention, follow-ups, debate posts), and the task or the image they
    refer to was compacted like any old turn -- that is what the "person" slot
    is for, and it has to keep meaning a PERSON: a woken run's wake is the head
    of its turn but never carries what a person sent, so letting the head take
    that slot spends it on a message with no media and drops the human's last
    real one out of the set entirely."""
    last = [i for i, msg in enumerate(messages) if is_input(msg)][-1:]
    person = [i for i, msg in enumerate(messages)
              if msg.get("role") == "user" and msg.get("injected_by") is None][-1:]
    head = [i for i, msg in enumerate(messages) if _opens_turn(msg)][-1:]
    return {*last, *person, *head}


class LayeredCompactionStrategy:
    """Implements progressive context compaction.
    
    This strategy applies compression techniques in layers, starting with
    fully reversible operations and progressively moving to more aggressive
    techniques only if needed to reach the target token budget.
    
    Usage:
        strategy = LayeredCompactionStrategy(
            tool_store, core_memory, archival_memory, config
        )

        result = strategy.compact(messages, current_tokens)

        # Use result.modified_messages as the new conversation history
    """

    def __init__(
        self,
        tool_store: ToolResultStore,
        core_memory: CoreMemory,
        archival_memory: ArchivalMemory,
        config: CompactionConfig | None = None,
        media_store: MediaStore | None = None,
        summarize: Callable[[str, str], Awaitable[str | None]] | None = None
    ):
        """Initialize the compaction strategy.

        Args:
            tool_store: Store for tool results
            core_memory: Core memory for important facts
            archival_memory: Archive for old messages
            config: Compaction configuration
            media_store: Optional store for inline media before compaction
            summarize: (content, tool_name) -> summary for a stored result, or
                None for none. The hook builds it; the engine stays free of the
                LLM layer and of system_config.
        """
        self.tool_store = tool_store
        self.core_memory = core_memory
        self.archival_memory = archival_memory
        self.config = config or CompactionConfig()
        self.media_store = media_store
        self.summarize = summarize
        #: Seconds left for the summaries of the current round. Pre-Layer T
        #: resets it per round; a direct call gets the full one.
        self._summary_budget = SUMMARY_ROUND_S
        #: Set per run by ``compact``; pre-set so a directly called layer
        #: cannot fail on a missing attribute instead of storing the media.
        #: It is a SHARED pot, not a private one — ``_store_inline_media`` says
        #: so out loud the first time anything lands in it.
        self._current_session_id = _SHARED_SESSION_ID
        self._warned_shared_session = False
        #: Background embeddings of oversized archived batches. Held because an
        #: unreferenced task can be garbage collected while it runs.
        self._index_tasks: set[asyncio.Task[None]] = set()
    
    def _add_media_hint_to_content(
        self,
        msg: dict[str, Any],
        hint_text: str
    ) -> None:
        """Add a media compaction hint to a message's content.
        
        For user/assistant messages with list content, appends a text item.
        For tool messages with string content, appends hint after the JSON.
        
        Args:
            msg: Message dict to modify
            hint_text: Hint text to add (e.g., "[Audio removed. read(ref=...)]")
        """
        import json
        
        content = msg.get("content")
        role = msg.get("role", "")
        
        if isinstance(content, list):
            # List content (user/assistant) - append text item
            content.append({"type": "text", "text": hint_text})
        elif isinstance(content, str):
            # String content (tool messages) - append to JSON or plain text
            if role == "tool":
                # Try to inject into JSON, otherwise append as text
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        # APPEND, never assign: one tool result routinely carries
                        # several attachments, and each gets its own hint through
                        # here. Assigning let the last one silently delete every
                        # earlier hint, so a removed attachment left no trace at
                        # all — the plain-text branch below always appended.
                        previous = data.get("_media_compacted")
                        data["_media_compacted"] = (
                            f"{previous}\n{hint_text}" if previous else hint_text)
                        msg["content"] = json.dumps(data, ensure_ascii=False)
                    else:
                        # Non-dict JSON, append as suffix
                        msg["content"] = content + f"\n\n{hint_text}"
                except (json.JSONDecodeError, TypeError):
                    # Plain text, append
                    msg["content"] = content + f"\n\n{hint_text}"
            else:
                # Other roles - append as text
                msg["content"] = content + f"\n\n{hint_text}"
        elif content is None:
            # No content yet - create it
            msg["content"] = hint_text
    
    def _estimate_request_bytes(self, messages: list[dict[str, Any]]) -> int:
        """Estimate the total request size in bytes.
        
        This is important for providers like Gemini that have byte-size limits (100MB).
        
        Args:
            messages: Conversation messages
            
        Returns:
            Estimated size in bytes
        """
        import json
        
        total_bytes = 0
        
        for msg in messages:
            # Estimate JSON overhead for message structure
            role = msg.get("role", "")
            total_bytes += len(role) + 20  # role + JSON structure
            
            content = msg.get("content")
            if isinstance(content, str):
                total_bytes += len(content.encode('utf-8'))
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        # Text content
                        if item.get("type") == "text":
                            text = item.get("text", "")
                            total_bytes += len(text.encode('utf-8')) if isinstance(text, str) else 0
                        
                        # Inline media - check various formats
                        elif item.get("type") in _MEDIA_TYPES:
                            total_bytes += self._estimate_item_bytes(item)
            
            # Tool calls
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                total_bytes += len(json.dumps(tool_calls))
            
            # Multimodal content in tool responses (may have inline data or file paths)
            multimodal_content = msg.get("multimodal_content", [])
            if multimodal_content:
                for mm_item in multimodal_content:
                    if isinstance(mm_item, dict):
                        # Use _estimate_item_bytes which handles both inline data and file paths
                        total_bytes += self._estimate_item_bytes(mm_item)
        
        return total_bytes
    
    def _is_protected(self, msg: dict[str, Any]) -> bool:
        """Messages no layer may remove or archive. One rule, every layer.

        Each layer used to spell out its own version of this and they drifted:
        Layer 3 checked only the role, so with keep_system_messages off it
        dropped the prune breadcrumb — which is protected on its TYPE for a
        reason. That breadcrumb is the only thing telling the agent something
        left the view at all, and it carries the running total; dropping it
        resets the total to zero on the next prune, so the count silently
        restarts and the agent is told 12 messages went when it was 40.
        """
        if msg.get("role") == "system" and self.config.keep_system_messages:
            return True
        return _is_prune_notice(msg)

    def _protected_positions(self, messages: list[dict[str, Any]]) -> set[int]:
        """Positions no removing layer may take -- Pre-Layer P and both passes
        of Layer 3.

        `_is_protected` answers by what a message IS; this adds where it STANDS:
        the first input (the task everything refers to -- in a continued
        session it sits at the front and was the first thing to go), what the
        current request stands on (`_request_user_indices`), and the round the
        model has not seen yet. A cut deep enough reaches that round -- Pre-Layer
        P ranks the pointer Pre-Layer T just left there cheapest of all, Layer
        3's cut walks up to it from the front -- and then the call went out and
        the model never saw what came back. The pairs tool_preload adds to a
        new user message belong to it, and alone they could fill Layer 3's
        working tail.

        Pre-Layer P and Layer 3 used to build their own copies, and Layer 3
        built one only for its cut to a target: its age pass, which runs first,
        dropped the task and a woken run's last human message that the cut
        would have kept. So a first input too big for the target now stays
        there for good, as it always did in a run, and before turn
        drop_after_turns of a chat.

        `is_input`, not the bare role: a run woken at a fresh prompt has no
        `user` message at all, and a gate on one left the wake, that run's only
        instruction, an ordinary candidate.
        """
        inputs = [i for i, msg in enumerate(messages) if is_input(msg)]
        return ({i for i, msg in enumerate(messages) if self._is_protected(msg)}
                | set(inputs[:1])
                | _request_user_indices(messages)
                | set(_arrival_indices(messages)))

    async def compact(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        force: bool = False,
        trigger_event: str | None = None,
        session_id: str = "default",
        manual: bool = False,
        rewrite_layers: bool = True,
        context_window: int | None = None,
        arrivals_only: bool = False,
        tokens_after_arrivals: int | None = None,
    ) -> CompactionResult:
        """Apply layered compaction to messages.

        Args:
            messages: Conversation messages
            current_tokens: Current token count (calculated if not provided)
            force: Force compaction even if below target threshold
            manual: A person asked for it (/compact). Layer 1 (reversible)
                runs whatever the token count; what it takes is still decided
                by its own rules (keep_last, min_size). Layers 2 and 3 keep
                their thresholds.
            rewrite_layers: False holds back the layers that rewrite messages
                (P, 1, 2, 3) — the hook's hysteresis. The media passes and
                Pre-Layer T still run.
            context_window: The model's window, for Pre-Layer T. None skips it.
            arrivals_only: The hook came in for Pre-Layer T alone (below its
                gate or held): no other pass runs.
            tokens_after_arrivals: The caller's reading with T's results left
                out. Without it the estimate of what T stored is subtracted —
                wrong where a provider count from before the arrival dominates.
            trigger_event: Optional event that triggered compaction:
                - 'user_message': New user message arrived
                - 'final_response': Agent sent final response
                Used for config-based media compaction.
            session_id: Session ID for media storage
            
        Returns:
            CompactionResult with modified messages
        """
        # Store session_id for use in _compact_multimodal_content
        self._current_session_id = session_id
        force = force or manual
        if current_tokens is None:
            current_tokens = self._estimate_messages_tokens(messages)
        
        result = CompactionResult(
            original_tokens=current_tokens,
            final_tokens=current_tokens,
            tokens_saved=0,
            modified_messages=messages.copy()
        )
        result.shapes_before = [_message_shape(m) for m in result.modified_messages]

        # Pre-Layer T: a new tool result too big for the window is stored before
        # any request carries it. Layer 1 takes large results too, but only from
        # layer1_threshold on — until then a result that took a 128k window's
        # third went out with every call, or sank it. The bound is the window,
        # not a token count: a 1M model may carry a whole chapter, and the read
        # tool pages 5000 characters at a time. Only the current round is
        # touched, so the messages a request already carried stay as they were
        # sent; neither the thresholds nor the hysteresis hold it back.
        arrivals = self.oversized_arrivals(result.modified_messages, context_window)
        if arrivals:
            # One budget for the whole round, spent by the summaries in it.
            self._summary_budget = SUMMARY_ROUND_S
            estimated = 0
            for i in arrivals:
                msg = result.modified_messages[i]
                reference = await self._store_tool_result(msg, msg["content"], on_arrival=True)
                result.modified_messages[i] = {**msg, "content": reference}
                result.tool_results_stored += 1
                estimated += (estimate_content_tokens(msg["content"])
                              - estimate_content_tokens(reference))
            result.layers_applied.append("T")
            after = (max(0, current_tokens - estimated) if tokens_after_arrivals is None
                     else min(current_tokens, tokens_after_arrivals))
            # Booked on the caller's scale, so _scale_offset stays the reading
            # minus the estimate once the layers take their baseline.
            result.tokens_saved += current_tokens - after
            # Both readings the layer gates use: Layer 2 reads final_tokens, and
            # left at the reading from before it ran after T had already taken
            # the context back under Layer 1's threshold.
            current_tokens = result.final_tokens = after
            messages = result.modified_messages
            if arrivals_only:
                return self._finalize(result)

        # Check byte size - Gemini has 100MB limit, force compaction if exceeded
        request_bytes = self._estimate_request_bytes(messages)
        bytes_exceeded = request_bytes > self.config.max_request_bytes
        
        if bytes_exceeded:
            logger.warning(
                f"Request size {request_bytes / (1024*1024):.1f}MB exceeds "
                f"{self.config.max_request_bytes / (1024*1024):.0f}MB limit - forcing compaction"
            )
            force = True  # Force compaction to reduce byte size
        
        # Also force compaction if always_compact_media is enabled
        # (need to run compact() to apply the media compaction even if under token threshold)
        always_compact_media = self.config.always_compact_media_keep_last > 0
        
        # Also force compaction if max_messages exceeded
        # (need to run compact() to apply Pre-Layer P even if under token threshold)
        max_messages_exceeded = (
            self.config.max_messages > 0 and 
            len(messages) > self.config.max_messages
        )
        
        # Check if compaction needed (unless forced or special conditions)
        if not force and not always_compact_media and not max_messages_exceeded and current_tokens <= self.config.target_tokens:
            logger.debug(
                f"No compaction needed: {current_tokens} tokens "
                f"<= {self.config.target_tokens} target"
            )
            return self._finalize(result) if arrivals else result

        # The event that sets off the event-triggered media pass (below), None
        # for none. Only while always_compact_media_keep_last is 0: the
        # always-compact pass handles media more precisely.
        media_event = trigger_event if self.config.always_compact_media_keep_last == 0 and (
            (trigger_event == "user_message" and self.config.compact_media_after_user_message)
            or (trigger_event == "final_response" and self.config.compact_media_after_final_response)
        ) else None

        # Why this run, for the log: what set it off, not that it was entered.
        # A bare FORCED and the tokens against target_tokens -- where a
        # compaction ENDS -- read as a demand for a reduction:
        # "[FORCED, MEDIA_ALWAYS, TOKENS(88565>70000)]" ending in "0.0%
        # reduction, layers applied: []" was chased as a failure, and was the
        # media pass the hook forces on every call, with no layer due below
        # layer1_threshold. "held": the hook's hysteresis holds the layers that
        # rewrite messages (P, 1, 2, 3), so none of them runs on it.
        held = "" if rewrite_layers else ", held"
        triggers = []
        if manual:
            triggers.append("MANUAL")
        if bytes_exceeded:
            triggers.append(f"BYTES({request_bytes / (1024*1024):.1f}MB>"
                            f"{self.config.max_request_bytes / (1024*1024):.0f}MB)")
        if max_messages_exceeded:
            triggers.append(f"MSG_LIMIT({len(messages)}>{self.config.max_messages}{held})")
        if always_compact_media:
            triggers.append("MEDIA_ALWAYS")
        if media_event:
            triggers.append(f"MEDIA_EVENT({media_event})")
        if current_tokens >= self.config.layer1_threshold:
            triggers.append(f"TOKENS({current_tokens}>={self.config.layer1_threshold}{held})")
        if not triggers:
            # A caller forcing it without a reason, or the context past
            # target_tokens alone: then only the media dedup runs.
            triggers.append("FORCED" if force else
                            f"ABOVE_TARGET({current_tokens}>{self.config.target_tokens})")
        
        logger.info(
            f"Starting compaction [{', '.join(triggers)}]: {current_tokens} tokens, "
            f"{request_bytes / (1024*1024):.1f}MB, {len(messages)} messages, "
            f"max_messages={self.config.max_messages}"
        )
        
        # Pre-Layer P: Prune by message count - runs FIRST if message limit exceeded
        # This is independent of token thresholds - too many messages waste API overhead
        if (rewrite_layers and self.config.max_messages > 0
                and len(result.modified_messages) > self.config.max_messages):
            await self._prune_by_message_count(result)
            if result.messages_pruned > 0:
                result.layers_applied.append("P")
                # Recalculate tokens after pruning — on the caller's scale, as
                # the Layer 2/3 gates below read it (see _scale_offset).
                result.final_tokens = self._estimate_messages_tokens(result.modified_messages)
                current_tokens = result.final_tokens + self._scale_offset(result)

        # Pre-Layer M: Always compact media (keep last N) - runs regardless of token count
        # This is the most aggressive media compaction, runs first if enabled
        if self.config.always_compact_media_keep_last > 0:
            await self._compact_media_always(result)
            # Mark that always-compact media was applied (shown as "M" in UI)
            if result.media_always_compacted > 0 and "M" not in result.layers_applied:
                result.layers_applied.append("M")
        
        # Pre-Layer: Media deduplication (always run if enabled)
        # This is independent of token thresholds - duplicates waste space regardless
        if self.config.deduplicate_media:
            await self._deduplicate_media(result)
        
        # Pre-Layer: Event-triggered media compaction
        # Compact all media if configured to do so on user message or final response
        if media_event:
            await self._compact_media_after_event(result, trigger=media_event)
        
        # If byte size is the issue, compact media aggressively
        # Keep media only in the last 2 messages (of any role), evict the large
        # items (10 KB and up) in all others. Measured again: request_bytes is
        # from before the passes above, and when they already brought the
        # request under the limit, this one took every large item outside the
        # last two messages for nothing.
        bytes_exceeded = bytes_exceeded and (
            self._estimate_request_bytes(result.modified_messages)
            > self.config.max_request_bytes)
        if bytes_exceeded:
            await self._compact_media_for_byte_limit(result)
            # Mark that size-limit compaction was applied (shown as "B" in UI)
            if "B" not in result.layers_applied:
                result.layers_applied.append("B")
        
        if not rewrite_layers:
            return self._finalize(result)

        # Apply layers progressively based on TOKEN thresholds
        # Layer 1/2/3 should ONLY run based on token thresholds
        # The 'force' flag is used to enter compact() even when below threshold,
        # but it should NOT force Layer 1 to run if we're just doing media compaction
        
        # Layer 1: Apply only if above threshold (or bytes exceeded which sets force)
        # Note: always_compact_media triggers compact() but should NOT trigger Layer 1
        # A manual compaction runs Layer 1 whatever the token count: a person
        # typing /compact wants it now, and was told "saved 0 tokens" whenever
        # the session sat below the automatic trigger. ONLY Layer 1 — it is
        # the reversible one. Layers 2 and 3 take messages out of the view,
        # and below their thresholds that is not what /compact is for.
        # The bytes as they are after the media passes: when those, Pre-Layer B
        # included, already took the request under the limit, Layer 1's byte
        # mode evicted the newest media for nothing. The tokens as they were
        # before them, on purpose: a media pass that evicted has broken the
        # cache on this call already, and Layer 1 rides along. Read after it,
        # a growing loop crossed the threshold again one quiet call later and
        # paid Layer 1 as a break of its own.
        bytes_exceeded = bytes_exceeded and (
            self._estimate_request_bytes(result.modified_messages)
            > self.config.max_request_bytes)
        layer1_needed = manual or current_tokens >= self.config.layer1_threshold or bytes_exceeded
        if layer1_needed:
            await self._apply_layer1(result, bytes_exceeded=bytes_exceeded)
            result.layers_applied.append(1)

            # Check both token AND byte targets
            final_bytes = self._estimate_request_bytes(result.modified_messages)
            if (result.final_tokens <= self.config.target_tokens and
                final_bytes <= self.config.target_request_bytes):
                return self._finalize(result)

        # Layers 2 and 3 read their thresholds on the hook's scale. After Layer 1
        # final_tokens is an estimate of the messages alone, while the hook
        # decided on max(provider count, estimate + tool definitions) and the
        # hysteresis records the deepest layer due on that scale: a reading of
        # 200,085 over an estimate below 200,000 marked Layer 3 as done without
        # running it, and held it back for another min_tokens_between_compactions.
        offset = self._scale_offset(result)

        # Layer 2: Only apply if above threshold (turn-based archival)
        if result.final_tokens + offset >= self.config.layer2_threshold:
            await self._apply_layer2(result)
            result.layers_applied.append(2)

            if result.final_tokens <= self.config.target_tokens:
                return self._finalize(result)

        # Layer 3: Only apply if above threshold (turn-based dropping)
        if result.final_tokens + offset >= self.config.layer3_threshold:
            await self._apply_layer3(result)
            result.layers_applied.append(3)
        
        return self._finalize(result)
    
    def _compute_media_hash(self, item: dict[str, Any]) -> str | None:
        """Identity of a media item, for duplicate detection.

        A path identifies a file; everything else is identified by its actual
        payload, pulled out by the ONE shared extractor
        (`extract_inline_media`) that also feeds `_store_inline_media`.

        This used to be a fourth hand-written format reader and recognised
        exactly one of the five wire shapes — the OpenAI data URL. Anthropic
        `source.data`, Gemini `inline_data`, `audio_url` and `video_url` all
        hashed to None, so deduplication silently never fired for them: the
        same 40 MB image twice in a conversation stayed twice in the request.

        It also hashed only the first 1000 base64 characters. Two different
        videos sharing a prefix (container header plus metadata) compared
        equal, and one of them was dropped as a duplicate. Hashing the full
        payload costs a hash over bytes we already hold in memory.

        Args:
            item: Media item dict (from multimodal_content or content list)

        Returns:
            Hash string or None if the item carries neither path nor payload
        """
        file_path = item.get("path", "")
        if file_path:
            normalized = os.path.normpath(file_path)
            return hashlib.md5(normalized.encode()).hexdigest()[:16]

        try:
            raw_bytes, _, _ = extract_inline_media(item)
        except ValueError:
            # The extractor deliberately raises on a corrupt payload rather
            # than pretending there is no media. Identity is a different
            # question: an item we cannot decode is simply not provably equal
            # to anything, so it must not be merged with another one — and it
            # must not take the whole compaction down either.
            logger.debug("Media item has an undecodable payload, not hashable")
            return None

        if raw_bytes:
            return hashlib.md5(raw_bytes).hexdigest()[:16]

        return None

    def _get_media_filename(self, item: dict[str, Any]) -> str:
        """Extract original filename from media item.
        
        Args:
            item: Media item dict
            
        Returns:
            Filename string or 'unknown'
        """
        file_path = item.get("path", "")
        if file_path:
            return os.path.basename(file_path)
        
        # Try name field
        name = item.get("name", "")
        if name:
            return name
        
        # Try to extract from description
        desc = item.get("description", "")
        if desc:
            return desc[:50]  # Use first 50 chars of description
        
        return "unknown"
    
    def _iter_media(
        self,
        messages: list[dict[str, Any]],
        *,
        mm_types: tuple[str, ...] | None = None,
    ) -> Iterator[tuple[int, int, dict[str, Any], bool]]:
        """Every media item in the conversation — both storage places, one walk.

        Yields ``(msg_idx, item_idx, item, in_mm)``. ``in_mm`` is not decoration:
        it decides HOW the item leaves later, and the two ways are genuinely
        different (see ``_evict_media``).

        ``mm_types=None`` means every dict in ``multimodal_content`` counts.
        That is what three of the four selections want, because a tool
        attachment carries no dependable ``type`` — only the always-compact rule
        narrows it, and it says so.
        """
        for msg_idx, msg in enumerate(messages):
            content = msg.get("content")
            if isinstance(content, list):
                for item_idx, item in enumerate(content):
                    if isinstance(item, dict) and item.get("type", "") in _MEDIA_TYPES:
                        yield msg_idx, item_idx, item, False

            mm_content = msg.get("multimodal_content")
            if isinstance(mm_content, list):
                for item_idx, item in enumerate(mm_content):
                    if not isinstance(item, dict):
                        continue
                    if mm_types is None or item.get("type", "") in mm_types:
                        yield msg_idx, item_idx, item, True

    def _media_subject(self, item: dict[str, Any], with_filename: bool = True) -> str:
        """What a hint calls the item it replaces (see ``_MEDIA_SUBJECTS``)."""
        subject = _MEDIA_SUBJECTS.get(str(item.get("type", "")), "Media")
        if with_filename:
            return f"{subject} '{self._get_media_filename(item)}'"
        return subject

    def _media_hint(
        self,
        subject: str,
        reason: str,
        path: str,
        *,
        unrecoverable: str = "",
    ) -> str:
        """The ONE place a model-facing media hint is written.

        The path stays in DOUBLE QUOTES because that is literally what the model
        copies into ``read(ref="…")``. Every eviction path used to spell this
        out for itself, which is how eleven of these hints kept pointing at
        ``recall(query=…)`` long after that tool was deleted — a dead
        instruction handed over at the exact moment the bytes left the context.
        """
        if path:
            return f'[{subject} {reason} read(ref="{path}") loads it again.]'
        return unrecoverable or f"[{subject} {reason}]"

    def _can_evict(self, item: dict[str, Any], in_mm: bool) -> bool:
        """Whether eviction takes anything out. A content item with no path and
        no bytes -- an image by remote URL -- has nothing to store, and its hint
        would replace the only address it has (Layer 1 keeps these too)."""
        return in_mm or bool(item.get("path")) or bool(self._estimate_item_bytes(item))

    async def _evict_media(
        self,
        result: CompactionResult,
        picks: list[_MediaPick],
        *,
        store: bool = False,
        count_bytes: bool = True,
    ) -> int:
        """Throw the picked media out and leave exactly one hint per item.

        The ONE eviction. Everything above it only decides WHICH items go. The
        asymmetry between the two storage places is real and lives here:

        * ``content`` item → REPLACED in place by a ``{"type": "text"}`` item,
          because a content list is positional and the model reads it in order.
        * ``multimodal_content`` item → DELETED, hint appended to the content,
          because that list is re-encoded verbatim at call time and has no slot
          for prose.

        ``store`` saves an inline payload to disk first, but only for content
        items — a ``multimodal_content`` item already IS a file reference. An
        item that ends up with no address gets a hint WITHOUT a ``read(...)``
        suffix: dedup used to substitute the literal "N/A" there, handing the
        model ``read(ref="N/A")`` — the dead instruction ``_media_hint`` exists
        to prevent, and ``read(ref="")`` for an item carrying an empty path.

        Returns the number of items evicted; ``tokens_saved`` and (unless
        ``count_bytes`` is off) ``media_bytes_saved`` are booked here. The
        per-reason counter stays with the caller — one of them assigns where the
        others add, and that difference is load-bearing in ``_finalize``.
        """
        messages = result.modified_messages
        mm_removals: dict[int, list[tuple[int, str]]] = defaultdict(list)
        evicted = 0

        for msg_idx, item_idx, item, in_mm, subject, reason in picks:
            path = item.get("path", "")
            if not self._can_evict(item, in_mm):
                continue
            if store and not in_mm and not path:
                source = item.get("source", {})
                item_type = item.get("type", "")
                media_type = (source.get("media_type", item_type)
                              if isinstance(source, dict) else item_type)
                # Off the event loop: this base64-decodes and writes a file, and
                # the byte-limit caller arrives with dozens of items at 90 MB.
                path = await asyncio.to_thread(
                    self._store_inline_media,
                    item, str(media_type), self._current_session_id) or path

            hint = self._media_hint(subject, reason, path)

            if in_mm:
                mm_removals[msg_idx].append((item_idx, hint))
            else:
                messages[msg_idx]["content"][item_idx] = {"type": "text", "text": hint}

            evicted += 1
            result.tokens_saved += estimate_media_tokens(item)
            if count_bytes:
                result.media_bytes_saved += self._estimate_item_bytes(item)
            logger.debug("Evicted media: %s", hint)

        # Delete from the tail forwards so the lower indices stay valid.
        for msg_idx, removals in mm_removals.items():
            msg = messages[msg_idx]
            mm_list = msg.get("multimodal_content", [])
            for item_idx, hint in sorted(removals, key=lambda x: x[0], reverse=True):
                # Every index came from enumerating THIS list, nothing shrinks it
                # between selection and here, and the descending order keeps the
                # lower ones valid — so a miss is impossible, not merely unlikely.
                # It used to be skipped silently, which would have left three
                # counters claiming an eviction that never happened.
                assert item_idx < len(mm_list), (
                    f"multimodal_content shrank under the eviction: index "
                    f"{item_idx} of {len(mm_list)} in message {msg_idx}")
                del mm_list[item_idx]
                self._add_media_hint_to_content(msg, hint)

        return evicted

    async def _deduplicate_media(self, result: CompactionResult) -> None:
        """Selection: the same bytes twice — every occurrence but the newest goes.

        Nothing is written to disk here, and that is deliberate: the payload is
        still in the conversation a few messages further down.

        The newest stays although the older copy would be the cache-friendly
        one: evicting the newer writes the hint at the end, while evicting the
        older rewrites the prompt from that message on. Keeping the older was
        built and measured wrong twice. Layer 1 keeps media only on the newest
        tool results and in the last user message, so in the same run it took
        the older copy too and neither was left. And a path item is identified
        by its path, not its bytes: a tool that renders again to the same file
        had its new render hidden behind "shown earlier" — while the old slot,
        read from disk at call time, carried the new bytes anyway.
        """
        if not self.config.deduplicate_media:
            return

        by_hash: dict[str, list[tuple[int, int, dict[str, Any], bool]]] = defaultdict(list)
        for found in self._iter_media(result.modified_messages):
            media_hash = self._compute_media_hash(found[2])
            if media_hash:
                by_hash[media_hash].append(found)

        picks = [
            _MediaPick(msg_idx, item_idx, item, in_mm, self._media_subject(item),
                       "- duplicate compacted. Newer version exists later in "
                       "conversation.")
            for occurrences in by_hash.values() if len(occurrences) > 1
            # Ascending by message index, so the LAST one is the newest.
            for msg_idx, item_idx, item, in_mm in sorted(occurrences,
                                                         key=lambda o: o[0])[:-1]
        ]

        result.media_deduplicated += await self._evict_media(
            result, picks, count_bytes=False)

        if result.media_deduplicated > 0:
            result.final_tokens = self._estimate_messages_tokens(result.modified_messages)
            logger.info(f"Media deduplication: {result.media_deduplicated} duplicates compacted")

    def _store_inline_media(
        self,
        item: dict[str, Any],
        media_type: str,
        session_id: str
    ) -> str | None:
        """Store inline base64 media to disk before compaction.
        
        Args:
            item: Media item with various formats:
                - source.data (Anthropic/Gemini format)
                - image_url.url (OpenAI format)
                - inline_data.data (Gemini native format)
                - audio_url (data URL format)
            media_type: MIME type (e.g., "audio/flac", "image/png")
            session_id: Session ID for organization
            
        Returns:
            File path if stored successfully, None if media_store not available or failed
        """
        if not self.media_store or not self.config.store_media_before_compaction:
            return None

        # media_store_max_files is a PER-SESSION quota, so everyone who arrives
        # without a session id shares one pot and evicts each other's files.
        # Once per strategy is enough to find the caller; per item would be
        # dozens of lines in a single byte-limit compaction.
        if session_id == _SHARED_SESSION_ID and not self._warned_shared_session:
            self._warned_shared_session = True
            logger.warning(
                "Storing media under the shared '%s' session — no session id "
                "reached the compaction, so this run shares one per-session "
                "file quota with every other such caller", _SHARED_SESSION_ID)

        try:
            # ONE extractor for every wire shape, shared with media_ops:
            # agent_system.utils.multimodal_tool_content.extract_inline_media.
            # This used to be four hand-written format branches here, and they
            # were one short — `video_url` was missing, so inline video was
            # evicted without ever being stored and could not be restored.
            raw_bytes, mime, source_name = extract_inline_media(item)
            if raw_bytes is None:
                # Kept for the diagnostic, not for the outcome: without it the
                # encode below raises and the outer handler returns None too.
                # A mutation removing this stays green for exactly that reason.
                logger.debug(f"No inline data found in item with keys: {list(item.keys())}")
                return None

            # Raw bytes straight through: media_store.store() takes
            # `str | bytes` and b64decodes a string right back. Encoding
            # here would cost an encode, a decode and a 1.33x copy per
            # item — on the byte-limit path that is dozens of items at 90 MB.
            media_type = mime or media_type
            source_name = source_name or item.get("name")

            # Store the media
            stored_path = self.media_store.store(
                data=raw_bytes,
                media_type=media_type,
                session_id=session_id,
                source_name=source_name
            )
            
            return stored_path
            
        except Exception as e:
            logger.warning(f"Failed to store inline media: {e}")
            return None
    
    async def _compact_media_for_byte_limit(
        self,
        result: CompactionResult,
        keep_last_n: int = 2
    ) -> None:
        """Aggressively compact media to reduce request byte size.
        
        This is called when the request size exceeds max_request_bytes (e.g., Gemini's 100MB limit).
        Compacts ALL media except in the last N messages.
        
        Args:
            result: CompactionResult to update
            keep_last_n: Number of recent messages to keep media for
        """
        messages = result.modified_messages
        if not messages:
            return

        # Selection: everything outside the last N messages, but only items big
        # enough to be part of the problem — a 2 KB thumbnail never blew a
        # 90 MB request, and evicting it costs a hint that is nearly as long.
        cutoff = max(0, len(messages) - keep_last_n)
        picks = []
        for msg_idx, item_idx, item, in_mm in self._iter_media(messages):
            if msg_idx >= cutoff:
                continue
            item_bytes = self._estimate_item_bytes(item)
            if item_bytes < 10000:
                continue
            picks.append(_MediaPick(
                msg_idx, item_idx, item, in_mm, self._media_subject(item),
                f"removed to reduce request size. Saved {item_bytes / 1024:.0f}KB."))

        bytes_before = result.media_bytes_saved
        # store=True like every other eviction: this one fires at 90 MB, i.e.
        # when the payload is at its largest, and used to drop inline data with
        # no copy on disk and a hint that could name no address.
        compacted_count = await self._evict_media(result, picks, store=True)

        if compacted_count > 0:
            result.final_tokens = self._estimate_messages_tokens(messages)
            result.media_compacted_after_event += compacted_count
            bytes_saved = result.media_bytes_saved - bytes_before
            logger.warning(
                f"Byte limit compaction: {compacted_count} media items compacted, "
                f"saved {bytes_saved / (1024*1024):.1f}MB"
            )
    
    def _estimate_item_bytes(self, item: dict) -> int:
        """Estimate bytes for a single media item.
        
        Checks for inline data first, then falls back to file size if path exists.
        Returns base64-encoded size estimate (file_size * 4/3) for path-based items.
        Returns 0 for already-compacted items (they have no data).
        """
        # Skip already compacted items - they have no data
        if item.get("compacted"):
            return 0

        # Every wire shape, through the reader the token estimate uses. The
        # hand-written branches here missed a dict inline_data (it counted as
        # len(dict) = 2 bytes), video_url and a string image_url, so the
        # request-size gate could not see those payloads at all.
        payload = inline_payload(item)
        if payload is not None:
            return payload[0]

        # A bare payload on the item itself: older entries and hand-built items.
        for field_name in ("data", "inline_data", "content"):
            field_data = item.get(field_name)
            if isinstance(field_data, (str, bytes)) and field_data:
                return len(field_data)
        
        # Fallback: check file path and get actual file size
        # This is important for multimodal_content items that reference files
        # where the request encodes it from: data/... lands in the data directory
        file_path = str(resolve_data_path(item.get("path", ""))) if item.get("path") else ""
        if file_path and os.path.isfile(file_path):
            try:
                file_size = os.path.getsize(file_path)
                # Base64 encoding increases size by ~33%
                return int(file_size * 4 / 3)
            except (OSError, IOError):
                pass
        
        return 0
    
    async def _compact_media_after_event(
        self,
        result: CompactionResult,
        trigger: str = "user_message"
    ) -> None:
        """Compact all media items except those in the most recent message.
        
        This is triggered by config options:
        - compact_media_after_user_message: When a new user message arrives
        - compact_media_after_final_response: When agent sends final response
        
        Args:
            result: CompactionResult to update
            trigger: What triggered this compaction ('user_message' or 'final_response')
        """
        messages = result.modified_messages
        if not messages:
            return

        # Selection: everything but the newest message — and, while the turn
        # runs, the request the agent loop's notes may follow. After the final
        # response the request is done with.
        keep = {len(messages) - 1}
        if trigger == "user_message":
            keep.update(_request_user_indices(messages))
        picks = []
        for msg_idx, item_idx, item, in_mm in self._iter_media(messages):
            if msg_idx in keep:
                continue
            if item.get("compacted") and not in_mm:
                continue  # already a placeholder
            picks.append(_MediaPick(
                msg_idx, item_idx, item, in_mm,
                # The content variant names no file and never has — its hint
                # sits in the reading order right where the item was, so the
                # position already says which one went.
                self._media_subject(item, with_filename=in_mm),
                f"removed after {trigger}."))

        result.media_compacted_after_event += await self._evict_media(
            result, picks, store=True)

        if result.media_compacted_after_event > 0:
            result.final_tokens = self._estimate_messages_tokens(messages)
            logger.info(
                f"Media compaction after {trigger}: "
                f"{result.media_compacted_after_event} items compacted"
            )
    
    async def _compact_media_always(
        self,
        result: CompactionResult
    ) -> None:
        """Always compact media items, keeping only the last N messages with media
        and whatever arrived since the model's last answer.
        
        This runs regardless of token count and is controlled by
        config.always_compact_media_keep_last (0 = disabled).
        
        Unlike _compact_media_after_event, this keeps the last N messages that 
        CONTAIN media, not just the last N messages overall.
        
        Args:
            result: CompactionResult to update
        """
        messages = result.modified_messages
        keep_count = self.config.always_compact_media_keep_last
        if not messages or keep_count <= 0:
            return

        # Selection: the last N messages that CARRY media keep theirs, the rest
        # lose it. multimodal_content is narrowed to real media here, unlike
        # everywhere else — this is the only rule that lets an item decide
        # whether a whole MESSAGE counts as recent, so a stray attachment must
        # not spend one of the N slots.
        # Only what can be evicted counts: a message whose sole media is a
        # remote URL never loses it, and counted it kept the window one over
        # the limit, so every new image broke the cache again.
        found = [(msg_idx, item_idx, item, in_mm)
                 for msg_idx, item_idx, item, in_mm
                 in self._iter_media(messages, mm_types=("image", "audio", "video"))
                 if self._can_evict(item, in_mm)]
        with_media = sorted({msg_idx for msg_idx, _, _, _ in found})
        if len(with_media) <= keep_count:
            return

        # Past the limit, down to below it (always_compact_media_headroom).
        headroom = max(0, self.config.always_compact_media_headroom)
        # And whatever the model has not seen yet: parallel calls bring several images in one
        # round, and the pass kept only the newest -- shorts_producer on Sonnet (01.10.2026) asked
        # for five previews at once and saw one of them.
        protected = set(with_media[-max(1, keep_count - headroom):]) | set(_arrival_indices(messages))
        picks = [
            _MediaPick(msg_idx, item_idx, item, in_mm,
                       self._media_subject(item), "compacted.")
            for msg_idx, item_idx, item, in_mm in found
            if msg_idx not in protected
        ]

        compacted_count = await self._evict_media(result, picks, store=True)
        result.media_always_compacted = compacted_count

        if compacted_count > 0:
            result.final_tokens = self._estimate_messages_tokens(messages)
            logger.info(
                f"Always-compact media: {compacted_count} items compacted, "
                f"{len(protected & set(with_media))} of {len(with_media)} messages with media kept "
                f"(keep_last={keep_count}, headroom={headroom})"
            )

    async def _compact_multimodal_content(
        self,
        content: list,
        result: CompactionResult,
        session_id: str = "default",
        preserve_media: bool = False
    ) -> list:
        """Compact multimodal content by replacing large items with references.

        Handles:
        - text_file items: Move large content to the tool-result store
        - audio items: Replace large base64 data with [Audio removed] placeholder
        - image items: Replace large base64 data with [Image removed] placeholder

        Args:
            content: Multimodal content list (text, image, text_file, audio, etc.)
            result: CompactionResult to update tokens_saved
            session_id: Session ID for media storage
            preserve_media: If True, skip audio/image/video compaction (preserve media in last user msg)

        Returns:
            Compacted content list with large items replaced
        """
        compacted = []

        for item in content:
            if not isinstance(item, dict):
                compacted.append(item)
                continue

            item_type = item.get("type", "")

            # An attached text file goes to the tool-result store. It used to
            # become a $VAR, which is gone; the store holds the same thing
            # (content, ref, summary) and its refs already work with list and
            # read, so this is one store instead of two rather than a new path.
            if item_type == "text_file":
                file_content = item.get("content", "")
                file_name = item.get("name") or "file"
                token_count = estimate_content_tokens(file_content)

                if token_count >= self.config.tool_result_min_size and file_content:
                    # Content-derived key, so re-attaching the same file lands
                    # on the same row instead of filling the store with copies.
                    file_key = (
                        f"file_{file_name}_"
                        f"{hashlib.sha256(file_content.encode()).hexdigest()[:16]}"
                    )
                    reference = await asyncio.to_thread(
                        self.tool_store.store_and_reference,
                        tool_call_id=file_key,
                        tool_name=file_name,
                        content=file_content,
                        session_id=session_id,
                        summary=" ".join(file_content.split())[:200],
                    )
                    ref_id = json.loads(reference).get("ref_id", "")
                    hint = (
                        f'[File "{file_name}" stored, {token_count} tokens. '
                        f'read(ref="{ref_id}", find="...") for the parts you need.]'
                    )
                    compacted.append({"type": "text", "text": hint})
                    result.tool_results_stored += 1
                    result.tokens_saved += token_count - estimate_content_tokens(hint)
                    logger.debug(
                        f"Stored text_file '{file_name}' ({token_count} tokens) → {ref_id}"
                    )
                    continue

            # Inline audio/image payloads: save to disk first, so the placeholder
            # can name an address instead of an apology. Skipped entirely when
            # preserve_media is set (the last user message keeps its media).
            elif item_type in _MEDIA_TYPES:
                if preserve_media:
                    compacted.append(item)
                    continue

                inline_tokens = estimate_media_tokens(item)
                # Only a payload: an image by remote URL costs as much, but
                # there is nothing to store, and the hint would lose the URL.
                if (inline_tokens >= self.config.tool_result_min_size
                        and inline_payload(item) is not None):
                    subject = self._media_subject(item, with_filename=False)
                    # Only steers the stored file's extension — _store_inline_media
                    # re-derives the type from the payload it actually finds.
                    source = item.get("source", {})
                    media_type = (source.get("media_type", subject.lower())
                                  if isinstance(source, dict) else subject.lower())

                    item_bytes = self._estimate_item_bytes(item)
                    stored_path = self._store_inline_media(item, media_type, session_id)

                    compacted.append({"type": "text", "text": self._media_hint(
                        subject, "removed.", stored_path or "",
                        # The advice used to be "use store_fact BEFORE
                        # compaction" — read at the one moment compaction has
                        # already happened. What is left to do is ask again.
                        unrecoverable=(
                            f"[{subject} removed - not recoverable. Ask for it "
                            f"again if you still need it.]"),
                    )})
                    result.tokens_saved += inline_tokens
                    result.media_compacted_after_event += 1
                    result.media_bytes_saved += item_bytes
                    logger.debug(
                        f"Removed {subject.lower()} inline data: {inline_tokens:,} "
                        f"tokens, {item_bytes / 1024:.0f}KB saved")
                    continue


            # Keep item as-is (including small items and non-compactable types)
            compacted.append(item)
        
        return compacted
    
    async def _compact_multimodal_content_items(
        self,
        mm_content: list,
        result: CompactionResult,
        msg: dict[str, Any] | None = None
    ) -> tuple[list, int]:
        """Compact multimodal_content items (file paths) by removing from list and adding hint to content.
        
        This handles MultimodalToolContent objects that reference files which will be
        base64-encoded at LLM call time. We compact by:
        1. Removing the item from multimodal_content
        2. Adding a text hint to the message content explaining how to restore
        
        Args:
            mm_content: List of MultimodalToolContent dicts with path, type, mime_type
            result: CompactionResult for tracking
            msg: Message dict to add hints to (if None, hints are not added)
            
        Returns:
            Tuple of (compacted list with large items removed, tokens saved)
        """
        compacted = []
        tokens_saved = 0
        hints_to_add: list[str] = []
        
        for item in mm_content:
            if not isinstance(item, dict):
                compacted.append(item)
                continue
            
            item_type = item.get("type", "")
            file_path = item.get("path", "")
            
            if not file_path:
                compacted.append(item)
                continue
            
            inline_tokens = estimate_media_tokens(item)
            
            if inline_tokens >= self.config.tool_result_min_size:
                # Compact large audio/image/video files
                # Estimate bytes for tracking (base64 encoded size)
                item_bytes = self._estimate_item_bytes(item)
                
                hints_to_add.append(self._media_hint(
                    self._media_subject(item, with_filename=False),
                    "compacted.", file_path))
                tokens_saved += inline_tokens
                
                # Track media compaction for UI
                result.media_compacted_after_event += 1
                result.media_bytes_saved += item_bytes
                
                logger.info(f"Compacted {item_type} file: {inline_tokens:,} tokens, {item_bytes / 1024:.0f}KB saved (path: {file_path})")
                # Don't append to compacted - item is removed
                    
            else:
                # Small enough to keep
                compacted.append(item)
        
        # Add hints to message content
        if msg is not None:
            for hint in hints_to_add:
                self._add_media_hint_to_content(msg, hint)
        
        return compacted, tokens_saved
    
    def oversized_arrivals(self, messages: list[dict[str, Any]],
                           context_window: int | None) -> list[int]:
        """Pre-Layer T's work: new tool results over the window share.

        Cheap enough for every call — it looks at the current round only. A
        retrieval answer is exempt for the reason Layer 1 gives.
        """
        share = self.config.tool_result_max_window_share
        window_limit = context_window * share if context_window and share > 0 else None
        summarizing = self.summarize is not None and self.config.tool_result_summary_from > 0
        if window_limit is None and not summarizing:
            return []
        picked = []
        for i in _arrival_indices(messages):
            message = messages[i]
            content = message.get("content")
            if (message.get("role") != "tool" or not isinstance(content, str)
                    or _is_retrieval_result(message)):
                continue
            # Over the share: out of the window's way, as before. Over the
            # summary floor: only what a summary can actually replace -- taking
            # a structured result out for a pointer nobody summarizes would
            # cost the next agent a read call instead of saving it one.
            if window_limit is not None and estimate_content_tokens(content) > window_limit:
                picked.append(i)
            elif summarizing and self._summary_prose(content, message.get("name")) is not None:
                picked.append(i)
        return picked

    def _summary_prose(self, content: str, tool_name: str | None) -> str | None:
        """The text a summary would be written from, if one is due for this
        result at all: the right tool, prose, and enough of it to be worth a
        model."""
        floor = self.config.tool_result_summary_from
        patterns = self.config.tool_result_summary_tools
        if floor <= 0:
            return None
        # No patterns = every tool. With patterns, a result has to be NAMED to
        # pass: a nameless one (a session restored mid tool-turn carries no
        # name) can be matched by no pattern the operator could write, and
        # summarizing what they did not name is what this key exists to stop.
        # fnmatchcase, not fnmatch: fnmatch lowercases both sides on Windows
        # and nowhere else, so a camelCase tool name would be filtered one way
        # on a developer's machine and the other way on the server.
        if patterns and not (tool_name
                             and any(fnmatchcase(tool_name, p) for p in patterns)):
            return None
        text = _prose_of(content)
        if text is None or estimate_content_tokens(text) < floor:
            return None
        return text

    async def _store_tool_result(self, msg: dict[str, Any], content: str,
                                 on_arrival: bool = False) -> str:
        """Store one tool result and return the placeholder that replaces it.

        ``on_arrival`` is Pre-Layer T's call, the one result of the round the
        model has not seen yet. Layer 1 comes through here too, with the whole
        history at once: a summary per result there would be dozens of model
        calls in one hook, and the hook's budget ends the compaction for all
        of them (hooks/registry.py). What Layer 1 stores keeps its preview.
        """
        # A bare ref+token_count gives the model nothing to decide what to
        # find= for — it can only page blindly. A cheap preview (no LLM call)
        # is enough to point it at find=.
        preview = " ".join(content.split())[:200]
        written = await self._written_summary(msg, content) if on_arrival else None
        return await asyncio.to_thread(
            self.tool_store.store_and_reference,
            tool_call_id=msg.get("tool_call_id", ""),
            tool_name=msg.get("name", "unknown"),
            content=content,
            summary=written or preview,
            inline_summary=written,
        )

    async def _written_summary(self, msg: dict[str, Any], content: str) -> str | None:
        """What a cheap model says this result contains, or None for the preview.

        Only prose is worth it: a structured result is read back whole, and a
        summary of JSON would be a second, lossy shape of the same thing.
        """
        # The profile decides whether there IS a summarizer (the hook builds
        # none without one); _summary_prose decides what is worth one, and it
        # is the same question the selection asked.
        text = (self._summary_prose(content, msg.get("name"))
                if self.summarize is not None else None)
        if text is None or self._summary_budget < SUMMARY_MIN_CALL_S:
            # The budget is the ROUND's, not the call's: several results arrive
            # together (a fan-out to sub-agents is this feature's own case), and
            # the hook that runs all of this is dropped whole when its own
            # budget ends -- with the storing every other result was due.
            return None
        started = time.monotonic()
        try:
            # Only the head of it: the result Pre-Layer T exists for can be a
            # whole book, and the cheap call would be the run's dearest.
            head = text[:SUMMARY_INPUT_CHARS]
            if len(text) > SUMMARY_INPUT_CHARS:
                head += "\n[… the rest is only in the stored result]"
            answer = await asyncio.wait_for(
                self.summarize(head, str(msg.get("name") or "unknown")), timeout=self._summary_budget)
        except Exception as exc:  # noqa: BLE001 - CancelledError is not an Exception and stays
            logger.warning("Summary of a %s result failed, keeping the preview: %s", msg.get("name"), exc)
            return None
        finally:
            self._summary_budget -= time.monotonic() - started
        summary = " ".join(str(answer or "").split())
        if not summary or len(summary) > len(text) * SUMMARY_MAX_SHARE:
            # Measured BEFORE the cap: a model that echoes instead of
            # summarizing would otherwise pass as a summary of everything it
            # did not write -- a prefix of the result, cut mid-word, resent for
            # the rest of the session.
            return None
        return summary[:SUMMARY_CHARS] + ("…" if len(summary) > SUMMARY_CHARS else "")

    async def _apply_layer1(self, result: CompactionResult, bytes_exceeded: bool = False) -> None:
        """Layer 1: Reversible compaction.

        - Store tool outputs with references (auto-archives large results > max_size)
        - Move attached text files to the tool-result store
        - Evict inline media, storing it to disk first
        """
        logger.debug("Applying Layer 1: Reversible compaction")
        self._take_baseline(result)

        messages = result.modified_messages

        # Process messages in reverse (newer first, but skip last N tool results UNLESS too large)
        tool_results_seen = 0

        # Once, outside the loop: it reads roles and markers only, the loop
        # writes back `content` and `multimodal_content`, and the index range is
        # fixed -- so the answer cannot change under it. Asked per multimodal
        # user message it was three full passes over the history each time, for
        # a set that is the same every time. The cost of reading it here is that
        # a Layer 1 with no media at all now pays for it; three O(n) passes next
        # to the two token estimates this method already makes either way.
        request_user_indices = _request_user_indices(messages)

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            is_tool = msg.get("role") == "tool"
            if is_tool:
                tool_results_seen += 1

            # Process multimodal_content (file paths that will be base64-encoded
            # at call time) — but not on the newest tool results, and not on a
            # retrieval answer. Those were evicted too: a media file the agent
            # had just loaded, or had just restored with read(), was gone before
            # the model ever saw it, so it loaded it again — every call while
            # the layer fired. Still over the byte limit after the media passes,
            # nothing is kept: Pre-Layer B never touches the last messages, so a
            # large file in the newest result stayed in every request and each
            # one went out over the provider's cap. Only this layer can take it.
            mm_content = msg.get("multimodal_content")
            keep_media = not bytes_exceeded and is_tool and (
                tool_results_seen <= self.config.tool_result_keep_last
                or _is_retrieval_result(msg))
            if mm_content and isinstance(mm_content, list) and not keep_media:
                compacted_mm, mm_tokens_saved = await self._compact_multimodal_content_items(mm_content, result, msg)
                if mm_tokens_saved > 0:
                    messages[i] = {**msg, "multimodal_content": compacted_mm}
                    result.tokens_saved += mm_tokens_saved
                    # Everything below builds on messages[i]. Spreading the old
                    # msg there put the ORIGINAL multimodal_content back: the
                    # media stayed in the request while the counters and the
                    # hint said it was gone.
                    msg = messages[i]

            # Process tool results
            if is_tool:
                # Already a pointer — re-archiving it stores a pointer to a
                # pointer and gains nothing, the content is long gone from
                # this message. Layer 2 has this exact guard (see the comment
                # there: chains 20 levels deep, 637 of 1286 entries nothing
                # but pointers); Layer 1 runs BEFORE Layer 2 and never had it,
                # so a small-enough placeholder could loop back through here
                # on a later turn and get "archived" again.
                if _ref_type(msg) is not None:
                    continue

                content = msg.get("content", "")
                # Handle multimodal content - compact text_file items
                if isinstance(content, list):
                    compacted_content = await self._compact_multimodal_content(
                        content, result, session_id=self._current_session_id
                    )
                    if compacted_content != content:
                        messages[i] = {**msg, "content": compacted_content}
                    continue
                token_count = estimate_content_tokens(content)
                
                # Always archive if exceeds max size (even if in last N)
                # CRITICAL: Archive very large results (>2x max_inline_size) even if it's the very last one
                # This prevents huge tool results from breaking LLM calls
                is_extremely_large = token_count >= (self.config.tool_result_max_inline_size * 2)
                is_too_large = token_count >= self.config.tool_result_max_inline_size
                is_old_enough = tool_results_seen > self.config.tool_result_keep_last
                should_archive = token_count >= self.config.tool_result_min_size and (
                    is_extremely_large or is_too_large or is_old_enough
                )

                # A retrieval tool's OWN answer must stay in the conversation.
                # Storing it away replaces the content the agent just fetched
                # with a reference to it — the agent asks again, that answer is
                # stored too, and retrieval can never complete. Observed live:
                # 25 read results externalised in one turn, the first of which
                # already contained the answer the agent then reported missing.
                if should_archive and _is_retrieval_result(msg):
                    logger.debug(
                        "Keeping retrieval result %s inline (externalising it "
                        "would undo the retrieval)", msg.get("name"))
                    should_archive = False

                if should_archive:
                    reference = await self._store_tool_result(msg, content)
                    messages[i] = {**msg, "content": reference}
                    result.tool_results_stored += 1
                    result.tokens_saved += token_count - estimate_content_tokens(reference)

                    if is_too_large:
                        logger.debug(
                            f"Auto-archived large tool result '{msg.get('name', 'unknown')}' "
                            f"({token_count} tokens, exceeds max_inline_size)"
                        )
            
            # Long assistant messages used to be replaced by a $VAR reference
            # here. Measured over 1000 production compactions that fired 9
            # times against 3730 tool results, while rewriting an old message
            # broke the provider prompt cache from that point on every time.
            # Layer 2 archives them by age instead, which is what actually ran.

            # Process user messages with multimodal content
            # Only skip the current request's user message for audio/image/video
            # media preservation; text_file items should always be processed
            # (they are code/text)
            elif msg.get("role") == "user":
                content = msg.get("content")
                if content and isinstance(content, list):
                    # For the current request, only compact text_file items
                    # (preserve audio/image/video inline data)
                    compacted_content = await self._compact_multimodal_content(
                        content, result, session_id=self._current_session_id,
                        preserve_media=i in request_user_indices
                    )
                    if compacted_content != content:
                        messages[i] = {**msg, "content": compacted_content}
        
        result.final_tokens = self._estimate_messages_tokens(messages)
        logger.debug(
            f"Layer 1 complete: stored {result.tool_results_stored} tool results, "
            f"saved {result.tokens_saved} tokens"
        )
    
    async def _apply_layer2(self, result: CompactionResult) -> None:
        """Layer 2: Semi-reversible compaction.
        
        - Archive old messages with summaries
        - Ensures tool_calls and tool_results are archived together
        """
        logger.debug("Applying Layer 2: Semi-reversible compaction")
        self._take_baseline(result)

        messages = result.modified_messages

        # Tool calls and their results are archived together
        groups = self._tool_call_groups(messages)

        # Find turn boundaries (user messages nobody injected)
        turn_starts = [i for i, msg in enumerate(messages) if _opens_turn(msg)]

        # Calculate turn number for each message
        current_turn = len(turn_starts)

        # Collect indices to archive (including tool_call pairs)
        indices_to_archive = set()
        
        for i, msg in enumerate(messages):
            role = msg.get("role")

            # Never archive system messages if configured
            if role == "system" and self.config.keep_system_messages:
                continue

            # Already a pointer — archiving it again stores a pointer to a
            # pointer and gains nothing, because the content is long gone from
            # this message. Measured before this guard: chains 20 levels deep,
            # 637 of 1286 entries in one session being nothing but pointers, and
            # the summaries degrading to `User: {"type": "archived_ref"...}`. An
            # agent following such a ref never reaches the text, which is what
            # made retrieval look broken.
            #
            # The guard covers EVERY placeholder type, not just archive refs.
            # Layer 1 runs immediately before this and turns old tool results
            # into `tool_result_ref` — those were then archived here, producing
            # `archived_ref -> tool_result_ref`, and the chain resolver only
            # follows archive refs, so the walk ended on the pointer and handed
            # the agent JSON instead of the text. Same defect, other entrance.
            if _ref_type(msg) is not None:
                continue

            # Calculate message age in turns
            message_turn = sum(1 for ui in turn_starts if ui <= i)
            turns_old = current_turn - message_turn

            if turns_old >= self.config.archive_after_turns:
                indices_to_archive.update(groups.get(i, (i,)))

        # Drop placeholders that entered through the tool-pair expansion above:
        # that branch adds indices directly, so the per-message guard never sees
        # them. Measured after guarding only the loop, 13 of 57 entries in a live
        # session were still pointers — all of them halves of a tool pair.
        # Retrieval answers stay too, as in Layer 1 and the archive path: their
        # body was just pulled OUT of storage, and putting it back is the loop
        # _archive_pruned describes.
        indices_to_archive = {i for i in indices_to_archive
                              if _ref_type(messages[i]) is None
                              and not _is_retrieval_result(messages[i])}

        # Archive collected messages — in conversation order: the archive
        # timestamps each row as it is written, and the history listing follows
        # them. A set iterates in hash order once the indices outgrow its table.
        # One batch, not one store() per message: see _store_batch for why a
        # row-by-row write left duplicates behind on failure.
        ordered = sorted(indices_to_archive)
        archive_ids = await self._store_batch([messages[i] for i in ordered], "Layer 2")
        for i, archive_id in zip(ordered, archive_ids, strict=True):
            msg = messages[i]
            original_role = msg.get("role", "system")
            
            # Create compact reference as JSON (preserves structure, valid for tool messages)
            summary = self.archival_memory._generate_summary(msg)
            if len(summary) > self.config.max_summary_tokens * 4:  # ~4 chars per token
                summary = summary[:self.config.max_summary_tokens * 4] + "..."
            
            import json
            # Preserve original role to maintain message structure (important for Gemini)
            archived_msg: dict[str, Any] = {
                "role": original_role,
                "content": json.dumps({
                    "type": "archived_ref",
                    "ref_id": archive_id,
                    "summary": summary
                })
            }
            
            # CRITICAL: Preserve tool_calls for assistant messages!
            # Gemini requires tool_calls (with thought_signatures) for proper function call handling.
            # Without tool_calls, the message structure becomes invalid for the LLM.
            if original_role == "assistant" and msg.get("tool_calls"):
                archived_msg["tool_calls"] = msg["tool_calls"]
            
            # Preserve tool_call_id and name for tool messages (required for pairing and conversion)
            if original_role == "tool":
                if msg.get("tool_call_id"):
                    archived_msg["tool_call_id"] = msg["tool_call_id"]
                if msg.get("name"):
                    archived_msg["name"] = msg["name"]

            # Who added it: an archived follow-up or loop note must not come
            # back as a message a person wrote.
            if msg.get("injected_by"):
                archived_msg["injected_by"] = msg["injected_by"]
            # Which run it opened, which ids its calls' tools ran under: a session
            # read back finds its sub-agents' runs by them, archived or not.
            for stamp in ("request_id", "tool_request_ids", "step"):
                if msg.get(stamp):
                    archived_msg[stamp] = msg[stamp]

            messages[i] = archived_msg
            result.messages_archived += 1
        
        result.final_tokens = self._estimate_messages_tokens(messages)
        logger.debug(
            f"Layer 2 complete: archived {result.messages_archived} messages "
            f"(including tool_call pairs)"
        )

    async def _apply_layer3(self, result: CompactionResult) -> None:
        """Layer 3: last-resort compaction — messages leave the conversation.

        "Irreversible" used to be literal: this layer deleted outright, while
        Pre-Layer P right next to it wrote everything to the archive first. Same
        operation, two different answers to "is the content gone afterwards".
        It goes through the same archive-then-delete path now, so what leaves
        the view here is still reachable through the retrieval tools.
        """
        logger.debug("Applying Layer 3: dropping old messages")
        self._take_baseline(result)

        messages = result.modified_messages

        # Tool calls and their results leave together -- and stay together: a
        # unit with one protected message in it stays whole.
        groups = self._tool_call_groups(messages)
        protected = self._protected_positions(messages)

        # Find turn boundaries (user messages nobody injected)
        turn_starts = [i for i, msg in enumerate(messages) if _opens_turn(msg)]
        current_turn = len(turn_starts)

        indices_to_remove = set()
        for i in range(len(messages)):
            unit = groups.get(i, (i,))
            if not protected.isdisjoint(unit):
                continue

            # Calculate message age
            message_turn = sum(1 for ui in turn_starts if ui <= i)
            turns_old = current_turn - message_turn

            if turns_old >= self.config.drop_after_turns:
                indices_to_remove.update(unit)

        indices_to_remove |= self._select_down_to_target(
            messages, groups, protected, indices_to_remove)

        result.messages_dropped = await self._archive_then_remove(
            result, indices_to_remove, "Layer 3"
        )

        # The summaries on the remaining archive references STAY. This pass used
        # to strip them down to a bare ref_id, which was consistent while there
        # was no way to follow a reference: a pointer you cannot dereference is
        # just ballast, so the label was worth nothing. With the retrieval tools
        # in place the trade inverts — the summary IS the catalogue entry the
        # agent reads to decide whether a ref is worth fetching, and an
        # unlabelled address is the one form it can never act on. If the space
        # is genuinely needed, dropping the whole placeholder is the better
        # move: the content stays findable through list(section='history').
        
        result.final_tokens = self._estimate_messages_tokens(messages)
        logger.debug(
            f"Layer 3 complete: dropped {result.messages_dropped} messages "
            f"(including tool_call pairs)"
        )

        # Note: max_messages limit is now handled by Pre-Layer P at the start of compact()
        # This ensures message count is limited even when token thresholds aren't reached

    def _select_down_to_target(
        self,
        messages: list[dict[str, Any]],
        groups: dict[int, frozenset[int]],
        protected: set[int],
        selected: set[int],
    ) -> set[int]:
        """More of the oldest messages, until what stays is at target_tokens.

        Age alone cut shallow, and shallow cuts are what the prompt cache cannot
        afford (rarely and deep). An agent
        run is ONE user turn, so nothing was ever old enough: Layer 3 removed
        nothing and the context grew without bound above every threshold. A chat
        above the threshold lost the one or two turns that had just aged out —
        and every such run rewrote the front of the conversation and re-billed
        the whole prompt, a few calls apart (measured: 225k tokens every 9 calls).

        What stays: everything `protected` holds (_protected_positions), and
        the working tail — the newest tool_result_keep_last tool-call units or
        messages outside it, about the window Layer 1 leaves inline. Units
        leave whole.

        The estimate counts inline media by what it costs (estimate_media_tokens): at
        0.25 tokens per base64 character an uploaded image in the protected
        last user message "weighed" 256k tokens, and the cut emptied a 40-turn
        chat of 38k real tokens down to five messages on the upload call.
        """
        estimate = self._estimate_messages_tokens
        excess = estimate(messages) - self.config.target_tokens
        if selected:
            excess -= estimate([messages[i] for i in sorted(selected)])
        if excess <= 0:
            return set()

        tail_start = len(messages)
        units = 0
        i = len(messages) - 1
        while i >= 0 and units < max(1, self.config.tool_result_keep_last):
            if i not in protected:
                tail_start = min(groups.get(i, (i,)))
                units += 1
            i = min(tail_start, i) - 1

        chosen: set[int] = set()
        for i in range(tail_start):
            if excess <= 0:
                break
            if i in protected or i in selected or i in chosen:
                continue
            unit = groups.get(i, (i,))
            if any(j in protected or j >= tail_start for j in unit):
                continue
            chosen.update(unit)
            excess -= estimate([messages[j] for j in sorted(unit)])
        return chosen

    async def _prune_by_message_count(self, result: CompactionResult) -> None:
        """Pre-Layer P: Prune oldest messages to enforce max_messages limit.

        Runs BEFORE the token-based layers to prevent excessive message counts
        that waste API overhead even when the token count is low. Measured over
        the last 1000 production compactions, this is the layer that actually
        does the work: it fired 476 times against Layer 2's 115 and Layer 3's
        zero — the turn-based layers barely apply because a "turn" is a USER
        message, and an agent run is one user message with hundreds of steps.

        It therefore also owns the recoverability of what it removes. Everything
        that leaves the view is written to the archive FIRST, so the agent can
        find it again through the retrieval tools; before that, this was the one
        place in the system that destroyed content outright.

        What stays is `_protected_positions`: system messages if
        keep_system_messages is True, the first input, what the current request
        stands on, the unsent round. Tool call/result pairs move together.
        """
        messages = result.modified_messages
        max_msgs = self.config.max_messages

        if len(messages) <= max_msgs:
            return

        # Prune down to the low-water mark, not back to the limit. Trimming to
        # exactly max_messages means the next step is over it again and prunes
        # again, and every prune rewrites the front of the conversation — so
        # the provider prompt cache was being thrown away on EVERY step once a
        # session reached the limit. A deep cut buys max_messages - target
        # quiet messages for the same single cache break.
        #
        # An absolute mark, not a distance below the limit: a distance
        # inherited by an agent with a smaller max_messages cut to the
        # protected minimum (max(1, 40 - 50)). A mark that does not fit the
        # limit only makes the cut shallower — half the limit.
        target = self.config.max_messages_prune_to
        if not 0 < target <= max_msgs:
            if target:
                logger.warning(
                    f"Pre-Layer P: max_messages_prune_to={target} does not fit "
                    f"max_messages={max_msgs}; pruning to half the limit")
            target = max(1, max_msgs // 2)
        excess = len(messages) - target

        # The breadcrumb below is itself a message. Removing exactly `excess`
        # and then adding it would land one over the target — with the target
        # at the limit, over the limit, re-triggering on every following call —
        # so pay for it here, but only when there is not already one in the
        # list to replace.
        if not any(_is_prune_notice(m) for m in messages):
            excess += 1

        logger.info(
            f"Pre-Layer P: {len(messages)} messages exceeds limit of {max_msgs}, "
            f"pruning ~{excess} oldest down to {target}"
        )
        self._take_baseline(result)

        protected = self._protected_positions(messages)
        indices_to_remove = self._select_prune_candidates(
            messages, protected, excess
        )

        pruned_count = await self._archive_then_remove(
            result, indices_to_remove, "Pre-Layer P"
        )

        result.messages_pruned = pruned_count
        result.final_tokens = self._estimate_messages_tokens(messages)

        if len(messages) > max_msgs:
            # Everything left is protected (a long leading system block, the
            # only user message, the round the model has not seen). Saying so
            # once per call beats an INFO line that reads like work was done
            # while the list never shrinks.
            logger.warning(
                f"Pre-Layer P: still {len(messages)} messages over the limit of "
                f"{max_msgs} after pruning {pruned_count} — the remainder is "
                f"protected (system messages, first/last user message, the unsent round)"
            )

        logger.info(
            f"Pre-Layer P: pruned {pruned_count} messages, "
            f"now {len(messages)} messages, {result.final_tokens:,} tokens"
        )

    def _select_prune_candidates(
        self,
        messages: list[dict[str, Any]],
        protected: set[int],
        excess: int
    ) -> set[int]:
        """Pick which indices to remove: oldest first, cheapest-to-lose first.

        Age stays the primary criterion — the tail is the agent's working set
        and must not be touched. But among the oldest candidates, a placeholder
        is strictly cheaper to drop than real content: its body already sits in
        a store, so losing it costs an address, while real content costs an
        archive write and a retrieval turn to get back.

        The candidate window is deliberately narrow: twice what we need (at
        least 20), but it ends before the newer half of what stays. A global
        sort by cheapness would reach into the recent tail and strip the
        pointers the agent is actively working with — and a deep prune needs
        more than half the list, so twice the need alone spans all of it. A
        tool-call unit at the window's edge still leaves whole.
        """
        candidates = [i for i in range(len(messages)) if i not in protected]
        stays = max(0, len(candidates) - excess)
        window = candidates[:min(max(2 * excess, 20), len(candidates) - (stays + 1) // 2)]

        def cost(i: int) -> tuple[int, int]:
            # 0/1: placeholder (body is stored elsewhere), 2: real content.
            ref = _ref_type(messages[i])
            rank = 0 if ref == TOOL_RESULT_REF_TYPE else 1 if ref else 2
            return (rank, i)

        groups = self._tool_call_groups(messages)

        indices_to_remove: set[int] = set()
        for i in sorted(window, key=cost):
            if len(indices_to_remove) >= excess:
                break
            if i in indices_to_remove:
                continue
            # Whole group at once. Selecting one member and cascading from it is
            # NOT enough: reaching a tool result first pulls in its assistant,
            # and the assistant's OTHER results are then never expanded, because
            # the loop skips an index it already holds. The old code got away
            # with a per-index cascade only because it walked strictly ascending
            # and so always met the assistant before its results — the cost sort
            # inverts exactly that order, and preferentially so.
            group = groups.get(i, (i,))
            # `protected` holds tool results too — the unsent round — so a group
            # can be partly protected: its assistant call sits before the round,
            # its results inside it. Evicting the closure would take the call and
            # orphan the results. Skipping the whole group keeps BOTH invariants
            # — nothing protected leaves, and no pair is split.
            if any(j in protected for j in group):
                continue
            indices_to_remove.update(group)

        return indices_to_remove

    @staticmethod
    def _tool_call_groups(
        messages: list[dict[str, Any]],
    ) -> dict[int, frozenset[int]]:
        """Map each index to the tool-call unit it belongs to.

        An assistant with parallel tool_calls plus ALL of its results is one
        indivisible unit: half a pair is a 400 from every provider, and the
        broken list is then persisted and re-sent on every following step.
        Every layer that moves or removes messages takes its units from here.

        Indices with no tool involvement are absent — callers treat that as a
        group of one. Groups hold only assistant/tool indices, so a group can
        never contain a protected system or user message.
        """
        # Union-find, NOT one set per assistant. A per-assistant dict is
        # last-writer-wins, and two assistants that share a tool_call id produce
        # overlapping units: the second write leaves the first unit's exclusive
        # members pointing at a stale set, and removing the shared part strands
        # them. Reproduced with a two-call assistant and a later one-call
        # assistant reusing an id — the result was a tool message with no
        # tool_call. Colliding ids are not hypothetical: the Gemini batch client
        # mints `call_{name}_{index}`, which repeats across assistant turns.
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_a] = root_b

        # A result belongs to the most recent assistant that issued its id —
        # by position, not by id alone. Linking every message that shares an id
        # merged the colliding turns into one unit: Layer 3 then dragged a young
        # assistant out with an old one and left that assistant's other result
        # behind as a tool message without its call.
        issued_by: dict[str, int] = {}
        for i, msg in enumerate(messages):
            role = msg.get("role")
            if role == "tool":
                owner = issued_by.get(msg.get("tool_call_id") or "")
                if owner is not None:
                    union(owner, i)
                continue
            if role != "assistant" or not msg.get("tool_calls"):
                continue
            find(i)  # an assistant with tool_calls is always its own component
            ids = [tc.get("id") for tc in msg.get("tool_calls") or []
                   if isinstance(tc, dict) and tc.get("id")]
            for tc_id in ids:
                issued_by[tc_id] = i
            if not ids:
                # tool_calls without usable ids: nothing links the results to
                # this message, so both halves would be free to move apart.
                # Absorb the contiguous run of tool messages that follows —
                # the shape every provider emits — instead of splitting a pair.
                for j in range(i + 1, len(messages)):
                    if messages[j].get("role") != "tool":
                        break
                    union(i, j)

        components: dict[int, set[int]] = {}
        for index in list(parent):
            components.setdefault(find(index), set()).add(index)

        groups: dict[int, frozenset[int]] = {}
        for members in components.values():
            unit = frozenset(members)
            for member in members:
                groups[member] = unit
        return groups

    async def _archive_then_remove(
        self,
        result: CompactionResult,
        indices_to_remove: set[int],
        caller: str,
    ) -> int:
        """Store the selected messages, then delete them. Returns how many went.

        The ONLY way a message may leave the conversation. Both callers used to
        carry their own copy of this sequence and they disagreed on the one
        thing that matters: Pre-Layer P archived first, Layer 3 deleted outright.
        A single path means a new layer cannot get that wrong by omission.

        Zero means nothing was removed — the archive write failed and the
        messages are still in the list. That order is deliberate: the write is
        all-or-nothing (one malformed message aborts the batch), so deleting
        anyway would destroy the whole batch while the breadcrumb promises they
        can be looked up. An over-long context is a cost; destroyed content is
        not recoverable, and the remaining layers still run.
        """
        if not indices_to_remove:
            return 0
        messages = result.modified_messages

        snapshot = list(messages)
        selected = [messages[i] for i in sorted(indices_to_remove)]

        # The archive holds text only. Media still inline in a message that is
        # about to leave would go nowhere: Pre-Layer P runs before every other
        # media pass, and Layer 3 meets whatever Layer 1 left inline. Save it to
        # disk first; the hint with its path is text and is archived with it.
        # On COPIES: eviction rewrites a message in place, and when the archive
        # write fails the messages stay in the conversation — with their media,
        # not with a hint claiming it was archived.
        leaving = CompactionResult(original_tokens=0, final_tokens=0, tokens_saved=0,
                                   modified_messages=copy.deepcopy(selected))
        picks = [
            _MediaPick(msg_idx, item_idx, item, in_mm, self._media_subject(item),
                       "- archived with its message.")
            for msg_idx, item_idx, item, in_mm in self._iter_media(leaving.modified_messages)
        ]
        if picks:
            await self._evict_media(leaving, picks, store=True)

        # Placeholders are skipped inside: their body is already in a store, and
        # archiving a pointer would only produce a pointer to a pointer.
        if not await self._archive_pruned(leaving.modified_messages, caller):
            logger.warning(
                f"{caller}: skipping the removal of {len(selected)} messages — "
                f"the archive write failed and dropping them would destroy them"
            )
            return 0
        result.media_bytes_saved += leaving.media_bytes_saved

        for idx in sorted(indices_to_remove, reverse=True):
            del messages[idx]

        # Note: _ensure_valid_message_sequence rebuilds the tool-call units at
        # each iteration, since indices move as it deletes.
        extra = self._ensure_valid_message_sequence(messages, caller)

        # That pass deletes on its own account, so ask the list what actually
        # went rather than trusting the selection. Identity, not equality:
        # duplicate contents are common and nothing here rewrites a message.
        survivors = {id(m) for m in messages}
        removed = [m for m in snapshot if id(m) not in survivors]
        if extra:
            # These are already gone — the sequence fix deletes to make the
            # request valid at all, and that cannot be undone. Archive what we
            # can and say so if it fails.
            already = {id(m) for m in selected}
            extra_messages = [m for m in removed if id(m) not in already]
            # Their media needs a disk copy as much as the selection's did.
            # They are out of the list already, so the eviction works on a
            # list of their own; the dicts are the same objects that get archived.
            loose = CompactionResult(original_tokens=0, final_tokens=0, tokens_saved=0,
                                     modified_messages=extra_messages)
            loose_picks = [
                _MediaPick(msg_idx, item_idx, item, in_mm, self._media_subject(item),
                           "- archived with its message.")
                for msg_idx, item_idx, item, in_mm in self._iter_media(extra_messages)
            ]
            if loose_picks:
                await self._evict_media(loose, loose_picks, store=True)
                result.media_bytes_saved += loose.media_bytes_saved
            if not await self._archive_pruned(extra_messages, caller):
                logger.error(
                    f"{caller}: {extra} messages removed by the sequence fix "
                    f"could not be archived and are lost"
                )

        self._leave_prune_notice(messages, len(removed))
        return len(removed)

    async def _archive_pruned(self, removed: list[dict[str, Any]], caller: str) -> bool:
        """Write pruned messages to the archive. True when they are safe to drop.

        Placeholders are skipped — their body is already stored and archiving
        one would store a pointer, not content.

        Never raises: bookkeeping must not sink the compaction it belongs to.
        But it does REPORT, and the caller must not delete on False: the write
        is all-or-nothing (one malformed message aborts the batch), so a
        swallowed failure would delete every message of that prune while the
        breadcrumb still promises they can be looked up. Keeping an over-long
        context is recoverable; destroying the content is not — and the
        token-based layers remain as the backstop.
        """
        # Retrieval answers are exempt for the same reason Layer 1 exempts them
        # (see RETRIEVAL_MARKER): their body is content the agent just pulled OUT
        # of storage, so putting it back is a loop with no exit. Measured live
        # before this guard: the archive filled with the agent's own list()
        # answers, the next search ranked those above the original message, and
        # the agent followed refs to its own earlier replies until it gave up.
        # Removing them from the conversation is still fine — they are
        # re-derivable by asking again.
        payload = [m for m in removed
                   if _ref_type(m) is None and not _is_retrieval_result(m)]
        if not payload:
            return True

        # Embedding is the whole cost of a large batch: 4682 messages take 0.08 s
        # as rows and 80 s with the vector index. In steady state that never
        # matters (the largest of 1000 production prunes was 11 messages), but a
        # model that emits a runaway tool_call batch produces exactly the huge
        # first prune where a stall would hurt most. Past the cap the rows go in
        # here and the embedding follows in a background task.
        try:
            await self._store_batch(payload, caller)
            return True
        except Exception as e:  # noqa: BLE001 - see docstring
            logger.error(
                f"{caller}: archiving {len(payload)} pruned messages failed, "
                f"keeping them in the conversation instead of destroying them: {e}"
            )
            return False

    async def _store_batch(self, payload: list[dict[str, Any]], caller: str) -> list[str]:
        """Archive a batch in ONE transaction; ids in input order.

        The embedding goes inline while the batch is small and into the
        background past the cap -- too large to embed inside this request, but
        NOT a reason to leave it out of the index: a half-indexed archive answers
        every similarity search without saying which half it searched.

        One transaction is also what makes a failure clean. Writing row by row
        committed the rows before the one that failed; the caller then kept the
        messages, and the next compaction archived them AGAIN -- every retry a
        second copy in the archive and a second hit in every search. (The
        rollback that makes a failed WRITE clean too is in _store_many_rows.)

        A hook timeout inside this call cannot be rolled back -- the worker
        thread commits regardless, and the placeholders are lost with the pass.
        The ids are derived from the messages for that (see _entry_id): the
        next pass writes onto the same rows instead of beside them.
        """
        if len(payload) <= _SEMANTIC_INDEX_MAX_BATCH:
            return await asyncio.to_thread(self.archival_memory.store_many, payload, None)
        ids, documents, metadatas = await asyncio.to_thread(
            self.archival_memory.store_many_unindexed, payload, None)
        if self.archival_memory.enable_semantic_search:
            self._index_in_background(ids, documents, metadatas, caller)
        return ids

    def _index_in_background(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]],
        caller: str,
    ) -> None:
        """Embed a large archived batch after the request it belongs to.

        In chunks, and each chunk in a thread: embedding is ~17 ms per message,
        so a runaway batch is a minute of work that must not sit in the hook's
        budget and must not hold the event loop either. The task is kept in a
        set -- an unreferenced task can be garbage collected mid-run -- and a
        failure costs similarity search on those entries, never the content.

        It starts at the next suspension point, which can still be inside this
        same compaction; "background" means "not in the caller's critical path",
        not "after the response". What it does NOT survive is the loop closing
        under it, and it says so in the log when that happens.
        """
        async def run() -> None:
            done = 0
            try:
                # One batch at a time across the whole process. The vector
                # store holds its lock across the embedding, and every chunk
                # occupies a default-executor thread while it waits -- the same
                # pool every foreground `to_thread` uses, including other
                # sessions' archiving. Unbounded, a handful of runaway prunes
                # would move the stall from this request into theirs.
                chunk = _SEMANTIC_INDEX_MAX_BATCH   # read once: two reads can disagree
                async with _index_slot():
                    for start in range(0, len(ids), chunk):
                        end = start + chunk
                        if not self.archival_memory.is_open:
                            logger.info("%s: indexing of %d archived messages stopped at "
                                        "%d -- the session was closed", caller, len(ids), done)
                            return
                        indexed = await asyncio.to_thread(
                            self.archival_memory.index_batch,
                            ids[start:end], documents[start:end], metadatas[start:end])
                        if not indexed and not self.archival_memory.is_open:
                            # Asked AGAIN, because a chunk is seconds long and
                            # the loop is free during it: an eviction lands
                            # inside the chunk, and index_batch then reports a
                            # refused batch because the store is gone. Blaming
                            # the vector store for a shutdown is what the check
                            # above exists to avoid -- once before is not enough.
                            logger.info("%s: indexing of %d archived messages stopped at "
                                        "%d -- the session was closed", caller, len(ids), done)
                            return
                        # The text of a finished chunk is not needed again, and
                        # the whole payload is held by this closure until the
                        # task ends -- which, behind the one slot, can be
                        # several batches' worth of waiting.
                        # ponytail: frees the RUNNING task's text as it goes;
                        # queued tasks still hold theirs. Re-read the rows by id
                        # instead if a host ever queues enough to matter.
                        documents[start:end] = [""] * (end - start)
                        if not indexed:
                            # index_batch reports rather than raises: the rows
                            # are committed, so this costs similarity search on
                            # the rest of the batch and nothing else. Saying
                            # "indexed" here is what made the old skip
                            # invisible.
                            logger.error(
                                "%s: indexing stopped after %d of %d archived messages; "
                                "the rest is readable by ref and found by a filter whose "
                                "every word it contains, not by meaning", caller, done, len(ids))
                            return
                        done = min(end, len(ids))
            except asyncio.CancelledError:
                # A one-shot CLI run cancels every pending task when its loop
                # closes (agent_run, agent-cli, chat), and CancelledError is a
                # BaseException -- caught here only to leave a record, because
                # silence would restore exactly the half-indexed archive this
                # path exists to prevent.
                logger.warning(
                    "%s: indexing of %d archived messages was cancelled after %d (process "
                    "ending?); the rest is readable by ref and found by a filter whose "
                    "every word it contains, not by meaning", caller, len(ids), done)
                raise
            except Exception as exc:  # noqa: BLE001 — the rows are committed
                logger.error("%s: indexing %d archived messages failed at %d (they are "
                             "readable by ref, and found by a filter whose every word they "
                             "contain): %s", caller, len(ids), done, exc)
                return
            logger.info("%s: indexed %d archived messages in the background", caller, done)

        task = asyncio.create_task(run())
        self._index_tasks.add(task)
        task.add_done_callback(self._index_tasks.discard)
        logger.info("%s: %d archived messages are being indexed in the background; they "
                    "are already readable by ref and found by their words", caller, len(ids))

    def _leave_prune_notice(
        self, messages: list[dict[str, Any]], removed: int
    ) -> None:
        """Leave exactly ONE breadcrumb saying that older turns left the view.

        Constant cost regardless of how much went, and it is the only thing
        telling the agent there is something to look up at all — without it a
        bulk removal is indistinguishable from a conversation that never had
        those turns.
        """
        if removed <= 0:
            return

        # Replace, never stack. ALL existing notices go, not just the first:
        # a second one can arrive through a merged or restored history, and
        # removing only one leaves a permanent pair with disagreeing totals.
        # The carried total is the MAXIMUM of the notices found, not their sum.
        # Duplicates only ever arise from one lineage — a merged or restored
        # history where the same running count appears twice at different ages —
        # so summing would double-count the shared part of a counter whose only
        # job is not to drift.
        carried = 0
        for i in range(len(messages) - 1, -1, -1):
            if not _is_prune_notice(messages[i]):
                continue
            try:
                previous = json.loads(messages[i].get("content") or "{}")
                carried = max(carried, int(previous.get("total_removed") or 0))
            except (ValueError, TypeError):
                pass
            del messages[i]
        total = removed + carried

        insert_at = len(messages)
        for i, msg in enumerate(messages):
            if msg.get("role") != "system":
                insert_at = i
                break

        messages.insert(insert_at, {
            "role": "system",
            "content": json.dumps({
                "type": PRUNE_NOTICE_TYPE,
                "total_removed": total,
                # No promise that all N sit in the history archive: the count
                # includes placeholders whose bodies live in the tool-result
                # store instead. "Retrievable" is true for all of them.
                # Every call named here must WORK. This said "section='history'
                # or 'all'", and list(section='all') without a filter is an
                # error by design — so the one message telling the agent where
                # its history went also handed it a call that fails.
                "hint": (
                    f"{total} earlier messages of this conversation were moved "
                    f"out of view to keep it within limits. They are stored, "
                    f"not gone: list(section='history') shows the most recent "
                    f"of them, list(filter='...') searches everything, and "
                    f"read(ref=...) fetches one."
                )
            })
        })

    def _ensure_valid_message_sequence(
        self, 
        messages: list[dict[str, Any]], 
        caller: str
    ) -> int:
        """Ensure first non-system message is 'user' to satisfy Gemini requirements.
        
        Gemini requires: user -> assistant (with tool_calls) -> tool responses
        If first non-system msg is assistant/tool, we remove until we hit a user msg.
        
        CRITICAL: This function NEVER removes all user messages. At least one user
        message must remain for a valid API request. If only one user message is
        left and it's not at the start, we add a minimal fallback user message.
        
        Note: This method rebuilds the tool-call units at each iteration since indices change after deletions.
        
        Args:
            messages: List of messages (modified in place)
            caller: Name of calling function for logging
            
        Returns:
            Number of additional messages removed
        """
        extra_removed = 0
        
        while messages:
            # Rebuilt each iteration: indices move after deletions
            groups = self._tool_call_groups(messages)

            first_non_system_idx = None
            for i, msg in enumerate(messages):
                if msg.get("role") != "system":
                    first_non_system_idx = i
                    break
            
            if first_non_system_idx is None:
                break  # Only system messages left
            
            first_msg = messages[first_non_system_idx]
            if is_input(first_msg):
                # Good -- the conversation opens on something the model can be
                # asked to answer. `is_input`, not the bare role: a woken run
                # opens with a `developer` wake, which every route that needs a
                # user turn first lowers to one (llm/message_roles). Asking for
                # the role deleted it here, and with it every message behind it
                # until the loop found a `user` one -- the run's only
                # instruction, gone from the view mid-run.
                break

            # Check if we would remove ALL user messages by continuing
            # Count remaining user messages
            user_message_count = sum(1 for msg in messages if is_input(msg))
            
            if user_message_count == 0:
                # CRITICAL: No user messages left at all - add a fallback
                logger.warning(
                    f"{caller}: No user messages remaining after pruning! "
                    f"Adding fallback user message to prevent empty contents error."
                )
                # Insert a minimal user message at the appropriate position
                fallback_msg = {
                    "role": "user",
                    "content": "Continue with the task."
                }
                # Insert after system messages
                insert_idx = first_non_system_idx
                messages.insert(insert_idx, fallback_msg)
                break
            
            # First non-system message is not user: it goes, with the rest of
            # its tool-call unit (an assistant's results, a result's assistant).
            indices_to_remove: set[int] = {first_non_system_idx}
            indices_to_remove.update(groups.get(first_non_system_idx, ()))

            # Remove these messages
            for idx in sorted(indices_to_remove, reverse=True):
                if idx < len(messages):
                    del messages[idx]
                    extra_removed += 1
            
            logger.debug(
                f"{caller}: removed {len(indices_to_remove)} more messages "
                f"to ensure first non-system message is 'user'"
            )
        
        # Final safety check: ensure at least one user message exists
        user_message_count = sum(1 for msg in messages if is_input(msg))
        if user_message_count == 0:
            logger.warning(
                f"{caller}: Final check - no user messages! Adding fallback."
            )
            # Find position after system messages
            insert_idx = 0
            for i, msg in enumerate(messages):
                if msg.get("role") != "system":
                    insert_idx = i
                    break
            else:
                insert_idx = len(messages)
            
            fallback_msg = {
                "role": "user",
                "content": "Continue with the task."
            }
            messages.insert(insert_idx, fallback_msg)
        
        return extra_removed

    def _estimate_messages_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate total tokens in messages, aligned with LLM token estimation.

        Uses the same estimation path as context_summarizer to avoid drift,
        while skipping media items marked as compacted since they won't be
        encoded at LLM call time.
        """
        sanitized_messages: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                sanitized_messages.append(msg)
                continue

            msg_copy = dict(msg)
            for key in ("multimodal_content", "content"):
                parts = msg_copy.get(key)
                if isinstance(parts, list):
                    msg_copy[key] = [part for part in parts
                                     if not (isinstance(part, dict) and part.get("compacted"))]

            sanitized_messages.append(msg_copy)

        return estimate_token_count(sanitized_messages)
    
    @staticmethod
    def _scale_offset(result: CompactionResult) -> int:
        """How far the caller's token count lies above the message estimate.

        Tool definitions and whatever the provider counts beyond the heuristic.
        Taken against the estimate before the first rewriting layer, with the
        media savings booked until then added back, so the offset is not the
        media the passes just removed. 0 while no layer has taken its baseline.
        """
        if result.estimated_before is None:
            return 0
        return max(0, result.original_tokens - result.saved_before_baseline
                   - result.estimated_before)

    def _take_baseline(self, result: CompactionResult) -> None:
        """Estimate the messages once, before the first layer rewrites them.

        tokens_saved used to be original_tokens - final_tokens: the first is
        what the hook measured (real prompt tokens, tool definitions included),
        the second a messages-only estimate. A layer that changed nothing still
        "saved" the tool definitions — and that positive number made every such
        call count as a compaction: status line, history write, usage-tracker
        invalidation. Both sides of the difference now come from the estimate.
        Taken lazily because an estimate of a large session costs ~50 ms and the
        media-only passes, which run on every call, book their own savings.
        """
        if result.estimated_before is None:
            result.estimated_before = self._estimate_messages_tokens(result.modified_messages)
            result.saved_before_baseline = result.tokens_saved

    def _finalize(self, result: CompactionResult) -> CompactionResult:
        """Finalize compaction result."""
        if result.estimated_before is not None:
            result.tokens_saved = result.saved_before_baseline + max(
                0, result.estimated_before - self._estimate_messages_tokens(result.modified_messages))
        # else: only media passes ran, and they booked their savings themselves.
        # Reported on the scale of original_tokens, so the two stay comparable.
        result.final_tokens = max(0, result.original_tokens - result.tokens_saved)

        # THE INVARIANT (utils/reasoning_artifacts.py): provider reasoning
        # artifacts (OpenAI encrypted reasoning items, Gemini thought
        # signatures) are integrity-protected over the EXACT history that
        # produced them. If this compaction pass mutated the history in ANY
        # way — tool results swapped for refs, attached files stored away,
        # media evicted, messages archived/dropped/pruned — those artifacts
        # are stale and will fail provider verification on a later turn
        # (HTTP 400 "encrypted content … could not be verified", deep into a
        # run). Invalidate them HERE, at the mutation site, so the chain
        # resets deterministically instead of failing reactively.
        mutated = (
            result.tool_results_stored
            + result.messages_archived
            + result.messages_dropped
            + result.media_deduplicated
            + result.media_compacted_after_event
            + result.media_always_compacted
            + result.messages_pruned
        ) > 0 or result.media_bytes_saved > 0
        if mutated:
            # Only from the first changed message on. An assistant turn before
            # it was produced by a history that is still byte-identical, so its
            # artifacts still verify. Stripping all of them turned the eviction
            # of one image near the end into a rewrite of every replayed turn
            # (the Responses client sends them verbatim): measured, the break
            # moved from message 101 to message 2 and re-billed 10x as much.
            start = 0
            if result.shapes_before is not None:
                first = _first_changed_index(result.shapes_before, result.modified_messages)
                start = 0 if first is None else first
            invalidated = invalidate_reasoning_artifacts(result.modified_messages, start=start)
            if invalidated:
                logger.info(
                    f"Compaction mutated history -> invalidated reasoning "
                    f"artifacts on {invalidated} message(s)"
                )

        logger.info(
            f"Compaction complete: {result.original_tokens} -> {result.final_tokens} tokens "
            f"({result.reduction_percent:.1f}% reduction), "
            f"layers applied: {result.layers_applied}"
        )

        return result
    
    async def get_restoration_context(self) -> str:
        """Generate context section explaining how to restore information.

        This is added to the system prompt so the LLM knows how to access
        stored/archived information.

        KEEP THIS BYTE-STABLE. The block is inserted directly after the
        system prompt (hooks.py), so it stands BEFORE the whole conversation:
        any change to it invalidates the provider prompt cache for EVERYTHING
        behind it. Counters like "There are 47 stored tool results" change
        with every offloaded tool result -- measured on a multi-turn run:
        prefix break at message 50 of 197, cache rate 8-13 % instead of
        50-65 %. The numbers do not guide the model's actions either: it
        reacts to the reference IN the text, not to a total. So only constant
        descriptions here, nothing that moves per turn.

        The variables section was exactly such a violation: it listed EVERY
        variable created, with name and summary, so it changed with each new
        one -- and invalidated the cache for the whole conversation behind
        it. It is gone with the $VAR substitution.

        Returns:
            System prompt section
        """
        sections = []

        # Tool result references - wrap sync SQLite operation
        tool_stats = await asyncio.to_thread(self.tool_store.get_stats)
        if tool_stats["total_entries"] > 0:
            sections.append(
                f"{TOOL_RESULTS_SECTION}\n"
                "Some tool results have been stored externally. When you see a JSON "
                "reference with `type: tool_result_ref`, use list(section='tool_results') "
                "to see what it contains, then read(ref=ref_id, find=\"...\") for just "
                "the matching part, or read(ref=ref_id) to page through it."
            )
        
        # Archival memory - wrap sync SQLite operation
        archive_stats = await asyncio.to_thread(self.archival_memory.get_stats)
        if archive_stats["total_messages"] > 0:
            sections.append(
                "## Conversation Archive\n"
                "Older messages have been archived. When you see a JSON reference "
                "with `type: archived_ref`, read(ref=ref_id) returns that message; "
                "list(section='history') shows what else is back there."
            )
        
        # Core memory
        core_section = self.core_memory.to_system_prompt_section()
        if core_section:
            sections.append(core_section)
        
        if not sections:
            return ""
        
        return (
            "\n# Context Engineer - Stored Information\n\n"
            "Some information has been stored externally to save context space. "
            "Use the provided tools to retrieve full content when needed.\n\n"
            + "\n\n".join(sections)
        )
