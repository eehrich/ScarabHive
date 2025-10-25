"""Tests for agent-level hook configuration."""
import pytest
from agent_system.config.models import AgentConfig, HooksConfig


def test_hooks_config_default():
    """Test HooksConfig with default values."""
    config = HooksConfig()
    
    assert config.enabled is True
    assert config.overrides == {}


def test_hooks_config_disabled_specific_hooks():
    """Test disabling specific hooks via overrides."""
    config = HooksConfig(
        enabled=True,
        overrides={
            'markdown_formatter.format_markdown_output': {'enabled': False},
            'request_logger.log_pre_llm': {'enabled': False}
        }
    )
    
    assert config.enabled is True
    assert len(config.overrides) == 2
    assert config.overrides['markdown_formatter.format_markdown_output']['enabled'] is False
    assert config.overrides['request_logger.log_pre_llm']['enabled'] is False


def test_hooks_config_enabled_specific_when_globally_disabled():
    """Test enabling specific hooks when globally disabled via overrides."""
    config = HooksConfig(
        enabled=False,
        overrides={
            'llm_message_validator.validate_messages': {'enabled': True}
        }
    )
    
    assert config.enabled is False
    assert len(config.overrides) == 1
    assert config.overrides['llm_message_validator.validate_messages']['enabled'] is True


def test_hooks_config_overrides():
    """Test per-hook configuration overrides."""
    config = HooksConfig(
        enabled=True,
        overrides={
            'markdown_formatter.format_markdown_output': {
                'enabled': True,
                'timeout': 5.0,
                'config': {
                    'convert_to_html': False,
                    'enable_code_highlighting': True
                }
            },
            'context_optimizer.optimize_context': {
                'enabled': False
            }
        }
    )
    
    assert 'markdown_formatter.format_markdown_output' in config.overrides
    assert config.overrides['markdown_formatter.format_markdown_output']['timeout'] == 5.0
    assert config.overrides['context_optimizer.optimize_context']['enabled'] is False


def test_agent_config_with_hooks():
    """Test AgentConfig with hooks configuration."""
    hooks_config = HooksConfig(
        enabled=True,
        overrides={
            'request_logger.log_pre_llm': {'enabled': False}
        }
    )
    
    agent_config = AgentConfig(
        llm_profile='turbo',
        max_steps=10,
        hooks=hooks_config
    )
    
    assert agent_config.hooks is not None
    assert agent_config.hooks.enabled is True
    assert len(agent_config.hooks.overrides) == 1


def test_agent_config_without_hooks():
    """Test AgentConfig without hooks configuration (defaults)."""
    agent_config = AgentConfig(
        llm_profile='normal',
        max_steps=20
    )
    
    # Hooks should be None (optional)
    assert agent_config.hooks is None


def test_hooks_config_from_dict():
    """Test creating HooksConfig from dictionary (YAML loading)."""
    data = {
        'enabled': True,
        'overrides': {
            'plugin_a.hook_x': {'enabled': False},
            'plugin_b.hook_y': {'enabled': False},
            'plugin_c.hook_z': {
                'enabled': False,
                'timeout': 10.0
            }
        }
    }
    
    config = HooksConfig(**data)
    
    assert config.enabled is True
    assert len(config.overrides) == 3
    assert config.overrides['plugin_a.hook_x']['enabled'] is False


@pytest.mark.asyncio
async def test_hook_integration_manager_is_enabled():
    """Test HookIntegrationManager.is_enabled() with agent config."""
    from agent_system.servers.agent.components.hook_integration import HookIntegrationManager
    from unittest.mock import Mock
    
    # Mock agent with hooks enabled
    agent_enabled = Mock()
    agent_enabled.agent_config = Mock()
    agent_enabled.agent_config.hooks = HooksConfig(enabled=True)
    
    manager_enabled = HookIntegrationManager(agent_enabled)
    assert manager_enabled.is_enabled() is True
    
    # Mock agent with hooks disabled
    agent_disabled = Mock()
    agent_disabled.agent_config = Mock()
    agent_disabled.agent_config.hooks = HooksConfig(enabled=False)
    
    manager_disabled = HookIntegrationManager(agent_disabled)
    assert manager_disabled.is_enabled() is False
    
    # Mock agent without hooks config
    agent_no_config = Mock()
    agent_no_config.agent_config = Mock(spec=[])  # No hooks attribute
    
    manager_no_config = HookIntegrationManager(agent_no_config)
    assert manager_no_config.is_enabled() is True  # Default


@pytest.mark.asyncio
async def test_hook_integration_manager_is_hook_enabled():
    """Test HookIntegrationManager.is_hook_enabled() logic."""
    from agent_system.servers.agent.components.hook_integration import HookIntegrationManager
    from unittest.mock import Mock
    
    # Test 1: Globally enabled, specific hook disabled via override
    agent = Mock()
    agent.agent_config = Mock()
    agent.agent_config.hooks = HooksConfig(
        enabled=True,
        overrides={
            'markdown_formatter.format_markdown_output': {'enabled': False}
        }
    )
    
    manager = HookIntegrationManager(agent)
    assert manager.is_hook_enabled('markdown_formatter.format_markdown_output') is False
    assert manager.is_hook_enabled('request_logger.log_pre_llm') is True
    
    # Test 2: Agent with override can enable specific hook
    agent2 = Mock()
    agent2.agent_config = Mock()
    agent2.agent_config.hooks = HooksConfig(
        enabled=True,  # Agent hooks enabled
        overrides={
            'llm_message_validator.validate_messages': {'enabled': True},
            'markdown_formatter.format_markdown_output': {'enabled': False}  # Override to disable
        }
    )
    
    manager2 = HookIntegrationManager(agent2)
    assert manager2.is_hook_enabled('llm_message_validator.validate_messages') is True  # Override enables
    assert manager2.is_hook_enabled('markdown_formatter.format_markdown_output') is False  # Override disables
    assert manager2.is_hook_enabled('request_logger.log_pre_llm') is True  # No override, uses metadata default
    
    # Test 3: Agent without overrides uses hook metadata defaults
    agent3 = Mock()
    agent3.agent_config = Mock()
    agent3.agent_config.hooks = HooksConfig(enabled=True)  # enabled field is for registration, not filtering
    
    manager3 = HookIntegrationManager(agent3)
    # Without override, uses default_enabled from hook metadata (passed by registry)
    assert manager3.is_hook_enabled('any_hook', default_enabled=True) is True
    assert manager3.is_hook_enabled('any_hook', default_enabled=False) is False


def test_hooks_config_validation():
    """Test that hook names follow expected pattern."""
    # Valid hook names in overrides
    valid_config = HooksConfig(
        overrides={
            'markdown_formatter.format_markdown_output': {'enabled': False},
            'request_logger.log_pre_llm': {'enabled': True},
            'llm_message_validator.validate_messages': {'enabled': True, 'timeout': 5.0}
        }
    )
    assert len(valid_config.overrides) == 3
    
    # Hook names should contain plugin.hook_name pattern
    for hook_name in valid_config.overrides.keys():
        assert '.' in hook_name
        plugin_name, hook_method = hook_name.split('.', 1)
        assert len(plugin_name) > 0
        assert len(hook_method) > 0
