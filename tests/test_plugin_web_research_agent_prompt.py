from pathlib import Path

from plugins.web_research_agent import server as web_research_agent


def test_web_research_agent_uses_plugin_prompt():
    # Create the agent using the factory; it should load the plugin-local prompt
    agent = web_research_agent.create_web_research_agent(name='web_test', config={
        # minimal parent_llm dummy to satisfy LLM profile resolution in plugin factory
        'parent_llm': {
            'llm': {'provider': 'ollama', 'model': 'gpt-oss:20b'}, 
            'llm_system': {
                'models': {
                    'gpt-oss:20b': {'provider': 'ollama', 'model': 'gpt-oss:20b'}
                }, 
                'profiles': {
                    'normal': {'model_ref': 'gpt-oss:20b'},
                    'fast': {'model_ref': 'gpt-oss:20b'}
                }, 
                'default_profile': 'fast'
            }
        },
    }, ssl_verify=True)

    # The plugin code attaches research_config.prompts as a SimpleNamespace if found
    prompts = getattr(agent.agent_config, 'prompts', None)
    # Some code paths may attach prompts as a SimpleNamespace on the AgentConfig
    assert prompts is not None, "AgentConfig.prompts not set"

    # system_prompt should be present (raw YAML string)
    system_prompt = getattr(prompts, 'system_prompt', None)
    assert system_prompt is not None and isinstance(system_prompt, str), "Plugin prompt not loaded into memory"

    # Ensure it contains an indicative phrase from the plugin prompt
    assert 'specialized web research assistant' in system_prompt.lower()


def test_global_prompt_unchanged():
    # Ensure the global prompt file does not contain the plugin-specific wording
    p = Path('config/prompts/system_prompt.yaml')
    assert p.exists()
    txt = p.read_text(encoding='utf-8')
    assert 'specialized web research assistant' not in txt.lower()
