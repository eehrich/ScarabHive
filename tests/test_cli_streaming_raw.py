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
            if e.get("type") == "mcp_result":
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
    def __init__(self, name, system_config, mcp_config, registry, llm=None, llm_factory=None, events=None, session_service=None):
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
            if e.get("type") == "mcp_result":
                result.setdefault("calls", []).append({
                    "server": e.get("server"),
                    "action": e.get("action"),
                    "result": e.get("result"),
                })
            if e.get("type") == "final":
                result["summary"] = e.get("summary")
        return result


def test_cli_raw_flag_outputs_json(monkeypatch, capsys):
    # Prepare a dummy agent that yields one mcp_result and a final summary
    events = [
        {"type": "mcp_call", "server": "s", "action": "a", "params": {"x": 1}},
        {"type": "mcp_result", "server": "s", "action": "a", "result": {"ok": True}},
        {"type": "final", "summary": "done"},
        {"type": "end"},
    ]

    # capture events in global used by AgentStub
    global DEFAULT_EVENTS
    DEFAULT_EVENTS = events
    monkeypatch.setattr('agent_system.servers.agent.server.Agent', AgentStub)
    monkeypatch.setattr('agent_system.agent_cli.Agent', AgentStub)

    # Provide a fake initialization service that returns empty registry/session service
    from unittest.mock import MagicMock

    mock_registry = MagicMock()
    mock_registry.list.return_value = []
    mock_registry.get.return_value = None

    mock_session_service = MagicMock()
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
    # The CLI prints some status lines before the JSON blob; extract the last JSON object
    # by finding the first ``{`` that starts the JSON object at or after the last newline.
    json_start = out.find('{')
    assert json_start != -1, f"No JSON object found in output: {out!r}"
    parsed = json.loads(out[json_start:])
    assert parsed["task"] == "do it"
    assert parsed["summary"] == "done"


def test_cli_streaming_prints_human_readable(monkeypatch, capsys):
    events = [
        {"type": "mcp_call", "server": "s", "action": "a", "params": {"x": 1}},
        {"type": "mcp_result", "server": "s", "action": "a", "result": {"ok": True}},
        {"type": "final", "summary": "done"},
        {"type": "end"},
    ]
    global DEFAULT_EVENTS
    DEFAULT_EVENTS = events
    monkeypatch.setattr('agent_system.servers.agent.server.Agent', AgentStub)
    monkeypatch.setattr('agent_system.agent_cli.Agent', AgentStub)

    from unittest.mock import MagicMock

    mock_registry = MagicMock()
    mock_registry.list.return_value = []
    mock_registry.get.return_value = None

    mock_session_service = MagicMock()
    fake_session_manager = MagicMock()

    class FakeInitService:
        def __init__(self, *_args, **_kwargs):
            self.session_manager = fake_session_manager

        def initialize_for_cli(self):
            return mock_registry, mock_session_service

    monkeypatch.setattr('agent_system.services.initialization_service.InitializationService', FakeInitService)

    monkeypatch.setattr("sys.argv", ["agent-cli", "--show-mcp", "run", "do it"])
    cli.main()
    out = capsys.readouterr().out
    # Should contain human readable header and summary (now printed during streaming without "Summary:" prefix)
    assert "MCP CALL" in out
    assert "MCP RESULT" in out
    assert "done" in out  # The summary content
