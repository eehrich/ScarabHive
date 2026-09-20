"""Tests fuer llm/cache_key.py — prompt_cache_key "auto"-Ableitung + Breakpoints."""

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
        # Kern-Fall (User-Einwand): System-Prompt allein >PREFIX_CHARS
        # (z.B. scene_planner). Das Fenster zaehlt erst AB der ersten
        # Nicht-System-Message — verschiedene Buecher muessen trotz
        # identischem Riesen-System-Prompt verschiedene Keys bekommen.
        big_sys = "regelwerk " * 800  # ~8000 Zeichen
        a = derive_prompt_cache_key("auto", _chat(big_sys, "Buch A Synopsis"))
        b = derive_prompt_cache_key("auto", _chat(big_sys, "Buch B Synopsis"))
        assert a != b

    def test_different_system_prompt_different_key(self):
        a = derive_prompt_cache_key("auto", _chat("agent eins", "task"))
        b = derive_prompt_cache_key("auto", _chat("agent zwei", "task"))
        assert a != b

    def test_divergence_behind_prefix_window_same_key(self):
        # Divergenz JENSEITS der ersten PREFIX_CHARS Zeichen der
        # Task-Message trennt die Keys absichtlich nicht.
        stable_task = "x" * PREFIX_CHARS
        a = derive_prompt_cache_key("auto", _chat("sys", stable_task + "Szene 1"))
        b = derive_prompt_cache_key("auto", _chat("sys", stable_task + "Szene 2 anders"))
        assert a == b

    def test_appended_messages_keep_key(self):
        # Folge-Turns derselben Session (auch nachgeschobene System-
        # Injections) aendern den Key nie — nur fuehrender System-Prompt
        # + erste Task-Message zaehlen.
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
    """Ein pro Call neu gebauter Block im Kopf darf den Key nicht verschieben.

    Wer dort etwas einhaengt, baut es jeden Schritt neu — die Todo-Liste war
    der gemessene Fall, heute steht dort noch die Restoration des
    context_engineer. Mitgehasht wanderte der Key mit dem Text: jede Abhakung
    eine neue Shard, und damit war auch der System-Prompt DAVOR nicht mehr
    lesbar.
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
        # Gegenprobe: ohne Marker hat sie jemand bewusst gesetzt, niemand baut
        # sie neu — das IST ein anderer Prompt und gehoert in eine andere
        # Gruppe. Ohne diese Haelfte wuerde ein Key, der gar nichts mehr
        # unterscheidet, genauso gruen aussehen.
        a = derive_prompt_cache_key("auto", self._with_block("Regel A", marker=None))
        b = derive_prompt_cache_key("auto", self._with_block("Regel B", marker=None))
        assert a != b

    def test_the_prompt_behind_the_block_still_counts(self):
        # Und der Block verdeckt nicht, was hinter ihm steht: zwei Agenten mit
        # demselben Block bleiben getrennt.
        a = self._with_block("- [ ] Kapitel 1")
        b = self._with_block("- [ ] Kapitel 1")
        b[0] = {"role": "system", "content": "ein anderer agent"}
        assert derive_prompt_cache_key("auto", a) != derive_prompt_cache_key("auto", b)


class TestTheKeyTheRequestReallyCarries:
    """Der Key im PAYLOAD, nicht der aus einer nachgebauten Liste.

    Die erste Fassung dieser Regel las `injected_by` aus dem fertigen Payload —
    dort ist der Marker laengst weg (httpx whitelistet die API-Felder, der
    Responses-Client baut neue Items). Isoliert gemessen war sie gruen und in
    Produktion wirkungslos. Diese Tests fahren beide echten Payload-Bauten.
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
        """Dieselbe Historie, aber als Content-LISTE.

        ChatMessage.content ist ``List[ContentItem]`` — pydantic macht daraus
        TextContent-Objekte, keine Dicts. Der alte Aufrufpunkt sah nur Dicts
        (model_dump hatte sie plattgemacht); wer die Objekte nur als Dict liest,
        hasht von so einer Message nichts als den Rollen-Marker.
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
        assert "payload" in seen, "der Test hat den Request nie erreicht und misst nichts"
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
        assert a == b, "der Key wandert mit der Todo-Liste"
        assert a != other, "zwei Agenten teilen sich eine Shard"

    @pytest.mark.asyncio
    async def test_list_shaped_content_still_tells_two_agents_apart(self):
        """Der Fall, der diese Regel fast unbrauchbar gemacht haette.

        polish_pipeline, paragraph_breaks, metadata_enrichment und die
        Audio-Pipeline bauen ihre Messages mit ``content=[TextContent(...)]``.
        Werden nur Dict-Parts gelesen, bleibt davon der Rollen-Marker uebrig —
        und JEDER Agent mit Listen-Content bekommt denselben Key, dauerhaft,
        nicht nur einmal."""
        a = await self._httpx_payload(self._list_history("Du bist der Polisher.",
                                                         "Pruefe Szene 3 (Buch 7)."))
        b = await self._httpx_payload(self._list_history("Ein GANZ anderer Agent.",
                                                         "Pruefe Szene 3 (Buch 99)."))
        assert a != b, "zwei fremde Agenten auf einer Shard"
        # Und dieselben Texte als String muessen denselben Key ergeben: das
        # Format darf die Gruppe nicht trennen (s. TestFormats).
        from agent_system.llm.models import ChatMessage
        plain = [ChatMessage(role="system", content="Du bist der Polisher."),
                 ChatMessage(role="user", content="Pruefe Szene 3 (Buch 7).")]
        assert a == await self._httpx_payload(plain)

    @pytest.mark.asyncio
    async def test_streaming_and_non_streaming_agree_on_the_key(self):
        """Beide Wege gruppieren dieselbe Konversation — sonst sitzt ein Agent
        je nach Aufruf auf zwei Shards. Der Streaming-Pfad ist der, den die
        meisten Agenten wirklich fahren."""
        history = self._history("- [ ] Kapitel 1")
        assert (await self._httpx_payload(history, streaming=True)
                == await self._httpx_payload(history, streaming=False))

    def test_responses_keeps_the_shard_when_the_block_is_rebuilt(self):
        a = self._responses_payload(self._history("- [ ] Kapitel 1"))
        b = self._responses_payload(self._history("- [x] Kapitel 1\n- [ ] Kapitel 2"))
        other = self._responses_payload(self._history("- [ ] Kapitel 1", prompt="Ein ANDERER Agent."))
        assert a == b, "der Key wandert mit der Todo-Liste"
        assert a != other, "zwei Agenten teilen sich eine Shard"


class TestFormats:
    def test_chat_and_responses_format_extract_same_text(self):
        # Beide Serialisierungen desselben Prompts -> gleicher Key
        # (Format-Wechsel Chat <-> Responses darf die Gruppe nicht trennen).
        a = derive_prompt_cache_key("auto", _chat("sys-prompt", "task-text"))
        b = derive_prompt_cache_key("auto", _responses("sys-prompt", "task-text"))
        assert a == b

    def test_non_text_parts_and_junk_are_skipped(self):
        msgs = [
            {"role": "system", "content": "sys"},
            "kein-dict",
            42,  # und auch nichts, was eine Rolle haette
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
    """Kumulative Segment-Leiter: [static]S[append]S[volatile] + Rung-Registry.

    GPT-5.6-Regel (E2E-kartiert): Struktur des Vorgaengers exakt
    reproduzieren, nur anhaengen; wandernde/entfernte Marker brechen Reads.
    """

    def _task(self, static, append, volatile):
        from agent_system.llm.cache_key import CACHE_BP_SENTINEL as S
        return static + S + append + S + volatile

    def _big(self, tag, n=1):
        # >= MIN_RUNG_CHARS (4096), damit eine neue Rung entsteht
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
        # Erste Rung = append-Ende; volatile bleibt unmarkiert
        assert out == [("STAT", True), ("APP1", True), ("VOL", False)]

    def test_small_growth_repeats_structure(self):
        # Delta < MIN_RUNG: KEINE neue Rung — der Zuwachs verschmilzt
        # unmarkiert mit volatile, die Struktur bleibt exakt reproduziert.
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
        # Alte Rung reproduziert + neue Rung angehaengt
        assert out == [("STAT", True), (a1, True), (delta, True), ("VOL2", False)]
        # Call 3 ohne Wachstum: identische Struktur wie Call 2
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
        # Bruch: append beginnt anders -> Rung-Liste reset, neue erste Rung
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
        # Registry blieb unberuehrt
        assert reg.rungs_for("k2", "APPx") == []

    def test_budget_caps_declared_markers_without_ladder(self):
        # Nicht-Leiter-Pfad (z.B. Anthropic): Budget von hinten, BP0 faellt.
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
        # Konstruktions-Muster der Pipeline: "\n" VOR dem Sentinel, damit
        # der Strip exakt den Join ohne Marker ergibt.
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
        # Der auto-Key wird aus dem POST-Split-Payload gehasht — mit und
        # ohne Sentinel muss derselbe Key entstehen (Split strippt ihn).
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
