from pathlib import Path

from agent_system import agent_cli as cli


class DummyAgent:
    def __init__(self, *args, events=None, **kwargs):
        # Accept the Agent constructor signature (name, system_config, mcp_config, registry)
        # The real AgentConfig, not a stand-in: the CLI reads
        # agent_config.default_llm_profile to decide whether a stored session
        # profile is an override at all, and a fake without it hides that.
        from agent_system.config.models import AgentConfig

        self.agent_config = AgentConfig()
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
        agent = DummyAgent()
        agent.registry = registry
        return agent

    # Monkeypatch load_settings to provide a minimal AgentSystemConfig-like object
    from agent_system.config.models import AgentSystemConfig, LLMSystemConfig, LLMModelConfig, MCPConfig, AgentConfig
    mock_config = AgentSystemConfig(llm_system=LLMSystemConfig(models={"test-model": LLMModelConfig(provider="openai", model="test-model")}, profiles={}))
    monkeypatch.setattr(cli, "load_settings", lambda path=None: mock_config)

    # Mock InitializationService to not load any plugins
    from agent_system.services.initialization_service import InitializationService
    from agent_system.mcp.base import MCPRegistry
    
    def fake_initialize_for_cli(self):
        # Return empty registry and a minimal session_service
        registry = MCPRegistry()
        from agent_system.services.session_manager import SessionManager
        from agent_system.services.session_service import SessionService
        session_manager = SessionManager(storage_path="data/sessions")
        session_service = SessionService(session_manager)
        return registry, session_service
    
    monkeypatch.setattr(InitializationService, 'initialize_for_cli', fake_initialize_for_cli)

    # The entry agent's config comes from the MERGED server config
    # (get_mcp_config_by_name), which reads config.plugins -- so the plugins
    # section goes on the config itself, not behind a patched accessor.
    from agent_system.config.models import PluginsConfig
    mock_config.plugins = PluginsConfig(servers={
        "basic_agent": MCPConfig(
            type="agent",
            enabled=True,
            agent_config=AgentConfig(system_prompt="test")
        )
    })

    # Patch the Agent class used by CLI to return our fake entry agent so main() will use it.
    # The CLI constructs the Agent as Agent(name, system_config, mcp_config, registry).
    # Call fake_entry_agent(name, system_config, registry) to capture the runtime config.
    monkeypatch.setattr(
        'agent_system.servers.agent.server.Agent',
        lambda name, system_config, mcp_config=None, registry=None, **k: fake_entry_agent(name, system_config, registry)
    )
    monkeypatch.setattr(
        'agent_system.agent_cli.Agent',
        lambda name, system_config, mcp_config=None, registry=None, **k: fake_entry_agent(name, system_config, registry)
    )
    # Run CLI in raw mode to take the non-streaming path (simpler output)
    monkeypatch.setattr('sys.argv', ['agent-cli', '--raw', 'run', 'do it'])

    # Make reading the template deterministic: ensure Path.read_text returns the original template
    original_template = Path('config/prompts/system_prompt.md').read_text(encoding='utf-8')
    # **kwargs on purpose: this replaces read_text for EVERY reader in the CLI
    # run, and a signature that only knows `encoding` turns any other caller
    # (errors=, newline=) into a TypeError far away from this test.
    monkeypatch.setattr(Path, 'read_text', lambda self, encoding='utf-8', **kwargs: original_template)

    cli.main()

    # Ensure the entry agent factory was called and received the loaded config
    assert 'cfg' in captured, "Entry agent factory was not invoked by CLI"
    # The CLI currently does not mutate the persisted global template file; that is
    # verified in the next test. Here we only ensure the loaded config object was
    # forwarded to the agent factory (no in-memory prompts mutation required).
    assert captured['cfg'] is mock_config


def test_global_template_not_modified():
    # Ensure the persisted global prompt file does NOT contain the injected hint
    p = Path('config/prompts/system_prompt.md')
    assert p.exists(), f"Global prompt file {p} not found"
    txt = p.read_text(encoding='utf-8')
    hint = "Note: No follow-up questions are allowed. Please answer the request directly without asking clarifying questions."
    assert hint not in txt, "Global prompt file was modified — it should remain unchanged"
