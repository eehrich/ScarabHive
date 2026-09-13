"""Agent watchdog, stage 1: what the judge sees, how its answer is read, and
that watching never holds up or disturbs the watched agent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_system.hooks import HookContext, HookType
from plugins.agent_watchdog import hooks as watchdog_hooks
from plugins.agent_watchdog import window as watchdog_window
from plugins.agent_watchdog.hooks import AgentWatchdogPlugin
from plugins.agent_watchdog.window import build_excerpt, parse_verdict

PLUGIN_DIR = Path(watchdog_hooks.__file__).parent


def _messages():
    return [
        {"role": "system", "content": "You are a helper. " * 50 + "DELIVER: a JSON list of names."},
        {"role": "user", "content": "Find every caller of load_config."},
        {"role": "assistant", "content": "", "reasoning_content": "I should grep for load_config first.",
         "tool_calls": [
             {"id": "a", "function": {"name": "grep", "arguments": '{"query": "SECRET BODY TEXT"}'}},
             {"id": "b", "function": {"name": "grep", "arguments": '{"query": "x"}'}},
             {"id": "c", "function": {"name": "read", "arguments": '{"path": "y"}'}},
             {"id": "d", "function": {"name": "read", "arguments": '{"path": "z"}'}},
         ]},
        {"role": "tool", "tool_call_id": "a", "content": '{"matches": [1]}'},
        {"role": "tool", "tool_call_id": "b", "content": "[]"},
        {"role": "tool", "tool_call_id": "c", "content": '{"status": "error", "error": "nope"}'},
    ]


def _excerpt(**overrides):
    kwargs = dict(task_chars=4000, spec_chars=40, reasoning_chars=8000, max_tool_calls=20)
    kwargs.update(overrides)
    return build_excerpt(_messages(), {"role": "assistant", "content": "Now I will read the file."}, **kwargs)


# --- the excerpt --------------------------------------------------------------

class TestExcerpt:
    def test_tool_calls_carry_status_never_arguments(self):
        excerpt = _excerpt()
        calls = excerpt["recent_tool_calls"]
        assert [c["result"] for c in calls] == ["ok", "empty", "error", "pending"]
        assert "SECRET BODY TEXT" not in json.dumps(excerpt)
        assert calls[0]["arguments"] != calls[1]["arguments"]

    @pytest.mark.parametrize("content,status", [
        ('{"status": "success", "files": [], "total_found": 0, "skipped": {"notes": ["x"]}}', "empty"),
        ('{"status": "success", "matches": [{"line": 1}], "total_matches": 1}', "ok"),
        ('{"status": "success", "stdout": "done"}', "ok"),
    ])
    def test_a_successful_search_with_no_hits_is_empty(self, content, status):
        """Measured live: an empty file_ops search read as "ok"."""
        assert watchdog_window._result_status(content) == status

    def test_expected_result_is_the_tail_of_the_system_prompt(self):
        spec = _excerpt()["expected_result"]
        assert spec.endswith("DELIVER: a JSON list of names.")
        assert len(spec) == 40

    def test_thinking_is_the_most_recent_part_within_budget(self):
        excerpt = _excerpt(reasoning_chars=20)
        assert len(excerpt["recent_thinking"]) <= 20
        assert excerpt["recent_thinking"].endswith("read the file.")

    def test_no_visible_task_means_no_judge_call(self):
        assert build_excerpt([{"role": "system", "content": "x"}], None, task_chars=10,
                             spec_chars=10, reasoning_chars=10, max_tool_calls=5) is None

    def test_a_follow_up_request_shows_the_new_question_last(self):
        """Measured live: with only the first user message the judge saw the
        previous assignment and called a healthy follow-up "drifted"."""
        messages = _messages() + [
            {"role": "assistant", "content": "beta.py:3, gamma.py:3"},
            {"role": "user", "content": "Neue Frage: wo wird alpha importiert?"},
            {"role": "user", "content": "Und bitte nur .py-Dateien."},
        ]
        excerpt = build_excerpt(messages, None, task_chars=4000, spec_chars=10,
                                reasoning_chars=100, max_tool_calls=5)
        assert excerpt["user_messages"] == [
            "Find every caller of load_config.",
            "Neue Frage: wo wird alpha importiert?",
            "Und bitte nur .py-Dateien.",
        ]

    def test_the_newest_user_message_wins_the_budget(self):
        messages = [{"role": "user", "content": "old " * 50},
                    {"role": "user", "content": "the current question"}]
        excerpt = build_excerpt(messages, None, task_chars=25, spec_chars=10,
                                reasoning_chars=10, max_tool_calls=5)
        assert excerpt["user_messages"][-1] == "the current question"
        assert sum(len(m) for m in excerpt["user_messages"]) <= 25

    def test_the_current_answer_is_not_counted_twice(self):
        messages = _messages()
        current = {"role": "assistant", "content": "unique thought"}
        excerpt = build_excerpt(messages + [current], dict(current), task_chars=100,
                                spec_chars=10, reasoning_chars=8000, max_tool_calls=5)
        assert excerpt["recent_thinking"].count("unique thought") == 1


# --- reading the verdict --------------------------------------------------------

class TestVerdict:
    excerpt = {"user_messages": ["Find every caller"], "expected_result": "", "recent_thinking":
               "I keep searching the same folder again", "recent_tool_calls": []}

    def _raw(self, **fields):
        base = {"verdict": "steer", "reason": "r", "evidence": "searching the same folder",
                "message": "Search the tests folder next."}
        base.update(fields)
        return json.dumps(base)

    def test_a_grounded_steer_is_kept(self):
        verdict, fail_open = parse_verdict(self._raw(), self.excerpt)
        assert (verdict["verdict"], fail_open) == ("steer", None)

    @pytest.mark.parametrize("quote", ["searching the same folder...",
                                       "…searching the same folder", "... searching the same folder …"])
    def test_an_ellipsis_marking_a_cut_is_not_part_of_the_quote(self, quote):
        """Measured live: a word-for-word quote ending in "..." was discarded."""
        verdict, fail_open = parse_verdict(self._raw(evidence=quote), self.excerpt)
        assert (verdict["verdict"], fail_open) == ("steer", None)

    def test_fenced_json_is_read(self):
        verdict, fail_open = parse_verdict("```json\n" + self._raw() + "\n```", self.excerpt)
        assert (verdict["verdict"], fail_open) == ("steer", None)

    @pytest.mark.parametrize("raw,reason", [
        ("not json at all", "unparseable"),
        (json.dumps({"verdict": "panic", "evidence": "x"}), "unknown_verdict"),
    ])
    def test_broken_answers_read_as_continue(self, raw, reason):
        verdict, fail_open = parse_verdict(raw, self.excerpt)
        assert (verdict["verdict"], fail_open) == ("continue", reason)

    @pytest.mark.parametrize("fields,reason", [
        ({"evidence": ""}, "no_evidence"),
        ({"evidence": "the agent gave up entirely"}, "evidence_not_verbatim"),
        ({"evidence": "..."}, "evidence_not_verbatim"),
        ({"message": ""}, "no_message"),
    ])
    def test_an_ungrounded_intervention_reads_as_continue(self, fields, reason):
        verdict, fail_open = parse_verdict(self._raw(**fields), self.excerpt)
        assert (verdict["verdict"], fail_open) == ("continue", reason)


# --- the hook -------------------------------------------------------------------

class FakeLLM:
    model_name = "judge-model"

    def __init__(self, answer: str = "", release: asyncio.Event | None = None, exc=None):
        self.answer, self.release, self.exc, self.calls = answer, release, exc, 0

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.calls += 1
        self.system_prompt = messages[0].content
        self.excerpt = json.loads(messages[1].content)
        if self.release is not None:
            await self.release.wait()
        if self.exc:
            raise self.exc
        return {"assistant": {"content": self.answer}, "usage": {"total_tokens": 42}}


class FakeAgent:
    system_config = None

    def __init__(self):
        self.ids = []

    async def next_internal_tool_request_id(self, base):
        self.ids.append(base)
        return f"{base}_001"


@pytest.fixture
def plugin(tmp_path):
    return AgentWatchdogPlugin(PLUGIN_DIR, project_root=tmp_path)


def _context(step, request_id="req-1", hook_config=None, agent=None):
    return HookContext(
        hook_type=HookType.POST_LLM_CALL, request_id=request_id, session_id="s",
        agent=agent or FakeAgent(), agent_name="observed", messages=_messages(),
        llm_response={"assistant": {"role": "assistant", "content": "Now I will read the file."}},
        step=step, llm=SimpleNamespace(model_name="observed-model"),
        hook_config=hook_config or {},
    )


def _log(plugin):
    path = plugin._log_path
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


async def _drain(plugin):
    for task in list(plugin._running.values()):
        await task


class TestHook:
    @pytest.mark.asyncio
    async def test_the_agent_is_not_held_up_by_the_judge(self, plugin):
        release = asyncio.Event()
        plugin._judge_llm = FakeLLM(json.dumps({"verdict": "continue", "reason": "ok",
                                                "evidence": "read the file"}), release)

        result = await asyncio.wait_for(plugin.observe_step(_context(step=10)), timeout=1)

        assert result.success and not result.modified
        assert not result.metadata.get("continue") if result.metadata else True
        assert "req-1" in plugin._running, "the judge must still be running"
        assert _log(plugin) == []
        release.set()
        await _drain(plugin)
        entry = _log(plugin)[0]
        assert (entry["verdict"], entry["usage"], entry["judge_model"]) == (
            "continue", {"total_tokens": 42}, "judge-model")
        assert plugin._running == {}, "per-request state must clean itself up"

    @pytest.mark.asyncio
    async def test_checks_only_when_due(self, plugin):
        plugin._judge_llm = FakeLLM("{}")
        for step in (1, 9, 11, 19):
            await plugin.observe_step(_context(step=step))
            assert plugin._running == {}
        await plugin.observe_step(_context(step=20))
        assert "req-1" in plugin._running
        await _drain(plugin)

    @pytest.mark.asyncio
    async def test_agent_override_changes_the_rhythm_and_typos_fall_back(self, plugin):
        plugin._judge_llm = FakeLLM("{}")
        await plugin.observe_step(_context(step=3, hook_config={"first_check_step": 3}))
        assert "req-1" in plugin._running
        await _drain(plugin)
        # every_n_steps "ten" would be 0 if used as is: a modulo by zero on every
        # step of the agent. It falls back to the plugin value (10) instead.
        await plugin.observe_step(_context(step=20, hook_config={"every_n_steps": "ten"}))
        assert "req-1" in plugin._running
        await _drain(plugin)

    @pytest.mark.asyncio
    async def test_judge_prompt_per_agent_and_unreadable_falls_back(self, plugin, tmp_path):
        (tmp_path / "writer_judge.md").write_text("WRITER CRITERIA", encoding="utf-8")
        llm = FakeLLM("{}")
        plugin._judge_llm = llm

        await plugin.observe_step(_context(step=10, hook_config={"judge_prompt": "writer_judge.md"}))
        await _drain(plugin)
        assert llm.system_prompt == "WRITER CRITERIA"
        assert _log(plugin)[-1]["judge_prompt"] == "writer_judge.md"

        await plugin.observe_step(_context(step=10))
        await _drain(plugin)
        assert llm.system_prompt == plugin._prompt, "an agent without override gets the built-in"
        assert "judge_prompt" not in _log(plugin)[-1]

        await plugin.observe_step(_context(step=10, hook_config={"judge_prompt": "missing.md"}))
        await _drain(plugin)
        assert llm.system_prompt == plugin._prompt
        assert _log(plugin)[-1]["judge_prompt_error"]

    @pytest.mark.asyncio
    async def test_a_second_check_does_not_overlap_a_running_one(self, plugin):
        release = asyncio.Event()
        llm = FakeLLM("{}", release)
        plugin._judge_llm = llm
        await plugin.observe_step(_context(step=10, hook_config={"every_n_steps": 1}))
        await plugin.observe_step(_context(step=11, hook_config={"every_n_steps": 1}))
        release.set()
        await _drain(plugin)
        assert llm.calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("llm,fail_open", [
        (FakeLLM(exc=RuntimeError("provider refused")), "judge_error"),
        (None, "no_judge_llm"),
    ])
    async def test_a_failing_judge_is_logged_as_continue(self, plugin, llm, fail_open):
        plugin._judge_llm = llm
        await plugin.observe_step(_context(step=10))
        await _drain(plugin)
        entry = _log(plugin)[0]
        assert (entry["verdict"], entry["fail_open"]) == ("continue", fail_open)

    @pytest.mark.asyncio
    async def test_status_goes_to_a_child_id_and_never_reads_as_error(self, plugin, monkeypatch):
        seen = {}

        class Scope:
            def __init__(self, bus, server, request_id, start_msg=None, end_msg=None):
                seen["id"] = request_id

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def end(self, message):
                seen["end"] = message

            async def error(self, message, meta=None):
                seen["error"] = message

        monkeypatch.setattr(watchdog_hooks, "StatusScope", Scope)
        plugin._judge_llm = FakeLLM(json.dumps({
            "verdict": "abort", "reason": "loops", "evidence": "read the file",
            "message": "Lies die Datei config.py."}))
        await plugin.observe_step(_context(step=10))
        await _drain(plugin)
        assert seen["id"] == "req-1_001"
        assert seen["end"].startswith("abort: loops")
        assert "error" not in seen
        assert _log(plugin)[0]["message"] == "Lies die Datei config.py."


def _progress(chars, previous, request_id="req-1", hook_config=None, text=None):
    return HookContext(
        hook_type=HookType.LLM_PROGRESS, request_id=request_id, session_id="s",
        agent=FakeAgent(), agent_name="observed", step=4,
        llm=SimpleNamespace(model="observed-model"), hook_config=hook_config or {},
        reasoning_text=text if text is not None else "x" * chars,
        reasoning_chars=chars, previous_reasoning_chars=previous,
    )


async def _remember(plugin, request_id="req-1"):
    await plugin.remember_task(HookContext(
        hook_type=HookType.PRE_LLM_CALL, request_id=request_id, session_id="s",
        messages=_messages()))


class TestReasoning:
    @pytest.mark.asyncio
    async def test_checks_when_the_interval_is_crossed_not_on_every_tick(self, plugin):
        plugin._judge_llm = FakeLLM("{}")
        await _remember(plugin)
        for chars, previous in ((2000, 0), (18000, 16000), (22000, 20000), (38000, 36000)):
            await plugin.observe_reasoning(_progress(chars, previous))
            assert plugin._running == {}, f"no check due at {chars}"
        await plugin.observe_reasoning(_progress(20500, 18500))
        assert "req-1" in plugin._running
        await _drain(plugin)
        await plugin.observe_reasoning(_progress(4000, 2000, hook_config={"every_n_reasoning_chars": 3000}))
        assert "req-1" in plugin._running, "the per-agent interval must apply"
        await _drain(plugin)

    @pytest.mark.asyncio
    async def test_the_judge_sees_the_running_call_s_thinking_and_the_task(self, plugin):
        llm = FakeLLM(json.dumps({"verdict": "continue", "reason": "ok",
                                  "evidence": "weighing option B"}))
        plugin._judge_llm = llm
        await _remember(plugin)
        thinking = "old part " * 2000 + "now weighing option B against A"
        await plugin.observe_reasoning(_progress(len(thinking), 0, text=thinking,
                                                 hook_config={"every_n_reasoning_chars": 1000}))
        await _drain(plugin)

        assert llm.excerpt["user_messages"] == ["Find every caller of load_config."]
        assert llm.excerpt["call_in_progress"] is True
        assert llm.excerpt["recent_thinking"] == thinking[-8000:]
        entry = _log(plugin)[-1]
        assert (entry["trigger"], entry["reasoning_chars"], entry["verdict"]) == (
            "reasoning", len(thinking), "continue")
        assert "fail_open" not in entry, "evidence from the running thinking must count as verbatim"

    @pytest.mark.asyncio
    async def test_without_remember_task_there_is_no_call_but_a_line(self, plugin):
        llm = FakeLLM("{}")
        plugin._judge_llm = llm
        await plugin.observe_reasoning(_progress(20000, 18000))
        assert llm.calls == 0
        assert _log(plugin)[-1]["fail_open"] == "task_not_visible"

    @pytest.mark.asyncio
    async def test_remembered_runs_are_bounded(self, plugin):
        for index in range(watchdog_hooks._REMEMBERED_RUNS + 5):
            await _remember(plugin, request_id=f"r{index}")
        assert len(plugin._tasks) == watchdog_hooks._REMEMBERED_RUNS
        assert "r0" not in plugin._tasks and f"r{watchdog_hooks._REMEMBERED_RUNS + 4}" in plugin._tasks

    @pytest.mark.asyncio
    async def test_step_checks_are_marked_as_such(self, plugin):
        plugin._judge_llm = FakeLLM("{}")
        await plugin.observe_step(_context(step=10))
        await _drain(plugin)
        assert _log(plugin)[-1]["trigger"] == "step"
        assert "call_in_progress" not in plugin._judge_llm.excerpt


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_waits_for_the_last_check(self, plugin):
        """Measured live: the CLI exited 0.35 s after starting the last check."""
        plugin._judge_llm = FakeLLM(json.dumps({"verdict": "continue", "reason": "ok",
                                                "evidence": "read the file"}))
        await plugin.observe_step(_context(step=10))
        await plugin.stop_plugin()
        assert _log(plugin)[0]["verdict"] == "continue"
        assert "fail_open" not in _log(plugin)[0]

    @pytest.mark.asyncio
    async def test_a_judge_still_running_at_shutdown_is_logged_as_cancelled(self, plugin):
        plugin._config["judge_timeout_seconds"] = 0.05
        plugin._judge_llm = FakeLLM("{}", asyncio.Event())   # never released
        await plugin.observe_step(_context(step=10))
        await plugin.stop_plugin()
        assert _log(plugin)[0]["fail_open"] == "cancelled_at_shutdown"
