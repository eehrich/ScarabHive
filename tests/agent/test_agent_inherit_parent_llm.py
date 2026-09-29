"""A sub-agent that asks for it runs on the LLM its caller's run was switched to.

agent_config.inherit_parent_llm, opt-in. The profile travels in a ContextVar that
the agent loop sets for each tool task (llm/caller_llm.py), so any plugin that
starts a sub-agent inside a tool call passes it on without knowing -- the tests
start the child the way sub_agent_manager does, from inside a tool call, awaited
and as a task of its own, but through no plugin at all.

Driven through real Agents and the real factory: the parent's switch is built
the way the API builds one (create_llm_from_profile), the child's the way the
agent builds it. Only the network boundary is replaced -- registry.build_client
hands out scripted clients that record which model answered.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agent_system.config.models import (AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile,
                                        LLMSystemConfig, ToolServerConfig)
from agent_system.llm import registry as llm_registry
from agent_system.llm.caller_llm import caller_llm_profile
from agent_system.llm.factory import create_llm_from_profile
from agent_system.llm.models import LLMServerError
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry

CONFIG = AgentSystemConfig(llm_system=LLMSystemConfig(
    models={name: LLMModelConfig(provider="openai", model=f"{name}-model", api_key="fake-key")
            for name in ("own", "picked", "advanced")},
    profiles={"own": LLMProfile(model_ref="own"), "picked": LLMProfile(model_ref="picked"),
              "advanced": LLMProfile(model_ref="advanced")},
    default_profile="own",
))


class _Scripted:
    """Answers on one model; calls the tool `spawn` first when its agent has it."""

    def __init__(self, model, answered):
        self.model = model
        self.answered = answered

    def supports_streaming(self):
        return True

    def set_app_title(self, name):
        """A fallback client is built through it."""

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.answered.append(self.model)
        offered = any(t.get("function", {}).get("name") == "spawn" for t in tools or [])
        if offered and messages[-1].role == "user":
            answer = {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "spawn", "arguments": "{}"}}]}
        else:
            answer = {"role": "assistant", "content": f"done on {self.model}"}
        yield {"type": "final", "assistant": answer}


@pytest.fixture
def answered(monkeypatch):
    """Every model that answered, in order; every client the factory builds is scripted."""
    log = []
    monkeypatch.setattr(llm_registry, "build_client",
                        lambda spec, ssl_verify=None, **_: _Scripted(spec.model, log))
    return log


class _SpawnTool:
    """Starts `child` inside its call, the way a sub-agent manager does."""

    def __init__(self, start):
        self.name = "spawn"
        self.start = start
        self.seen_profile = []

    def get_default_action(self):
        return "call"

    async def list_tools(self):
        return [SimpleNamespace(name="spawn", description="Starts the child.",
                                input_schema={"type": "object", "properties": {}})]

    async def call(self, action, params):
        self.seen_profile.append(caller_llm_profile())
        await self.start(params["request_id"])
        return {"ok": True}


def _agent(name, answered, *, inherit=False, spawns=None, llm_profile="own", advanced=None):
    config = AgentConfig(max_steps=3, llm_profile=llm_profile, llm_profile_advanced=advanced,
                         inherit_parent_llm=inherit)
    registry = ToolServerRegistry()
    tool = None
    if spawns is not None:
        tool = _SpawnTool(spawns)
        registry.register("spawn", tool)
        config.tools.allowed = ["*"]
    agent = Agent(name, CONFIG, ToolServerConfig(type="agent", enabled=True, agent_config=config),
                  registry, llm=_Scripted(f"{llm_profile}-model", answered))
    return agent, tool


def _child_run(child, **kwargs):
    async def start(request_id):
        [e async for e in child.run_events("child task", request_id=f"{request_id}_async_c",
                                           session_id=f"s-{child.name}", **kwargs)]
    return start


async def _run_parent(parent, switched_to=None):
    override = create_llm_from_profile(CONFIG, switched_to) if switched_to else None
    info = f"{switched_to}:openai/{switched_to}-model" if switched_to else None
    return [e async for e in parent.run_events("parent task", request_id="prt", session_id="s-parent",
                                               llm_override=override, llm_profile_info_override=info)]


@pytest.mark.asyncio
async def test_a_child_that_asks_runs_on_the_model_its_caller_was_switched_to(answered):
    child, _ = _agent("child", answered, inherit=True)
    parent, tool = _agent("parent", answered, spawns=_child_run(child))

    await _run_parent(parent, switched_to="picked")

    assert tool.seen_profile == ["picked"], "fixture: the parent's tool never saw its switch"
    # parent: tool call + answer on its switch; child: one answer, on the same model
    assert answered == ["picked-model", "picked-model", "picked-model"], answered


@pytest.mark.asyncio
async def test_a_child_that_does_not_ask_keeps_its_own_model(answered):
    child, _ = _agent("child", answered, inherit=False)
    parent, _ = _agent("parent", answered, spawns=_child_run(child))

    await _run_parent(parent, switched_to="picked")

    assert answered == ["picked-model", "own-model", "picked-model"], answered


@pytest.mark.asyncio
async def test_a_caller_on_its_own_configuration_passes_nothing_on(answered):
    """Only a switch travels: a parent running its own chain is no reason to move."""
    child, _ = _agent("child", answered, inherit=True, llm_profile="own")
    parent, tool = _agent("parent", answered, spawns=_child_run(child), llm_profile="picked")

    await _run_parent(parent)

    assert tool.seen_profile == [None]
    assert answered == ["picked-model", "own-model", "picked-model"], answered


@pytest.mark.asyncio
async def test_an_override_on_the_callers_own_profile_is_no_switch(answered):
    """--llm-params alone, or a web pick of the agent's own profile: nothing moves."""
    child, _ = _agent("child", answered, inherit=True, llm_profile="picked")
    parent, tool = _agent("parent", answered, spawns=_child_run(child), llm_profile="own")

    await _run_parent(parent, switched_to="own")

    assert tool.seen_profile == [None]
    assert answered == ["own-model", "picked-model", "own-model"], answered


@pytest.mark.asyncio
async def test_a_child_whose_own_profile_is_the_switch_still_passes_it_on(answered):
    """It followed its caller there: the switch came from above and goes on down."""
    grandchild, _ = _agent("grandchild", answered, inherit=True, llm_profile="own")
    child, child_tool = _agent("child", answered, inherit=True, llm_profile="picked",
                               spawns=_child_run(grandchild))
    parent, _ = _agent("parent", answered, spawns=_child_run(child))

    await _run_parent(parent, switched_to="picked")

    assert child_tool.seen_profile == ["picked"]
    assert answered == ["picked-model"] * 5, answered


@pytest.mark.asyncio
async def test_a_failing_inherited_profile_falls_back_to_the_childs_own_primary(monkeypatch):
    """Its chain stays the fallback, its own primary first -- with a one-entry
    chain the only fallback there is."""
    answered = []
    built = []

    class _Failing(_Scripted):
        async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
            self.answered.append(f"{self.model} failed")
            raise LLMServerError("upstream down", provider="openai", model=self.model, status_code=503)
            yield  # pragma: no cover

    def build(spec, ssl_verify=None, **_):
        built.append(spec.model)
        # The parent's switch is the first "picked" built, the child's the second.
        failing = spec.model == "picked-model" and built.count("picked-model") > 1
        return (_Failing if failing else _Scripted)(spec.model, answered)

    monkeypatch.setattr(llm_registry, "build_client", build)
    child, _ = _agent("child", answered, inherit=True, llm_profile="own")
    parent, _ = _agent("parent", answered, spawns=_child_run(child))

    await _run_parent(parent, switched_to="picked")

    assert "picked-model failed" in answered, "fixture: the child's inherited client never failed"
    assert answered[-2] == "own-model", answered  # the child's answer, on its own primary


def test_a_batch_profile_hands_on_its_name_through_the_wrapper(answered, monkeypatch):
    """A batch model comes back wrapped; the wrapper is what the run holds."""
    from agent_system.config.models import BatchProviderConfig, BatchSystemConfig
    from agent_system.llm import factory
    from agent_system.llm.batch.batch_client import BatchLLMClient

    config = AgentSystemConfig(llm_system=LLMSystemConfig(
        models={"cheap": LLMModelConfig(provider="batch", batch_provider="openai_httpx",
                                        model="cheap-model", api_key="fake-key")},
        profiles={"cheap": LLMProfile(model_ref="cheap")},
        default_profile="cheap",
        batch=BatchSystemConfig(providers={"openai_httpx": BatchProviderConfig()}),
    ))
    monkeypatch.setattr(factory, "get_batch_queue_manager", lambda: SimpleNamespace(name="stub"))

    client = create_llm_from_profile(config, "cheap")

    assert isinstance(client, BatchLLMClient), f"fixture: came back as {type(client).__name__}"
    assert client.profile_name == "cheap"


@pytest.mark.asyncio
async def test_switching_it_on_takes_effect_on_a_config_reload(answered):
    """The agent editor flips it and reloads: no restart, which drops runs in flight."""
    child, _ = _agent("child", answered, inherit=False)
    parent, _ = _agent("parent", answered, spawns=_child_run(child))

    changes = child.reload_config(ToolServerConfig(
        type="agent", enabled=True, agent_config=AgentConfig(max_steps=3, inherit_parent_llm=True)))
    await _run_parent(parent, switched_to="picked")

    assert changes["inherit_parent_llm"] == {"old": False, "new": True}
    assert answered == ["picked-model", "picked-model", "picked-model"], answered


@pytest.mark.asyncio
async def test_a_choice_made_for_the_childs_own_run_wins(answered):
    child, _ = _agent("child", answered, inherit=True, advanced=["advanced"])
    parent, _ = _agent("parent", answered, spawns=_child_run(child, use_advanced_model=True))

    await _run_parent(parent, switched_to="picked")

    assert answered == ["picked-model", "advanced-model", "picked-model"], answered


@pytest.mark.asyncio
async def test_use_advanced_model_without_an_advanced_chain_is_no_choice(answered):
    """The request had nothing to switch to: the child follows its caller."""
    child, _ = _agent("child", answered, inherit=True)
    parent, _ = _agent("parent", answered, spawns=_child_run(child, use_advanced_model=True))

    await _run_parent(parent, switched_to="picked")

    assert answered == ["picked-model", "picked-model", "picked-model"], answered


@pytest.mark.asyncio
async def test_a_child_started_as_a_task_of_its_own_follows_too(answered):
    """A background job is created inside the tool call, and copies its context."""
    child, _ = _agent("child", answered, inherit=True)

    async def start_in_background(request_id):
        await asyncio.create_task(_child_run(child)(request_id))

    parent, _ = _agent("parent", answered, spawns=start_in_background)

    await _run_parent(parent, switched_to="picked")

    assert answered == ["picked-model", "picked-model", "picked-model"], answered


@pytest.mark.asyncio
async def test_a_grandchild_follows_its_parent_not_its_grandparent(answered):
    grandchild, _ = _agent("grandchild", answered, inherit=True)
    # The child runs its own chain: the grandchild has nothing to follow.
    child, child_tool = _agent("child", answered, inherit=False, spawns=_child_run(grandchild))
    parent, _ = _agent("parent", answered, spawns=_child_run(child))

    await _run_parent(parent, switched_to="picked")

    assert child_tool.seen_profile == [None]
    assert answered == ["picked-model", "own-model", "own-model", "own-model", "picked-model"], answered

    # A child that followed passes it on.
    answered.clear()
    grandchild, _ = _agent("grandchild", answered, inherit=True)
    child, child_tool = _agent("child", answered, inherit=True, spawns=_child_run(grandchild))
    parent, _ = _agent("parent", answered, spawns=_child_run(child))

    await _run_parent(parent, switched_to="picked")

    assert child_tool.seen_profile == ["picked"]
    assert answered == ["picked-model"] * 5, answered


@pytest.mark.asyncio
async def test_a_profile_this_config_does_not_know_leaves_the_child_on_its_own(answered, caplog):
    child, _ = _agent("child", answered, inherit=True)

    async def start(request_id):
        # The caller's switch named a profile this process does not have.
        from agent_system.llm.caller_llm import context_for_tool
        await asyncio.create_task(_child_run(child)(request_id), context=context_for_tool("gone"))

    parent, _ = _agent("parent", answered, spawns=start)

    await _run_parent(parent, switched_to="picked")

    assert answered == ["picked-model", "own-model", "picked-model"], answered
    assert any("cannot run on the caller's LLM profile 'gone'" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_nothing_is_left_behind_in_the_callers_context(answered):
    child, _ = _agent("child", answered, inherit=True)
    parent, _ = _agent("parent", answered, spawns=_child_run(child))

    await _run_parent(parent, switched_to="picked")

    assert caller_llm_profile() is None
