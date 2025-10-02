import json

from agent_system import cli


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
    def __init__(self, name, system_config, mcp_config, registry, llm=None, llm_factory=None, events=None):
        self._events = events or DEFAULT_EVENTS
        # minimal attributes used by CLI
        self.registry = registry

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
    monkeypatch.setattr("sys.argv", ["agent-cli", "--show-mcp", "run", "do it"])
    cli.main()
    out = capsys.readouterr().out
    # Should contain human readable header and summary
    assert "MCP CALL" in out
    assert "MCP RESULT" in out
    assert "Summary:" in out
