"""
Configuration models for AgentSystem.

This module provides Pydantic models that match the new YAML configuration
structure with config.yaml as the master configuration and included files
for LLM and tool server configurations.

One module per part of that structure; every name is importable from here, as
it was when this package was a single file:

- ``llm``              -- ``llm_system:``: chat, TTS and decision models and their profiles
- ``agent``            -- an agent's ``agent_config`` and the ``plugins:`` section whose
                          server entries carry it, with the per-agent ``llm_params``
- ``external_servers`` -- ``external_servers:``: the MCP servers the system connects to
- ``auth``             -- ``auth:``: accounts, registration, anonymous access, route rules
- ``system``           -- every other section of the master config, and its root,
                          ``AgentSystemConfig``
"""
from .llm import (
    BatchProviderConfig,
    BatchSystemConfig,
    DecisionModelConfig,
    DecisionProfile,
    HTTPXTimeoutConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ModelCapabilitiesConfig,
    TTSModelConfig,
    TTSProfile,
)
from .agent import (
    LLM_PARAMS_PROTECTED_FIELDS,
    AgentConfig,
    AgentMetadata,
    HooksConfig,
    LoopDetectionConfig,
    PluginsConfig,
    ReasoningLoopConfig,
    SkillsConfig,
    TimeoutConfig,
    ToolConfig,
    ToolServerConfig,
    resolve_llm_params,
)
from .external_servers import (
    ExternalServerCacheConfig,
    ExternalServerConfig,
    ExternalServerConnectionConfig,
    ExternalServersConfig,
    MCPAuthConfig,
    MCPServersConfig,
    RemoteMCPConfig,
)
from .auth import (
    AnonymousAccessConfig,
    AuthConfig,
    EndpointSecurityConfig,
    EndpointSecurityRule,
    LLMSecurityConfig,
    PluginSecurityConfig,
    RegistrationConfig,
)
from .system import (
    AgentSystemConfig,
    CancellationConfig,
    ContextConfig,
    GlobalHooksConfig,
    HookOrderConfig,
    HookOverrideConfig,
    LoggingConfig,
    NetworkConfig,
    PathsConfig,
    SessionArchiveConfig,
    SessionPresenceConfig,
    SkillsSystemConfig,
    StatusConfig,
    VisionConfig,
    strip_empty_yaml_keys,
)

__all__ = [
    # llm
    "BatchProviderConfig",
    "BatchSystemConfig",
    "DecisionModelConfig",
    "DecisionProfile",
    "HTTPXTimeoutConfig",
    "LLMModelConfig",
    "LLMProfile",
    "LLMSystemConfig",
    "ModelCapabilitiesConfig",
    "TTSModelConfig",
    "TTSProfile",
    # agent
    "LLM_PARAMS_PROTECTED_FIELDS",
    "AgentConfig",
    "AgentMetadata",
    "HooksConfig",
    "LoopDetectionConfig",
    "PluginsConfig",
    "ReasoningLoopConfig",
    "SkillsConfig",
    "TimeoutConfig",
    "ToolConfig",
    "ToolServerConfig",
    "resolve_llm_params",
    # external_servers
    "ExternalServerCacheConfig",
    "ExternalServerConfig",
    "ExternalServerConnectionConfig",
    "ExternalServersConfig",
    "MCPAuthConfig",
    "MCPServersConfig",
    "RemoteMCPConfig",
    # auth
    "AnonymousAccessConfig",
    "AuthConfig",
    "EndpointSecurityConfig",
    "EndpointSecurityRule",
    "LLMSecurityConfig",
    "PluginSecurityConfig",
    "RegistrationConfig",
    # system
    "AgentSystemConfig",
    "CancellationConfig",
    "ContextConfig",
    "GlobalHooksConfig",
    "HookOrderConfig",
    "HookOverrideConfig",
    "LoggingConfig",
    "NetworkConfig",
    "PathsConfig",
    "SessionArchiveConfig",
    "SessionPresenceConfig",
    "SkillsSystemConfig",
    "StatusConfig",
    "VisionConfig",
    "strip_empty_yaml_keys",
]
