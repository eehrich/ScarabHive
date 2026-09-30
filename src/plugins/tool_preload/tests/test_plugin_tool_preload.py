"""Tests for the tool_preload plugin.

The fake agent implements the same contract as Agent.dispatch_tool_call: it
records call order, returns a canned result per tool, and raises
ToolDispatchError for tools outside the allowlist — the same rejection the real
dispatcher produces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.components.tool_execution import ToolDispatchError
from plugins.tool_preload.hooks import ToolPreloadPlugin, _fill_params

PLUGIN_DIR = Path(__file__).parent.parent


class FakeAgent:
    """dispatch_tool_call contract: records order, canned results, allowlist."""

    def __init__(self, results=None, allowed=None, raises=None):
        self.calls = []
        self.kwargs = []          # the runtime context each call was given
        self.results = results or {}
        self.allowed = allowed    # None = everything
        self.raises = raises or {}  # tool -> exception to raise

    async def dispatch_tool_call(self, tool_name, params, *, session_id=None,
                                 user_id=None, request_id=None):
        if self.allowed is not None and tool_name not in self.allowed:
            raise ToolDispatchError(
                f"Tool '{tool_name}' is not in this agent's allowed tools.")
        self.calls.append((tool_name, dict(params)))
        # Recorded, not swallowed: without _session_id the real dispatcher
        # cannot scope a session-scoped tool, and nothing else would notice.
        self.kwargs.append({"session_id": session_id, "user_id": user_id,
                            "request_id": request_id})
        if tool_name in self.raises:
            raise self.raises[tool_name]
        return self.results.get(tool_name, {"status": "success", "tool": tool_name})


def make_context(messages, rules=None, agent=None, config_extra=None):
    config = {"rules": rules or []}
    if config_extra:
        config.update(config_extra)
    return HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="req-1",
        session_id="sess-1",
        agent=agent if agent is not None else FakeAgent(),
        agent_name="test_agent",
        messages=messages,
        hook_config=config,
    )


def user(text):
    return ChatMessage(role="user", content=text)


@pytest.fixture
def plugin():
    return ToolPreloadPlugin(PLUGIN_DIR)


# ---------------------------------------------------------------------------
# Parameter templating
# ---------------------------------------------------------------------------
class TestFillParams:
    def test_lone_numeric_group_becomes_int(self):
        """Tool handlers validate ids as integers — scene_id='42' would miss."""
        out = _fill_params({"scene_id": "{sid}"}, {"sid": "42"})
        assert out == {"scene_id": 42}

    def test_lone_text_group_stays_str(self):
        assert _fill_params({"doc": "{doc}"}, {"doc": "mein-plan"}) == {"doc": "mein-plan"}

    def test_mixed_template_stays_str(self):
        """kapitel_{n} must not become an int even when n is numeric."""
        assert _fill_params({"label": "kapitel_{n}"}, {"n": "7"}) == {"label": "kapitel_7"}

    def test_non_string_values_pass_through(self):
        assert _fill_params({"limit": 5, "flag": True}, {}) == {"limit": 5, "flag": True}

    def test_missing_group_raises(self):
        """A literal '{doc}' as document name would be worse than no preload."""
        with pytest.raises(KeyError):
            _fill_params({"doc": "{doc}"}, {})


# ---------------------------------------------------------------------------
# Context variables — the second placeholder source
# ---------------------------------------------------------------------------
class TestContextVars:
    """State the user never types. v6 keeps its store namespace in
    ``json_namespace``, set once at bootstrap and inherited by every
    sub-agent, whose whole task text is "Aufgabe: World". Without this source
    a rule cannot name the store, and a read would silently hit the DEFAULT
    namespace — someone else's document."""

    CTX = {"json_namespace": 4711, "aufgabe": "World"}

    def test_lone_numeric_var_becomes_int(self):
        out = _fill_params({"namespace": "{{ json_namespace }}"}, {}, self.CTX)
        assert out == {"namespace": 4711}

    def test_without_spaces_too(self):
        """'{{name}}' without spaces: the group pattern matches its inner half,
        so this shape is the one the pass order has to survive."""
        out = _fill_params({"namespace": "{{json_namespace}}"}, {}, self.CTX)
        assert out == {"namespace": 4711}

    def test_context_wins_over_a_same_named_group(self):
        """``{{name}}`` must not be read as a group ``{name}`` in braces."""
        out = _fill_params({"namespace": "{{json_namespace}}"},
                           {"json_namespace": "999"}, self.CTX)
        assert out == {"namespace": 4711}

    def test_a_resolved_value_is_not_scanned_again(self):
        """The v6 coordinator puts the ENTIRE user task into ``brief``. With
        two sequential passes, a brief containing ``{sid}`` was read as a group
        reference: the rule died with a warning naming a group that appears in
        no YAML, and the operator hunted a config error that did not exist."""
        ctx = {"brief": "Schreibe eine Story ueber {sid} Szenen"}
        out = _fill_params({"doc": "b_{{ brief }}"}, {"sid": "42"}, ctx)
        assert out == {"doc": "b_Schreibe eine Story ueber {sid} Szenen"}

    def test_a_resolved_value_is_not_rewritten_by_a_group(self):
        """Same defect, silent variant: the context value itself contained a
        placeholder whose name a group happened to share."""
        out = _fill_params({"p": "x_{{ns}}"}, {"sid": "999"}, {"ns": "{sid}"})
        assert out == {"p": "x_{sid}"}

    def test_nested_params_are_templated(self):
        """Top-level-only templating let a nested namespace through verbatim —
        json_store then reads the DEFAULT namespace and hands over a foreign
        document, silently. That is the failure this feature exists to stop."""
        out = _fill_params({"filter": {"namespace": "{{ json_namespace }}"},
                            "docs": ["{{ json_namespace }}", "{sid}"]},
                           {"sid": "42"}, self.CTX)
        assert out == {"filter": {"namespace": 4711}, "docs": [4711, 42]}


    def test_mixed_stays_str(self):
        out = _fill_params({"doc": "kapitel_{{ aufgabe }}"}, {}, self.CTX)
        assert out == {"doc": "kapitel_World"}

    def test_both_sources_in_one_value(self):
        out = _fill_params({"p": "{{ json_namespace }}/{sid}"},
                           {"sid": "42"}, self.CTX)
        assert out == {"p": "4711/42"}

    def test_groups_keep_working_without_ctx(self):
        """The original source must be untouched — no ctx_vars argument at all
        is the pre-existing call shape."""
        assert _fill_params({"scene_id": "{sid}"}, {"sid": "42"}) == {"scene_id": 42}

    def test_missing_var_raises(self):
        """Same class as a missing group: skip the rule loudly. Falling back to
        the default namespace would hand the agent the wrong document without
        anyone noticing."""
        with pytest.raises(KeyError):
            _fill_params({"namespace": "{{ fehlt }}"}, {}, self.CTX)

    def test_none_valued_var_raises(self):
        """A var present but None is not a namespace — treat it as missing."""
        with pytest.raises(KeyError):
            _fill_params({"namespace": "{{ ns }}"}, {}, {"ns": None})

    def test_resolver_is_defensive(self):
        """Runs inside a hook: an agent without a tracker must degrade to 'no
        vars', never sink the request."""
        from plugins.tool_preload.hooks import _session_context_vars

        class Bare:
            pass

        class Ctx:
            agent = Bare()
            session_id = "s"

        assert _session_context_vars(Ctx()) == {}
        assert _session_context_vars(type("C", (), {"agent": None})()) == {}

    def test_session_vars_win_over_agent_defaults(self):
        """Same precedence the prompt rendering uses: set_context overrides the
        agent's static template_vars."""
        from plugins.tool_preload.hooks import _session_context_vars

        class Tracker:
            @staticmethod
            def get_session_template_vars(_sid):
                return {"json_namespace": 999}

        class Cfg:
            template_vars = {"json_namespace": 1, "genre": "krimi"}

        class Agent:
            agent_config = Cfg()
            _session_tracker = Tracker()

        class Ctx:
            agent = Agent()
            session_id = "s"

        assert _session_context_vars(Ctx()) == {"json_namespace": 999,
                                                "genre": "krimi"}


class TestIntConversionIsLossless:
    """``"42"`` → 42 earns its keep (handlers validate ids as integers), but
    only where the conversion changes nothing else."""

    def test_leading_zeros_stay_a_string(self):
        """'007' → 7 → json_store stringifies '7', while the prompt's own
        ``{{ json_namespace }}`` still renders '007'. Preload and the agent's
        own call would then read two different namespaces."""
        assert _fill_params({"p": "{{ ns }}"}, {}, {"ns": "007"}) == {"p": "007"}

    def test_plain_id_still_becomes_int(self):
        assert _fill_params({"p": "{{ ns }}"}, {}, {"ns": "42"}) == {"p": 42}

    def test_negative_is_consistent_with_positive(self):
        """Both round-trip through str, so both convert — the old isdigit()
        test made '-42' a string while '42' became an int, and the two then
        serialised differently into the dedup key."""
        assert _fill_params({"p": "{{ ns }}"}, {}, {"ns": "-42"}) == {"p": -42}

    def test_isdigit_but_not_int_does_not_raise(self):
        """'²'.isdigit() is True and int('²') raises ValueError — which is not
        a KeyError, so it escaped the per-rule handler and discarded the whole
        turn's preload, including every healthy rule."""
        assert _fill_params({"p": "{{ ns }}"}, {}, {"ns": "²"}) == {"p": "²"}

    def test_absurdly_long_digit_run_does_not_raise(self):
        """int() refuses beyond int_max_str_digits — same escape route."""
        lang = "1" * 5000
        assert _fill_params({"p": "{{ ns }}"}, {}, {"ns": lang}) == {"p": lang}

# ---------------------------------------------------------------------------
# Firing conditions
# ---------------------------------------------------------------------------
class TestFiring:
    @pytest.mark.asyncio
    async def test_fires_only_when_last_message_is_user(self, plugin):
        """One shot per user turn: mid-run steps end on assistant/tool messages,
        and re-firing there would preload on every one of 200 steps."""
        rules = [{"match": "Dok\\s+(?P<doc>\\S+)", "tool": "json_store_read",
                  "params": {"doc": "{doc}"}}]
        agent = FakeAgent()

        # The assistant message QUOTES the doc name — realistic (assistants
        # narrate what they read) and exactly what would fire on every step if
        # the role guard fell away.
        mid_run = [user("lies Dok plan.md"),
                   ChatMessage(role="assistant", content="Ich lese Dok plan.md")]
        result = await plugin.preload(make_context(mid_run, rules, agent))
        assert result.modified is False
        assert agent.calls == []

        fresh = [user("lies Dok plan.md")]
        result = await plugin.preload(make_context(fresh, rules, agent))
        assert result.modified is True
        assert agent.calls == [("json_store_read", {"doc": "plan.md"})]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("note_role", ["developer", "user"])
    async def test_a_loop_note_behind_the_turn_neither_hides_nor_fires_it(self, plugin, note_role):
        """The step budget note follows drained user input at the start of a step.

        It rides on `developer` since the loop got that role; a hook that
        scripts a turn (debate_forum, agent_continuation) still puts one on
        `user`. Both carry a marker, and the marker is what this must read."""
        rules = [{"match": "(?P<word>\\w+)", "tool": "json_store_read", "params": {"doc": "{word}"}}]
        note = ChatMessage(role=note_role, content="Step 29 of 30: wrap up",
                           injected_by="agent.step_budget")
        agent = FakeAgent()

        result = await plugin.preload(make_context([user("lies plan"), note], rules, agent))
        assert result.modified is True
        assert agent.calls == [("json_store_read", {"doc": "lies"})], "the note was read as the turn"

        later = [user("lies plan"), note, ChatMessage(role="assistant", content="ok"), note]
        result = await plugin.preload(make_context(later, rules, FakeAgent()))
        assert result.modified is False, "a note alone opened a new turn"

    @pytest.mark.asyncio
    async def test_no_rules_no_action(self, plugin):
        result = await plugin.preload(make_context([user("hallo")], []))
        assert result.modified is False

    @pytest.mark.asyncio
    async def test_no_match_no_action(self, plugin):
        agent = FakeAgent()
        rules = [{"match": "Szene\\s+(?P<sid>\\d+)", "tool": "get_scene",
                  "params": {"scene_id": "{sid}"}}]
        result = await plugin.preload(
            make_context([user("was ganz anderes")], rules, agent))
        assert result.modified is False
        assert agent.calls == []


# ---------------------------------------------------------------------------
# Message shape
# ---------------------------------------------------------------------------
class TestMessageShape:
    @pytest.mark.asyncio
    async def test_appends_the_exact_llm_tool_call_shape(self, plugin):
        """assistant(tool_calls) + tool with the SAME id, result as JSON string.

        Anything else is an invalid request on the next LLM call — the pair
        must be indistinguishable from one the LLM produced itself.
        """
        agent = FakeAgent(results={"json_store_read": {"status": "success",
                                                       "content": "Inhalt"}})
        rules = [{"match": "Dok\\s+(?P<doc>\\S+)", "tool": "json_store_read",
                  "params": {"doc": "{doc}"}}]
        messages = [user("lies Dok plan.md")]

        result = await plugin.preload(make_context(messages, rules, agent))

        out = result.context.messages
        assert out is not messages, "the core only takes a NEW list (identity check)"
        assert len(out) == 3
        assistant, tool = out[1], out[2]
        assert assistant.role == "assistant"
        # Internal only (clients drop it): context_engineer's Pre-Layer T counts
        # the round after the last assistant message the MODEL wrote as unsent.
        assert assistant.injected_by == "tool_preload"
        tc = assistant.tool_calls[0]
        assert tc["function"]["name"] == "json_store_read"
        assert json.loads(tc["function"]["arguments"]) == {"doc": "plan.md"}
        assert tool.role == "tool"
        assert tool.tool_call_id == tc["id"], "half a pair is a 400 from every provider"
        assert tool.name == "json_store_read"
        assert json.loads(tool.content) == {"status": "success", "content": "Inhalt"}
        # The runtime context must reach the dispatcher: without session_id the
        # real one cannot inject _session_id, and a session-scoped tool then
        # silently answers from the wrong namespace.
        assert agent.kwargs == [{"session_id": "sess-1", "user_id": None,
                                 "request_id": "req-1"}]

    @pytest.mark.asyncio
    async def test_tool_error_result_is_appended_like_any_result(self, plugin):
        """Error answers carry recovery context these days — the agent would
        have seen exactly this calling the tool itself."""
        agent = FakeAgent(results={"get_scene": {
            "status": "error", "error": "Scene 99 not found",
            "available": [{"id": 1}], "hint": "1 scene exists"}})
        rules = [{"match": "Szene\\s+(?P<sid>\\d+)", "tool": "get_scene",
                  "params": {"scene_id": "{sid}"}}]

        result = await plugin.preload(
            make_context([user("Szene 99 bitte")], rules, agent))

        assert result.modified is True
        payload = json.loads(result.context.messages[-1].content)
        assert payload["status"] == "error"
        assert payload["hint"] == "1 scene exists"


# ---------------------------------------------------------------------------
# Chains and ordering
# ---------------------------------------------------------------------------
class TestChains:
    @pytest.mark.asyncio
    async def test_calls_run_sequentially_in_configured_order(self, plugin):
        """'erst context_var setzen, dann Content laden' is a real dependency."""
        agent = FakeAgent()
        rules = [{"match": "Szene\\s+(?P<sid>\\d+)", "calls": [
            {"tool": "set_var", "params": {"name": "scene", "value": "{sid}"}},
            {"tool": "get_scene", "params": {"scene_id": "{sid}"}},
        ]}]

        result = await plugin.preload(
            make_context([user("arbeite an Szene 42")], rules, agent))

        assert agent.calls == [
            ("set_var", {"name": "scene", "value": 42}),
            ("get_scene", {"scene_id": 42}),
        ], "chain order is the contract"
        # Two pairs appended, each with matching ids
        out = result.context.messages
        assert [m.role for m in out[1:]] == ["assistant", "tool", "assistant", "tool"]
        assert out[1].tool_calls[0]["id"] == out[2].tool_call_id
        assert out[3].tool_calls[0]["id"] == out[4].tool_call_id

    @pytest.mark.asyncio
    async def test_each_pair_carries_the_step_its_results_feed(self, plugin):
        """The hook runs in the loop's step k, before the LLM call the live events
        number k + 1; a session read back numbers the pair by it, as the loop's own
        answers are (ChatMessage.step)."""
        rules = [{"match": "Szene\\s+(?P<sid>\\d+)", "calls": [
            {"tool": "set_var", "params": {"name": "scene", "value": "{sid}"}},
            {"tool": "get_scene", "params": {"scene_id": "{sid}"}},
        ]}]
        context = make_context([user("arbeite an Szene 42")], rules, FakeAgent())
        context.step = 2

        result = await plugin.preload(context)

        calls = [m for m in result.context.messages if m.role == "assistant"]
        assert len(calls) == 2, "fixture: two pairs appended"
        assert [m.step for m in calls] == [3, 3]

    @pytest.mark.asyncio
    async def test_disallowed_tool_aborts_the_chain_but_keeps_prior_pairs(self, plugin):
        """Later calls may depend on the rejected one — running them anyway
        executes a chain whose premise failed."""
        agent = FakeAgent(allowed={"set_var"})
        rules = [{"match": "Szene\\s+(?P<sid>\\d+)", "calls": [
            {"tool": "set_var", "params": {"name": "scene", "value": "{sid}"}},
            {"tool": "forbidden_tool", "params": {}},
            {"tool": "set_var", "params": {"name": "never", "value": "x"}},
        ]}]

        result = await plugin.preload(
            make_context([user("Szene 7")], rules, agent))

        assert agent.calls == [("set_var", {"name": "scene", "value": 7})]
        assert result.modified is True, "the completed pair stays"
        assert len(result.context.messages) == 3

    @pytest.mark.asyncio
    async def test_missing_group_skips_the_rule_not_the_turn(self, plugin):
        """A rule whose template references an uncaptured group is misconfigured;
        the OTHER rules must still run."""
        agent = FakeAgent()
        rules = [
            {"match": "Szene", "tool": "get_scene", "params": {"scene_id": "{sid}"}},
            {"match": "Szene\\s+(?P<sid>\\d+)", "tool": "get_scene2",
             "params": {"scene_id": "{sid}"}},
        ]

        result = await plugin.preload(
            make_context([user("Szene 42")], rules, agent))

        assert agent.calls == [("get_scene2", {"scene_id": 42})]
        assert result.modified is True


# ---------------------------------------------------------------------------
# Dedup and caps
# ---------------------------------------------------------------------------
class TestDedupAndCaps:
    @pytest.mark.asyncio
    async def test_identical_call_already_in_history_is_skipped(self, plugin):
        """Re-reading a doc the conversation already loaded buys nothing: the
        agent has it in context or edited it itself — in which case IT knows
        the current state better than a re-read."""
        agent = FakeAgent()
        rules = [{"match": "Dok\\s+(?P<doc>\\S+)", "tool": "json_store_read",
                  "params": {"doc": "{doc}"}}]
        messages = [
            user("lies Dok plan.md"),
            ChatMessage(role="assistant", content=None, tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "json_store_read",
                             "arguments": json.dumps({"doc": "plan.md"})}}]),
            ChatMessage(role="tool", tool_call_id="c1", name="json_store_read",
                        content=json.dumps({"status": "success"})),
            user("und nochmal Dok plan.md bitte"),
        ]

        result = await plugin.preload(make_context(messages, rules, agent))

        assert agent.calls == [], "the identical call ran again"
        assert result.modified is False

    @pytest.mark.asyncio
    async def test_dedup_false_runs_it_again(self, plugin):
        agent = FakeAgent()
        rules = [{"match": "Dok\\s+(?P<doc>\\S+)", "tool": "json_store_read",
                  "params": {"doc": "{doc}"}}]
        messages = [
            ChatMessage(role="assistant", content=None, tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "json_store_read",
                             "arguments": json.dumps({"doc": "plan.md"})}}]),
            ChatMessage(role="tool", tool_call_id="c1", name="json_store_read",
                        content="{}"),
            user("Dok plan.md nochmal"),
        ]

        result = await plugin.preload(make_context(
            messages, rules, agent, config_extra={"dedup": False}))

        assert agent.calls == [("json_store_read", {"doc": "plan.md"})]
        assert result.modified is True

    @pytest.mark.asyncio
    async def test_call_cap_bounds_a_greedy_ruleset(self, plugin):
        """One message must not become an unbounded tool storm."""
        agent = FakeAgent()
        rules = [{"match": "x", "tool": f"tool_{i}", "params": {}}
                 for i in range(10)]

        result = await plugin.preload(make_context(
            [user("x")], rules, agent, config_extra={"max_calls_per_turn": 3}))

        assert len(agent.calls) == 3
        assert result.modified is True

    @pytest.mark.asyncio
    async def test_the_cap_never_cuts_a_chain_in_half(self, plugin):
        """A chain is all-or-nothing.

        Cutting it mid-way leaves the context var set and the content never
        loaded — the run then proceeds against state nobody asked for, which is
        strictly worse than not preloading. A chain that does not fit is
        skipped whole; the agent makes those calls itself.
        """
        agent = FakeAgent()
        rules = [{"match": "Szene\\s+(?P<sid>\\d+)", "calls": [
            {"tool": "set_var", "params": {"name": "scene", "value": "{sid}"}},
            {"tool": "get_scene", "params": {"scene_id": "{sid}"}},
            {"tool": "get_notes", "params": {"scene_id": "{sid}"}},
        ]}]

        result = await plugin.preload(make_context(
            [user("Szene 42")], rules, agent, config_extra={"max_calls_per_turn": 2}))

        assert agent.calls == [], (
            f"the chain was cut after {len(agent.calls)} of 3 calls — the state "
            f"is set but the content never loaded")
        assert result.modified is False

    @pytest.mark.asyncio
    async def test_dedup_never_skips_only_part_of_a_chain(self, plugin):
        """The state-setting link must not be dropped while the rest runs.

        Turn 1 sets the var for scene 42 and loads it. Turn 2 asks for scene 7:
        different params, so nothing is deduped. But a rule whose FIRST link
        happens to repeat (a constant param) would otherwise be skipped alone,
        and the load would run against the previous scene's state.
        """
        agent = FakeAgent()
        rules = [{"match": "Szene\\s+(?P<sid>\\d+)", "calls": [
            {"tool": "set_mode", "params": {"mode": "edit"}},   # constant!
            {"tool": "get_scene", "params": {"scene_id": "{sid}"}},
        ]}]
        messages = [
            ChatMessage(role="assistant", content=None, tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "set_mode",
                             "arguments": json.dumps({"mode": "edit"})}}]),
            ChatMessage(role="tool", tool_call_id="c1", name="set_mode", content="{}"),
            user("jetzt Szene 7"),
        ]

        result = await plugin.preload(make_context(messages, rules, agent))

        assert agent.calls == [
            ("set_mode", {"mode": "edit"}),
            ("get_scene", {"scene_id": 7}),
        ], "the already-seen first link was skipped and the chain ran incomplete"
        assert result.modified is True

    @pytest.mark.asyncio
    async def test_a_fully_repeated_chain_is_skipped(self, plugin):
        """Per-rule dedup must still dedup: every link already there = redundant."""
        agent = FakeAgent()
        rules = [{"match": "Szene\\s+(?P<sid>\\d+)", "calls": [
            {"tool": "set_mode", "params": {"mode": "edit"}},
            {"tool": "get_scene", "params": {"scene_id": "{sid}"}},
        ]}]
        def pair(cid, name, args):
            return [ChatMessage(role="assistant", content=None, tool_calls=[{
                        "id": cid, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]),
                    ChatMessage(role="tool", tool_call_id=cid, name=name, content="{}")]
        messages = (pair("c1", "set_mode", {"mode": "edit"})
                    + pair("c2", "get_scene", {"scene_id": 7})
                    + [user("nochmal Szene 7")])

        result = await plugin.preload(make_context(messages, rules, agent))

        assert agent.calls == []
        assert result.modified is False


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------
class TestRobustness:
    @pytest.mark.asyncio
    async def test_invalid_regex_is_skipped_loudly_not_fatally(self, plugin):
        agent = FakeAgent()
        rules = [
            {"match": "([unclosed", "tool": "a", "params": {}},
            {"match": "ok", "tool": "b", "params": {}},
        ]
        result = await plugin.preload(make_context([user("ok")], rules, agent))
        assert agent.calls == [("b", {})]
        assert result.modified is True

    @pytest.mark.asyncio
    async def test_a_crash_inside_the_hook_never_sinks_the_run(self, plugin):
        """A preload is an optimisation. Failing it must cost the optimisation,
        never the request.

        Two distinct layers: a dispatch failure is handled INSIDE the chain
        (abort, keep prior pairs), while a bug in the mechanism itself — here a
        non-numeric cap in the config — must be caught by the outer guard.
        Only the second exercises that guard.
        """
        # A tool that RAISES mid-chain: the calls before it already ran, so
        # their pairs must survive — otherwise the model repeats them.
        agent = FakeAgent(raises={"second": RuntimeError("boom")})
        chain = [{"match": "x", "calls": [
            {"tool": "first", "params": {}},
            {"tool": "second", "params": {}},
            {"tool": "third", "params": {}},
        ]}]

        result = await plugin.preload(make_context([user("x")], chain, agent))
        assert result.success is True
        assert result.modified is True, "the completed pair was thrown away"
        assert agent.calls == [("first", {}), ("second", {})], (
            "the chain continued past the raising call")
        assert len(result.context.messages) == 5
        assert result.context.messages[1].tool_calls[0]["function"]["name"] == "first"
        # The raising call is recorded as the loop records it: an error result.
        assert result.context.messages[3].tool_calls[0]["function"]["name"] == "second"
        assert json.loads(result.context.messages[4].content) == {
            "error": "Tool 'second' execution failed: boom", "type": "RuntimeError"}

        # The outer guard: planning itself raises (broken config).
        result = await plugin.preload(make_context(
            [user("x")], chain, agent=FakeAgent(),
            config_extra={"max_calls_per_turn": "kaputt"}))
        assert result.success is True
        assert result.modified is False

    @pytest.mark.asyncio
    async def test_a_tool_that_ran_is_always_recorded(self, plugin):
        """The worst failure this plugin can have: run a tool and not say so.

        Reproduced before the fix: PyYAML resolves an unquoted `2026-01-01` to
        datetime.date, the params reached json.dumps AFTER dispatch, the
        TypeError discarded every pair, the hook reported "no preload" — and
        the model then called the same state-changing tool a second time.
        """
        import datetime

        agent = FakeAgent()
        # The date is on the SECOND call on purpose: if recording it fails, the
        # first call's pair must still survive, so the count below tells the
        # two failure modes apart (nothing recorded vs. partially recorded).
        rules = [{"match": "Bericht", "calls": [
            {"tool": "load_report", "params": {"id": 7}},
            {"tool": "set_scope", "params": {"since": datetime.date(2026, 1, 1)}},
        ]}]

        result = await plugin.preload(
            make_context([user("Mach den Bericht")], rules, agent))

        assert agent.calls, "fixture broken: nothing ran"
        assert result.modified is True, (
            "a tool ran but no pair was recorded — the model will call it again")
        # Every executed call has its pair, and the date survived as a string.
        assert len(result.context.messages) == 1 + 2 * len(agent.calls), (
            f"{len(agent.calls)} tools ran but only "
            f"{(len(result.context.messages) - 1) // 2} pairs were recorded")
        args = json.loads(result.context.messages[3].tool_calls[0]["function"]["arguments"])
        assert args["since"] == "2026-01-01"

    @pytest.mark.asyncio
    async def test_an_unrecordable_result_keeps_the_earlier_pairs(self, plugin):
        """The tool result is NOT normalised — it comes from the tool.

        A result that no serializer can render (here: an object whose __str__
        itself raises, so even default=str fails) must not discard the pairs of
        calls that already ran. Losing those makes the model repeat them.
        """
        class Unserializable:
            def __repr__(self): raise RuntimeError("nope")
            __str__ = __repr__

        agent = FakeAgent(results={"second": {"bad": Unserializable()}})
        rules = [{"match": "x", "calls": [
            {"tool": "first", "params": {}},
            {"tool": "second", "params": {}},
            {"tool": "third", "params": {}},
        ]}]

        result = await plugin.preload(make_context([user("x")], rules, agent))

        assert agent.calls == [("first", {}), ("second", {})], (
            "the chain continued past the unrecordable result")
        assert result.modified is True, "the first call's pair was thrown away"
        assert len(result.context.messages) == 3
        assert result.context.messages[1].tool_calls[0]["function"]["name"] == "first"

    @pytest.mark.asyncio
    async def test_match_input_is_bounded(self, plugin):
        """A regex runs on the event loop and cannot be interrupted.

        asyncio.wait_for only cancels at an await point and re.search has none,
        so the hook's declared timeout is inert — measured: a nested-quantifier
        rule stalled the loop for 4.8 s against a 1 s timeout that never fired.
        Bounding the INPUT is the part that is actually in this hook's hands.
        """
        from plugins.tool_preload.hooks import MATCH_TEXT_LIMIT

        agent = FakeAgent()
        # The trigger sits far past the cap, so a bounded match cannot see it.
        rules = [{"match": "MARKER", "tool": "t", "params": {}}]
        padded = "a" * (MATCH_TEXT_LIMIT + 500) + " MARKER"

        result = await plugin.preload(make_context([user(padded)], rules, agent))

        assert agent.calls == [], (
            f"the rule matched past {MATCH_TEXT_LIMIT} chars — the input to the "
            f"regex is not bounded")
        assert result.modified is False

    @pytest.mark.asyncio
    async def test_multimodal_user_message_matches_on_its_text_parts(self, plugin):
        agent = FakeAgent()
        rules = [{"match": "Dok\\s+(?P<doc>\\S+)", "tool": "json_store_read",
                  "params": {"doc": "{doc}"}}]
        messages = [ChatMessage(role="user", content=[
            {"type": "text", "text": "lies Dok plan.md"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "aaaa"}},
        ])]

        result = await plugin.preload(make_context(messages, rules, agent))

        assert agent.calls == [("json_store_read", {"doc": "plan.md"})]
        assert result.modified is True


# ---------------------------------------------------------------------------
# Through the real dispatcher (HookRegistry + Agent.dispatch_tool_call)
# ---------------------------------------------------------------------------
class _Server:
    """A tool server: records its calls, answers via ``answer(params)``."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def call(self, tool_name, params):
        self.calls.append(tool_name)
        return self.answer(params)


def _real_agent(servers, allowed, override):
    """A minimal agent running the REAL dispatch and authorization methods."""
    from types import SimpleNamespace
    from agent_system.servers.agent.server import Agent

    class _Agent:
        name = "probe_agent"
        dispatch_tool_call = Agent.dispatch_tool_call
        _resolve_flat_tool_name = Agent._resolve_flat_tool_name
        tool_dispatch_denial = Agent.tool_dispatch_denial

        def __init__(self):
            self._servers = servers
            overrides = {"tool_preload.preload": override} if override else {}
            self.agent_config = SimpleNamespace(
                tools=SimpleNamespace(allowed=allowed, blocked=[]),
                hooks=SimpleNamespace(enabled=True, overrides=overrides),
                template_vars={})

        def _get_server_from_any_registry(self, server_name):
            return self._servers.get(server_name)

    return _Agent()


_DOC_RULE = {"enabled": True, "rules": [
    {"match": r"document\s+(?P<doc>[\w-]+)", "tool": "docs_read",
     "params": {"doc": "{doc}"}}]}


async def _run_hooks(agent, messages):
    """One pre_llm_call pass as the agent runs it: schema default off, the
    agent's override decides (hook_runs_for), the registry builds hook_config."""
    from agent_system.hooks import HookRegistry
    from agent_system.servers.agent.components.hook_integration import hook_runs_for
    from plugins.tool_preload.plugin import PLUGIN_FACTORY

    registry = HookRegistry()
    await registry.register_hook(HookType.PRE_LLM_CALL, "tool_preload.preload",
                                 PLUGIN_FACTORY("tool_preload", None, None), enabled=False)
    ctx = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r1", session_id="s1",
                      agent_name="probe_agent", agent=agent, messages=messages)
    result = await registry.execute_hooks(
        HookType.PRE_LLM_CALL, ctx,
        hook_filter=lambda name, default: hook_runs_for(agent.agent_config.hooks, name, default))
    return result.messages


class TestThroughTheDispatcher:
    @pytest.mark.asyncio
    async def test_the_agents_override_switches_it_on_and_carries_the_rules(self):
        server = _Server(lambda p: {"status": "success", "doc": p["doc"]})
        system = ChatMessage(role="system", content="sys")

        msgs = await _run_hooks(_real_agent({"docs": server}, ["docs/*"], _DOC_RULE),
                                [system, user("edit document plan-a")])
        assert server.calls == ["docs_read"]
        assert json.loads(msgs[-1].content) == {"status": "success", "doc": "plan-a"}
        assert msgs[2].injected_by == "tool_preload"

        msgs = await _run_hooks(_real_agent({"docs": server}, ["docs/*"], None),
                                [system, user("edit document plan-a")])
        assert server.calls == ["docs_read"], "ran without the agent's override"
        assert len(msgs) == 2

    @pytest.mark.asyncio
    async def test_a_tool_outside_the_allowlist_is_never_called(self):
        server = _Server(lambda p: {"status": "success"})
        msgs = await _run_hooks(_real_agent({"docs": server}, ["other/*"], _DOC_RULE),
                                [user("document plan-a")])
        assert server.calls == []
        assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_a_raising_tool_reaches_the_model_as_an_error(self):
        """The model's own call would show the failure. With nothing recorded
        the model called the tool again without knowing it had just failed."""
        def boom(params):
            raise RuntimeError("disk gone")
        server = _Server(boom)
        msgs = await _run_hooks(_real_agent({"docs": server}, ["docs/*"], _DOC_RULE),
                                [user("document plan-a")])
        assert server.calls == ["docs_read"]
        assert [m.role for m in msgs] == ["user", "assistant", "tool"]
        assert msgs[1].tool_calls[0]["id"] == msgs[2].tool_call_id
        assert json.loads(msgs[2].content) == {
            "error": "Tool 'docs_read' execution failed: disk gone", "type": "RuntimeError"}

    @pytest.mark.asyncio
    async def test_every_request_is_a_prefix_of_the_next(self):
        """Two turns: the pairs only ever extend the list, the next step does
        not fire again, and the second turn's identical call is deduplicated."""
        server = _Server(lambda p: {"status": "success", "doc": p["doc"]})
        agent = _real_agent({"docs": server}, ["docs/*"], _DOC_RULE)
        msgs = [ChatMessage(role="system", content="sys"), user("document plan-a")]
        requests = []
        for turn in (1, 2):
            for step in ("tool", "answer"):
                msgs = await _run_hooks(agent, msgs)
                requests.append([(m.role, m.content) for m in msgs])
                msgs = msgs + [ChatMessage(role="assistant", content=f"{step} {turn}")]
            msgs = msgs + [user("again document plan-a")]

        for earlier, later in zip(requests, requests[1:]):
            assert later[:len(earlier)] == earlier
        assert server.calls == ["docs_read"]


class TestTimeBudget:
    @pytest.mark.asyncio
    async def test_no_call_starts_after_the_budget_and_each_left_is_noted(self):
        """The registry's timeout discards every pair, done calls included, and
        the model repeats them. The budget stops the chain before that."""
        import asyncio
        from plugins.tool_preload.hooks import SKIPPED_TEXT

        class _Slow:
            def __init__(self):
                self.calls = []

            async def call(self, tool_name, params):
                self.calls.append(tool_name)
                await asyncio.sleep(0.3)
                return {"status": "success", "tool": tool_name}

        server = _Slow()
        override = {"enabled": True, "max_seconds": 0.2, "rules": [
            {"match": "go", "calls": [{"tool": "docs_a"}, {"tool": "docs_b"},
                                      {"tool": "docs_c"}]}]}
        agent = _real_agent({"docs": server}, ["docs/*"], override)

        msgs = await _run_hooks(agent, [user("go")])

        assert server.calls == ["docs_a"]
        assert [m.role for m in msgs] == ["user"] + ["assistant", "tool"] * 3
        assert json.loads(msgs[2].content) == {"status": "success", "tool": "docs_a"}
        for tool_msg in (msgs[4], msgs[6]):
            assert json.loads(tool_msg.content) == {"error": SKIPPED_TEXT}

        # A skipped call is not "already made": the next turn runs the rule again.
        msgs = msgs + [ChatMessage(role="assistant", content="ok"), user("go")]
        await _run_hooks(agent, msgs)
        assert server.calls[1] == "docs_a"

    @pytest.mark.parametrize("value", ["soon", 0, -1, True, None, float("nan")])
    def test_an_invalid_budget_is_the_default(self, value):
        from plugins.tool_preload.hooks import DEFAULT_MAX_SECONDS, _max_seconds
        assert _max_seconds({"max_seconds": value}) == DEFAULT_MAX_SECONDS
        assert _max_seconds({"max_seconds": 3}) == 3.0


class TestFailureEndsOnlyItsRule:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("failing", ["docs_boom", "other_tool"])
    async def test_the_next_rule_still_runs(self, failing):
        """A raising tool (docs_boom) or a refused one (other_tool, outside the
        allowlist) ends the rest of ITS chain. The plan used to be one flat
        list, so the failure dropped every later rule too."""
        def answer(params):
            if params.get("which") == "boom":
                raise RuntimeError("boom")
            return {"status": "success"}

        server = _Server(answer)
        failing_call = ({"tool": "docs_boom", "params": {"which": "boom"}}
                        if failing == "docs_boom" else {"tool": "other_tool"})
        override = {"enabled": True, "rules": [
            {"match": "go", "calls": [failing_call, {"tool": "docs_after"}]},
            {"match": "go", "tool": "docs_independent"}]}
        agent = _real_agent({"docs": server, "other": _Server(answer)}, ["docs/*"], override)

        msgs = await _run_hooks(agent, [user("go")])

        ran = [m.tool_calls[0]["function"]["name"] for m in msgs if m.tool_calls]
        assert "docs_after" not in ran, "the failed rule's chain went on"
        assert ran[-1] == "docs_independent", "the independent rule was dropped"
        assert server.calls[-1] == "docs_independent"
