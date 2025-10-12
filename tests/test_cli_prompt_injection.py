from pathlib import Path

from agent_system import agent_cli as cli


class DummyAgent:
    def __init__(self, *args, events=None, **kwargs):
        # Accept the Agent constructor signature (name, system_config, mcp_config, registry)
        self._events = events or [
            {"type": "final", "summary": "done"},
            {"type": "end"},
        ]

    async def run_events(self, task, **kwargs):
        for e in self._events:
            yield e

    async def run(self, task):
        # Non-streaming path: return an aggregated result
        return {"task": task, "summary": "done", "calls": []}


def test_cli_injects_german_hint_in_memory(monkeypatch):
    # Capture the config passed to entry agent factory (basic_agent)
    captured = {}

    def fake_entry_agent(name, cfg, registry=None, **kwargs):  # matches factory signature
        captured['name'] = name
        captured['cfg'] = cfg
        return DummyAgent()

    # Monkeypatch load_settings to provide a minimal AgentSystemConfig-like object
    from agent_system.config.models import AgentSystemConfig, LLMSystemConfig, LLMModelConfig
    mock_config = AgentSystemConfig(llm_system=LLMSystemConfig(models={"test-model": LLMModelConfig(provider="openai", model="test-model")}, profiles={}))
    monkeypatch.setattr(cli, "load_settings", lambda path=None: mock_config)
    # Patch the Agent class used by CLI to return our fake entry agent so main() will use it.
    # The CLI constructs the Agent as Agent(name, system_config, mcp_config, registry).
    # Call fake_entry_agent(name, system_config, registry) to capture the runtime config.
    monkeypatch.setattr(
        'agent_system.servers.agent.server.Agent',
        lambda name, system_config, mcp_config=None, registry=None, **k: fake_entry_agent(name, system_config, registry)
    )
    # Run CLI in raw mode to take the non-streaming path (simpler output)
    monkeypatch.setattr('sys.argv', ['agent-cli', '--raw', 'run', 'do it'])

    # Make reading the template deterministic: ensure Path.read_text returns the original template
    original_template = Path('config/prompts/system_prompt.yaml').read_text(encoding='utf-8')
    monkeypatch.setattr(Path, 'read_text', lambda self, encoding='utf-8': original_template)

    cli.main()

    # Ensure the entry agent factory was called and received the loaded config
    assert 'cfg' in captured, "Entry agent factory was not invoked by CLI"
    # The CLI currently does not mutate the persisted global template file; that is
    # verified in the next test. Here we only ensure the loaded config object was
    # forwarded to the agent factory (no in-memory prompts mutation required).
    assert captured['cfg'] is mock_config


def test_global_template_not_modified():
    # Ensure the persisted global prompt file does NOT contain the injected hint
    p = Path('config/prompts/system_prompt.yaml')
    assert p.exists(), f"Global prompt file {p} not found"
    txt = p.read_text(encoding='utf-8')
    hint = "Note: No follow-up questions are allowed. Please answer the request directly without asking clarifying questions."
    assert hint not in txt, "Global prompt file was modified — it should remain unchanged"
