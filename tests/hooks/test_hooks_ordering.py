"""
Hook ordering anchors (begin/end), hook_filter failures and the explicit
timeout argument of HookRegistry.execute_hooks.
"""

import asyncio
import logging

import pytest

from agent_system.hooks import (
    CircularDependencyError,
    HookContext,
    HookRegistry,
    HookResult,
    HookType,
    PluginHook,
)
from agent_system.hooks.ordering import topological_sort


class _Hook(PluginHook):
    def __init__(self, name, calls=None, delay=0.0):
        super().__init__(name, {})
        self.calls = calls if calls is not None else []
        self.delay = delay

    async def on_pre_llm_call(self, context):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append(self.name)
        return HookResult(success=True, modified=False, context=context)


def _entry(name, before=(), after=(), category=None):
    return (name, _Hook(name), {"before": list(before), "after": list(after)},
            {"enabled": True, "category": category})


def _names(entries):
    return [name for name, _, _ in topological_sort(entries)]


def _context():
    return HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r",
                       session_id="s", agent_name="a")


class TestBeginEndAnchors:
    def test_after_end_runs_last_although_it_sorts_first(self):
        assert _names([_entry("a.last", after=["end"]), _entry("b.x"),
                       _entry("c.y")]) == ["b.x", "c.y", "a.last"]

    def test_before_begin_runs_first_although_it_sorts_last(self):
        assert _names([_entry("a.x"), _entry("b.y"),
                       _entry("z.first", before=["begin"])]) == ["z.first", "a.x", "b.y"]

    def test_before_end_and_after_begin_change_nothing(self):
        plain = [_entry("b.x"), _entry("a.y"), _entry("c.z")]
        anchored = [_entry("b.x", before=["end"]), _entry("a.y", after=["begin"]),
                    _entry("c.z", after=["begin"], before=["end"])]
        assert _names(plain) == _names(anchored) == ["a.y", "b.x", "c.z"]

    def test_hooks_chained_to_an_anchored_one_follow_it(self):
        # b.after_last is ordered after a hook that runs after "end"
        names = _names([_entry("z.last", after=["end"]),
                        _entry("b.after_last", after=["z.last"]), _entry("m.mid")])
        assert names == ["m.mid", "z.last", "b.after_last"]

    def test_first_and_last_together_with_categories(self):
        names = _names([_entry("m.first", before=["begin"]), _entry("a.last", after=["end"]),
                        _entry("k.one", category="inject"),
                        _entry("c.two", after=["inject"])])
        assert names == ["m.first", "k.one", "c.two", "a.last"]

    def test_genuine_cycle_is_still_reported(self):
        with pytest.raises(CircularDependencyError):
            topological_sort([_entry("a.x", after=["b.y"]), _entry("b.y", after=["a.x"])])

    @pytest.mark.asyncio
    async def test_execute_hooks_runs_the_after_end_hook_last(self):
        registry, calls = HookRegistry(), []
        await registry.register_hook(HookType.PRE_LLM_CALL, "a.last", _Hook("a.last", calls),
                                     order_spec={"after": ["end"]})
        await registry.register_hook(HookType.PRE_LLM_CALL, "b.x", _Hook("b.x", calls))
        await registry.execute_hooks(HookType.PRE_LLM_CALL, _context())
        assert calls == ["b.x", "a.last"]


class TestHookFilterFailure:
    @pytest.mark.asyncio
    async def test_a_raising_filter_falls_back_to_the_default(self, caplog):
        registry, calls = HookRegistry(), []
        await registry.register_hook(HookType.PRE_LLM_CALL, "a.on", _Hook("a.on", calls))
        await registry.register_hook(HookType.PRE_LLM_CALL, "b.off", _Hook("b.off", calls),
                                     enabled=False)

        def broken(name, default):
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="agent_system.hooks.registry"):
            await registry.execute_hooks(HookType.PRE_LLM_CALL, _context(), hook_filter=broken)
        assert calls == ["a.on"]
        assert "hook_filter failed" in caplog.text

    async def test_a_filter_with_the_older_signature_decides_alone(self):
        registry = HookRegistry()
        calls = []
        await registry.register_hook(HookType.PRE_LLM_CALL, "a.on", _Hook("a.on", calls), enabled=True)
        await registry.register_hook(HookType.PRE_LLM_CALL, "b.off", _Hook("b.off", calls), enabled=False)

        await registry.execute_hooks(HookType.PRE_LLM_CALL, _context(), hook_filter=lambda name: name == "b.off")

        assert calls == ["b.off"]


class TestExplicitTimeout:
    @pytest.mark.asyncio
    async def test_the_timeout_argument_overrides_the_hooks_own(self):
        registry, calls = HookRegistry(default_timeout=30.0), []
        await registry.register_hook(HookType.PRE_LLM_CALL, "a.slow",
                                     _Hook("a.slow", calls, delay=5.0), timeout=30.0)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await registry.execute_hooks(HookType.PRE_LLM_CALL, _context(), timeout=0.1)
        assert loop.time() - started < 2.0
        assert calls == []
        assert registry.get_stats("a.slow")["failures"] == 1

    @pytest.mark.asyncio
    async def test_without_the_argument_the_hooks_own_timeout_applies(self):
        registry, calls = HookRegistry(default_timeout=30.0), []
        await registry.register_hook(HookType.PRE_LLM_CALL, "a.slow",
                                     _Hook("a.slow", calls, delay=5.0), timeout=0.1)
        await registry.execute_hooks(HookType.PRE_LLM_CALL, _context())
        assert calls == []
