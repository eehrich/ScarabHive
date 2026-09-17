import json

from agent_system import agent_cli as cli


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


def test_cli_raw_flag_outputs_json(monkeypatch, capsys):
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
    monkeypatch.setattr('agent_system.agent_cli.Agent', AgentStub)

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


def test_cli_streaming_prints_human_readable(monkeypatch, capsys):
    events = [
        {"type": "tool_call", "server": "s", "action": "a", "params": {"x": 1}},
        {"type": "tool_result", "server": "s", "action": "a", "result": {"ok": True}},
        {"type": "final", "summary": "done"},
        {"type": "end"},
    ]
    global DEFAULT_EVENTS
    DEFAULT_EVENTS = events
    monkeypatch.setattr('agent_system.servers.agent.server.Agent', AgentStub)
    monkeypatch.setattr('agent_system.agent_cli.Agent', AgentStub)

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
