"""The detector is measured elsewhere; this is about the WIRING.

A guard that is unit-tested but wired up wrong is not a guard. These drive the
real ``_call_llm_with_streaming`` against a scripted stream, with a stand-in
``self`` — the method needs exactly two attributes, so a full Agent would be
scaffolding, not evidence.
"""

from types import SimpleNamespace

import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolServerConfig,
)
from agent_system.llm.models import LLMServerError
from agent_system.tools.base import ToolServerRegistry
from agent_system.servers.agent.reasoning_loop import ReasoningLoopError
from agent_system.servers.agent.server import Agent

# The real thing, from run #943509.
LOOP_LINE = ('Need perhaps "Bettkante" yes.\n\nNeed perhaps "Heimweg" yes.\n\n'
             'Need perhaps "Stadtrand" yes.\n\n')


def thinking(text: str, chunk: int = 400) -> list[dict]:
    """The stream as the gateway sends it: many small reasoning deltas."""
    return [{"type": "thinking_delta", "delta": text[i:i + chunk]}
            for i in range(0, len(text), chunk)]


FINAL = {"type": "final", "assistant": {"role": "assistant", "content": "done"}}


def healthy_text(chars: int) -> str:
    out, index = [], 0
    while sum(len(line) for line in out) < chars:
        out.append(f"Step {index}: scene {index % 37} still needs beat "
                   f"B{index % 91:02d} checked against {index * 7 % 1000} words.\n")
        index += 1
    return "".join(out)


class _ScriptedLLM:
    model = "test/thinking-model"

    def __init__(self, chunks):
        self._chunks = chunks

    def supports_streaming(self) -> bool:
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None,
                                   status_scope=None):
        for chunk in self._chunks:
            yield chunk


def _stand_in(enabled: bool = True, threshold: float = 0.5) -> SimpleNamespace:
    return SimpleNamespace(
        name="watcher",
        _reasoning_loop_config={"enabled": enabled,
                                "repetition_threshold": threshold},
    )


async def _drive(agent, llm, watch_reasoning: bool = True) -> list[dict]:
    return [event async for event in Agent._call_llm_with_streaming(
        agent, llm=llm, messages=[], tools_schema=[], cancellation_token=None,
        step=0, yield_pending_status_fn=lambda: [],
        watch_reasoning=watch_reasoning)]


class TestTheWatchdogReachesTheStream:

    async def test_a_looping_stream_aborts_the_call(self):
        llm = _ScriptedLLM(thinking(LOOP_LINE * 900) + [FINAL])
        with pytest.raises(ReasoningLoopError) as caught:
            await _drive(_stand_in(), llm)
        # The caller logs it and decides; both need the size of the damage.
        # One window: the detector judges once it has 20.000 characters to
        # judge. Both bounds, because the lower one alone let a much later first
        # judgement through. Like the long window's bound below, this pins when
        # it first judges, not the interval — an interval that divides the
        # window (10k, 20k) fires at the same point.
        assert 20_000 <= caught.value.characters < 30_000
        assert "repeats" in caught.value.reason

    async def test_a_healthy_stream_runs_to_the_end(self):
        """The expensive runs in the corpus think this long and deliver."""
        llm = _ScriptedLLM(thinking(healthy_text(250_000)) + [FINAL])
        events = await _drive(_stand_in(), llm)
        assert events[-1]["type"] == "thinking_complete"
        assert events[-1]["assistant"]["content"] == "done"

    async def test_the_thinking_still_reaches_the_client(self):
        """Watching must not swallow the live view it watches."""
        llm = _ScriptedLLM(thinking(healthy_text(5_000)) + [FINAL])
        events = await _drive(_stand_in(), llm)
        assert [e for e in events if e["type"] == "reasoning_delta"]


class TestWhenItMustStayOut:

    async def test_the_retry_runs_unwatched(self):
        """The retry this detector asks for is not watched again — otherwise
        the same stream would abort forever. That is what makes one retry per
        step true by construction instead of by a counter."""
        llm = _ScriptedLLM(thinking(LOOP_LINE * 900) + [FINAL])
        events = await _drive(_stand_in(), llm, watch_reasoning=False)
        assert events[-1]["type"] == "thinking_complete"

    async def test_disabled_by_config_never_aborts(self):
        llm = _ScriptedLLM(thinking(LOOP_LINE * 900) + [FINAL])
        events = await _drive(_stand_in(enabled=False), llm)
        assert events[-1]["type"] == "thinking_complete"

    async def test_the_configured_threshold_is_the_one_that_acts(self):
        llm = _ScriptedLLM(thinking(LOOP_LINE * 900) + [FINAL])
        events = await _drive(_stand_in(threshold=0.99), llm)
        assert events[-1]["type"] == "thinking_complete"


def _real_agent(with_fallback_chain: bool = False) -> Agent:
    """A real Agent, so the run loop and the config defaults are the ones in
    production — the wiring is what is under test, not a rebuilt version.

    ``with_fallback_chain`` gives the agent somewhere to switch TO. Without it
    ``fallback_profiles`` is empty and no test could tell a deliberate "stay on
    this model" from a switch that merely had no candidate.
    """
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4",
                                        api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4"),
                  "backup": LLMProfile(model_ref="gpt-4")},
        default_profile="normal",
    )
    agent_config = AgentConfig(
        max_steps=3,
        llm_profile=["normal", "backup"] if with_fallback_chain else "normal",
    )
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    return Agent("test_agent", AgentSystemConfig(llm_system=llm_system),
                 server_config, ToolServerRegistry())


class _CountingLLM:
    """Loops in its thinking for the first N calls, then answers."""

    model = "test/thinking-model"

    def __init__(self, loops_for_calls: int):
        self._loops_for_calls = loops_for_calls
        self.calls = 0

    def supports_streaming(self) -> bool:
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None,
                                   status_scope=None):
        self.calls += 1
        if self.calls <= self._loops_for_calls:
            for chunk in thinking(LOOP_LINE * 900):
                yield chunk
        else:
            for chunk in thinking(healthy_text(2_000)):
                yield chunk
        yield FINAL


class _TwoStepLLM:
    """Loops on the first call of each step, then answers.

    Step 1's answer asks for a tool so the run loop takes a second step; the
    tool does not exist, which the loop handles as an errored tool result and
    carries on — enough to reach step 2, which is all this needs.
    """

    model = "test/thinking-model"

    def __init__(self):
        self.calls = 0

    def supports_streaming(self) -> bool:
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None,
                                   status_scope=None):
        self.calls += 1
        if self.calls in (1, 3):          # the watched call of each step
            for chunk in thinking(LOOP_LINE * 900):
                yield chunk
            yield FINAL
            return
        for chunk in thinking(healthy_text(2_000)):
            yield chunk
        if self.calls == 2:               # end of step 1: ask for a tool
            yield {"type": "final",
                   "assistant": {"role": "assistant", "content": "",
                                 "tool_calls": [{"id": "t1", "type": "function",
                                                 "function": {"name": "no_such_tool",
                                                              "arguments": "{}"}}]}}
        else:
            yield FINAL


class _RecordingLLM(_CountingLLM):
    """Carries the hook notifier a real LLMClient has.

    The other stubs here deliberately do not: without it the run loop skips
    the notification entirely, which is why every earlier test walked past
    that branch without noticing it existed.
    """

    def __init__(self, loops_for_calls: int):
        super().__init__(loops_for_calls)
        self.notifications: list[dict] = []

    async def _notify_post_response(self, info: dict) -> None:
        self.notifications.append(info)


class TestTheRunLoopRetriesOnce:
    """The claim that was true only BY CONSTRUCTION until measured here."""

    @pytest.mark.asyncio
    async def test_a_loop_costs_one_retry_on_the_same_model(self):
        llm = _CountingLLM(loops_for_calls=1)
        agent = _real_agent()
        agent.llm = llm

        [event async for event in agent.run_events("do it", session_id="s1")]

        assert llm.calls == 2, "the aborted call was not retried exactly once"

    @pytest.mark.asyncio
    async def test_a_model_that_loops_again_does_not_retry_forever(self):
        """The retry runs unwatched, so a second loop cannot abort again. If
        that ever changes, this is the test that stops an endless retry from
        reaching production."""
        llm = _CountingLLM(loops_for_calls=99)
        agent = _real_agent()
        agent.llm = llm

        [event async for event in agent.run_events("do it", session_id="s2")]

        assert llm.calls == 2, f"expected one retry, got {llm.calls} calls"

    @pytest.mark.asyncio
    async def test_the_retry_stays_on_the_same_model(self):
        """The claim the whole design rests on, and it was only a comment.

        Every other recovery in this loop switches profiles. This one must
        not: a loop in the thinking says the sampling was unlucky, not that
        the model is broken, and a switch would move the run to another
        (usually more expensive) model for no reason. The agent HAS a chain
        here, so a switch is possible and its absence means something.
        """
        llm = _CountingLLM(loops_for_calls=1)
        agent = _real_agent(with_fallback_chain=True)
        agent.llm = llm
        assert agent.agent_config.fallback_profiles, "vacuous: no chain to switch to"

        switches = []
        agent._create_fallback_llm = lambda profile: switches.append(profile) or None

        [event async for event in agent.run_events("do it", session_id="s4")]

        assert llm.calls == 2, "the retry did not happen"
        assert switches == [], f"the retry switched profiles: {switches}"

    @pytest.mark.asyncio
    async def test_a_fallback_model_is_watched_from_its_first_call(self):
        """The exemption belongs to the client that earned it, not to the step.

        Model A loops and gets its one unwatched retry; that retry fails with
        a server error, so the loop switches to model B. B never looped and
        must be watched immediately. Bookkeeping that only remembers THAT a
        retry happened leaves B unguarded for the rest of the step — a model
        that did nothing wrong runs without the guard.
        """
        model_b = _CountingLLM(loops_for_calls=1)

        class _LoopsThenFails:
            model = "test/model-a"

            def __init__(self):
                self.calls = 0

            def supports_streaming(self) -> bool:
                return True

            async def chat_tools_streaming(self, messages, tools,
                                           cancellation_token=None, status_scope=None):
                self.calls += 1
                if self.calls == 1:
                    for chunk in thinking(LOOP_LINE * 900):
                        yield chunk
                    yield FINAL
                    return
                raise LLMServerError("upstream down", provider="test",
                                     model="test/model-a", status_code=503)

        model_a = _LoopsThenFails()
        agent = _real_agent(with_fallback_chain=True)
        agent.llm = model_a
        agent._create_fallback_llm = lambda profile: model_b

        [event async for event in agent.run_events("do it", session_id="s6")]

        assert model_a.calls == 2, "A did not get its one retry"
        assert model_b.calls == 2, "the fallback model ran unwatched"

    @pytest.mark.asyncio
    async def test_the_exemption_does_not_outlive_its_step(self):
        """The retry runs unwatched — for THAT call, not for the rest of the
        request. Hoisting the bookkeeping out of the step loop would leave a
        model unwatched for every following step, and every other test here
        would stay green."""
        llm = _TwoStepLLM()
        agent = _real_agent()
        agent.llm = llm

        [event async for event in agent.run_events("do it", session_id="s5")]

        # step 1: loop -> retry (2 calls), step 2: loop -> retry (2 calls).
        assert llm.calls == 4, (
            f"expected the watchdog armed again in step 2, got {llm.calls} calls")

    @pytest.mark.asyncio
    async def test_the_abort_reports_the_aborted_attempt(self):
        """An aborted call was still paid for, so it must be recorded.

        The client's own post-response notification sits after the stream,
        which an abort never reaches — so without this the message debugger
        keeps a request with no response, cost accounting misses the tokens,
        and every later recalibration of the threshold reads a database that
        structurally cannot contain the calls this guard aborted.
        """
        llm = _RecordingLLM(loops_for_calls=1)
        agent = _real_agent()
        agent.llm = llm

        [event async for event in agent.run_events("do it", session_id="s7")]

        aborts = [note for note in llm.notifications
                  if note.get("finish_reason") == "reasoning_loop_aborted"]
        assert len(aborts) == 1, f"expected one record, got {llm.notifications}"
        assert "reasoning loop" in aborts[0]["error"]
        assert aborts[0]["is_streaming"] is True
        assert aborts[0]["model"] == "test/thinking-model"
        # Attributable to a client. Most clients carry no _PROVIDER at all, so
        # a bare getattr default files their aborts under "unknown" while
        # every other row of the same client names its provider.
        assert aborts[0]["provider"] == "_RecordingLLM"
        # How much thinking was produced — the tokens are unknown at abort
        # time, so this is what a later recalibration can use.
        assert aborts[0]["response_data"]["reasoning_loop"]["characters"] >= 20_000
        # Present and numeric. NOT pinned: that the duration covers this
        # attempt rather than the whole step — the scripted clients answer
        # instantly, so both readings are 0 here and a timing assertion would
        # only add flakiness.
        assert isinstance(aborts[0]["duration_ms"], float)

    @pytest.mark.asyncio
    async def test_a_client_that_reports_its_abandon_leaves_one_row_with_the_reason(self):
        """Clients now report a stream their caller abandons -- with the usage
        so far. The server's own row came on top: two rows for one call. The
        abort's reason reaches the client's row instead (abandon_report)."""
        from agent_system.llm.models import LLMClient

        class _ReportingLLM(_CountingLLM):
            served_agent = None
            _last_response_duration_ms = None

            def __init__(self):
                super().__init__(loops_for_calls=1)
                self.rows: list[dict] = []

                async def record(info):
                    self.rows.append(info)
                self._on_post_llm_response = record

            _notify_post_response = LLMClient._notify_post_response

            async def chat_tools_streaming(self, *args, **kwargs):
                try:
                    async for chunk in _CountingLLM.chat_tools_streaming(self, *args, **kwargs):
                        yield chunk
                except GeneratorExit:
                    await self._notify_post_response({
                        "model": "test/thinking-model", "is_streaming": True,
                        "error": "stream abandoned by the caller", "usage": {"completion_tokens": 7}})
                    raise

        llm = _ReportingLLM()
        agent = _real_agent()
        agent.llm = llm

        [event async for event in agent.run_events("do it", session_id="s8")]

        aborts = [row for row in llm.rows if row.get("finish_reason") == "reasoning_loop_aborted"]
        assert len(aborts) == 1, llm.rows
        assert aborts[0]["usage"] == {"completion_tokens": 7}, "the client's usage was lost"
        assert "reasoning loop" in aborts[0]["error"]
        assert aborts[0]["response_data"]["reasoning_loop"]["characters"] >= 20_000
        assert not [row for row in llm.rows if row.get("error") == "stream abandoned by the caller"]

    @pytest.mark.asyncio
    async def test_a_healthy_model_is_called_once(self):
        """Without this the two tests above would also pass on a loop that
        simply calls twice every time."""
        llm = _CountingLLM(loops_for_calls=0)
        agent = _real_agent()
        agent.llm = llm

        [event async for event in agent.run_events("do it", session_id="s3")]

        assert llm.calls == 1


def long_period_text(unit_chars: int = 13_006, repeats: int = 34) -> str:
    """A loop whose period the 20.000-character window cannot span.

    Shaped after the real cases: the unit is varied prose with no periodicity
    of its own, so the only repetition in the text is the loop itself. At a
    period of ~13.000 the short window tops out at
    (20.000 - 13.000) / 20.000 = 0.35, below every usable threshold. Why 13.006
    and not a round number: see LONG_PERIOD in test_reasoning_loop.py — a round
    period lets every stride see the loop alike and pins nothing.
    """
    words = ["Szene", "Absatz", "Figur", "Motiv", "Wendung", "Kapitel",
             "Dialog", "Bild", "Satz", "Klang"]
    unit, index = [], 0
    while sum(len(part) for part in unit) < unit_chars:
        unit.append(f"{words[index % len(words)]} {index}: geprueft, "
                    f"Ergebnis {index * 17 % 997}. ")
        index += 1
    return "".join(unit)[:unit_chars] * repeats


class TestTheLongWindowIsWiredUpToo:
    """17 calls in 25 days ran into the output ceiling and delivered nothing;
    the nine still stored repeat like this — a judge that returns nothing reads
    downstream as "no findings"."""

    async def test_a_long_period_loop_aborts_the_call(self):
        llm = _ScriptedLLM(thinking(long_period_text()) + [FINAL])
        with pytest.raises(ReasoningLoopError) as caught:
            await _drive(_stand_in(), llm)
        # Past 200.000: proof that the LONG window did it. The short one
        # judges from 20.000 on, so a smaller number here would mean the
        # short window fired and this test proves nothing about the long one.
        assert caught.value.characters >= 200_000, (
            f"aborted after {caught.value.characters} characters — that is the "
            "short window, so the long one is not shown to be wired up")
        # And not much later than its first full window. This pins WHEN it first
        # judges, not the interval itself: the witness loops from character 0,
        # so any interval that divides the window fires at the same point
        # (measured: 50k/100k/200k all at 200.000, even behind a healthy
        # lead-in). What it catches is the gross delay a lower bound alone let
        # through — an interval of 400.000 or a window of 300.000, where the
        # abort came when the damage was almost fully paid. The interval is a
        # calibration knob and deliberately not pinned finer than that.
        assert caught.value.characters < 260_000, (
            f"the long window first judged at {caught.value.characters} "
            "characters — it waits longer than its geometry says it should")

    async def test_the_short_window_still_aborts_first_when_it_can(self):
        """Both are fed every delta; the one that sees it first must win.
        Otherwise a short-period loop would run ten times longer than before
        this change, waiting for a window that does not need to fill."""
        llm = _ScriptedLLM(thinking(LOOP_LINE * 900) + [FINAL])
        with pytest.raises(ReasoningLoopError) as caught:
            await _drive(_stand_in(), llm)
        assert caught.value.characters < 200_000, (
            "a short-period loop now waits for the long window")
