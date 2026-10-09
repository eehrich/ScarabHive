"""Derivation of the OpenAI ``prompt_cache_key`` from the prompt prefix.

GPT-5.6+ practically only matches the prompt cache when a
``prompt_cache_key`` is set (OpenAI docs, measured: byte-identical
10k prefix, 0 cached_tokens without a key). The key is a routing hint:
requests with the same key land on the same cache shard,
~15 req/min per key sustained.

A static key per agent collides as soon as several jobs/stories run in
parallel: different prefixes then share one shard (eviction thrash) and
blow the rate limit. Session IDs are no good either — the pipeline
creates new sessions per step, the cache group would be torn apart.

Solution ``prompt_cache_key: "auto"`` — the key is hashed at runtime
from:

1. the LEADING system/developer messages, in full — except the
   INJECTED ones (``injected_by`` set). The system prompt is constant
   per agent; hashing it in full is deterministic and prevents a long
   system prompt (>4k) from eating up the window before run-specific
   content becomes visible. An injected block there is the opposite of
   constant: it is rebuilt on every call, and hashed along, the key
   would move to a new shard with every checked-off todo item. The real
   remedy is the POSITION — a block at the end leaves the prefix before
   it byte-identical —, this rule covers what still sits in the head.

   ⚠️ The converse: what is marked no longer carries identity.
   An injected block that would have different text per run/story
   (e.g. a ``simple_prompt_inject`` with a run-specific
   ``template_vars`` variable) drops out of the key and puts two
   runs on the same shard. What distinguishes one run from another
   belongs in the system prompt or in the task message.
2. the FIRST non-system message, truncated to ``PREFIX_CHARS`` characters.
   Only the first: turns of the same session appended later never
   change the key — all calls of a conversation stay in the same
   cache group. Injected messages are skipped here too: a note behind the
   system prompt is the same for every run and names no run. Only when no
   user task follows does the first injected one count, as before.

This automatically gives: same stable prefix <-> same key.

- Agents with a run-specific block early in the task: the key is
  implicitly per run — parallel runs do not collide.
- Agents with a generic kickoff task: the key degenerates to the agent key
  (system hash) — exactly the behaviour of the static key, no loss.

PREFIX_CHARS = 4096 characters corresponds roughly to the minimum cacheable
unit of 1024 tokens: whoever shares the system prompt + this task start
belongs in the same cache group; what only diverges later does not
separate the keys, on purpose.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterator

PROMPT_CACHE_KEY_AUTO = "auto"
PREFIX_CHARS = 4096

_SYSTEM_ROLES = {"system", "developer"}

# --- Explicit cache breakpoints (GPT-5.6+) ----------------------------------
#
# Mapped empirically (2026-07-21, 15 experiments via OpenRouter /responses):
# Implicit 5.6 caching ONLY matches exact repetitions and conversation
# continuations — a request that shares a long prefix and then diverges in the
# middle of the last item ALWAYS caches 0 (even with 29k tokens of identical
# prefix). Fix per OpenAI docs: `prompt_cache_breakpoint` on the content part
# marks the end of a reusable prefix; hit = longest prefix made of
# byte-identical COMPLETE breakpoint blocks. Max 4 cache writes
# per request (the implicit breakpoint takes a slot -> max 3 explicit).
#
# Pipelines mark block boundaries in the task text with this sentinel; the
# OpenAI-capable clients split on it into content parts with breakpoint
# markers, all other provider paths STRIP the sentinel without residue.
CACHE_BP_SENTINEL = "\n<<<CACHE_BREAKPOINT>>>\n"
MAX_EXPLICIT_BREAKPOINTS = 3


def split_cache_breakpoint_blocks(text: str) -> list[str]:
    """Split text at CACHE_BP_SENTINEL into blocks (the sentinel is dropped).

    At most MAX_EXPLICIT_BREAKPOINTS boundaries are kept; further
    sentinels are merged into the last block. Empty blocks (sentinel
    at the start/end, double sentinel) are dropped. Without a sentinel: [text].
    """
    if CACHE_BP_SENTINEL not in text:
        return [text]
    raw = text.split(CACHE_BP_SENTINEL)
    blocks = [b for b in raw if b]
    if not blocks:
        return [""]
    if len(blocks) > MAX_EXPLICIT_BREAKPOINTS + 1:
        head = blocks[:MAX_EXPLICIT_BREAKPOINTS]
        tail = "".join(blocks[MAX_EXPLICIT_BREAKPOINTS:])
        blocks = head + [tail]
    return blocks


def strip_cache_breakpoints(text: str) -> str:
    """Remove the sentinel without residue (for providers without breakpoint support)."""
    if CACHE_BP_SENTINEL not in text:
        return text
    return "".join(text.split(CACHE_BP_SENTINEL))


# --- Segment ladder -----------
#
# Task-sequence agents (many single calls, growing shared prefix)
# declare in the task: [static] S [append_only] S [volatile]. The client
# adds a third marker (BP1) from a process registry at the
# append_only boundary of the PREDECESSOR call — its stored prefix is
# byte-identical -> read hit from call 2. BP2 (declared append end)
# writes the longer prefix for the follow-up call; BP0 (static end) makes
# prefix breaks cheap. Live validated (probe L): hit from call 2, writes only
# the delta, break = 1 miss + immediate relearn.

#: Marker styles per model (LLMModelConfig.prompt_cache_marker_style)
MARKER_STYLE_OPENAI = "openai"        # prompt_cache_breakpoint (GPT-5.6+)
MARKER_STYLE_ANTHROPIC = "anthropic"  # cache_control ephemeral
MARKER_STYLE_NONE = "none"            # strip markers (deepseek/gemini/...)

#: prompt_cache_mode values (AgentConfig, docs §4)
CACHE_MODE_AUTO = "auto"
CACHE_MODE_MULTI_TURN = "multi_turn"
CACHE_MODE_TASK_SEQUENCE = "task_sequence"
CACHE_MODE_ONE_SHOT = "one_shot"
CACHE_MODE_OFF = "off"

#: Minimum ladder growth: below ~1024 tokens (4096 characters) the
#: next call cannot form a cacheable read of its own on the delta.
LADDER_MIN_PREFIX_CHARS = 4096


class CacheBoundaryRegistry:
    """Process-global registry for the CUMULATIVE segment ladder.

    GPT-5.6 match rule (mapped end-to-end 2026-07-21): a stored
    breakpoint prefix only hits if the new request reproduces the block AND
    marker structure of its predecessor up to that point EXACTLY; only
    APPENDING is allowed. A moving or removed marker breaks all reads
    anchored behind it (measured: constantly only the static hit, or 0%).
    The API accepts 6+ markers without trouble — the limit of 4 only applies
    to NEW cache writes per request (ours are <=2).

    Therefore the registry stores the RUNG LIST per key (offsets in the
    append_only block): every call reproduces all previous rungs as
    marked blocks and appends at most one new one at the end
    (MIN_RUNG_CHARS thins them out). Validation by hash up to the last rung;
    break -> list reset, relearn from the next call.

    The commit happens at PLAN time: correct when sequential, in parallel it costs
    at most one miss with self-healing — never correctness.
    """

    _MAX_KEYS = 128  # > parallel jobs x GPT task-sequence agents (~10)
    #: New rung only from this growth on (~1024 tokens) — thins out the
    #: marker count; between rungs calls repeat exactly the same structure.
    MIN_RUNG_CHARS = 4096
    #: Safety cap: old rungs must NEVER be removed (breaks the
    #: structure reproduction), so appending stops — growth then stays
    #: in the unmarked tail, hits up to the last rung remain stable.
    MAX_RUNGS = 64

    def __init__(self) -> None:
        self._entries: dict[str, tuple[list[int], str]] = {}
        self._order: list[str] = []  # LRU, oldest first

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]

    def rungs_for(self, key: str, append_text: str) -> list[int]:
        """Valid rung list for the current append block (else [])."""
        entry = self._entries.get(key)
        if not entry:
            return []
        rungs, digest = entry
        if not rungs:
            return []
        last = rungs[-1]
        if last <= len(append_text) and self._digest(append_text[:last]) == digest:
            return rungs
        return []

    def commit(self, key: str, append_text: str, rungs: list[int]) -> list[int]:
        """Advance the rung list (plan time) and return it.

        Appends a new rung at the append end if it has grown by
        at least MIN_RUNG_CHARS since the last one.
        """
        if not append_text:
            return rungs
        last = rungs[-1] if rungs else 0
        if (
            len(rungs) < self.MAX_RUNGS
            and (len(append_text) - last >= self.MIN_RUNG_CHARS or not rungs)
        ):
            rungs = rungs + [len(append_text)]
        self._entries[key] = (rungs, self._digest(append_text[:rungs[-1]]))
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        while len(self._order) > self._MAX_KEYS:
            evicted = self._order.pop(0)
            self._entries.pop(evicted, None)
        return rungs


#: Module-wide registry — agent/client are process-wide singletons, the
#: ladder needs the state across client instances.
boundary_registry = CacheBoundaryRegistry()


def plan_cache_blocks(
    text: str,
    *,
    mode: str | None,
    key: str | None,
    max_markers: int = MAX_EXPLICIT_BREAKPOINTS,
    registry: CacheBoundaryRegistry | None = None,
) -> list[tuple[str, bool]] | None:
    """Translate sentinel text into a (block, marked?) list.

    Returns None = no sentinel contained (the caller leaves the content
    untouched). mode=off -> strip sentinels, no markers.
    mode=task_sequence + registry -> ladder: additional BP1 split at
    the predecessor boundary in the append block (= middle declared block
    for [static]S[append]S[volatile]) + plan-time commit of the new boundary.
    Budget: at most ``max_markers`` marked blocks; assigned from
    the BACK (BP2 write anchor > BP1 read anchor > BP0), so that with a tight
    budget (Anthropic: system/tool markers count too) the valuable
    anchors survive.
    """
    if CACHE_BP_SENTINEL not in text:
        return None
    if mode == CACHE_MODE_OFF:
        return [(strip_cache_breakpoints(text), False)]
    blocks = split_cache_breakpoint_blocks(text)
    if len(blocks) < 2:
        return [(blocks[0] if blocks else "", False)]

    # Cumulative ladder (task_sequence only, only with exactly 2 declared
    # boundaries [static]S[append]S[volatile] — more declared boundaries =
    # the pipeline knows better):
    if (
        mode == CACHE_MODE_TASK_SEQUENCE
        and registry is not None
        and key
        and len(blocks) == 3
    ):
        static_text, append_text, volatile_text = blocks
        rungs = registry.rungs_for(key, append_text)
        rungs = registry.commit(key, append_text, rungs)
        # Structure: [static(BP)] + one marked slice per rung + the
        # unmarked rest (append tail behind the last rung + volatile
        # MERGED — the moving append end must NEVER be marked,
        # otherwise the structure reproduction breaks on the next call).
        out: list[tuple[str, bool]] = [(static_text, True)]
        prev_r = 0
        for r in rungs:
            out.append((append_text[prev_r:r], True))
            prev_r = r
        out.append((append_text[prev_r:] + volatile_text, False))
        # Filter out empty slices (rung exactly at the end), order stable
        return [(blk, m) for blk, m in out if blk]

    # Without the ladder: mark all declared boundaries; assign the budget from
    # the BACK (sacrifice BP0 first — relevant for Anthropic, where system/
    # tool markers already occupy 2 of the 4 slots).
    n_boundaries = len(blocks) - 1
    marked_from = max(0, n_boundaries - max_markers)
    return [
        (blk, marked_from <= i < n_boundaries)
        for i, blk in enumerate(blocks)
    ]


def _key_field(msg: Any, name: str) -> Any:
    """A field of a message, dict or ChatMessage.

    The key is derived from the ORIGINAL messages, not from the finished
    payload: a system block carries no ``injected_by`` there (httpx drops it at
    its rung step, the Responses client builds new items and keeps it on
    developer items only), and without the marker the derivation cannot tell a
    block rebuilt on every call from a prompt.
    """
    if isinstance(msg, dict):
        return msg.get(name)
    return getattr(msg, name, None)


def _iter_msg_texts(msg: Any) -> Iterator[str]:
    """Text fragments of ONE message (role marker + text parts).

    Understands both formats:
    - Chat Completions: ``{"role": ..., "content": str | [{"type": "text",
      "text": ...}, ...]}``
    - Responses API input items: ``{"role": ..., "content":
      [{"type": "input_text"|"output_text", "text": ...}]}`` as well as
      function_call items (name/arguments/output).

    Non-text parts (images/audio) are skipped; the role is included
    so that role boundaries influence the hash.
    """
    role = _key_field(msg, "role")
    if role:
        yield f"<{role}>"
    content: Any = _key_field(msg, "content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for part in content:
            # Dict OR pydantic model: ChatMessage.content is
            # List[ContentItem], the parts there are TextContent/ImageContent,
            # not dicts. Reading dicts only left just the role marker of such
            # a message — the key was the same for EVERY agent with
            # list content, i.e. one shared shard instead of one
            # per run.
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if isinstance(text, str):
                yield text
    # Responses-API function_call / function_call_output items
    for key in ("name", "arguments", "output"):
        val = _key_field(msg, key)
        if isinstance(val, str) and val:
            yield val


def derive_prompt_cache_key(configured: str, messages: list) -> str:
    """Determine the effective prompt_cache_key.

    Static values pass through unchanged (explicit override).
    ``"auto"`` -> ``auto-<sha256[:16]>`` over the leading system messages
    (in full) + the first non-system message (truncated to PREFIX_CHARS) —
    details in the module docstring.
    """
    if configured != PROMPT_CACHE_KEY_AUTO:
        return configured
    h = hashlib.sha256()
    first: Any = None      # the message that names the run
    injected: Any = None   # the first injected one, if no task follows it
    for msg in messages:
        # Whatever is not a message is skipped — as before, except that a
        # ChatMessage object is now one. Without the second half such a
        # foreign body falls into the branch below, hashes nothing and BREAKS:
        # the task message after it would no longer enter the key, and two
        # runs would share a shard.
        if not isinstance(msg, dict) and not hasattr(msg, "role"):
            continue
        role = str(_key_field(msg, "role") or "")
        if role in _SYSTEM_ROLES:
            # Not an injected block. Whoever puts one into the leading run
            # rebuilds it on every step -- the todo list, the restored
            # context, a rendered prompt block. Hashed along, the key moved
            # with the text: every tick of a checkbox sent the run to a fresh
            # shard, so not even the system prompt in FRONT of the block could
            # be read back. The key answers "which prefix is this", and a text
            # that is rebuilt per call is not part of any prefix. Measured:
            # same agent, two todo states, two keys.
            #
            # An UNMARKED block still counts -- nobody rebuilds it, and it
            # really is a different prompt.
            if _key_field(msg, "injected_by"):
                continue
            for frag in _iter_msg_texts(msg):
                h.update(frag.encode("utf-8", "replace"))
            continue
        if _key_field(msg, "injected_by"):
            # The same rule outside the head: a note injected as a user
            # message right behind the system prompt (simple_prompt_inject,
            # after_system) is the same text for every run of the agent.
            # Taken for "the first message", it put every book on one shard.
            injected = injected if injected is not None else msg
            continue
        # A run whose only task is injected (forge news on a woken session, a
        # summary heading a compacted chat) keeps it: hashing the answer
        # behind it put those sessions on one key and moved it after call 1.
        first = msg if role == "user" or injected is None else injected
        break
    else:
        first = injected
    # First non-system message: truncated window, then stop —
    # later messages (also system injections pushed in afterwards) are
    # not part of the stable prefix.
    taken = 0
    for frag in _iter_msg_texts(first) if first is not None else ():
        if taken >= PREFIX_CHARS:
            break
        piece = frag[: PREFIX_CHARS - taken]
        h.update(piece.encode("utf-8", "replace"))
        taken += len(piece)
    return f"auto-{h.hexdigest()[:16]}"


# --- Anthropic cache_control (ephemeral breakpoints) -------------------------
#
# SHARED single source of truth for ALL Claude paths (native Anthropic SDK,
# Anthropic-via-OpenRouter in the httpx client, future Responses client). Anthropic
# caches everything UP TO AND INCLUDING a ``cache_control`` marker; ONE breakpoint
# at the prefix end covers the whole prefix before it. Hard limit: at most
# ANTHROPIC_MAX_CACHE_BLOCKS blocks with cache_control per request (system +
# tools + messages TOGETHER) — exceeding it = HTTP 400.
#
# Each client brings its own message format (chat-completions dicts
# vs. native Anthropic blocks vs. Responses input_items); the POLICY (what, how
# often, in which order to mark) is identical and lives here. The
# clients only compose the granular helpers at the places where their data is
# available (system early, tools late).

#: Anthropic cache marker. Deliberately copied per call (``dict(...)``) so that
#: no shared object is mutated by accident.
ANTHROPIC_EPHEMERAL: dict = {"type": "ephemeral"}

#: Hard API limit: at most this many cache_control blocks per request.
ANTHROPIC_MAX_CACHE_BLOCKS = 4

#: Text block types across the formats: chat-completions ``text``,
#: Responses API ``input_text``/``output_text``. System/tool markers belong
#: always on a text block (never on an image).
_ANTHROPIC_TEXT_TYPES = ("text", "input_text", "output_text")

#: Block types allowed to carry cache_control at the conversation TAIL: text
#: plus ``tool_result``. An agent turn often ends on a tool_result block
#: (native Anthropic path: ``{"role":"user","content":[{"type":"tool_result",
#: ...}]}``) — marking it lets the breakpoint move to the turn end in that
#: case too. Without it the native multi-turn path did not cache tool-ending
#: turns (the OpenRouter path does, because there tool results are string text
#: that is lifted into a text block) — this set establishes parity.
#: Anthropic allows cache_control on text/image/tool_use/tool_result/document.
_ANTHROPIC_TAIL_TYPES = _ANTHROPIC_TEXT_TYPES + ("tool_result",)

#: Roles whose presence proves a REAL ongoing conversation.
_HISTORY_ROLES = ("assistant", "tool")


def anthropic_cache_conversation(mode: str | None, has_history: bool) -> bool:
    """Whether the growing conversation tail is marked as a cache_control
    breakpoint (Anthropic multi-turn pattern: the marker moves to the end every
    round; the predecessor prefix is a byte prefix of the new request and is
    read instead of paid in full).

    - ``multi_turn``: from round 1 (declared conversation).
    - ``auto`` / ``None``: only once real history exists (>=1 assistant/
      tool message) — no wasted write on genuine single calls.
    - ``task_sequence`` (ladder) / ``one_shot`` / ``off``: no.
    """
    if mode == CACHE_MODE_MULTI_TURN:
        return True
    if mode in (None, CACHE_MODE_AUTO):
        return has_history
    return False


def messages_have_history(message_dicts: list) -> bool:
    """>=1 assistant/tool message => ongoing conversation (not a single call)."""
    return any(
        isinstance(m, dict) and m.get("role") in _HISTORY_ROLES
        for m in message_dicts
    )


def mark_last_text_block(blocks: list) -> bool:
    """cache_control: ephemeral on the LAST text block of a block list.

    Returns whether something was marked. Idempotent on the same block. Understands
    chat-completions and Responses text block types (_ANTHROPIC_TEXT_TYPES).
    """
    if not isinstance(blocks, list):
        return False
    for i in range(len(blocks) - 1, -1, -1):
        b = blocks[i]
        if isinstance(b, dict) and b.get("type") in _ANTHROPIC_TEXT_TYPES:
            b["cache_control"] = dict(ANTHROPIC_EPHEMERAL)
            return True
    return False


def mark_message_tail(msg: dict) -> bool:
    """cache_control on the last text block of ONE message; bare-string content
    is lifted into a text block first. Returns whether something was marked."""
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        if not content:
            return False
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": dict(ANTHROPIC_EPHEMERAL)}
        ]
        return True
    if isinstance(content, list):
        return mark_last_text_block(content)
    return False


def mark_last_system(message_dicts: list) -> bool:
    """cache_control on the LAST system message (whose marker covers the entire
    system prefix). Only the last — marking every one blows the 4-block limit together
    with tool/tail markers. Returns whether something was marked."""
    last_system = None
    for msg in message_dicts:
        if isinstance(msg, dict) and msg.get("role") == "system":
            last_system = msg
    if last_system is None:
        return False
    return mark_message_tail(last_system)


def mark_last_cacheable_block(blocks: list) -> bool:
    """cache_control: ephemeral on the LAST cacheable block of a list
    (text OR tool_result, see _ANTHROPIC_TAIL_TYPES). For the conversation
    tail, so that a turn ending in a tool_result still pulls the breakpoint to
    the end. Returns whether something was marked."""
    if not isinstance(blocks, list):
        return False
    for i in range(len(blocks) - 1, -1, -1):
        b = blocks[i]
        if isinstance(b, dict) and b.get("type") in _ANTHROPIC_TAIL_TYPES:
            b["cache_control"] = dict(ANTHROPIC_EPHEMERAL)
            return True
    return False


def mark_conversation_tail(message_dicts: list) -> bool:
    """cache_control on the tail of the LAST message (growing conversation
    prefix, multi-turn pattern). The caller gates via anthropic_cache_conversation.
    Returns whether something was marked.

    Unlike system/tools, the tail also marks a tool_result block (not
    only text), so that tool-ending agent turns pull the breakpoint to the end
    (parity of native path <-> OpenRouter path). Bare-string content (OpenAI-format
    tool/user message) is lifted into a text block first.

    Byte-stability caveat: with ``reasoning_details_mode=keep_last`` (default)
    older reasoning blocks of a THINKING model are removed every round —
    that changes the prefix and breaks conversation caching from the first
    reasoning-bearing turn. Non-thinking models and ``keep_all`` keep the
    prefix stable; thinking agents with full multi-turn caching need
    ``reasoning_details_mode: keep_all``."""
    if not message_dicts:
        return False
    # Past a developer note: that is the RUN talking, not the conversation, and
    # its text is rebuilt for every call. A breakpoint on it would make the
    # prefix up to the marker differ every turn -- the tail cache would never
    # hit again, which is the exact opposite of what marking it is for.
    tail = [m for m in message_dicts if not (isinstance(m, dict) and m.get("role") == "developer")]
    if not tail:
        return False
    msg = tail[-1]
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        if not content:
            return False
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": dict(ANTHROPIC_EPHEMERAL)}
        ]
        return True
    if isinstance(content, list):
        return mark_last_cacheable_block(content)
    return False


def mark_last_tool(tools: list) -> bool:
    """cache_control on the last tool definition (covers the whole tool block).
    Returns whether something was marked."""
    if not tools:
        return False
    last = tools[-1]
    if isinstance(last, dict):
        last["cache_control"] = dict(ANTHROPIC_EPHEMERAL)
        return True
    return False


def _iter_cache_carriers(container: Any) -> Iterator[dict]:
    """cache_control-bearing dicts from a container (message dict with a
    content list, tool dict, or raw text block)."""
    if not isinstance(container, dict):
        return
    content = container.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and "cache_control" in part:
                yield part
    elif "cache_control" in container:
        yield container


def cap_cache_control(
    ordered_groups: list,
    max_blocks: int = ANTHROPIC_MAX_CACHE_BLOCKS,
) -> None:
    """Defense in depth: across all ``ordered_groups`` (in Anthropic prefix
    order: tools, then system, then messages) keep only the LAST
    ``max_blocks`` cache_control blocks; remove cache_control from the
    earlier ones in place.

    A later breakpoint caches everything an earlier one would — sacrificing the
    earliest loses no coverage, but guarantees that no marker combination
    (system + tools + tail + sentinel splits) ever breaks the hard
    HTTP 400 limit. ``ordered_groups`` is a list of container lists
    (each group is walked in order)."""
    carriers: list[dict] = []
    for group in ordered_groups:
        for container in group or []:
            carriers.extend(_iter_cache_carriers(container))
    excess = len(carriers) - max_blocks
    for c in carriers[:excess] if excess > 0 else []:
        c.pop("cache_control", None)
