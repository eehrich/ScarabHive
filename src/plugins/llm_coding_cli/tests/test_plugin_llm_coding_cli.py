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


@pytest.fixture
def instance(monkeypatch):
    """Registers a stand-in under "cc" and sets the agent run's context; returns a setter for its answers."""
    made = {}

    def make(*answers, cli="claude"):
        made["server"] = _Instance(answers, cli)
        registry = ToolServerRegistry()
        registry.register("cc", made["server"])
        monkeypatch.setattr(app_state, "app_registry", registry)
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
        assert resumed["task"] == render(more[2:]) and "You write." not in resumed["task"]

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

    async def test_another_agent_run_starts_a_run_of_its_own(self, instance):
        server = instance(done(), done())
        llm = client()
        await llm.chat_tools(MESSAGES, [])
        more = MESSAGES + [{"role": "assistant", "content": "the answer"}, {"role": "user", "content": "Shorter."}]
        token = current_request_id.set("req-2")
        try:
            await llm.chat_tools(more, [])     # would go on from req-1's run, were runs not kept per agent run
        finally:
            current_request_id.reset(token)
        assert "resume" not in server.calls[1][1]


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
        monkeypatch.setattr(app_state, "app_registry", ToolServerRegistry())
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
