"""Tests for loading multiple instances of the same plugin."""


def test_multiple_plugin_instances_concept():
    """
    Test that demonstrates the concept of multiple plugin instances.
    
    The plugin configuration allows loading the same plugin type multiple times
    with different instance names and configurations:
    
    Example:
        ssh_server_1:
          type: ssh_control
          machines: [server1]
        
        ssh_server_2:
          type: ssh_control  
          machines: [server2]
    
    This test verifies that the YAML structure supports this pattern.
    """
    import yaml
    
    config_yaml = """
plugins:
  servers:
    # First instance of basic_agent
    agent_instance_1:
      type: basic_agent
      enabled: true
      agent_config:
        llm_profile: normal
        max_steps: 10
    
    # Second instance of basic_agent with different config
    agent_instance_2:
      type: basic_agent
      enabled: true
      agent_config:
        llm_profile: turbo
        max_steps: 20
    
    # Third instance - same type, different name
    my_custom_agent:
      type: basic_agent
      enabled: true
      agent_config:
        llm_profile: advanced
        max_steps: 30
"""
    
    config = yaml.safe_load(config_yaml)
    servers = config['plugins']['servers']
    
    # Verify we can have multiple instances with the same type
    assert len(servers) == 3
    assert 'agent_instance_1' in servers
    assert 'agent_instance_2' in servers
    assert 'my_custom_agent' in servers
    
    # Verify all are of type basic_agent
    assert servers['agent_instance_1']['type'] == 'basic_agent'
    assert servers['agent_instance_2']['type'] == 'basic_agent'
    assert servers['my_custom_agent']['type'] == 'basic_agent'
    
    # Verify each has different configuration
    assert servers['agent_instance_1']['agent_config']['max_steps'] == 10
    assert servers['agent_instance_2']['agent_config']['max_steps'] == 20
    assert servers['my_custom_agent']['agent_config']['max_steps'] == 30
    
    # Verify instance names are preserved (not types)
    instance_names = list(servers.keys())
    assert 'agent_instance_1' in instance_names
    assert 'agent_instance_2' in instance_names
    assert 'my_custom_agent' in instance_names


def test_multi_instance_plugin_loading():
    """Test that plugins can actually be loaded multiple times with different configs."""
    from unittest.mock import Mock
    from plugins.basic_agent.plugin import PLUGIN_FACTORY
    from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig
    
    # Create mock system config with LLM system
    system_config = Mock(spec=AgentSystemConfig)
    system_config.llm_system = Mock()
    system_config.llm_system.profiles = {
        'normal': Mock(model_ref='gpt-5-nano'),
        'turbo': Mock(model_ref='gpt-5-nano')
    }
    system_config.llm_system.models = {
        'gpt-5-nano': Mock(provider='openai', model='gpt-5-nano')
    }
    system_config.llm_system.default_profile = 'normal'
    system_config.network = Mock()
    system_config.network.ssl_verify = False
    
    # Load first instance
    mcp_config1 = MCPConfig(
        type='basic_agent',
        enabled=True,
        agent_config=AgentConfig(llm_profile='normal', max_steps=10)
    )
    plugin1 = PLUGIN_FACTORY(
        name='agent_instance_1',
        system_config=system_config,
        mcp_config=mcp_config1
    )
    
    # Load second instance with different config
    mcp_config2 = MCPConfig(
        type='basic_agent',
        enabled=True,
        agent_config=AgentConfig(llm_profile='turbo', max_steps=20)
    )
    plugin2 = PLUGIN_FACTORY(
        name='agent_instance_2',
        system_config=system_config,
        mcp_config=mcp_config2
    )
    
    # Verify both plugins loaded
    assert plugin1 is not None
    assert plugin2 is not None
    
    # Verify they are different instances
    assert plugin1 is not plugin2
    
    # Verify they have different names
    assert plugin1.name == 'agent_instance_1'
    assert plugin2.name == 'agent_instance_2'
    
    # Verify both have get_tools method (MCP plugins)
    assert hasattr(plugin1, 'get_tools')
    assert hasattr(plugin2, 'get_tools')
    
    tools1 = plugin1.get_tools()
    tools2 = plugin2.get_tools()
    
    assert len(tools1) > 0
    assert len(tools2) > 0


def test_multi_instance_hook_plugins():
    """Test that hook plugins can be loaded multiple times."""
    from plugins.context_optimizer.plugin import PLUGIN_FACTORY as OptimizerFactory
    
    # Load first instance with aggressive optimization
    optimizer1 = OptimizerFactory(
        name='optimizer_aggressive',
        system_config={},
        mcp_config={
            'max_tokens': 1000,
            'preserve_recent': 5
        }
    )
    
    # Load second instance with conservative optimization
    optimizer2 = OptimizerFactory(
        name='optimizer_conservative',
        system_config={},
        mcp_config={
            'max_tokens': 5000,
            'preserve_recent': 10
        }
    )
    
    # Verify both loaded
    assert optimizer1 is not None
    assert optimizer2 is not None
    
    # Verify they are different instances
    assert optimizer1 is not optimizer2
    
    # Verify both have get_hooks method
    assert hasattr(optimizer1, 'get_hooks')
    assert hasattr(optimizer2, 'get_hooks')
    
    hooks1 = optimizer1.get_hooks()
    hooks2 = optimizer2.get_hooks()
    
    assert len(hooks1) > 0
    assert len(hooks2) > 0


def test_plugin_type_vs_name_distinction():
    """
    Test that demonstrates why we need both 'type' and instance name.
    
    The 'type' specifies WHICH plugin to load (the plugin code).
    The instance name specifies HOW to identify this particular instance.
    
    This allows:
    - Multiple SSH control plugins for different server groups
    - Multiple agent plugins with different configurations
    - Multiple search plugins with different regions/settings
    """
    import yaml
    
    # Realistic example: Multiple SSH control plugins
    config_yaml = """
plugins:
  servers:
    ssh_production:
      type: ssh_control
      enabled: true
      machines:
        - name: prod-server-1
          host: 192.168.1.10
        - name: prod-server-2
          host: 192.168.1.11
    
    ssh_development:
      type: ssh_control
      enabled: true
      machines:
        - name: dev-server-1
          host: 192.168.2.10
        - name: dev-server-2
          host: 192.168.2.11
    
    ssh_testing:
      type: ssh_control
      enabled: false
      machines:
        - name: test-server
          host: 192.168.3.10
"""
    
    config = yaml.safe_load(config_yaml)
    servers = config['plugins']['servers']
    
    # All three use the same plugin type
    assert servers['ssh_production']['type'] == 'ssh_control'
    assert servers['ssh_development']['type'] == 'ssh_control'
    assert servers['ssh_testing']['type'] == 'ssh_control'
    
    # But they have different instance names
    instance_names = list(servers.keys())
    assert 'ssh_production' in instance_names
    assert 'ssh_development' in instance_names
    assert 'ssh_testing' in instance_names
    
    # And different configurations
    assert len(servers['ssh_production']['machines']) == 2
    assert len(servers['ssh_development']['machines']) == 2
    assert len(servers['ssh_testing']['machines']) == 1
    
    # And different enabled states
    assert servers['ssh_production']['enabled'] is True
    assert servers['ssh_development']['enabled'] is True
    assert servers['ssh_testing']['enabled'] is False


def test_instance_name_is_preserved():
    """Test that the instance name is passed to the plugin and preserved."""
    from unittest.mock import Mock
    from plugins.basic_agent.plugin import PLUGIN_FACTORY
    from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig
    
    # Create mock system config
    system_config = Mock(spec=AgentSystemConfig)
    system_config.llm_system = Mock()
    system_config.llm_system.profiles = {'normal': Mock(model_ref='gpt-5-nano')}
    system_config.llm_system.models = {'gpt-5-nano': Mock(provider='openai', model='gpt-5-nano')}
    system_config.llm_system.default_profile = 'normal'
    system_config.network = Mock()
    system_config.network.ssl_verify = False
    
    # Load with custom instance name
    custom_name = 'my_special_agent_v2'
    mcp_config = MCPConfig(
        type='basic_agent',
        enabled=True,
        agent_config=AgentConfig(llm_profile='normal', max_steps=10)
    )
    plugin = PLUGIN_FACTORY(
        name=custom_name,
        system_config=system_config,
        mcp_config=mcp_config
    )
    
    assert plugin is not None
    assert plugin.name == custom_name
    
    # The name should NOT be the type
    assert plugin.name != 'basic_agent'

