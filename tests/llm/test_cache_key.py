"""Tests for llm/cache_key.py -- prompt_cache_key "auto" derivation + breakpoints."""

import pytest

from agent_system.llm.cache_key import (
    CACHE_BP_SENTINEL,
    MAX_EXPLICIT_BREAKPOINTS,
    PREFIX_CHARS,
    PROMPT_CACHE_KEY_AUTO,
    derive_prompt_cache_key,
    split_cache_breakpoint_blocks,
    strip_cache_breakpoints,
)


def _chat(system: str, user: str) -> list:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _responses(system: str, user: str) -> list:
    return [
        {"role": "system", "content": [{"type": "input_text", "text": system}]},
        {"role": "user", "content": [{"type": "input_text", "text": user}]},
    ]


class TestStaticPassthrough:
    def test_static_value_unchanged(self):
        key = derive_prompt_cache_key("mein-agent", _chat("sys", "task"))
        assert key == "mein-agent"

    def test_auto_produces_hash(self):
        key = derive_prompt_cache_key(PROMPT_CACHE_KEY_AUTO, _chat("sys", "task"))
        assert key.startswith("auto-")
        assert len(key) == len("auto-") + 16


class TestPrefixGrouping:
    def test_same_prefix_same_key(self):
        a = derive_prompt_cache_key("auto", _chat("sys" * 100, "gleicher task"))
        b = derive_prompt_cache_key("auto", _chat("sys" * 100, "gleicher task"))
        assert a == b

    def test_early_divergence_different_key(self):
        a = derive_prompt_cache_key("auto", _chat("sys", "Buch A Synopsis ..."))
        b = derive_prompt_cache_key("auto", _chat("sys", "Buch B Synopsis ..."))
        assert a != b

    def test_long_system_prompt_does_not_mask_task(self):
        # Core case (user objection): the system prompt alone is >PREFIX_CHARS.
        # The window only counts FROM the first non-system message --
        # different books must get different keys despite an identical,
        # huge system prompt.
        big_sys = "regelwerk " * 800  # ~8000 Zeichen
        a = derive_prompt_cache_key("auto", _chat(big_sys, "Buch A Synopsis"))
        b = derive_prompt_cache_key("auto", _chat(big_sys, "Buch B Synopsis"))
        assert a != b

    def test_different_system_prompt_different_key(self):
        a = derive_prompt_cache_key("auto", _chat("agent eins", "task"))
        b = derive_prompt_cache_key("auto", _chat("agent zwei", "task"))
        assert a != b

    def test_divergence_behind_prefix_window_same_key(self):
        # Divergence BEYOND the first PREFIX_CHARS characters of the task
        # message deliberately does not separate the keys.
        stable_task = "x" * PREFIX_CHARS
        a = derive_prompt_cache_key("auto", _chat("sys", stable_task + "Szene 1"))
        b = derive_prompt_cache_key("auto", _chat("sys", stable_task + "Szene 2 anders"))
        assert a == b

    def test_appended_messages_keep_key(self):
        # Follow-up turns of the same session (including later system
        # injections) never change the key -- only the leading system prompt
        # + the first task message count.
        msgs = _chat("sys", "task")
        a = derive_prompt_cache_key("auto", msgs)
        longer = msgs + [
            {"role": "assistant", "content": "antwort"},
            {"role": "system", "content": "mid-run injection"},
            {"role": "user", "content": "folgefrage"},
        ]
        b = derive_prompt_cache_key("auto", longer)
        assert a == b


class TestInjectedBlocksDoNotMoveTheKey:
    """A block rebuilt on every call at the head must not move the key.

    Whoever hangs something there rebuilds it every step -- the todo list was
    the measured case, today the context_engineer restoration still sits
    there. Hashed along, the key moved with the text: every ticked item a
    new shard, and with it the system prompt BEFORE it was no longer
    readable from the cache.
    """

    @staticmethod
    def _with_block(text: str, role: str = "system", marker: str | None = "todo") -> list:
        block = {"role": role, "content": text}
        if marker:
            block["injected_by"] = marker
        return [{"role": "system", "content": "der agent"}, block,
                {"role": "user", "content": "die aufgabe"}]

    def test_a_rebuilt_block_keeps_the_key(self):
        a = derive_prompt_cache_key("auto", self._with_block("- [ ] Kapitel 1"))
        b = derive_prompt_cache_key("auto", self._with_block("- [x] Kapitel 1\n- [ ] Kapitel 2"))
        assert a == b

    def test_a_rebuilt_developer_note_keeps_the_key(self):
        a = derive_prompt_cache_key("auto", self._with_block("Schritt 3 von 30", role="developer"))
        b = derive_prompt_cache_key("auto", self._with_block("Schritt 4 von 30", role="developer"))
        assert a == b

    def test_an_unmarked_block_is_part_of_the_prompt(self):
        # Control: without a marker someone set it deliberately, nobody
        # rebuilds it -- that IS a different prompt and belongs in a different
        # group. Without this half, a key that distinguishes nothing at all
        # would look just as green.
        a = derive_prompt_cache_key("auto", self._with_block("Regel A", marker=None))
        b = derive_prompt_cache_key("auto", self._with_block("Regel B", marker=None))
        assert a != b

    def test_the_prompt_behind_the_block_still_counts(self):
        # And the block does not hide what stands behind it: two agents with
        # the same block stay apart.
        a = self._with_block("- [ ] Kapitel 1")
        b = self._with_block("- [ ] Kapitel 1")
        b[0] = {"role": "system", "content": "ein anderer agent"}
        assert derive_prompt_cache_key("auto", a) != derive_prompt_cache_key("auto", b)


class TestTheKeyTheRequestReallyCarries:
    """The key in the PAYLOAD, not one from a rebuilt list.

    The first version of this rule read `injected_by` from the finished payload,
    where a system block no longer carries it (httpx drops it at its rung step,
    the Responses client builds new items). Measured in isolation it was green,
    and in production it did nothing. These tests drive both real payload builds.
    """

    PROMPT = "Du bist der Agent, und das sind deine Regeln. " * 30

    @classmethod
    def _history(cls, todo: str, prompt: str | None = None):
        from agent_system.llm.models import ChatMessage
        return [
            ChatMessage(role="system", content=prompt or cls.PROMPT),
            ChatMessage(role="system", content=todo, injected_by="todo"),
            ChatMessage(role="user", content="Schreib Kapitel eins."),
        ]

    @classmethod
    def _list_history(cls, prompt: str, task: str):
        """The same history, but as a content LIST.

        ChatMessage.content is ``List[ContentItem]`` -- pydantic turns that
        into TextContent objects, not dicts. The old call site only saw dicts
        (model_dump had flattened them); whoever reads the objects only as
        dicts hashes nothing but the role marker from such a message.
        """
        from agent_system.llm.models import ChatMessage
        return [ChatMessage(role="system", content=[{"type": "text", "text": prompt}]),
                ChatMessage(role="user", content=[{"type": "text", "text": task}])]

    @staticmethod
    async def _httpx_payload(messages, *, streaming=False):
        from unittest.mock import patch
        from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient

        seen = {}

        class _Resp:
            status_code = 200
            text = "{}"
            headers: dict = {}

            @staticmethod
            def json():
                return {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                        "usage": {}}

        class _Stream:
            status_code = 200
            headers: dict = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def aiter_lines(self):
                yield "data: [DONE]"

            async def aiter_bytes(self, chunk_size=None):
                yield b"data: [DONE]\n\n"

            async def aread(self):
                return b"{}"

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def aclose(self):
                return None

            async def post(self, url=None, headers=None, json=None, **kw):
                seen["payload"] = json
                return _Resp()

            def stream(self, method=None, url=None, headers=None, json=None, **kw):
                seen["payload"] = json
                return _Stream()

        with patch("httpx.AsyncClient", lambda **kw: _Client()):
            client = HTTPXOpenAIClient(api_key="k", model="openai/gpt-5.1",
                                       base_url="https://openrouter.ai/api/v1")
            client.prompt_cache_key = "auto"
            if streaming:
                async for _ in client._make_request_streaming(messages, None):
                    pass
            else:
                await client._make_request_non_streaming(messages, None)
        assert "payload" in seen, "the test never reached the request and measures nothing"
        return seen["payload"]["prompt_cache_key"]

    @staticmethod
    def _responses_payload(messages):
        from unittest.mock import patch
        from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient

        with patch("httpx.AsyncClient"):
            client = OpenAIResponsesClient(api_key="k", model="openai/gpt-5.1",
                                           base_url="https://openrouter.ai/api/v1")
        client.prompt_cache_key = "auto"
        return client._build_payload(messages, tools=None)["prompt_cache_key"]

    @pytest.mark.asyncio
    async def test_chat_completions_keeps_the_shard_when_the_block_is_rebuilt(self):
        a = await self._httpx_payload(self._history("- [ ] Kapitel 1"))
        b = await self._httpx_payload(self._history("- [x] Kapitel 1\n- [ ] Kapitel 2"))
        other = await self._httpx_payload(self._history("- [ ] Kapitel 1", prompt="Ein ANDERER Agent."))
        assert a == b, "the key moves with the todo list"
        assert a != other, "two agents share a shard"

    @pytest.mark.asyncio
    async def test_list_shaped_content_still_tells_two_agents_apart(self):
        """The case that almost made this rule unusable.

        Several pipelines, among them the audio pipeline, build their
        messages with ``content=[TextContent(...)]``. If only dict parts are
        read, nothing but the role marker is left -- and EVERY agent with
        list content gets the same key, permanently, not just once."""
        a = await self._httpx_payload(self._list_history("Du bist der Polisher.",
                                                         "Pruefe Szene 3 (Buch 7)."))
        b = await self._httpx_payload(self._list_history("Ein GANZ anderer Agent.",
                                                         "Pruefe Szene 3 (Buch 99)."))
        assert a != b, "two unrelated agents on one shard"
        # And the same texts as a string must give the same key: the format
        # must not split the group (see TestFormats).
        from agent_system.llm.models import ChatMessage
        plain = [ChatMessage(role="system", content="Du bist der Polisher."),
                 ChatMessage(role="user", content="Pruefe Szene 3 (Buch 7).")]
        assert a == await self._httpx_payload(plain)

    @pytest.mark.asyncio
    async def test_streaming_and_non_streaming_agree_on_the_key(self):
        """Both paths group the same conversation -- otherwise an agent sits
        on two shards depending on the call. The streaming path is the one
        most agents actually use."""
        history = self._history("- [ ] Kapitel 1")
        assert (await self._httpx_payload(history, streaming=True)
                == await self._httpx_payload(history, streaming=False))

    def test_responses_keeps_the_shard_when_the_block_is_rebuilt(self):
        a = self._responses_payload(self._history("- [ ] Kapitel 1"))
        b = self._responses_payload(self._history("- [x] Kapitel 1\n- [ ] Kapitel 2"))
        other = self._responses_payload(self._history("- [ ] Kapitel 1", prompt="Ein ANDERER Agent."))
        assert a == b, "the key moves with the todo list"
        assert a != other, "two agents share a shard"

    @classmethod
    def _noted_task(cls, task: str, note: bool = True):
        from agent_system.llm.models import ChatMessage
        head = [ChatMessage(role="system", content=cls.PROMPT)]
        if note:
            head.append(ChatMessage(role="user", content="Schon oft gelesen: Akten, Stempel.",
                                    injected_by="often_read_inject"))
        return head + [ChatMessage(role="user", content=task)]

    @pytest.mark.asyncio
    async def test_a_note_behind_the_system_prompt_keeps_books_apart(self):
        """A user note injected right behind the system prompt is the same for
        every run of the agent; hashed as "the first message" it put every
        book on one shard."""
        one, two = "Buch 1, Kapitel 3: Mila am Hafen.", "Buch 2, Kapitel 3: Jonas im Stellwerk."
        chat = [await self._httpx_payload(self._noted_task(t)) for t in (one, two)]
        responses = [self._responses_payload(self._noted_task(t)) for t in (one, two)]
        for name, (a, b) in (("chat", chat), ("responses", responses)):
            assert a != b, f"{name}: two books share one shard"
        assert chat[0] == await self._httpx_payload(self._noted_task(one, note=False)), \
            "the note moved the key"

    def test_an_injected_task_without_a_user_task_behind_it_still_names_the_run(self):
        """Forge news on a woken session, a summary heading a compacted chat:
        the injected message is the only task. Skipped, the key fell to the
        answer behind it -- one key for all such sessions, and a new one after
        the first call."""
        def run(news, *answers):
            return derive_prompt_cache_key("auto", [
                {"role": "system", "content": "der agent"},
                {"role": "developer", "content": "Wake up."},
                {"role": "user", "content": news, "injected_by": "forge"}, *answers])
        answer = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
        assert run("[forge] Issue 12 opened") != run("[forge] PR 7 merged")
        assert run("[forge] Issue 12 opened") == run("[forge] Issue 12 opened", answer)


class TestFormats:
    def test_chat_and_responses_format_extract_same_text(self):
        # Both serializations of the same prompt -> same key
        # (switching format Chat <-> Responses must not split the group).
        a = derive_prompt_cache_key("auto", _chat("sys-prompt", "task-text"))
        b = derive_prompt_cache_key("auto", _responses("sys-prompt", "task-text"))
        assert a == b

    def test_non_text_parts_and_junk_are_skipped(self):
        msgs = [
            {"role": "system", "content": "sys"},
            "kein-dict",
            42,  # and nothing that would have a role either
            None,
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                    {"type": "text", "text": "task"},
                ],
            },
        ]
        key = derive_prompt_cache_key("auto", msgs)
        assert key == derive_prompt_cache_key("auto", _chat("sys", "task"))

    def test_empty_messages_still_deterministic(self):
        a = derive_prompt_cache_key("auto", [])
        b = derive_prompt_cache_key("auto", [])
        assert a == b and a.startswith("auto-")


class TestSegmentLadder:
    """Cumulative segment ladder: [static]S[append]S[volatile] + rung registry.

    GPT-5.6 rule (mapped end to end): reproduce the predecessor's structure
    exactly, only append; moving/removed markers break reads.
    """

    def _task(self, static, append, volatile):
        from agent_system.llm.cache_key import CACHE_BP_SENTINEL as S
        return static + S + append + S + volatile

    def _big(self, tag, n=1):
        # >= MIN_RUNG_CHARS (4096), so that a new rung is created
        return (tag + " zeile. ") * (600 * n)

    def test_first_call_marks_static_and_first_rung(self):
        from agent_system.llm.cache_key import (
            CacheBoundaryRegistry, plan_cache_blocks,
        )
        reg = CacheBoundaryRegistry()
        out = plan_cache_blocks(
            self._task("STAT", "APP1", "VOL"),
            mode="task_sequence", key="k1", registry=reg,
        )
        # First rung = end of append; volatile stays unmarked
        assert out == [("STAT", True), ("APP1", True), ("VOL", False)]

    def test_small_growth_repeats_structure(self):
        # Delta < MIN_RUNG: NO new rung -- the growth merges unmarked with
        # volatile, the structure stays exactly reproduced.
        from agent_system.llm.cache_key import (
            CacheBoundaryRegistry, plan_cache_blocks,
        )
        reg = CacheBoundaryRegistry()
        plan_cache_blocks(self._task("STAT", "APP1", "VOL"),
                          mode="task_sequence", key="k1", registry=reg)
        out = plan_cache_blocks(self._task("STAT", "APP1neu", "VOL2"),
                                mode="task_sequence", key="k1", registry=reg)
        assert out == [("STAT", True), ("APP1", True), ("neuVOL2", False)]

    def test_large_growth_appends_new_rung(self):
        from agent_system.llm.cache_key import (
            CacheBoundaryRegistry, plan_cache_blocks,
        )
        reg = CacheBoundaryRegistry()
        a1 = self._big("a")
        plan_cache_blocks(self._task("STAT", a1, "VOL"),
                          mode="task_sequence", key="k1", registry=reg)
        delta = self._big("b")
        out = plan_cache_blocks(self._task("STAT", a1 + delta, "VOL2"),
                                mode="task_sequence", key="k1", registry=reg)
        # Old rung reproduced + new rung appended
        assert out == [("STAT", True), (a1, True), (delta, True), ("VOL2", False)]
        # Call 3 without growth: identical structure to call 2
        out3 = plan_cache_blocks(self._task("STAT", a1 + delta, "VOL3"),
                                 mode="task_sequence", key="k1", registry=reg)
        assert out3 == [("STAT", True), (a1, True), (delta, True), ("VOL3", False)]

    def test_prefix_break_relearns(self):
        from agent_system.llm.cache_key import (
            CacheBoundaryRegistry, plan_cache_blocks,
        )
        reg = CacheBoundaryRegistry()
        plan_cache_blocks(self._task("STAT", "APP1", "VOL"),
                          mode="task_sequence", key="k1", registry=reg)
        # Break: append starts differently -> rung list reset, new first rung
        out = plan_cache_blocks(self._task("STAT", "ANDERS", "VOL"),
                                mode="task_sequence", key="k1", registry=reg)
        assert out == [("STAT", True), ("ANDERS", True), ("VOL", False)]

    def test_other_modes_no_ladder(self):
        from agent_system.llm.cache_key import (
            CacheBoundaryRegistry, plan_cache_blocks,
        )
        reg = CacheBoundaryRegistry()
        for mode in ("auto", "multi_turn", "one_shot", None):
            out = plan_cache_blocks(self._task("STAT", "APP", "VOL"),
                                    mode=mode, key="k2", registry=reg)
            assert out == [("STAT", True), ("APP", True), ("VOL", False)], mode
        # The registry stayed untouched
        assert reg.rungs_for("k2", "APPx") == []

    def test_budget_caps_declared_markers_without_ladder(self):
        # Non-ladder path (e.g. Anthropic): budget from the back, BP0 drops.
        from agent_system.llm.cache_key import CACHE_BP_SENTINEL as S
        from agent_system.llm.cache_key import plan_cache_blocks
        text = "A" + S + "B" + S + "C" + S + "D"
        out = plan_cache_blocks(text, mode=None, key="k", max_markers=2)
        assert out == [("A", False), ("B", True), ("C", True), ("D", False)]

    def test_mode_off_strips(self):
        from agent_system.llm.cache_key import plan_cache_blocks
        out = plan_cache_blocks(self._task("A", "B", "C"), mode="off", key="k")
        assert out == [("ABC", False)]

    def test_no_sentinel_returns_none(self):
        from agent_system.llm.cache_key import plan_cache_blocks
        assert plan_cache_blocks("plain", mode="task_sequence", key="k") is None

    def test_registry_keys_are_isolated(self):
        from agent_system.llm.cache_key import (
            CacheBoundaryRegistry, plan_cache_blocks,
        )
        reg = CacheBoundaryRegistry()
        plan_cache_blocks(self._task("S", "BUCH-A-DIGEST", "V"),
                          mode="task_sequence", key="buchA", registry=reg)
        out = plan_cache_blocks(self._task("S", "BUCH-B-DIGEST", "V"),
                                mode="task_sequence", key="buchB", registry=reg)
        assert out == [("S", True), ("BUCH-B-DIGEST", True), ("V", False)]


class TestBreakpointSplit:
    def test_no_sentinel_passthrough(self):
        assert split_cache_breakpoint_blocks("abc") == ["abc"]
        assert strip_cache_breakpoints("abc") == "abc"

    def test_split_and_strip_roundtrip(self):
        text = "stabil" + CACHE_BP_SENTINEL + "variabel"
        assert split_cache_breakpoint_blocks(text) == ["stabil", "variabel"]
        assert strip_cache_breakpoints(text) == "stabilvariabel"

    def test_strip_preserves_explicit_newline_separator(self):
        # Construction pattern of the pipeline: "\n" BEFORE the sentinel, so
        # that the strip yields exactly the join without a marker.
        text = "zeile1\n" + CACHE_BP_SENTINEL + "zeile2"
        assert strip_cache_breakpoints(text) == "zeile1\nzeile2"

    def test_cap_at_max_breakpoints(self):
        blocks = ["b%d" % i for i in range(6)]
        text = CACHE_BP_SENTINEL.join(blocks)
        out = split_cache_breakpoint_blocks(text)
        assert len(out) == MAX_EXPLICIT_BREAKPOINTS + 1
        assert "".join(out) == "".join(blocks)

    def test_empty_blocks_dropped(self):
        text = CACHE_BP_SENTINEL + "inhalt" + CACHE_BP_SENTINEL
        assert split_cache_breakpoint_blocks(text) == ["inhalt"]

    def test_auto_key_ignores_sentinel_position(self):
        # The auto key is hashed from the POST-split payload -- with and
        # without the sentinel the same key must result (the split strips it).
        raw = "instruktion " * 200
        with_sent = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [
                {"type": "text", "text": raw[:500]},
                {"type": "text", "text": raw[500:]},
            ]},
        ]
        without = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": raw},
        ]
        assert derive_prompt_cache_key("auto", with_sent) == \
            derive_prompt_cache_key("auto", without)
