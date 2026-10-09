import json
import sys

import pytest

from agent_system import agent_cli as cli
from agent_system.cli_utils.commands import run as run_cmd


class DummyAgent:
    def __init__(self, *args, events=None, **kwargs):
        # Accept Agent constructor args but only keep events
        self._events = events or []

    async def run_events(self, task, **kwargs):
        for e in self._events:
            yield e

    async def run(self, task):
        # Simulate the non-streaming run path by consuming events and
        # returning a final aggregated result dict
        result = {"task": task, "calls": []}
        for e in self._events:
            if e.get("type") == "tool_result":
                result.setdefault("calls", []).append({
                    "server": e.get("server"),
                    "action": e.get("action"),
                    "result": e.get("result"),
                })
            if e.get("type") == "final":
                result["summary"] = e.get("summary")
        return result


# Module-level default events used by AgentStub when no events passed to constructor
DEFAULT_EVENTS = []


class AgentStub:
    # accept the modern Agent signature but only keep events
    def __init__(self, name, system_config, server_config, registry, llm=None, llm_factory=None, events=None, session_service=None):
        self._events = events or DEFAULT_EVENTS
        # minimal attributes used by CLI
        self.registry = registry
        self._session_service = session_service
        # Mock _session_tracker for session saving
        from unittest.mock import MagicMock
        self._session_tracker = MagicMock()
        self._session_tracker.get_session_messages.return_value = []
        # Mock agent_config with llm_profile
        self.agent_config = MagicMock()
        self.agent_config.llm_profile = "normal"

    async def run_events(self, task, **kwargs):
        for e in self._events:
            yield e

    async def run(self, task):
        # keep compatibility with non-streaming path
        result = {"task": task, "calls": []}
        for e in self._events:
            if e.get("type") == "tool_result":
                result.setdefault("calls", []).append({
                    "server": e.get("server"),
                    "action": e.get("action"),
                    "result": e.get("result"),
                })
            if e.get("type") == "final":
                result["summary"] = e.get("summary")
        return result


def test_cli_raw_flag_outputs_json(stubbed_cli, monkeypatch, capsys):
    # Prepare a dummy agent that yields one tool_result and a final summary
    events = [
        {"type": "tool_call", "server": "s", "action": "a", "params": {"x": 1}},
        {"type": "tool_result", "server": "s", "action": "a", "result": {"ok": True}},
        {"type": "final", "summary": "done"},
        {"type": "end"},
    ]

    # capture events in global used by AgentStub
    global DEFAULT_EVENTS
    DEFAULT_EVENTS = events
    monkeypatch.setattr('agent_system.servers.agent.server.Agent', AgentStub)
    monkeypatch.setattr('agent_system.servers.agent.entry.Agent', AgentStub)

    # Provide a fake initialization service that returns empty registry/session service
    from unittest.mock import AsyncMock, MagicMock

    mock_registry = MagicMock()
    mock_registry.list.return_value = []
    mock_registry.get.return_value = None

    # AsyncMock for the awaited half: a plain MagicMock's save_session
    # returns something un-awaitable — the CLI catches the TypeError and
    # writes 'Failed to save session: object MagicMock can't be used in
    # await expression' as an ERROR into the real logs/cli.log on every
    # run of this test, while the test itself stays green. A double that
    # violates the async contract of the thing it doubles tests a CLI
    # that does not exist.
    mock_session_service = MagicMock()
    mock_session_service.save_session = AsyncMock(return_value=True)
    mock_session_service.open_for_run = AsyncMock(return_value=False)
    fake_session_manager = MagicMock()

    class FakeInitService:
        def __init__(self, *_args, **_kwargs):
            self.session_manager = fake_session_manager

        def initialize_for_cli(self):
            return mock_registry, mock_session_service

    monkeypatch.setattr('agent_system.services.initialization_service.InitializationService', FakeInitService)

    # Run with --raw via argv (which uses non-streaming mode)
    monkeypatch.setattr("sys.argv", ["agent-cli", "--raw", "run", "do it"])
    # Call main
    cli.main()

    out = capsys.readouterr().out
    # The CLI prints some status lines before the JSON blob; extract just the JSON object
    # by finding the LAST valid JSON object (ignore any error messages that might also contain JSON)
    # Find all JSON objects and use the last complete one
    json_objects = []
    i = 0
    while i < len(out):
        if out[i] == '{':
            json_text = out[i:]
            brace_count = 0
            json_end = 0
            for j, ch in enumerate(json_text):
                if ch == '{':
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = j + 1
                        try:
                            json_objects.append(json.loads(json_text[:json_end]))
                        except json.JSONDecodeError:
                            pass  # Not valid JSON, continue
                        break
            i += json_end if json_end > 0 else 1
        else:
            i += 1
    
    assert len(json_objects) > 0, f"No valid JSON objects found in output: {out!r}"
    parsed = json_objects[-1]  # Use the last JSON object (the actual CLI output)
    assert parsed["task"] == "do it"
    assert parsed["summary"] == "done"
    # The double honours the async contract AND the CLI really persisted:
    # without this, reverting save_session to a plain MagicMock stays green
    # while every test run writes an ERROR into the real logs/cli.log.
    assert mock_session_service.save_session.await_count >= 1, (
        "the CLI never saved the session - the run ended in the catch block")


def test_cli_streaming_prints_human_readable(stubbed_cli, monkeypatch, capsys):
    events = [
        {"type": "tool_call", "server": "s", "action": "a", "params": {"x": 1}},
        {"type": "tool_result", "server": "s", "action": "a", "result": {"ok": True}},
        {"type": "final", "summary": "done"},
        {"type": "end"},
    ]
    global DEFAULT_EVENTS
    DEFAULT_EVENTS = events
    monkeypatch.setattr('agent_system.servers.agent.server.Agent', AgentStub)
    monkeypatch.setattr('agent_system.servers.agent.entry.Agent', AgentStub)

    from unittest.mock import AsyncMock, MagicMock

    mock_registry = MagicMock()
    mock_registry.list.return_value = []
    mock_registry.get.return_value = None

    # AsyncMock for the awaited half: a plain MagicMock's save_session
    # returns something un-awaitable — the CLI catches the TypeError and
    # writes 'Failed to save session: object MagicMock can't be used in
    # await expression' as an ERROR into the real logs/cli.log on every
    # run of this test, while the test itself stays green. A double that
    # violates the async contract of the thing it doubles tests a CLI
    # that does not exist.
    mock_session_service = MagicMock()
    mock_session_service.save_session = AsyncMock(return_value=True)
    mock_session_service.open_for_run = AsyncMock(return_value=False)
    fake_session_manager = MagicMock()

    class FakeInitService:
        def __init__(self, *_args, **_kwargs):
            self.session_manager = fake_session_manager

        def initialize_for_cli(self):
            return mock_registry, mock_session_service

    monkeypatch.setattr('agent_system.services.initialization_service.InitializationService', FakeInitService)

    monkeypatch.setattr("sys.argv", ["agent-cli", "--show-tools", "run", "do it"])
    cli.main()
    out = capsys.readouterr().out
    # Should contain human readable header and summary (now printed during streaming without "Summary:" prefix)
    assert "TOOL CALL" in out
    assert "TOOL RESULT" in out
    assert "done" in out  # The summary content
    # The double honours the async contract AND the CLI really persisted:
    # without this, reverting save_session to a plain MagicMock stays green
    # while every test run writes an ERROR into the real logs/cli.log.
    assert mock_session_service.save_session.await_count >= 1, (
        "the CLI never saved the session - the run ended in the catch block")


@pytest.fixture
def stubbed_cli(monkeypatch):
    """The CLI on AgentStub: no plugins, a session service that opens and saves nothing, and
    no log file, tool integration or batch system of the real configuration."""
    from unittest.mock import AsyncMock, MagicMock

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(run_cmd, "setup_role_logging", lambda *args, **kwargs: None)
    for name in ("initialize_tools", "init_batch_system", "shutdown_tools", "shutdown_batch_system"):
        monkeypatch.setattr(run_cmd, name, nothing)
    monkeypatch.setattr('agent_system.servers.agent.server.Agent', AgentStub)
    monkeypatch.setattr('agent_system.servers.agent.entry.Agent', AgentStub)
    registry = MagicMock()
    registry.list.return_value = []
    service = MagicMock()
    service.save_session = AsyncMock(return_value=True)
    service.open_for_run = AsyncMock(return_value=False)

    class FakeInitService:
        def __init__(self, *_args, **_kwargs):
            self.session_manager = MagicMock()

        def initialize_for_cli(self):
            return registry, service

    monkeypatch.setattr('agent_system.services.initialization_service.InitializationService', FakeInitService)


SAID_NO = {"type": "error", "message": "the model said no"}


@pytest.mark.parametrize("flag", ["--raw", "--no-status"])
def test_a_run_that_ends_in_errors_shows_them(stubbed_cli, monkeypatch, capsys, flag):
    """Both modes collect the run through collect_final_result; the errors show
    after the run. The exit code stays 0, as before: a failed run exiting 1 is
    a contract the callers (publish_pipeline, the writer bench) have to learn
    first."""
    monkeypatch.setattr(sys.modules[__name__], "DEFAULT_EVENTS", [SAID_NO, {"type": "end"}])
    monkeypatch.setattr("sys.argv", ["agent-cli", flag, "run", "do it"])

    cli.main()

    assert "the model said no" in capsys.readouterr().out


def test_the_answer_and_an_error_show_as_they_arrive(stubbed_cli, monkeypatch, capsys):
    """A Ctrl-C mostly lands out of the event loop (on Windows every time), and
    then only the stop is printed after the run: the answer and an error have
    to be out once their events have passed, as when the stream printed them
    itself. An error shows once, not again after the run."""
    shown = []

    async def run_events(self, task, **kwargs):
        yield {"type": "error", "message": "the model said no"}
        shown.append(capsys.readouterr().out)
        yield {"type": "final", "summary": "the answer"}
        shown.append(capsys.readouterr().out)
        yield {"type": "cancelled"}
        yield {"type": "end"}

    monkeypatch.setattr(AgentStub, "run_events", run_events)
    monkeypatch.setattr("sys.argv", ["agent-cli", "--no-status", "run", "do it"])

    cli.main()

    assert "the model said no" in shown[0], "the error waited for the end of the run"
    assert "the answer" in shown[1], "the answer waited for the end of the run"
    after = capsys.readouterr().out
    assert "Cancelled by user" in after
    assert "the model said no" not in after, "the error was shown twice"


def test_a_name_that_is_no_agent_lists_the_agents_and_exits_1(stubbed_cli, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agent-cli", "run", "do it", "--agent", "no_such_agent_here"])

    with pytest.raises(SystemExit) as refused:
        cli.main()

    assert refused.value.code == 1
    err = capsys.readouterr().err
    assert "no_such_agent_here" in err and "Available agents" in err, err


def test_an_error_no_event_showed_is_named_after_the_run(stubbed_cli, monkeypatch, capsys):
    """An exception out of the run has no event: collect_final_result puts it
    into the errors, and the block after the run is where it shows -- next to
    the errors the stream showed as they came, which it does not repeat."""
    async def run_events(self, task, **kwargs):
        yield {"type": "error", "message": "the model said no"}
        raise RuntimeError("the store went away")

    monkeypatch.setattr(AgentStub, "run_events", run_events)
    monkeypatch.setattr("sys.argv", ["agent-cli", "--no-status", "run", "do it"])

    cli.main()

    out = capsys.readouterr().out
    assert "the store went away" in out, "an error without an event was never shown"
    assert out.count("the model said no") == 1, out
