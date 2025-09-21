import json

from agent_system import cli


class DummyAgent:
    def __init__(self, events):
        self._events = events

    async def run_events(self, task):
        for e in self._events:
            # ✅ Removed asyncio.sleep(0) - not needed in test dummy
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


def test_cli_raw_flag_outputs_json(monkeypatch, capsys):
    # Prepare a dummy agent that yields one mcp_result and a final summary
    events = [
        {"type": "mcp_call", "server": "s", "action": "a", "params": {"x": 1}},
        {"type": "mcp_result", "server": "s", "action": "a", "result": {"ok": True}},
        {"type": "final", "summary": "done"},
        {"type": "end"},
    ]

    # monkeypatch MainAgent (not Agent) since CLI uses MainAgent
    monkeypatch.setattr(cli, "MainAgent", lambda *a, **k: DummyAgent(events))

    # Run with --raw via argv (which uses non-streaming mode)
    monkeypatch.setattr("sys.argv", ["agent-cli", "--raw", "run", "do it"])
    # Call main
    cli.main()

    out = capsys.readouterr().out
    # raw should be valid JSON
    parsed = json.loads(out)
    assert parsed["task"] == "do it"
    assert parsed["summary"] == "done"


def test_cli_streaming_prints_human_readable(monkeypatch, capsys):
    events = [
        {"type": "mcp_call", "server": "s", "action": "a", "params": {"x": 1}},
        {"type": "mcp_result", "server": "s", "action": "a", "result": {"ok": True}},
        {"type": "final", "summary": "done"},
        {"type": "end"},
    ]
    monkeypatch.setattr(cli, "MainAgent", lambda *a, **k: DummyAgent(events))
    monkeypatch.setattr("sys.argv", ["agent-cli", "--show-mcp", "run", "do it"])
    cli.main()
    out = capsys.readouterr().out
    # Should contain human readable header and summary
    assert "MCP CALL" in out
    assert "MCP RESULT" in out
    assert "Summary:" in out
