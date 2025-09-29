from pathlib import Path

from agent_system import cli


class DummyAgent:
    def __init__(self, events=None):
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

    # Monkeypatch load_settings to provide a proper config
    from agent_system.config.models import AgentConfig, LLMSystemConfig, LLMModelConfig
    mock_config = AgentConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")},
            default_model="test-model"
        )
    )
    monkeypatch.setattr(cli, "load_settings", lambda path=None: mock_config)
    # Patch the basic_agent factory symbol the CLI resolves (simulate entry agent creation)
    monkeypatch.setattr(cli, 'make_entry_agent', lambda config, registry=None: fake_entry_agent('basic_agent', config, registry))
    # Run CLI in raw mode to take the non-streaming path (simpler output)
    monkeypatch.setattr('sys.argv', ['agent-cli', '--raw', 'run', 'do it'])

    # Make reading the template deterministic: ensure Path.read_text returns the original template
    original_template = Path('config/prompts/system_prompt.yaml').read_text(encoding='utf-8')
    monkeypatch.setattr(Path, 'read_text', lambda self, encoding='utf-8': original_template)

    cli.main()

    # Ensure MainAgent was called and the runtime prompts.system_prompt exists
    assert 'cfg' in captured, "Entry agent factory was not invoked by CLI"
    prompts = getattr(captured['cfg'], 'prompts', None)
    assert prompts is not None, "captured config has no prompts attribute"

    # The CLI adds this exact hint string in English
    hint = "Note: No follow-up questions are allowed. Please answer the request directly without asking clarifying questions."

    # System prompt should be present in-memory and contain the hint
    system_prompt = getattr(prompts, 'system_prompt', None)
    assert system_prompt is not None, "CLI did not set prompts.system_prompt in-memory"
    assert hint in system_prompt, "English hint not found in in-memory system prompt"


def test_global_template_not_modified():
    # Ensure the persisted global prompt file does NOT contain the injected hint
    p = Path('config/prompts/system_prompt.yaml')
    assert p.exists(), f"Global prompt file {p} not found"
    txt = p.read_text(encoding='utf-8')
    hint = "Note: No follow-up questions are allowed. Please answer the request directly without asking clarifying questions."
    assert hint not in txt, "Global prompt file was modified — it should remain unchanged"
