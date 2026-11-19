"""A network outage -- no LLM of a step's chain could even be reached -- is waited out: the step waits
and asks again from the base the round started on (agent_config.network_outage_seconds), instead of
ending the run or staying on the chain's last, priciest member once the network is back. A round that
reached any provider is no outage: it ends as it always did, without asking again.

Time is the test's: the outage clock moves by the waits and by every call (CALL_S), nothing sleeps.
"""
import asyncio
import errno
import socket
import ssl
import time

import anyio
import httpcore
import httpx
import pytest

from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig, ToolServerConfig,
)
from agent_system.llm import network_state
from agent_system.llm.models import LLMConnectionError, LLMServerError
from agent_system.servers.agent.mixins.llm_loop import fallback as fallback_module
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry

#: Seconds every LLM call takes on the outage clock.
CALL_S = 5.0
CLOCK = {"t": 0.0}


@pytest.fixture
def system_config_with_profiles():
    models = {"gemini": LLMModelConfig(provider="gemini_sdk", model="gemini-2.5-flash", context_window=200000),
              "openai": LLMModelConfig(provider="openai", model="gpt-4o-mini", context_window=128000)}
    profiles = {name: LLMProfile(model_ref=name) for name in models}
    return AgentSystemConfig(llm_system=LLMSystemConfig(models=models, profiles=profiles, default_profile="gemini"))


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The outage clock: advanced by the waits (which do not sleep) and by the calls."""
    CLOCK["t"] = 0.0

    async def wait(seconds, token):
        CLOCK["t"] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(fallback_module, "_now", lambda: CLOCK["t"])
    monkeypatch.setattr(network_state, "_now", lambda: CLOCK["t"])
    monkeypatch.setattr(fallback_module, "_wait_unless_cancelled", wait)
    return CLOCK


async def _run(agent):
    events = []
    async for event in agent.run_events("test task"):
        events.append(event)
        if event.get("type") == "end":
            break
    return events


def _final(events):
    finals = [e for e in events if e.get("type") == "final"]
    assert finals, f"no final event; events={[e.get('type') for e in events]}"
    return str(finals[-1].get("summary"))


def _errors(events):
    return [e for e in events if e.get("type") == "error"]


def _caused(error, cause):
    error.__cause__ = cause
    return error


def _unreachable(model):
    """What a client raises when it never got through: the connect error kept as the cause."""
    return _caused(LLMConnectionError("HTTP request failed after 3 attempts (ConnectError)", model=model),
                   _caused(httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
                           socket.gaierror(11001, "getaddrinfo failed")))


def _read_timeout(model):
    return _caused(LLMConnectionError("Request timed out: ReadTimeout", model=model),
                   httpx.ReadTimeout("read timed out"))


def _refused_connection(model):
    return _caused(LLMConnectionError("HTTP request failed after 3 attempts (ConnectError)", model=model),
                   _caused(httpx.ConnectError("All connection attempts failed"), ConnectionRefusedError(111, "refused")))


def _refused():
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    return httpx.HTTPStatusError("400", request=request, response=httpx.Response(400, request=request))


class _Flaky:
    """Fails the calls *fail* names (every call with always=True) with *exc*, then a tool call and the
    final answer. Every call takes CALL_S on the outage clock, or what *takes* says for its number.
    Each client has a host of its own unless *host* names one: two hosts that cannot be reached are
    the network gone (llm/network_state.py), one is a provider."""

    def __init__(self, name, fail=(), exc=None, model=None, always=False, host=None, takes=None):
        self.name = name
        self.model = model or f"m-{name}"
        self.base_url = f"https://{host or name + '.example'}/v1"
        self.fail = set(fail)
        self.always = always
        self.takes = dict(takes or {})
        self.exc = exc or _unreachable(self.model)
        self.call_count = 0
        self.answers = 0

    def supports_streaming(self):
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        CLOCK["t"] += self.takes.get(self.call_count, CALL_S)
        if self.always or self.call_count in self.fail:
            raise self.exc
        self.answers += 1
        if self.answers == 1:
            return {"assistant": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "some_tool", "arguments": "{}"}}]}}
        return {"assistant": {"role": "assistant", "content": f"FINAL-{self.name}", "tool_calls": None}}


def _agent(system_config, primary, fallback, **agent_cfg):
    agent_cfg.setdefault("llm_profile", ["gemini", "openai"])
    agent_cfg.setdefault("max_steps", 3)
    agent_config = AgentConfig(**agent_cfg)
    agent_config.tools.allowed = ["*"]
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    agent = Agent("test_agent", system_config, server_config, ToolServerRegistry(), llm=primary)
    agent._create_fallback_llm = lambda profile: fallback   # the net boundary; the switch runs as in production
    return agent


@pytest.mark.asyncio
async def test_an_outage_is_waited_out_and_the_run_comes_back_to_its_own_model(system_config_with_profiles):
    # Two rounds of the chain fail (primary, fallback, primary, fallback), then the network is back
    primary = _Flaky("primary", fail=(1, 2))
    fallback = _Flaky("fallback", fail=(1, 2))
    events = await _run(_agent(system_config_with_profiles, primary, fallback))

    assert "FINAL-primary" in _final(events), "the run stayed on the fallback the outage moved it to"
    assert fallback.call_count == 2 and fallback.answers == 0, fallback.call_count
    assert primary.answers == 2, "the step after the wait did not ask the run's own model"


@pytest.mark.asyncio
async def test_the_outage_budget_counts_the_calls_and_ends_the_run_when_spent(system_config_with_profiles):
    # One provider behind both profiles: gone while the network is there
    primary = _Flaky("primary", always=True, host="one.example")
    fallback = _Flaky("fallback", always=True, host="one.example")
    # A round is two calls (10 s), the waits 10, 20, 40; the budget counts from the first failure
    # (5 s): the fourth round ends at 110 s, 105 s in, past the 100 s budget. Counted from the
    # first wait instead, a fifth round would run.
    events = await _run(_agent(system_config_with_profiles, primary, fallback, network_outage_seconds=100))

    assert _errors(events), "the run did not end when the budget was spent"
    assert (primary.call_count, fallback.call_count) == (4, 4), (primary.call_count, fallback.call_count)


@pytest.mark.asyncio
async def test_no_budget_ends_the_run_as_before(system_config_with_profiles):
    primary = _Flaky("primary", always=True)
    fallback = _Flaky("fallback", always=True)
    events = await _run(_agent(system_config_with_profiles, primary, fallback, network_outage_seconds=0))

    assert _errors(events)
    assert (primary.call_count, fallback.call_count) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_error, fallback_error", [
    # The endpoint answered: waiting would not change the refusal
    (lambda m: _refused(), lambda m: _refused()),
    # The request reached the provider before the read timed out: asked again it is paid again
    (_read_timeout, _read_timeout),
    # A refused connection: the host is there -- a wrong port, a local server not started
    (_refused_connection, _refused_connection),
    # One member was reached (a 502): the network is up, the run ends as before -- it used to wait
    # for the unreachable fallback alone, its own model never asked again
    (lambda m: LLMServerError("HTTP 502", model=m, status_code=502), _unreachable),
    (_unreachable, lambda m: _refused()),
    (_unreachable, _read_timeout),
], ids=["refused_both", "read_timeout_both", "connection_refused_both", "502_then_unreachable",
        "unreachable_then_refused", "unreachable_then_read_timeout"])
async def test_a_round_that_reached_a_provider_is_no_outage(system_config_with_profiles, primary_error,
                                                            fallback_error):
    primary = _Flaky("primary", always=True, exc=primary_error("m-primary"))
    fallback = _Flaky("fallback", always=True, exc=fallback_error("m-fallback"))
    events = await _run(_agent(system_config_with_profiles, primary, fallback))

    assert _errors(events)
    assert (primary.call_count, fallback.call_count) == (1, 1)


def _cannot_tell(model):
    """A coding CLI's failed run that says nothing of the network: measured, agy with the network gone
    ends at once on an unknown model."""
    return LLMServerError("antigravity agy: run r1 failed: model is not recognized", model=model, reach_unknown=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_error, fallback_error", [
    (_cannot_tell, _unreachable),   # the CLI first, the link after it found no route
    (_unreachable, _cannot_tell),   # the CLI last: the chain ends on its error, the outage is still the one before
], ids=["cli_then_unreachable", "unreachable_then_cli"])
async def test_a_link_that_cannot_tell_leaves_the_outage_to_the_others(system_config_with_profiles, primary_error,
                                                                         fallback_error):
    primary = _Flaky("primary", fail=(1, 2), exc=primary_error("m-primary"))
    fallback = _Flaky("fallback", fail=(1, 2), exc=fallback_error("m-fallback"))
    events = await _run(_agent(system_config_with_profiles, primary, fallback))

    assert "FINAL-primary" in _final(events), "the step did not wait for the network"
    assert fallback.call_count == 2 and primary.answers == 2, (primary.call_count, fallback.call_count)


@pytest.mark.asyncio
async def test_links_that_cannot_tell_are_no_outage(system_config_with_profiles):
    """Without a link that found no route nothing shows the network is gone: the run ends as before."""
    primary = _Flaky("primary", always=True, exc=_cannot_tell("m-primary"))
    fallback = _Flaky("fallback", always=True, exc=_cannot_tell("m-fallback"))
    events = await _run(_agent(system_config_with_profiles, primary, fallback))

    assert _errors(events)
    assert (primary.call_count, fallback.call_count) == (1, 1)


@pytest.mark.asyncio
async def test_an_answer_gives_a_later_outage_the_whole_budget(system_config_with_profiles):
    """Each outage is waited out on its own: the budget counts from the start of the current one."""
    primary = _Flaky("primary", fail=(1, 2, 4, 5), host="one.example")   # 3 answers (tool call), 6 the final
    fallback = _Flaky("fallback", always=True, host="one.example")
    # one outage: 10 s calls, wait 10, 10 s calls, wait 5 (cut to the 35 s) -- two outages do not fit one
    events = await _run(_agent(system_config_with_profiles, primary, fallback, network_outage_seconds=35))

    assert "FINAL-primary" in _final(events), [e.get("type") for e in events]
    assert primary.call_count == 6


@pytest.mark.asyncio
async def test_a_cancel_ends_the_wait(system_config_with_profiles, monkeypatch):
    """Through the run's own cancellation token, as a person's cancel arrives."""
    monkeypatch.undo()   # the real wait: the clock fixture's does not sleep
    monkeypatch.setattr(fallback_module, "OUTAGE_FIRST_WAIT_S", 30.0)

    class _CancelsDuringTheWait(_Flaky):
        async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
            asyncio.get_running_loop().call_later(0.3, cancellation_token.cancel)
            return await super().chat_tools(messages, tools, cancellation_token, status_scope)

    primary = _Flaky("primary", always=True)
    fallback = _CancelsDuringTheWait("fallback", always=True)
    started = time.monotonic()
    events = await asyncio.wait_for(_run(_agent(system_config_with_profiles, primary, fallback)), timeout=10)

    assert time.monotonic() - started < 10
    assert (primary.call_count, fallback.call_count) == (1, 1), "fixture: the cancel came before the wait"
    assert any(e.get("type") == "cancelled" for e in events), [e.get("type") for e in events]


def _chain(*errors):
    """errors[0] raised from errors[1] raised from ..."""
    for outer, inner in zip(errors, errors[1:]):
        outer.__cause__ = inner
    return errors[0]


class ClientConnectorError(Exception):
    """aiohttp's name for a connect failure (the Gemini SDK's transport), matched by name."""


@pytest.mark.parametrize("error, expected", [
    (_chain(LLMConnectionError("x"), httpx.ConnectError("dns"), socket.gaierror(11001, "getaddrinfo failed")), True),
    (_chain(LLMConnectionError("x"), httpx.ConnectTimeout("connect timed out")), True),
    (_chain(LLMConnectionError("x"), httpx.ConnectError("x"), OSError(errno.ENETUNREACH, "unreachable")), True),
    (_chain(LLMConnectionError("x"), ClientConnectorError("x")), True),
    (_chain(LLMConnectionError("x"), httpx.ConnectError("x"), ConnectionRefusedError(errno.ECONNREFUSED, "no")), False),
    (_chain(LLMConnectionError("x"), httpx.ConnectError("x"), ssl.SSLCertVerificationError("bad cert")), False),
    # A connection that was up broke: the dropped socket's ETIMEDOUT under a read error is no outage
    (_chain(LLMConnectionError("x"), httpx.ReadError("x"), httpcore.ReadError("x"),
            anyio.BrokenResourceError(), OSError(errno.ETIMEDOUT, "timed out")), False),
    (_chain(LLMConnectionError("x"), httpx.ReadTimeout("x")), False),
    (_chain(LLMConnectionError("x"), httpx.RemoteProtocolError("x")), False),
    (LLMConnectionError("Ollama answered 200 with a body that is no JSON"), False),
], ids=["dns", "connect_timeout", "no_route", "aiohttp_connector", "refused", "tls", "read_after_connect",
        "read_timeout", "broken_stream", "no_cause"])
def test_only_a_request_that_could_not_leave_counts_as_never_reached(error, expected):
    assert fallback_module._never_reached(error) is expected


@pytest.mark.asyncio
async def test_the_outage_is_counted_from_the_first_failure_not_from_the_rounds_start(system_config_with_profiles):
    """Measured: a call streamed for 54 minutes, the network went during it, and its run ended at once --
    the budget had counted from the call's start."""
    primary = _Flaky("primary", fail=(1, 2), takes={1: 3000.0}, host="one.example")
    fallback = _Flaky("fallback", fail=(1, 2), host="one.example")
    events = await _run(_agent(system_config_with_profiles, primary, fallback, network_outage_seconds=105))

    assert "FINAL-primary" in _final(events), "the run ended at once: the healthy call spent the budget"
    assert primary.answers == 2


@pytest.mark.asyncio
async def test_while_the_network_is_gone_the_budget_does_not_run(system_config_with_profiles):
    """Other runs find two hosts unreachable: the network. The run waits past its budget and comes back."""
    network_state.note_unreachable("a.example", "another run")
    network_state.note_unreachable("b.example", "another run")
    primary = _Flaky("primary", fail=range(1, 9))
    fallback = _Flaky("fallback", always=True)
    events = await _run(_agent(system_config_with_profiles, primary, fallback, network_outage_seconds=35))

    assert "FINAL-primary" in _final(events), "the run gave up while the network was gone"
    assert CLOCK["t"] > 35 * 4, "fixture: the outage did not outlast the budget"


@pytest.mark.asyncio
async def test_a_chain_that_cannot_tell_waits_when_other_runs_found_the_network_gone(system_config_with_profiles):
    network_state.note_unreachable("a.example")   # what other runs of the process just saw
    network_state.note_unreachable("b.example")
    primary = _Flaky("primary", fail=(1, 2), exc=_cannot_tell("m-primary"))
    fallback = _Flaky("fallback", fail=(1, 2), exc=_cannot_tell("m-fallback"))
    events = await _run(_agent(system_config_with_profiles, primary, fallback))

    assert "FINAL-primary" in _final(events), "the chain that cannot tell did not wait for the network"
    assert primary.answers == 2


@pytest.mark.parametrize("hosts, answered, age, down", [
    (["a"], False, 0, False),            # one host: a provider, a base_url with a typo
    (["a", "b"], False, 0, True),
    (["a", "a"], False, 0, False),       # the same host twice is still one
    (["a", "b"], True, 0, False),        # an answer since: the network is there
    (["a", "b"], False, network_state.EVIDENCE_S + 1, False),   # old evidence
], ids=["one_host", "two_hosts", "same_host_twice", "answered_since", "stale"])
def test_the_network_counts_as_gone_on_two_hosts_since_the_last_answer(hosts, answered, age, down):
    for host in hosts:
        network_state.note_unreachable(host)
    if answered:
        network_state.note_answer()
    CLOCK["t"] += age
    assert network_state.network_down() is down


@pytest.mark.asyncio
async def test_a_run_alone_does_not_keep_its_own_outage_alive(system_config_with_profiles):
    """Two hosts of its own chain behind a firewall: no other run sees the network gone, so the
    budget runs as before and the run ends instead of waiting forever."""
    primary = _Flaky("primary", always=True)
    fallback = _Flaky("fallback", always=True)
    events = await _run(_agent(system_config_with_profiles, primary, fallback, network_outage_seconds=100))

    assert _errors(events), "the run waited on its own evidence"
    assert (primary.call_count, fallback.call_count) == (4, 4), (primary.call_count, fallback.call_count)


@pytest.mark.asyncio
async def test_even_with_the_network_gone_a_run_waits_no_longer_than_the_longest_outage(
        system_config_with_profiles, monkeypatch):
    """Past LONGEST_OUTAGE_S the budget runs again, and the waits on the way do not overflow
    (more than a thousand of them doubled the first one past what a float holds)."""
    monkeypatch.setattr(network_state, "network_down", lambda exclude=None: True)
    monkeypatch.setattr(network_state, "LONGEST_OUTAGE_S", 1500 * 130.0)
    primary = _Flaky("primary", always=True)
    fallback = _Flaky("fallback", always=True)
    events = await _run(_agent(system_config_with_profiles, primary, fallback, network_outage_seconds=100))

    assert _errors(events), "the run never ended"
    assert "too large" not in str(_errors(events)), _errors(events)
    assert primary.call_count > 1100, "fixture: fewer waits than overflowed before"


class _OpenAIStyle:
    def __init__(self, url):
        self._base_url = url


class _SdkStyle:
    def __init__(self, model):
        self.model = model


def test_a_client_is_counted_by_the_host_it_calls():
    assert fallback_module._host_of(_Flaky("x", host="api.one.example")) == "api.one.example"
    assert fallback_module._host_of(_OpenAIStyle("https://api.two.example/v1")) == "api.two.example"
    # two models of one SDK client go to one provider: one host, not two
    assert fallback_module._host_of(_SdkStyle("model-pro")) == fallback_module._host_of(_SdkStyle("model-flash"))
