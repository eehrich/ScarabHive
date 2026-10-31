"""llm_coding_cli: a coding CLI as one link of a profile chain, against a stand-in of a coding_cli instance -- a real
ToolServer, so call_with_status, the status scope and the runtime parameters run as for the real one."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from agent_system import app_state
from agent_system.config.models import AgentSystemConfig, LLMModelConfig, ToolServerConfig
from agent_system.core.cancellation import CancellationToken
from agent_system.core.request_context import current_run_session, current_run_user
from agent_system.llm.models import LLMQuotaExhaustedError, LLMServerError
from agent_system.tools.base import ToolServer, ToolServerRegistry
from agent_system.tools.status import current_request_id
from plugins.llm_coding_cli import client as client_module
from plugins.llm_coding_cli.client import CodingCliClient, render


def done(text="the answer", **extra):
    return {"status": "success", "run_id": "r1", "state": "done", "model": "opus-x",
            "result": {"untrusted": True, "content": text}, **extra}


class _Instance(ToolServer):
    """Stand-in of a coding_cli instance: answers from ``answers`` in turn, records every call."""

    def __init__(self, answers, cli="claude"):
        super().__init__("cc", AgentSystemConfig(), ToolServerConfig(type="coding_cli", enabled=True))
        self.cli = cli
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.waiting = asyncio.Event()
        self.cancelled = False
        self.stopped = False             # by cancel_run

    async def _answer(self, name, params):
        self.calls.append((name, {k: v for k, v in params.items() if k != "_status"}))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if answer in ("hang", "hang unheard"):
            self.waiting.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                # "hang unheard": the cancel lands outside the handler (its status scope), which stops nothing
                self.cancelled = answer == "hang"
                raise
        return answer

    async def cc_run_task(self, params):
        return await self._answer("run_task", params)

    async def cc_get_run(self, params):
        return await self._answer("get_run", params)

    async def cc_cancel_run(self, params):
        self.calls.append(("cancel_run", {k: v for k, v in params.items() if k != "_status"}))
        self.stopped = True
        return {"status": "success", "run_id": params["run_id"], "state": "cancelled"}


@pytest.fixture(autouse=True)
def no_conversations(monkeypatch):
    """The runs a session may go on with live in the module: each test starts without any."""
    monkeypatch.setattr(client_module, "_conversations", type(client_module._conversations)())


@pytest.fixture
def instance(monkeypatch):
    """Registers a stand-in under "cc" and sets the agent run's context; returns a setter for its answers."""
    made = {}

    def make(*answers, cli="claude"):
        made["server"] = _Instance(answers, cli)
        registry = ToolServerRegistry()
        registry.register("cc", made["server"])
        monkeypatch.setattr(app_state, "registry", registry)
        return made["server"]

    tokens = (current_request_id.set("req-1"), current_run_user.set("ada"), current_run_session.set("sess-1"))
    yield make
    current_request_id.reset(tokens[0])
    current_run_user.reset(tokens[1])
    current_run_session.reset(tokens[2])


def client(provider="claude_code"):
    return CodingCliClient(provider, "cc", context_window=200000)


MESSAGES = [{"role": "system", "content": "You write."}, {"role": "user", "content": "Write the scene."}]


class TestCalls:
    async def test_a_call_is_one_run_whose_answer_is_the_assistant_message(self, instance):
        server = instance(done())
        out = await client().chat_tools(MESSAGES, [{"type": "function", "function": {"name": "t"}}])
        assert out["assistant"] == {"role": "assistant", "content": "the answer", "served_by": "claude_code:cc"}
        assert out["usage"]["cost"] == 0.0 and out["finish_reason"] == "stop"
        (name, params), = server.calls
        assert name == "run_task" and params["wake"] is False
        assert (params["_user_id"], params["_session_id"], params["_request_id"]) == ("ada", "sess-1", "req-1")
        assert params["task"] == render(MESSAGES) and "resume" not in params

    async def test_the_agent_runs_tool_bindings_go_with_every_run_and_none_without(self, instance):
        from agent_system.core.request_context import current_tool_bindings

        bindings = {"files/*_write": {"path": "out.md"}}
        server = instance(done("first"), done("second"), done("free"))
        llm = client()
        token = current_tool_bindings.set(bindings)
        try:
            await llm.chat_tools(MESSAGES, [])
            more = MESSAGES + [{"role": "assistant", "content": "first"}, {"role": "user", "content": "Shorter."}]
            await llm.chat_tools(more, [])
        finally:
            current_tool_bindings.reset(token)
        await client().chat_tools(MESSAGES, [])
        assert [(p.get("resume"), p.get("pin")) for _, p in server.calls] == [
            (None, bindings), ("r1", bindings), (None, None)]
        assert "pin" not in server.calls[2][1], "a run without bindings sent a pin (a workdir without pins refuses one)"

    async def test_a_running_run_is_waited_for_and_stops_with_its_caller(self, instance):
        server = instance({"status": "success", "run_id": "r1", "state": "running"},
                          {"status": "success", "run_id": "r1", "state": "running"}, done("late"))
        assert (await client().chat_tools(MESSAGES, []))["assistant"]["content"] == "late"
        assert [(n, p.get("run_id"), p.get("stop_if_cancelled"), p.get("wait_s")) for n, p in server.calls[1:]] \
            == [("get_run", "r1", True, 600), ("get_run", "r1", True, 600)]

    async def test_a_second_call_of_the_same_agent_run_resumes_with_the_new_messages(self, instance):
        server = instance(done("first"), done("second"))
        llm = client()
        await llm.chat_tools(MESSAGES, [])
        more = MESSAGES + [{"role": "assistant", "content": "first"}, {"role": "user", "content": "Shorter."}]
        assert (await llm.chat_tools(more, []))["assistant"]["content"] == "second"
        resumed = server.calls[1][1]
        assert resumed["resume"] == "r1"
        # Its own answer is the CLI's last turn already: only what came after it.
        assert resumed["task"] == render(more[3:]) and "You write." not in resumed["task"]

    @pytest.mark.parametrize("second", [
        [{"role": "system", "content": "Summarize."}, {"role": "user", "content": "Tool result B."}],   # a hook's call
        [{"role": "system", "content": "Summarize."}, {"role": "user", "content": "A."},
         {"role": "user", "content": "B."}],                                     # a hook's call, longer than the run's
        [{"role": "system", "content": "You write."}],                                               # compacted
        MESSAGES])                                                                                   # nothing new
    async def test_a_call_that_does_not_go_on_from_the_last_one_starts_a_run_of_its_own(self, instance, second):
        server = instance(done("first"), done("second"))
        llm = client()
        await llm.chat_tools(MESSAGES, [])
        assert (await llm.chat_tools(second, []))["assistant"]["content"] == "second"
        fresh = server.calls[1][1]
        assert "resume" not in fresh and fresh["task"] == render(second)

    async def test_a_continued_session_resumes_its_run_with_a_client_of_its_own(self, instance):
        """A sub-agent continued: a new agent run (request id) of the same session, built anew -- the CLI's
        conversation goes on, with only the new message."""
        server = instance(done(), done("again"))
        await client().chat_tools(MESSAGES, [])
        more = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        token = current_request_id.set("sub_cont_2")
        try:
            assert (await client().chat_tools(more, []))["assistant"]["content"] == "again"
        finally:
            current_request_id.reset(token)
        assert (server.calls[1][1]["resume"], server.calls[1][1]["task"]) == ("r1", render(more[3:]))

    @pytest.mark.parametrize("context", [current_run_session, current_run_user], ids=["session", "user"])
    async def test_another_session_or_user_starts_a_run_of_its_own(self, instance, context):
        server = instance(done(), done())
        await client().chat_tools(MESSAGES, [])
        more = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        token = context.set("other")
        try:
            await client().chat_tools(more, [])
        finally:
            context.reset(token)
        assert "resume" not in server.calls[1][1]

    async def test_a_hooks_call_in_between_leaves_the_agent_its_run(self, instance):
        server = instance(done(), done("summary", run_id="r2"), done("again", run_id="r3"))
        await client().chat_tools(MESSAGES, [])
        await client().chat_tools([{"role": "system", "content": "Summarize."}, {"role": "user", "content": "X"}], [])
        more = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        await client().chat_tools(more, [])
        assert [p.get("resume") for _, p in server.calls] == [None, None, "r1"]

    async def test_the_longest_conversation_it_goes_on_from_is_resumed(self, instance):
        server = instance(done(), done("second", run_id="r2"), done("third", run_id="r3"))
        await client().chat_tools(MESSAGES, [])
        two = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        await client().chat_tools(two, [])
        three = two + [{"role": "assistant", "content": "second"}, {"role": "user", "content": "Again."}]
        await client().chat_tools(three, [])
        assert [(p.get("resume"), p["task"]) for _, p in server.calls[1:]] == [
            ("r1", render(two[3:])), ("r2", render(three[5:]))]

    @pytest.mark.parametrize("refusal", ["resume: another run works in that worktree",
                                         "pin: a resume keeps run r1's -- {}",
                                         "not started: the worktree carries .agents/mcp_config.json -- agy would",
                                         "not started: run r1 holds no usable session id"])
    async def test_a_run_that_cannot_be_resumed_is_started_fresh_not_given_up(self, instance, refusal):
        from agent_system.core.request_context import current_tool_bindings

        bindings = {"files/*_write": {"path": "out.md"}}
        server = instance(done(), {"status": "error", "error": refusal}, done("fresh", run_id="r2"))
        token = current_tool_bindings.set(bindings)
        try:
            await client().chat_tools(MESSAGES, [])
            more = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
            assert (await client().chat_tools(more, []))["assistant"]["content"] == "fresh"
        finally:
            current_tool_bindings.reset(token)
        assert [p.get("resume") for _, p in server.calls] == [None, "r1", None]
        assert (server.calls[2][1]["task"], server.calls[2][1]["pin"]) == (render(more), bindings)

    async def test_a_run_another_has_gone_on_from_is_not_resumed_again(self, instance):
        """r2 went on from r1: the CLI's conversation holds r2's turns. A history that matches r1's but not r2's --
        compacted after the first call -- would get them twice in r1's conversation: it starts afresh."""
        server = instance(done(), done("second", run_id="r2"), done("third", run_id="r3"))
        await client().chat_tools(MESSAGES, [])
        two = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        await client().chat_tools(two, [])
        compacted = MESSAGES + [{"role": "user", "content": "Summary of what came after."}]
        await client().chat_tools(compacted, [])
        assert [p.get("resume") for _, p in server.calls] == [None, "r1", None]

    @pytest.mark.parametrize("ending", [{"status": "success", "run_id": "r2", "state": "failed", "turns": 3},
                                        done("", run_id="r2")], ids=["failed", "no_answer"])
    async def test_a_resumed_run_that_failed_is_not_resumed_from_again(self, instance, ending):
        """It went on, so the CLI's conversation holds its turns -- whether it answered or not."""
        server = instance(done(), ending, done("third", run_id="r3"))
        await client().chat_tools(MESSAGES, [])
        two = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        with pytest.raises(LLMServerError):
            await client().chat_tools(two, [])
        await client().chat_tools(two, [])
        assert [p.get("resume") for _, p in server.calls] == [None, "r1", None]

    async def test_a_resume_held_up_for_another_reason_is_not_started_fresh(self, instance):
        """Busy, or the budget: a fresh run would be held up the same way -- the chain moves on."""
        server = instance(done(), {"status": "error", "error": "not started: 1 run(s) still going (r9) -- wait"},
                          done("third", run_id="r3"))
        await client().chat_tools(MESSAGES, [])
        two = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        with pytest.raises(LLMServerError):
            await client().chat_tools(two, [])
        await client().chat_tools(two, [])        # refused before it started: r1 is still the one to go on with
        assert [p.get("resume") for _, p in server.calls] == [None, "r1", "r1"]

    async def test_an_answer_of_another_link_goes_with_the_resumed_task(self, instance):
        """Only the run's own answer is the CLI's last turn: one another link of the chain gave is news to it."""
        server = instance(done(), done("again", run_id="r2"))
        await client().chat_tools(MESSAGES, [])
        more = MESSAGES + [{"role": "assistant", "content": "the API's answer"}, {"role": "user", "content": "Go."}]
        await client().chat_tools(more, [])
        assert (server.calls[1][1]["resume"], server.calls[1][1]["task"]) == ("r1", render(more[2:]))


class TestFallback:
    async def test_the_subscriptions_limit_blocks_the_link_until_it_resets(self, instance):
        import time

        instance({"status": "error", "error": "not started: 5h window at 92 %",
                  "limit": {"window": "five_hour", "resets_at": time.time() + 3600, "resets": "21:00"}})
        with pytest.raises(LLMQuotaExhaustedError) as raised:
            await client().chat_tools(MESSAGES, [])
        assert 3500 < raised.value.retry_after <= 3600

    async def test_a_limit_without_a_reset_time_still_blocks(self, instance):
        instance({"status": "success", "run_id": "r1", "state": "failed", "note": "stopped by the subscription",
                  "limit": {"window": "seven_day", "resets_at": None}})
        with pytest.raises(LLMQuotaExhaustedError) as raised:
            await client().chat_tools(MESSAGES, [])
        assert raised.value.retry_after is None

    @pytest.mark.parametrize("answer,words", [
        ({"status": "error", "error": "not started: 2 run(s) still going"}, "still going"),
        ({"status": "success", "run_id": "r1", "state": "failed", "note": "git broke"}, "git broke"),
        (done(result_cut=True), "longer than the instance hands on"),
        (done("  "), "without an answer"),
        ("not a dict", "no answer from coding_cli"),
        (PermissionError("runs/r1.json is locked"), "PermissionError: runs/r1.json is locked")])
    async def test_a_run_that_brings_no_answer_moves_the_chain_on(self, instance, caplog, answer, words):
        instance(answer)
        with caplog.at_level(logging.WARNING, logger="plugins.llm_coding_cli.client"), \
                pytest.raises(LLMServerError, match=words):
            await client().chat_tools(MESSAGES, [])
        assert any("next link of the chain answers" in r.getMessage() for r in caplog.records), "a silent fallback"

    @pytest.mark.parametrize("provider,turns,why,second,runs", [
        ("antigravity", 0, "authentication failed or timed out", done("late"), 2),  # renewed meanwhile: it answers
        ("antigravity", 0, "authentication failed or timed out", "again", 2),       # really gone: once, then on
        ("antigravity", 3, "authentication failed or timed out", None, 1),          # it ran: not the login at start
        ("antigravity", 0, "the model is overloaded", None, 1),                     # another failure before a turn
        ("claude_code", 0, "authentication failed or timed out", None, 1)])         # agy only
    async def test_an_agy_run_that_found_its_login_expired_starts_once_more(self, instance, monkeypatch, provider,
                                                                             turns, why, second, runs):
        import plugins.llm_coding_cli.client as module

        monkeypatch.setattr(module, "AUTH_RETRY_S", 0)
        monkeypatch.setattr(module, "_login_lost_until", {})   # what an earlier case's lost login left
        expired = {"status": "success", "run_id": "r1", "state": "failed", "turns": turns,
                   "result": {"untrusted": True, "content": why}}
        server = instance(expired, expired if second == "again" else second,
                          cli=module.CLI_OF[provider])
        if second is not None and second != "again":
            assert (await client(provider).chat_tools(MESSAGES, []))["assistant"]["content"] == "late"
        else:
            with pytest.raises(LLMServerError, match="failed"):
                await client(provider).chat_tools(MESSAGES, [])
        assert [name for name, _ in server.calls] == ["run_task"] * runs

    async def test_a_login_that_is_gone_costs_no_wait_per_call(self, instance, monkeypatch):
        """Its second run failed the same way: the next calls on that instance -- another client's too -- start one
        run each, until a run answers again."""
        import plugins.llm_coding_cli.client as module

        monkeypatch.setattr(module, "AUTH_RETRY_S", 0)
        monkeypatch.setattr(module, "_login_lost_until", {})
        expired = {"status": "success", "run_id": "r1", "state": "failed", "turns": 0,
                   "result": {"untrusted": True, "content": "authentication failed or timed out"}}
        server = instance(expired, expired, expired, done("back"), expired, expired, cli="antigravity")
        with pytest.raises(LLMServerError):
            await client("antigravity").chat_tools(MESSAGES, [])
        with pytest.raises(LLMServerError):
            await client("antigravity").chat_tools(MESSAGES, [])     # another agent's client: the same login
        assert len(server.calls) == 3, server.calls          # two for the first call, one for the next
        assert (await client("antigravity").chat_tools(MESSAGES, []))["assistant"]["content"] == "back"
        with pytest.raises(LLMServerError):
            await client("antigravity").chat_tools(MESSAGES, [])
        assert len(server.calls) == 6, "after a run that answered, a login moment gets its second run again"

    async def test_one_users_lost_login_leaves_anothers_its_second_run(self, instance, monkeypatch):
        """agy may keep the login in the user's home of the instance: one user's lost login is not another's."""
        import plugins.llm_coding_cli.client as module

        monkeypatch.setattr(module, "AUTH_RETRY_S", 0)
        monkeypatch.setattr(module, "_login_lost_until", {})
        expired = {"status": "success", "run_id": "r1", "state": "failed", "turns": 0,
                   "result": {"untrusted": True, "content": "authentication failed or timed out"}}
        server = instance(expired, expired, expired, done("bob's"), cli="antigravity")
        with pytest.raises(LLMServerError):
            await client("antigravity").chat_tools(MESSAGES, [])      # ada's: gone
        token = current_run_user.set("bob")
        try:
            assert (await client("antigravity").chat_tools(MESSAGES, []))["assistant"]["content"] == "bob's"
        finally:
            current_run_user.reset(token)
        assert len(server.calls) == 4, server.calls

    async def test_no_run_without_the_agents_session(self, instance):
        server = instance(done())
        token = current_run_session.set(None)
        try:
            with pytest.raises(LLMServerError, match="request, user and session"):
                await client().chat_tools(MESSAGES, [])
        finally:
            current_run_session.reset(token)
        assert server.calls == []

    async def test_a_missing_instance_or_another_cli_moves_the_chain_on(self, instance, monkeypatch):
        instance(done(), cli="antigravity")
        with pytest.raises(LLMServerError, match="needs 'claude'"):
            await client("claude_code").chat_tools(MESSAGES, [])
        assert (await client("antigravity").chat_tools(MESSAGES, []))["assistant"]["content"] == "the answer"
        monkeypatch.setattr(app_state, "registry", ToolServerRegistry())
        with pytest.raises(LLMServerError, match="not running in this process"):
            await client().chat_tools(MESSAGES, [])


class TestCancel:
    async def test_cancelling_the_agent_stops_the_wait_and_so_the_run(self, instance):
        server = instance({"status": "success", "run_id": "r1", "state": "running"}, "hang")
        token = CancellationToken("req-1")
        call = asyncio.ensure_future(client().chat_tools(MESSAGES, [], cancellation_token=token))
        await asyncio.wait_for(server.waiting.wait(), 5)
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, 5)
        assert server.cancelled, "the get_run that waits was not cancelled -- coding_cli would not stop the run"

    async def test_a_cancel_that_misses_the_wait_stops_the_run_all_the_same(self, instance):
        server = instance({"status": "success", "run_id": "r1", "state": "running"}, "hang unheard")
        token = CancellationToken("req-1")
        call = asyncio.ensure_future(client().chat_tools(MESSAGES, [], cancellation_token=token))
        await asyncio.wait_for(server.waiting.wait(), 5)
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, 5)
        assert not server.cancelled, "the stand-in's wait heard the cancel: this test would prove nothing"
        assert server.stopped and ("cancel_run", "r1") in [(n, p.get("run_id")) for n, p in server.calls]


class TestDebuggerNotices:
    async def test_one_pre_and_one_post_notice_per_call_and_the_failure_named(self, instance):
        instance(done(), {"status": "error", "error": "not started: busy"})
        seen = []

        async def pre(info):
            seen.append(("pre", info))

        async def post(info):
            seen.append(("post", info))

        llm = client()
        llm._on_pre_llm_request, llm._on_post_llm_response = pre, post
        await llm.chat_tools(MESSAGES, [])
        with contextlib.suppress(LLMServerError):
            token = current_request_id.set("req-2")
            try:
                await llm.chat_tools(MESSAGES, [])
            finally:
                current_request_id.reset(token)
        assert [kind for kind, _ in seen] == ["pre", "post", "pre", "post"]
        ok, failed = seen[1][1], seen[3][1]
        assert (ok["model"], ok["usage"]["cost"], ok["error"], ok["routing"]["selected"]) == (
            "opus-x", 0.0, None, "claude_code:cc")
        assert "not started: busy" in failed["error"]

    async def test_a_refused_resume_and_the_fresh_run_are_two_requests(self, instance):
        instance(done(), {"status": "error", "error": "resume: another run works in that worktree"},
                 done("fresh", run_id="r2"))
        seen = []

        async def pre(info):
            seen.append(("pre", info))

        async def post(info):
            seen.append(("post", info))

        await client().chat_tools(MESSAGES, [])
        llm = client()
        llm._on_pre_llm_request, llm._on_post_llm_response = pre, post
        more = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        await llm.chat_tools(more, [])
        assert [kind for kind, _ in seen] == ["pre", "post", "pre", "post"]
        assert seen[0][1]["payload"]["resume"] == "r1" and "resume refused" in seen[1][1]["error"]
        assert seen[2][1]["payload"]["task"] == render(more) and "resume" not in seen[2][1]["payload"]
        assert seen[3][1]["error"] is None

    async def test_a_cancel_in_the_refused_resumes_notice_answers_it_once(self, instance):
        instance(done(), {"status": "error", "error": "resume: another run works in that worktree"})
        seen = []

        async def pre(info):
            seen.append(("pre", info))

        async def post(info):
            seen.append(("post", info))
            raise asyncio.CancelledError

        await client().chat_tools(MESSAGES, [])
        llm = client()
        llm._on_pre_llm_request, llm._on_post_llm_response = pre, post
        more = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        with pytest.raises(asyncio.CancelledError):
            await llm.chat_tools(more, [])
        assert [kind for kind, _ in seen] == ["pre", "post"]

    async def test_a_fresh_run_that_fails_after_a_refused_resume_is_answered_once(self, instance):
        instance(done(), {"status": "error", "error": "resume: another run works in that worktree"},
                 {"status": "error", "error": "not started: busy"})
        seen = []

        async def note(kind, info):
            seen.append((kind, info))

        await client().chat_tools(MESSAGES, [])
        llm = client()
        llm._on_pre_llm_request = lambda info: note("pre", info)
        llm._on_post_llm_response = lambda info: note("post", info)
        more = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        with pytest.raises(LLMServerError):
            await llm.chat_tools(more, [])
        assert [kind for kind, _ in seen] == ["pre", "post", "pre", "post"]
        assert "not started: busy" in seen[3][1]["error"]


class TestUsage:
    """The run's tokens reach the usage store with the cache in them: the tracker reads them through
    normalize_usage, so that is what each case is read back by."""

    @pytest.mark.parametrize("raw, prompt, cached, written, completion", [
        # Claude Code: input_tokens is only what neither came from nor went into the cache
        ({"input_tokens": 18, "cache_creation_input_tokens": 661, "cache_read_input_tokens": 5963,
          "output_tokens": 291}, 18 + 661 + 5963, 5963, 661, 291),
        # agy, as measured on a server: the cache reads beside input_tokens (here five times it), the thinking
        # inside output_tokens (total_tokens = input + output)
        ({"input_tokens": 94119, "output_tokens": 5423, "thinking_tokens": 0, "cache_read_tokens": 483751,
          "total_tokens": 99542}, 94119 + 483751, 483751, 0, 5423),
        ({"input_tokens": 158036, "output_tokens": 14179, "thinking_tokens": 12166, "cache_read_tokens": 432979,
          "total_tokens": 172215}, 158036 + 432979, 432979, 0, 14179),
        (None, 0, 0, 0, 0)], ids=["claude_code", "agy", "agy_thinking", "none"])
    async def test_the_runs_tokens_and_cache_are_counted_at_no_cost(self, instance, raw, prompt, cached, written,
                                                                     completion):
        from agent_system.llm.pricing import normalize_usage

        instance(done(**({"usage": raw} if raw else {})))
        posted = []

        async def post(info):
            posted.append(info)

        llm = client()
        llm._on_post_llm_response = post
        usage = (await llm.chat_tools(MESSAGES, []))["usage"]
        read = normalize_usage(usage)
        assert (read.prompt_tokens, read.cached_tokens, read.cache_write_tokens, read.completion_tokens) == (
            prompt, cached, written, completion)
        assert read.provider_cost == 0.0 and usage["total_tokens"] == prompt + completion
        assert posted[0]["usage"] == usage
        # A sum over the CLI's own model calls: never the size of the agent's context (the usage store's flag)
        assert usage["reflects_context"] is False


def test_render_keeps_order_roles_and_says_what_it_cannot_pass_on():
    text = render([{"role": "system", "content": "Rules."},
                   {"role": "user", "content": [{"type": "text", "text": "Look"}, {"type": "image_url"}]},
                   {"role": "assistant", "content": None, "tool_calls": [
                       {"function": {"name": "store_read", "arguments": "{\"id\": 1}"}}]},
                   {"role": "tool", "content": "{\"ok\": true}"}])
    assert text == ("# Instructions\n\nRules.\n\n## user\n\nLook\n[image_url not passed on]\n\n"
                    "## assistant\n\n[called store_read with {\"id\": 1}]\n\n## tool\n\n{\"ok\": true}")


def test_the_registry_builds_both_providers_from_a_model_entry():
    from agent_system.llm import registry

    for provider in ("claude_code", "antigravity"):
        built = registry.get_provider(provider)(LLMModelConfig(provider=provider, model="cc"))  # conftest fakes build_client
        assert isinstance(built, CodingCliClient) and (built.provider, built.instance) == (provider, "cc")
    with pytest.raises(ValueError, match="names the coding_cli instance"):
        registry.get_provider("claude_code")(LLMModelConfig(provider="claude_code"))
