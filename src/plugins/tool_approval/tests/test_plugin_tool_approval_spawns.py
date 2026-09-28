"""Sub-runs and spawns: a run's rules cannot be left behind by starting other work.

Through the real path: a parent Agent whose model calls a real
SubAgentManagerServer, which starts real child Agents (scripted models) with
the hierarchical request ids it mints, on a real SessionService in tmp_path.
What is pinned:

* a sub-run whose agent has the hook on is held to its parent's deny rules,
  asks where its parent asks (in the parent's stream), cannot widen what its
  parent lets through unasked, and gets the strictest ``unattended``;
* a spawn of work that passes no approval -- a sub-agent with the hook off
  (create and continue), an agent called as a tool, Claude Code, a state
  machine -- is blocked in auto, asked with a warning in ask even where an
  allow rule names the call, and blocked where nobody can be asked;
* inside a script nothing is asked: deny rules (inherited ones too) and
  unguarded spawns block, everything else runs;
* the policies are kept while a run below them lives, and dropped after.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.core.request_context import register_request_user
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder, run_is_live
from agent_system.servers.agent.components.tool_execution import ToolDispatchError
from agent_system.servers.agent.server import Agent
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from agent_system.tools.base import ToolServer
from plugins.sub_agent_manager.server import SubAgentManagerServer
from plugins.tool_approval.policy import Policy, PolicyStore
from plugins.tool_approval.rules import parse_rules
from plugins.tool_approval.spawns import infer_sub_agent_operation
from plugins.tool_approval.tests.test_plugin_tool_approval_hook import (  # noqa: F401 -- fixtures
    _agent,
    _Model,
    _Probe,
    _question_of,
    approval,
    watched,
)


def _call(call_id: str, tool: str, **arguments: Any) -> Dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}


def _create(call_id: str, agent_type: str, **extra: Any) -> Dict[str, Any]:
    return _call(call_id, "sam_manage_sub_agent", operation="create", agent_type=agent_type, task="go", **extra)


class _Plain(ToolServer):
    """A server of a given plugin type, with one tool per name; records calls."""

    def __init__(self, name: str, kind: str, tools: List[str]):
        super().__init__(name, AgentSystemConfig(), ToolServerConfig(type=kind, enabled=True))
        self._tools = tools
        self.calls: List[str] = []

    def get_tools(self):
        return [{"type": "function", "function": {"name": f"{self.name}_{tool}", "description": tool,
                                                   "parameters": {"type": "object", "properties": {}}}}
                for tool in self._tools]

    async def call(self, tool, params):
        self.calls.append(tool)
        return {"status": "ok"}


class _Family:
    """A parent agent, a sub-agent manager, and child agents -- one with the
    approval hook on (``guarded``), one without (``open``) -- sharing a
    registry, a probe server and a session store."""

    def __init__(self, tmp_path, parent: Dict[str, Any], guarded: Dict[str, Any]):
        self.service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))
        self.probe = _Probe()
        self.parent = _agent(self.probe, parent)
        registry = self.parent.registry
        registry.register("sam", SubAgentManagerServer(
            "sam", AgentSystemConfig(), ToolServerConfig(type="sub_agent_manager", enabled=True, allowed_agents=["*"])))
        self.parent.agent_config.tools.allowed = ["probe/*", "sam/*", "coder/*", "stategraph/*", "child_open/*"]
        self.children: Dict[str, Agent] = {}
        for name, override in (("child_guarded", guarded), ("child_open", None)):
            child = _agent(self.probe, override, name=name, registry=registry)
            registry.register(name, child)
            self.children[name] = child
        for agent in (self.parent, *self.children.values()):
            agent._session_service = self.service

    def child(self, name: str, *rounds) -> Agent:
        agent = self.children[name]
        agent.llm = _Model(*rounds)
        return agent

    async def run(self, request_id: str, *rounds, session_id: str = "sess-p", answer=None) -> List[Dict[str, Any]]:
        self.parent.llm = _Model(*rounds)
        register_request_user(request_id, "alice")
        events = []
        asked = set()
        async for event in self.parent.run_events("go", request_id=request_id, session_id=session_id):
            events.append(event)
            question = _question_of(event)
            if question is not None and answer is not None and question["id"] not in asked:
                asked.add(question["id"])
                await answer(question)
        return events

    def parent_results(self) -> List[Dict[str, Any]]:
        return self.parent.llm.results()

    def texts(self) -> List[str]:
        return [f"{r['tool']}:{r['text']}" for r in self.probe.received]


def _questions(events) -> List[Dict[str, Any]]:
    return [q for q in map(_question_of, events) if q is not None]


class TestSubRunsInherit:

    async def test_a_sub_agent_is_held_to_its_parents_deny_rules(self, approval, tmp_path):
        await approval()
        family = _Family(tmp_path, {"mode": "auto", "deny": ["probe/probe_wipe"]}, {"mode": "off"})
        child = family.child("child_guarded", [_call("c1", "probe_wipe", text="x"), _call("c2", "probe_echo", text="y")])

        await family.run("inherit1", [_create("p1", "child_guarded")])

        assert family.texts() == ["probe_echo:y"]
        blocked = child.llm.results()[0]
        assert blocked["type"] == "ToolCallBlocked" and "probe/probe_wipe" in blocked["error"]

    async def test_a_parent_in_ask_makes_the_sub_agent_ask_in_the_parents_stream(self, approval, watched, tmp_path):
        plugin = await approval()
        family = _Family(tmp_path, {"mode": "ask", "allow": ["sam/*"]}, {"mode": "auto"})
        family.child("child_guarded", [_call("c1", "probe_echo", text="y")])

        async def allow(question):
            plugin.broker.answer(question["id"], "allow_once")

        events = await family.run(watched("inherit2"), [_create("p1", "child_guarded")], answer=allow)

        [question] = _questions(events)
        assert question["tool"] == "probe_echo" and question["request_id"].startswith("inherit2_")
        assert family.texts() == ["probe_echo:y"]

    async def test_a_sub_agent_cannot_widen_what_its_parent_lets_through_unasked(self, approval, watched, tmp_path):
        plugin = await approval()
        family = _Family(tmp_path, {"mode": "ask", "allow": ["sam/*"]}, {"mode": "ask", "allow": ["probe/*"]})
        family.child("child_guarded", [_call("c1", "probe_echo", text="y")])

        async def allow(question):
            plugin.broker.answer(question["id"], "allow_once")

        events = await family.run(watched("inherit3"), [_create("p1", "child_guarded")], answer=allow)

        assert [q["tool"] for q in _questions(events)] == ["probe_echo"], "the child's allow rule widened the parent's"

    async def test_an_async_sub_agent_inherits_after_its_parent_ended(self, approval, tmp_path, monkeypatch):
        """create(blocking=false): the manager sets the sub-agent up after the
        parent's run has ended, and a sweep in that gap -- before the sub-agent's
        own stream begins -- must not take the parent's policy with it."""
        plugin = await approval()
        family = _Family(tmp_path, {"mode": "auto", "deny": ["probe/probe_wipe"]}, {"mode": "off"})
        child = family.child("child_guarded", [_call("c1", "probe_wipe", text="x"), _call("c2", "probe_echo", text="y")])
        sam = family.parent.registry.get("sam")
        prepare = sam._prepare_agent
        swept_after_the_end: List[bool] = []

        async def prepared_after_a_sweep(*args, **kwargs):
            for _ in range(250):                          # until the parent's run has ended
                if not run_is_live("async1"):
                    break
                await asyncio.sleep(0.02)
            plugin.policies._last_sweep = float("-inf")   # the next record sweeps
            plugin.policies.record("sweeper1", Policy.own("off", (), (), "block"))
            swept_after_the_end.append(not run_is_live("async1"))
            return await prepare(*args, **kwargs)

        monkeypatch.setattr(sam, "_prepare_agent", prepared_after_a_sweep)
        await family.run("async1", [_create("p1", "child_guarded", blocking=False)])
        for _ in range(250):
            if len(child.llm.requests) >= 2:
                break
            await asyncio.sleep(0.02)

        assert swept_after_the_end == [True], "fixture: no sweep while neither run streamed"
        assert len(child.llm.requests) >= 2, "fixture: the async sub-agent did not run"
        assert family.texts() == ["probe_echo:y"]
        assert child.llm.results()[0]["type"] == "ToolCallBlocked"

    async def test_an_agent_called_as_a_tool_inherits(self, approval):
        """Its run has the id of the call that started it (<run>_004)."""
        await approval()
        probe = _Probe()
        parent = _agent(probe, {"mode": "auto", "deny": ["probe/probe_wipe"]})
        tool_agent = _agent(probe, {"mode": "off"}, name="helper")
        tool_agent.llm = _Model([_call("c1", "probe_wipe", text="x")])
        call = {"id": "p1", "name": "helper_execute_task", "server": "helper", "arguments": {}, "source": "model"}
        parent.registry.register("helper", tool_agent)
        _, block = await parent._hook_manager.execute_pre_tool_hooks(call, step=1, request_id="astool1", session_id="s")
        assert block is None, "fixture: the parent's call was blocked"

        [_ async for _ in tool_agent.run_events("task", request_id="astool1_004", session_id="s-helper")]

        assert probe.received == []
        assert tool_agent.llm.results()[0]["type"] == "ToolCallBlocked"

    async def test_nobody_reading_holds_the_sub_run_to_the_strictest_unattended(self, approval, watched, tmp_path):
        await approval()
        family = _Family(tmp_path, {"mode": "ask", "allow": ["sam/*"], "unattended": "block"},
                         {"mode": "ask", "unattended": "allow"})
        child = family.child("child_guarded", [_call("c1", "probe_echo", text="y")])

        await family.run(watched("inherit4", attended=False), [_create("p1", "child_guarded")])

        assert family.texts() == []
        assert "nobody is watching" in child.llm.results()[0]["error"]


class TestUnguardedSpawns:

    async def test_auto_blocks_a_sub_agent_without_approvals(self, approval, tmp_path):
        await approval()
        family = _Family(tmp_path, {"mode": "auto"}, {})
        open_child = family.child("child_open", [_call("c1", "probe_wipe", text="x")])

        await family.run("open1", [_create("p1", "child_open")])

        error = family.parent_results()[0]["error"]
        assert "agent 'child_open'" in error and "without tool approvals" in error
        assert "Use an agent that has approvals" in error, "blocked for another reason than auto's"
        assert open_child.llm.requests == [] and family.texts() == []

    async def test_a_create_without_its_operation_is_found_too(self, approval, tmp_path):
        """The manager infers `create` from agent_type alone; so does the check."""
        await approval()
        family = _Family(tmp_path, {"mode": "auto"}, {})
        open_child = family.child("child_open", [_call("c1", "probe_wipe", text="x")])

        await family.run("open2", [_call("p1", "sam_manage_sub_agent", agent_type="child_open", task="go")])

        assert "without tool approvals" in family.parent_results()[0]["error"]
        assert open_child.llm.requests == []

    async def test_ask_asks_with_a_warning_though_an_allow_rule_names_the_spawn(self, approval, watched, tmp_path):
        plugin = await approval()
        family = _Family(tmp_path, {"mode": "ask", "allow": ["sam/*"]}, {})
        family.child("child_open", [_call("c1", "probe_wipe", text="x")])

        async def allow(question):
            plugin.broker.answer(question["id"], "allow_once")

        events = await family.run(watched("open3"), [_create("p1", "child_open")], answer=allow)

        [question] = _questions(events)
        assert question["tool"] == "sam_manage_sub_agent"
        assert "child_open" in question["warning"] and "WITHOUT tool approvals" in question["warning"]
        assert family.texts() == ["probe_wipe:x"], "the person allowed a sub-agent without approvals"

    async def test_nobody_to_ask_blocks_it_whatever_unattended_says(self, approval, watched, tmp_path):
        await approval()
        family = _Family(tmp_path, {"mode": "ask", "allow": ["sam/*"], "unattended": "allow"}, {})
        open_child = family.child("child_open", [_call("c1", "probe_wipe", text="x")])

        await family.run(watched("open4", attended=False), [_create("p1", "child_open")])

        assert "nobody is watching" in family.parent_results()[0]["error"]
        assert open_child.llm.requests == []

    async def test_a_grant_for_the_tool_does_not_cover_a_spawn_without_approvals(self, approval, watched, tmp_path):
        plugin = await approval()
        family = _Family(tmp_path, {"mode": "ask"}, {})
        family.child("child_guarded", [])
        family.child("child_open", [])
        answers = iter(["allow_session", "deny"])

        async def answer(question):
            plugin.broker.answer(question["id"], next(answers))

        events = await family.run(watched("open5"), [_create("p1", "child_guarded")], [_create("p2", "child_open")],
                                  answer=answer)

        questions = _questions(events)
        assert [q["warning"] is None for q in questions] == [True, False], \
            "a grant for spawning an agent with approvals let one without them through"

    async def test_continuing_a_sub_agent_is_checked_like_creating_it(self, approval, tmp_path):
        """The target of a continue is the agent of the sub-session named."""
        await approval()
        family = _Family(tmp_path, {"mode": "off"}, {"mode": "off"})   # the child takes the parent's mode
        family.child("child_guarded", [])
        family.child("child_open", [])
        await family.run("cont1", [_create("p1", "child_open"), _create("p2", "child_guarded")])
        open_id, guarded_id = [r["instance_id"] for r in family.parent_results()]

        family.parent.agent_config.hooks.overrides["tool_approval.check_tool_call"]["mode"] = "auto"
        open_child = family.child("child_open", [_call("c1", "probe_echo", text="open")])
        family.child("child_guarded", [_call("c1", "probe_echo", text="guarded")])
        await family.run("cont2", [_call("p3", "sam_manage_sub_agent", operation="continue", instance_id=open_id,
                                         message="more")],
                         [_call("p4", "sam_manage_sub_agent", operation="continue", instance_id=guarded_id,
                                message="more")])

        [blocked, continued] = family.parent_results()[-2:]   # the session holds the first run's too
        assert "agent 'child_open'" in blocked["error"] and open_child.llm.requests == []
        assert continued.get("status") != "error", continued
        assert family.texts() == ["probe_echo:guarded"]

    async def test_another_sessions_sub_agent_is_left_to_the_manager(self, approval, tmp_path):
        """The manager refuses to continue a sub-agent of another session: that is
        its answer, not a blocked spawn."""
        await approval()
        family = _Family(tmp_path, {"mode": "off"}, {})
        family.child("child_open", [])
        await family.run("foreign1", [_create("p1", "child_open")], session_id="sess-other")
        [foreign] = [r["instance_id"] for r in family.parent_results()]

        family.parent.agent_config.hooks.overrides["tool_approval.check_tool_call"]["mode"] = "auto"
        await family.run("foreign2", [_call("p2", "sam_manage_sub_agent", operation="continue", instance_id=foreign,
                                            message="more")])

        result = family.parent_results()[-1]
        assert result.get("type") != "ToolCallBlocked" and "does not belong" in json.dumps(result), result

    async def test_other_work_that_passes_no_approval_is_a_spawn_too(self, approval, tmp_path):
        """An agent called as a tool, Claude Code, a state machine that runs on."""
        await approval()
        family = _Family(tmp_path, {"mode": "auto"}, {})
        registry = family.parent.registry
        coder = _Plain("coder", "coding_cli", ["run_task", "get_run"])
        machines = _Plain("stategraph", "stategraph", ["run_machine", "control_run", "get_run", "send_event"])
        registry.register("coder", coder)
        registry.register("stategraph", machines)
        hooks = family.parent._hook_manager

        async def decide(tool, server, **arguments):
            call = {"id": "x", "name": tool, "server": server, "arguments": arguments, "source": "model"}
            _, block = await hooks.execute_pre_tool_hooks(call, step=1, request_id="other1", session_id="s")
            return block

        assert "agent 'child_open'" in await decide("child_open_execute_task", "child_open", task="t")
        assert await decide("child_guarded_execute_task", "child_guarded", task="t") is None
        # a plain Agent runs itself on any name a script may call it with
        assert "agent 'child_open'" in (await decide("child_open_list_available_tools", "child_open", task="t") or "")
        assert "Claude Code" in await decide("coder_run_task", "coder", task="t")
        assert await decide("coder_get_run", "coder", run_id="r") is None
        assert "state machine 'm1'" in await decide("stategraph_run_machine", "stategraph", machine_id="m1")
        assert "state machine" in await decide("stategraph_send_event", "stategraph", run_id="r1", name="go")
        assert await decide("stategraph_get_run", "stategraph", run_id="r1") is None
        for action in ("continue", "step", "run_to", "resume", "fork", "terminate"):
            assert "state machine" in (await decide("stategraph_control_run", "stategraph", run_id="r1",
                                                    action=action) or ""), action
        for action in ("pause", "set_breakpoints", "evaluate"):
            assert await decide("stategraph_control_run", "stategraph", run_id="r1", action=action) is None, action

    async def test_a_schema_agents_tool_list_is_no_spawn(self, approval):
        """basic_agent routes list_available_tools to a method that only lists;
        its execute_task runs the agent."""
        from agent_system.config.models import AgentConfig, ToolConfig
        from plugins.basic_agent.server import BasicAgent

        await approval()
        probe = _Probe()
        parent = _agent(probe, {"mode": "auto"})
        lister = BasicAgent("lister", parent.system_config, ToolServerConfig(
            type="basic_agent", enabled=True, agent_config=AgentConfig(llm_profile="normal", tools=ToolConfig(allowed=[]))),
            parent.registry)
        parent.registry.register("lister", lister)

        async def decide(tool):
            call = {"id": "x", "name": tool, "server": "lister", "arguments": {"task": "t"}, "source": "tool_script"}
            _, block = await parent._hook_manager.execute_pre_tool_hooks(call, step=0, request_id="lister1_001_ts01",
                                                                         session_id="s")
            return block

        assert await decide("lister_list_available_tools") is None
        assert "agent 'lister'" in (await decide("lister_execute_task") or "")

    async def test_a_state_machine_agent_is_never_guarded(self, approval, tmp_path):
        """Its tool: activities pass no hook, whatever its own config says."""
        await approval()
        family = _Family(tmp_path, {"mode": "auto"}, {})
        machine = _agent(family.probe, {"mode": "ask"}, name="machine_agent", registry=family.parent.registry)
        machine.server_config.type = "stategraph_machine"
        family.parent.registry.register("machine_agent", machine)
        machine.llm = _Model([])

        await family.run("machine1", [_create("p1", "machine_agent")])

        assert "agent 'machine_agent'" in family.parent_results()[0]["error"]
        assert machine.llm.requests == []
        call = {"id": "x", "name": "machine_agent_list_available_tools", "server": "machine_agent",
                "arguments": {"task": "t"}, "source": "tool_script"}
        _, block = await family.parent._hook_manager.execute_pre_tool_hooks(call, step=0, request_id="machine1_001_ts01",
                                                                            session_id="s")
        assert "agent 'machine_agent'" in (block or ""), "a machine agent passed as read-only"

    async def test_an_agent_the_manager_does_not_have_is_left_to_the_manager(self, approval, tmp_path):
        await approval()
        family = _Family(tmp_path, {"mode": "auto"}, {})

        await family.run("unknown1", [_create("p1", "no_such_agent")])

        result = family.parent_results()[0]
        assert result.get("type") != "ToolCallBlocked" and "not" in json.dumps(result).lower()


class TestScripts:

    async def _script_call(self, agent: Agent, tool: str, request_id: str, **params):
        return await agent.dispatch_tool_call(tool, params, session_id="s", request_id=request_id,
                                              hook_source="tool_script")

    async def test_nothing_inside_a_script_is_asked(self, approval, watched):
        plugin = await approval()
        probe = _Probe()
        agent = _agent(probe, {"mode": "ask"})
        root = watched("script1")
        stream = StatusEventForwarder()
        await stream.start_forwarding(root)
        try:
            result = await asyncio.wait_for(self._script_call(agent, "probe_echo", f"{root}_001_ts01", text="x"), 5)
        finally:
            await stream.stop_forwarding()

        assert result["status"] == "ok" and probe.received == [{"tool": "probe_echo", "text": "x"}]
        assert plugin.broker.pending() == []

    async def test_a_deny_rule_blocks_a_script_call(self, approval):
        await approval()
        probe = _Probe()
        agent = _agent(probe, {"mode": "ask", "deny": ["probe/probe_wipe"]})

        with pytest.raises(ToolDispatchError, match="not allowed for this agent"):
            await self._script_call(agent, "probe_wipe", "script2_001_ts01", text="x")
        assert probe.received == []

    async def test_an_inherited_deny_rule_blocks_a_sub_agents_script_call(self, approval):
        await approval()
        probe = _Probe()
        parent = _agent(probe, {"mode": "auto", "deny": ["probe/probe_wipe"]})
        child = _agent(probe, {"mode": "off"})
        call = {"id": "p1", "name": "probe_echo", "server": "probe", "arguments": {"text": "p"}, "source": "model"}
        await parent._hook_manager.execute_pre_tool_hooks(call, step=1, request_id="script3", session_id="s")

        with pytest.raises(ToolDispatchError, match="not allowed for this agent"):
            await self._script_call(child, "probe_wipe", "script3_002_sub_ab12_001_ts01", text="x")
        assert probe.received == []

    async def test_a_spawn_without_approvals_inside_a_script_is_blocked(self, approval, tmp_path):
        await approval()
        family = _Family(tmp_path, {"mode": "ask"}, {})
        family.child("child_open", [_call("c1", "probe_wipe", text="x")])

        with pytest.raises(ToolDispatchError, match="a script cannot ask"):
            await self._script_call(family.parent, "sam_manage_sub_agent", "script4_001_ts01",
                                    operation="create", agent_type="child_open", task="go")
        assert family.children["child_open"].llm.requests == []


class TestPolicies:

    def test_the_stricter_mode_and_every_deny_rule_hold(self):
        parent = Policy.own("ask", tuple(parse_rules(["a/*"], "deny")), tuple(parse_rules(["x/*"], "allow")), "block")
        child = Policy.own("off", tuple(parse_rules(["b/*"], "deny")), (), "allow").under(parent)

        assert child.mode == "ask"
        assert {rule.tool for rule in child.deny} == {"a/*", "b/*"}
        assert child.allows("x_t", "x", {}) and not child.allows("y_t", "y", {})
        assert child.unattended == "block"

    async def test_a_policy_lives_as_long_as_a_run_under_it(self):
        now = [0.0]
        store = PolicyStore(clock=lambda: now[0])
        parent = Policy.own("auto", (), (), "block")
        root, below = StatusEventForwarder(), StatusEventForwarder()
        await root.start_forwarding("life1")
        await below.start_forwarding("life1_004_async_ab")
        try:
            store.record("life1", parent)
            await root.stop_forwarding()           # the run ended, its async sub-agent works on
            now[0] = 1000.0
            store.record("other1", parent)         # sweeps
            assert store.inherited("life1_004_async_ab_002") is parent
        finally:
            await below.stop_forwarding()
        now[0] = 2000.0
        store.record("other2", parent)
        assert store.inherited("life1_004_async_ab_002") is None, "an ended run's policy was kept"

    def test_a_policy_outlives_its_run_long_enough_for_a_late_sub_agent(self):
        """An async sub-agent's stream starts only once the manager has set it up:
        a sweep between the parent's end and that start must not take the policy."""
        from plugins.tool_approval.policy import SWEEP_SECONDS

        now = [0.0]
        store = PolicyStore(clock=lambda: now[0])
        parent = Policy.own("auto", (), (), "block")
        store.record("late1", parent)             # no stream: the run has ended
        now[0] = SWEEP_SECONDS / 2
        store._last_sweep = -SWEEP_SECONDS
        store.record("other1", parent)            # sweeps
        assert store.inherited("late1_003_async_x") is parent
        now[0] = 2 * SWEEP_SECONDS
        store.record("other2", parent)            # sweeps
        assert store.inherited("late1_003_async_x") is None

    async def test_the_cap_forgets_ended_runs_before_a_live_one(self, monkeypatch):
        """A parent waiting on a long sub-agent is the oldest entry; past the cap
        the ended runs go first."""
        import plugins.tool_approval.policy as policy_module

        monkeypatch.setattr(policy_module, "MAX_RUNS", 3)
        store = PolicyStore()
        policy = Policy.own("auto", (), (), "block")
        live = StatusEventForwarder()
        await live.start_forwarding("cap_parent1")
        try:
            store.record("cap_parent1", policy)
            for n in range(4):
                store.record(f"cap_ended{n}", policy)
            assert store.inherited("cap_parent1_003_sub_x") is policy, "the live parent was forgotten"
            assert len(store) == 3
        finally:
            await live.stop_forwarding()

    def test_a_stale_entry_below_a_stricter_run_only_adds(self):
        """An ended client run under a reused id (``abc_003``) left its policy; the
        new run ``abc`` is stricter. Its sub-agent must be held to the new one."""
        store = PolicyStore()
        store.record("reuse1_003", Policy.own("auto", (), (), "allow"))
        strict = Policy.own("ask", tuple(parse_rules(["probe/*"], "deny")), (), "block")
        store.record("reuse1", strict)

        inherited = store.inherited("reuse1_003_sub_x")

        assert inherited.mode == "ask" and [rule.tool for rule in inherited.deny] == ["probe/*"]
        assert inherited.unattended == "block"

    def test_a_run_is_not_its_own_ancestor_and_ids_split_at_underscores(self):
        store = PolicyStore()
        policy = Policy.own("auto", (), (), "block")
        store.record("anc1", policy)

        assert store.inherited("anc1") is None
        assert store.inherited("anc1_003_sub_x") is policy
        assert store.inherited("anc10_003") is None


@pytest.mark.parametrize("arguments", [
    {"operation": "poll", "instance_id": "i"},
    {"agent_type": "a", "task": "t"},
    {"agent_type": "a", "instance_id": "i"},
    {"instance_id": "i", "message": "m"},
    {"instance_id": "i", "task": "m"},
    {"instance_id": "i"},
    {},
])
def test_the_operation_is_read_as_the_manager_reads_it(arguments):
    server = SubAgentManagerServer("sam", AgentSystemConfig(), ToolServerConfig(allowed_agents=["*"]))
    managers = arguments.get("operation") or server._infer_operation(arguments)

    assert infer_sub_agent_operation(arguments) == managers


def _hook_runs_cases() -> List[tuple]:
    from agent_system.config.models import HooksConfig as AgentHooksConfig
    name = "tool_approval.check_tool_call"
    return [
        (AgentHooksConfig(), False),
        (AgentHooksConfig(), True),
        (AgentHooksConfig(overrides={name: {"enabled": True}}), False),
        (AgentHooksConfig(overrides={name: {"enabled": False}}), True),
        (AgentHooksConfig(enabled=False, overrides={name: {"enabled": True}}), False),
        (AgentHooksConfig(overrides={name: {"mode": "ask"}}), True),
        (None, True),
    ]


@pytest.mark.parametrize("hooks, default", _hook_runs_cases())
def test_whether_the_hook_runs_for_an_agent_is_the_managers_answer(hooks, default):
    """hook_runs_for decides for a sub-agent what the agent's own
    HookIntegrationManager decides for it."""
    from types import SimpleNamespace

    from agent_system.servers.agent.components.hook_integration import HookIntegrationManager, hook_runs_for

    name = "tool_approval.check_tool_call"
    manager = HookIntegrationManager.__new__(HookIntegrationManager)
    manager._hooks_config, manager._enabled = hooks, True
    manager.agent = SimpleNamespace()
    managers = manager.is_enabled() and bool(manager.is_hook_enabled(name, default))

    assert hook_runs_for(hooks, name, default) is managers


class TestGrants:
    """"Allow for this session" holds where it was given, and nowhere looser."""

    async def test_a_script_is_never_allowed_for_the_session(self, approval, watched):
        """One click on one script's code would let every later script -- and
        every call inside one -- run unread."""
        from plugins.tool_approval.broker import AnswerRejected

        plugin = await approval()
        probe = _Probe()
        agent = _agent(probe, {"mode": "ask"})
        scripts = _Plain("ts", "tool_script", ["run_script"])
        agent.registry.register("ts", scripts)
        agent.agent_config.tools.allowed = ["probe/*", "ts/*"]
        agent.llm = _Model([_call("s1", "ts_run_script", script="a = 1\nb = 2")],
                           [_call("s2", "ts_run_script", script="c = 3\nd = 4")])
        refused: List[int] = []
        asked: List[Dict[str, Any]] = []

        async for event in agent.run_events("go", request_id=watched("grant1"), session_id="sess-g"):
            question = _question_of(event)
            if question is not None and question["id"] not in {q["id"] for q in asked}:
                asked.append(question)
                try:
                    plugin.broker.answer(question["id"], "allow_session")
                except AnswerRejected as exc:
                    refused.append(exc.status)
                    plugin.broker.answer(question["id"], "allow_once")

        assert [q["decisions"] for q in asked] == [["allow_once", "deny"]] * 2, "a later script ran unasked"
        assert refused == [422, 422]
        assert "  │ a = 1" in asked[0]["arguments"], "the script's code was not shown"
        assert scripts.calls == ["ts_run_script"] * 2

    async def test_a_grant_in_a_sub_agents_session_does_not_reach_a_stricter_chain(self, approval, watched, tmp_path):
        """Granted while its parent did not ask, a sub-agent's grant must not let
        the call through once a parent above it asks."""
        plugin = await approval()
        family = _Family(tmp_path, {"mode": "off"}, {"mode": "ask"})
        family.child("child_guarded", [_call("c1", "probe_wipe", text="first")],
                     [_call("c2", "probe_wipe", text="granted")])
        granted_in: List[str] = []

        async def for_the_session(question):
            granted_in.append(question["tool"])
            plugin.broker.answer(question["id"], "allow_session")

        await family.run(watched("chain1"), [_create("p1", "child_guarded")], answer=for_the_session)
        [child_id] = [r["instance_id"] for r in family.parent_results()]
        # where it was given, the grant holds: the second call ran unasked
        assert granted_in == ["probe_wipe"] and family.texts() == ["probe_wipe:first", "probe_wipe:granted"]

        family.parent.agent_config.hooks.overrides["tool_approval.check_tool_call"].update(
            {"mode": "ask", "allow": ["sam/*"]})
        family.child("child_guarded", [_call("c1", "probe_wipe", text="second")])
        asked: List[str] = []

        async def deny(question):
            asked.append(question["tool"])
            plugin.broker.answer(question["id"], "deny")

        await family.run(watched("chain2"), [_call("p2", "sam_manage_sub_agent", operation="continue",
                                                   instance_id=child_id, message="again")], answer=deny)

        assert asked == ["probe_wipe"], "the sub-agent's own grant let it past the parent that asks"
        assert family.texts() == ["probe_wipe:first", "probe_wipe:granted"]

    def test_a_grant_holds_only_in_every_session_it_was_given_for(self):
        from plugins.tool_approval.broker import ApprovalBroker

        broker = ApprovalBroker()
        broker.grant([("alice", "a"), ("alice", "b")], "probe/probe_wipe")

        assert broker.granted([("alice", "a"), ("alice", "b")], "probe/probe_wipe")
        assert not broker.granted([("alice", "a"), ("alice", "c")], "probe/probe_wipe")
        assert not broker.granted([("bob", "a")], "probe/probe_wipe"), "another user's grant in the same session"
        assert not broker.granted([], "probe/probe_wipe")


class TestAgentCalledAsATool:

    async def test_it_runs_in_its_callers_session_not_one_the_model_names(self, approval):
        """No agent tool offers a session_id; one in the arguments would have run
        the agent in a session of the model's choosing, under that session's grants."""
        from plugins.basic_agent.server import BasicAgent
        from agent_system.config.models import AgentConfig, ToolConfig

        probe = _Probe()
        caller = _agent(probe, None, name="caller")
        helper = _agent(probe, None, name="helper", registry=caller.registry)
        lister = BasicAgent("lister", caller.system_config, ToolServerConfig(
            type="basic_agent", enabled=True, agent_config=AgentConfig(llm_profile="normal", tools=ToolConfig(allowed=[]))),
            caller.registry)
        caller.agent_config.tools.allowed = ["helper", "lister/*"]
        sessions: List[Any] = []

        for agent in (helper, lister):
            caller.registry.register(agent.name, agent)

            async def recording(task, request_id=None, session_id=None, **kwargs):
                sessions.append(session_id)
                yield {"type": "final", "summary": "done", "content": "done"}

            agent.run_events = recording

        await caller.dispatch_tool_call("helper", {"task": "t", "session_id": "other-session"},
                                        session_id="sess-caller", request_id="astool2_001")
        await caller.dispatch_tool_call("lister_execute_task", {"task": "t", "session_id": "other-session"},
                                        session_id="sess-caller", request_id="astool2_002")

        assert sessions == ["sess-caller", "sess-caller"]


class TestOffKeepsTheInstanceRules:

    async def test_an_agent_that_says_off_keeps_the_instances_deny_rules(self, approval):
        await approval({"deny": ["probe/probe_wipe"]})
        probe = _Probe()
        agent = _agent(probe, {"mode": "off", "deny": ["probe/probe_echo"]})
        agent.llm = _Model([_call("c1", "probe_wipe", text="x"), _call("c2", "probe_echo", text="y")])

        [_ async for _ in agent.run_events("go", request_id="offdeny1", session_id="s")]

        assert probe.received == [{"tool": "probe_echo", "text": "y"}], "off dropped the instance's rule"

    async def test_an_instance_that_is_off_checks_nothing(self, approval):
        await approval({"mode": "off", "deny": ["probe/probe_wipe"]})
        probe = _Probe()
        agent = _agent(probe, {"mode": "off"})
        agent.llm = _Model([_call("c1", "probe_wipe", text="x")])

        [_ async for _ in agent.run_events("go", request_id="offdeny2", session_id="s")]

        assert probe.received == [{"tool": "probe_wipe", "text": "x"}]


class TestGrantsStayWithTheirOwner:

    async def test_a_grant_is_never_written_into_another_users_session(self, approval, watched):
        """alice starts a run under the id of bob's ended run: bob's asking level
        comes along. Her "allow for this session" must not land in bob's session."""
        plugin = await approval()
        probe = _Probe()
        bob = _agent(probe, {"mode": "ask", "allow": ["probe/probe_echo"]}, name="bobs_agent")
        register_request_user("bobrun1", "bob")
        call = {"id": "b1", "name": "probe_echo", "server": "probe", "arguments": {"text": "b"}, "source": "model"}
        _, block = await bob._hook_manager.execute_pre_tool_hooks(call, step=1, request_id="bobrun1", session_id="sess-bob")
        assert block is None, "fixture: bob's call was stopped"

        alice = _agent(probe, {"mode": "ask"}, name="alices_agent")
        alice.llm = _Model([_call("a1", "probe_wipe", text="a")], [_call("a2", "probe_wipe", text="again")])
        asked: List[str] = []
        async for event in alice.run_events("go", request_id=watched("bobrun1_x", user="alice"), session_id="sess-alice"):
            question = _question_of(event)
            if question is not None and question["id"] not in asked:
                asked.append(question["id"])
                plugin.broker.answer(question["id"], "allow_session")

        assert not plugin.broker.granted([("bob", "sess-bob")], "probe/probe_wipe"), "written into bob's session"
        assert len(asked) == 2, "a chain with bob's level held a grant"
