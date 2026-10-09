"""A tool call travels through the real `agent-cli run`: schema out, call back, result shown.

Drives the real main() in-process: argument parsing, config loading, plugin
discovery and bootstrap, the Agent's step loop, tool dispatch into the real
weather plugin and the CLI's transcript. Replaced are only the three edges a
test must not cross:

* the LLM -- at the registry's ``build_client`` seam, by a scripted model that
  calls the weather tool once and then answers;
* the weather plugin's HTTP sources -- by a canned forecast;
* the configuration -- a temp config dir named by AGENT_CONFIG_PATH, so
  neither the repository's config/*.yaml nor config/secrets.env is read.

This used to start `python -m agent_system.agent_cli` as a subprocess with
nothing faked. main() moves into the repository before it loads the config
(paths.enter_project), so the config copied into tmp_path was never read:
the child ran the developer's real config with the keys from secrets.env --
a paid LLM call, logs/cli.log appended, every enabled plugin bootstrapped on
its store under data/ (message_debugger's request log, coding_cli's sweep of
data/coding_cli/runs among them). And it asserted only exit code 0, because
a real model does not reliably call the tool.
"""
from __future__ import annotations

import io
import json
import logging
import socket
import sys
import threading
from asyncio import proactor_events, selector_events  # both, on every platform: only Windows imports the first
from pathlib import Path

import pytest
import yaml

import agent_system.agent_cli as agent_cli
from agent_system.cli_utils.commands import run as run_cmd
from agent_system.cli_utils.event_loop import close_cli_loop
from agent_system.llm import registry as llm_registry

REPO = Path(__file__).resolve().parents[2]

TASK = "wie wird das wetter morgen in München"
AGENT = "weather_test_agent"
TOOL = "weather_forecast"
# Only the canned forecast carries these: seen in a tool message or on
# stdout, they prove the plugin really ran and its result travelled on.
SKY = "FAKE-SKY-4711"
ANSWER = "Morgen in München: FAKE-ANSWER-0815"


class ScriptedLLM:
    """A model that calls the weather tool once, then answers.

    Built by the real factory through the registry seam, so the agent gets it
    the way it gets a real client. Every client built in the run shares one
    ``log``: the test reads what the model was shown -- copied when shown,
    the run goes on working on its message list.
    """

    def __init__(self, log: list, model: str | None = None, provider: str | None = None):
        self.log = log
        self.model = model
        self.provider = provider

    def supports_streaming(self) -> bool:
        return False

    def set_app_title(self, title: str) -> None:
        pass

    async def chat(self, messages, cancellation_token=None):
        # A plain completion (a session title, a summary) -- not the step loop.
        return "title"

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        offered = [t["function"]["name"] for t in tools or []]
        self.log.append({"messages": [(m.role, str(m.content)) for m in messages], "tools": offered})
        if len(self.log) == 1:
            if TOOL not in offered:
                # Answer instead of calling: the assertions below say what was missing.
                return {"assistant": {"role": "assistant", "content": f"no {TOOL} among {offered}"}}
            return {"assistant": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_weather_1", "type": "function",
                "function": {"name": TOOL,
                             "arguments": json.dumps({"location": "München", "days": 2})}}]}}
        return {"assistant": {"role": "assistant", "content": ANSWER}}

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        raise AssertionError("supports_streaming() is False: the streaming path must not be taken")
        yield  # an async generator, as the real one is


def _write_config(config_dir: Path) -> Path:
    """One agent that may use every tool ("*"), the weather plugin, a fake model."""
    config_dir.mkdir()
    config = {
        "name": "cli-tool-invocation-test",
        "default_agent": AGENT,
        # Off: setup_logging would swap the root logger's handlers of the
        # whole pytest process for a file handler.
        "logging": {"enabled": False},
        "llm_system": {
            "models": {"fake-model": {"provider": "openai", "model": "fake-model",
                                      "api_key": "fake-key"}},
            "profiles": {"normal": {"model_ref": "fake-model"}},
            "default_profile": "normal",
        },
        "plugins": {
            "plugin_dirs": [str(REPO / "src" / "plugins")],
            "servers": {
                "weather": {"type": "weather", "enabled": True},
                AGENT: {
                    "type": "basic_agent",
                    "enabled": True,
                    "agent_config": {
                        "llm_profile": "normal",
                        "max_steps": 4,
                        "system_prompt": "You answer weather questions with your tools.",
                        "tools": {"allowed": ["*"]},
                    },
                },
            },
        },
    }
    path = config_dir / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def cli_run(tmp_path, monkeypatch):
    """main() on a temp config, a scripted model and a canned forecast."""
    # The variable, not --config: code below main() that loads the settings
    # itself (the capabilities registry, get_server_config) reads it too --
    # and would otherwise read the repository's config and its secrets.env.
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(_write_config(tmp_path / "config")))
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
    # main() chdirs into the repository; monkeypatch puts the cwd back afterwards.
    monkeypatch.chdir(tmp_path)

    llm_log: list = []
    monkeypatch.setattr(llm_registry, "build_client",
                        lambda cfg, ssl_verify=None, **_: ScriptedLLM(
                            llm_log, model=getattr(cfg, "model", None),
                            provider=getattr(cfg, "provider", None)))

    from plugins.weather import sources
    fetched: list = []

    async def forecast(location, days, units, ssl_verify, *rest):
        fetched.append({"location": location, "days": days})
        return {"location": location,
                "current": {"temperature": 17.5, "weather_desc": SKY},
                "forecast": [{"date": "2026-09-29", "min_temp": 9, "max_temp": 18}]}

    # Every source the plugin has: one it drops breaks nothing here, one it adds does not reach the network.
    names = [name for name in vars(sources) if name.startswith("fetch_")]
    assert names, "the weather plugin has no fetch_* source any more; this test replaces nothing"
    for name in names:
        monkeypatch.setattr(sources, name, forecast)

    # No network connection in this run, loopback included. Recorded as well
    # as refused: a plugin that catches the error would hide it.
    # Windows has no socketpair: Python builds one by connecting a socket to its own loopback listener, and
    # asyncio's event loop needs one. That connection goes nowhere, so it passes.
    outbound: list = []
    real_connect = socket.socket.connect
    real_socketpair = socket.socketpair
    pairing = threading.local()

    def socketpair(*args, **kwargs):
        pairing.active = True
        try:
            return real_socketpair(*args, **kwargs)
        finally:
            pairing.active = False

    def guarded_connect(sock, address):
        if sock.family != getattr(socket, "AF_UNIX", None) and not getattr(pairing, "active", False):
            outbound.append(address)
            raise OSError(f"test: outbound connection to {address!r} refused")
        return real_connect(sock, address)

    # An event loop connects without socket.connect: Windows' proactor through ConnectEx, and httpx's
    # AsyncClient -- the weather sources' -- goes that way.
    async def guarded_sock_connect(loop, sock, address):
        outbound.append(address)
        raise OSError(f"test: outbound connection to {address!r} refused")

    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    for loop_class in (proactor_events.BaseProactorEventLoop, selector_events.BaseSelectorEventLoop):
        monkeypatch.setattr(loop_class, "sock_connect", guarded_sock_connect)

    monkeypatch.setattr(sys, "argv", ["agent-cli", "--no-status", "--color", "never",
                                      "--show-tools", TASK])
    yield {"llm": llm_log, "fetched": fetched, "outbound": outbound}
    close_cli_loop()


def test_cli_run_calls_the_tool_the_model_asks_for_and_shows_it(cli_run, capsys, monkeypatch):
    # What setup_logging leaves on a terminal: a colouring console handler -- which --color never must silence.
    # (The run's own logging is off: it would swap the pytest process's handlers.)
    from agent_system.utils.logging import ColorizedFormatter
    console = logging.StreamHandler(io.StringIO())
    console.setFormatter(ColorizedFormatter("%(message)s", use_colors=True))
    monkeypatch.setattr(logging.getLogger(), "handlers", [*logging.getLogger().handlers])
    monkeypatch.setattr(run_cmd, "setup_role_logging",
                        lambda logging_config, role: logging.getLogger().addHandler(console))
    agent_cli.main()
    out = capsys.readouterr().out
    assert cli_run["outbound"] == [], f"the run tried to reach the network: {cli_run['outbound']}"

    llm, fetched = cli_run["llm"], cli_run["fetched"]
    assert llm, "the model was never asked"

    # Schema exposure: with tools.allowed ["*"] the plugin's tool reaches the model.
    assert TOOL in llm[0]["tools"], llm[0]["tools"]
    assert any(TASK in content for _role, content in llm[0]["messages"]), \
        "the task typed on the command line never reached the model"
    assert len(llm) == 2, f"expected a tool step and an answer step, the model was asked {len(llm)}x"

    # Dispatch: the arguments the model gave reached the plugin ...
    assert fetched == [{"location": "München", "days": 2}]
    # ... and the plugin's result went back to the model as the tool's answer.
    tool_messages = [content for role, content in llm[1]["messages"] if role == "tool"]
    assert len(tool_messages) == 1 and SKY in tool_messages[0], tool_messages

    # The transcript: the call with its arguments, its result, the answer.
    call_header = f"TOOL CALL -> server=weather action={TOOL}"
    result_header = f"TOOL RESULT <- server=weather action={TOOL}"
    assert call_header in out and result_header in out, out
    call_at, result_at = out.index(call_header), out.index(result_header)
    assert '"location": "München"' in out[call_at:result_at], out
    assert SKY in out[result_at:], out
    assert ANSWER in out[result_at:], out

    assert console.formatter.use_colors is False, "--color never, yet the log lines are coloured"
