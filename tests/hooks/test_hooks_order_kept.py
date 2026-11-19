"""execute_hooks sorts the hooks of a type again only when they changed: the sort ran for every hook call of every
step (about 1 % of the API's Python time under load). Any change -- a hook registered, one unregistered, one
replaced by another of the same count, an entry appended to the registry directly -- is sorted anew."""
from __future__ import annotations

from agent_system.hooks import HookContext, HookRegistry, HookResult, HookType, PluginHook
from agent_system.hooks import registry as registry_module


class _Recorder(PluginHook):
    def __init__(self, name: str, ran: list):
        super().__init__(name, {})
        self.ran = ran

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        self.ran.append(self.name)
        return HookResult(success=True, modified=False, context=context)


def _context() -> HookContext:
    return HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="s", agent_name="a", step=1)


async def _run(registry: HookRegistry, ran: list) -> list:
    ran.clear()
    await registry.execute_hooks(HookType.PRE_LLM_CALL, _context())
    return list(ran)


def _counted_sorts(monkeypatch) -> list:
    sorts = []
    sort = registry_module.topological_sort

    def counted(hooks_list):
        sorts.append([entry[0] for entry in hooks_list])
        return sort(hooks_list)
    monkeypatch.setattr(registry_module, "topological_sort", counted)
    return sorts


async def test_unchanged_hooks_are_sorted_once(monkeypatch):
    sorts, ran, registry = _counted_sorts(monkeypatch), [], HookRegistry()
    await registry.register_hook(HookType.PRE_LLM_CALL, "late", _Recorder("late", ran), {"after": ["early"]})
    await registry.register_hook(HookType.PRE_LLM_CALL, "early", _Recorder("early", ran))
    for _ in range(3):
        assert await _run(registry, ran) == ["early", "late"]
    assert len(sorts) == 1


async def test_a_registered_hook_is_sorted_in_and_runs_in_its_place(monkeypatch):
    sorts, ran, registry = _counted_sorts(monkeypatch), [], HookRegistry()
    await registry.register_hook(HookType.PRE_LLM_CALL, "late", _Recorder("late", ran))
    assert await _run(registry, ran) == ["late"]
    await registry.register_hook(HookType.PRE_LLM_CALL, "first", _Recorder("first", ran), {"before": ["late"]})
    assert await _run(registry, ran) == ["first", "late"]
    assert len(sorts) == 2


async def test_an_unregistered_hook_no_longer_runs(monkeypatch):
    ran, registry = [], HookRegistry()
    await registry.register_hook(HookType.PRE_LLM_CALL, "a", _Recorder("a", ran))
    await registry.register_hook(HookType.PRE_LLM_CALL, "b", _Recorder("b", ran))
    assert await _run(registry, ran) == ["a", "b"]
    assert await registry.unregister_hook(HookType.PRE_LLM_CALL, "a")
    assert await _run(registry, ran) == ["b"]


async def test_a_hook_replaced_by_another_of_the_same_count_runs_instead(monkeypatch):
    ran, registry = [], HookRegistry()
    await registry.register_hook(HookType.PRE_LLM_CALL, "kept", _Recorder("kept", ran))
    await registry.register_hook(HookType.PRE_LLM_CALL, "old", _Recorder("old", ran))
    assert await _run(registry, ran) == ["kept", "old"]
    await registry.unregister_hook(HookType.PRE_LLM_CALL, "old")
    await registry.register_hook(HookType.PRE_LLM_CALL, "new", _Recorder("new", ran))
    assert await _run(registry, ran) == ["kept", "new"]


async def test_a_hook_registered_again_under_its_name_runs_as_the_new_instance():
    ran, registry = [], HookRegistry()
    await registry.register_hook(HookType.PRE_LLM_CALL, "plugin.hook", _Recorder("old instance", ran))
    assert await _run(registry, ran) == ["old instance"]
    await registry.unregister_hook(HookType.PRE_LLM_CALL, "plugin.hook")
    await registry.register_hook(HookType.PRE_LLM_CALL, "plugin.hook", _Recorder("new instance", ran))
    assert await _run(registry, ran) == ["new instance"]


async def test_an_entry_appended_to_the_registry_directly_is_seen():
    ran, registry = [], HookRegistry()
    await registry.register_hook(HookType.PRE_LLM_CALL, "b", _Recorder("b", ran))
    assert await _run(registry, ran) == ["b"]
    registry._hooks[HookType.PRE_LLM_CALL].append(("a", _Recorder("a", ran), {"before": [], "after": []},
                                                   {"enabled": True, "timeout": 5.0, "category": None}))
    assert await _run(registry, ran) == ["a", "b"]
