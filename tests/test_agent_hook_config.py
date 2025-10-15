"""Tests for agent-level hook configuration."""
import pytest
from agent_system.config.models import AgentConfig, HooksConfig


def test_hooks_config_default():
    """Test HooksConfig with default values."""
    config = HooksConfig()
    
    assert config.enabled is True
    assert config.disabled_hooks == []
    assert config.enabled_hooks == []
    assert config.hook_overrides == {}


def test_hooks_config_disabled_hooks():
    """Test disabling specific hooks."""
    config = HooksConfig(
        enabled=True,
        disabled_hooks=['markdown_formatter.format_markdown_output', 'request_logger.log_pre_llm']
    )
    
    assert config.enabled is True
    assert len(config.disabled_hooks) == 2
    assert 'markdown_formatter.format_markdown_output' in config.disabled_hooks
    assert 'request_logger.log_pre_llm' in config.disabled_hooks


def test_hooks_config_enabled_hooks_whitelist():
    """Test enabling only specific hooks when globally disabled."""
    config = HooksConfig(
        enabled=False,
        enabled_hooks=['llm_message_validator.validate_messages']
    )
    
    assert config.enabled is False
    assert len(config.enabled_hooks) == 1
    assert 'llm_message_validator.validate_messages' in config.enabled_hooks


def test_hooks_config_hook_overrides():
    """Test per-hook configuration overrides."""
    config = HooksConfig(
        enabled=True,
        hook_overrides={
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
    
    assert 'markdown_formatter.format_markdown_output' in config.hook_overrides
    assert config.hook_overrides['markdown_formatter.format_markdown_output']['timeout'] == 5.0
    assert config.hook_overrides['context_optimizer.optimize_context']['enabled'] is False


def test_agent_config_with_hooks():
    """Test AgentConfig with hooks configuration."""
    hooks_config = HooksConfig(
        enabled=True,
        disabled_hooks=['request_logger.log_pre_llm']
    )
    
    agent_config = AgentConfig(
        llm_profile='turbo',
        max_steps=10,
        hooks=hooks_config
    )
    
    assert agent_config.hooks is not None
    assert agent_config.hooks.enabled is True
    assert len(agent_config.hooks.disabled_hooks) == 1


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
        'disabled_hooks': ['plugin_a.hook_x', 'plugin_b.hook_y'],
        'enabled_hooks': [],
        'hook_overrides': {
            'plugin_c.hook_z': {
                'enabled': False,
                'timeout': 10.0
            }
        }
    }
    
    config = HooksConfig(**data)
    
    assert config.enabled is True
    assert len(config.disabled_hooks) == 2
    assert 'plugin_a.hook_x' in config.disabled_hooks


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
    
    # Test 1: Globally enabled, specific hook disabled
    agent = Mock()
    agent.agent_config = Mock()
    agent.agent_config.hooks = HooksConfig(
        enabled=True,
        disabled_hooks=['markdown_formatter.format_markdown_output']
    )
    
    manager = HookIntegrationManager(agent)
    assert manager.is_hook_enabled('markdown_formatter.format_markdown_output') is False
    assert manager.is_hook_enabled('request_logger.log_pre_llm') is True
    
    # Test 2: Globally disabled, specific hook enabled via whitelist
    agent2 = Mock()
    agent2.agent_config = Mock()
    agent2.agent_config.hooks = HooksConfig(
        enabled=False,
        enabled_hooks=['llm_message_validator.validate_messages']
    )
    
    manager2 = HookIntegrationManager(agent2)
    assert manager2.is_hook_enabled('llm_message_validator.validate_messages') is True
    assert manager2.is_hook_enabled('markdown_formatter.format_markdown_output') is False
    
    # Test 3: Per-hook override
    agent3 = Mock()
    agent3.agent_config = Mock()
    agent3.agent_config.hooks = HooksConfig(
        enabled=True,
        hook_overrides={
            'context_optimizer.optimize_context': {
                'enabled': False
            }
        }
    )
    
    manager3 = HookIntegrationManager(agent3)
    assert manager3.is_hook_enabled('context_optimizer.optimize_context') is False
    assert manager3.is_hook_enabled('other_plugin.other_hook') is True


def test_hooks_config_validation():
    """Test that hook names follow expected pattern."""
    # Valid hook names
    valid_config = HooksConfig(
        disabled_hooks=[
            'markdown_formatter.format_markdown_output',
            'request_logger.log_pre_llm',
            'llm_message_validator.validate_messages'
        ]
    )
    assert len(valid_config.disabled_hooks) == 3
    
    # Hook names should contain plugin.hook_name pattern
    # (validation is done at schema level, here we just test the model accepts them)
    for hook_name in valid_config.disabled_hooks:
        assert '.' in hook_name
        plugin_name, hook_method = hook_name.split('.', 1)
        assert len(plugin_name) > 0
        assert len(hook_method) > 0
