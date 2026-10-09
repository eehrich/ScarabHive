"""The rest of the master config -- network, logging, context, status, sessions, paths, vision,
skill discovery and the global ``hooks:`` section -- and its root, AgentSystemConfig, which holds
the sections of the modules beside this one as well.
"""
from __future__ import annotations

import re
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator
from typing import Optional, Dict, List, Any, Union

from .agent import PluginsConfig
from .auth import AuthConfig
from .external_servers import MCPServersConfig
from .llm import LLMSystemConfig


# ===========================
# Core Configuration Models
# ===========================

class NetworkConfig(BaseModel):
    """Network configuration for the application"""
    ssl_verify: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    # Paths a client other than this machine may reach (auth/remote_paths.py);
    # everything else answers it 404. None: no restriction.
    remote_paths: Optional[List[str]] = None
    disable_cache: bool = True
    
    # HTTP connection pooling settings
    http_connection_limit: int = 10  # Total HTTP connections for MCP streamable transport
    http_connection_limit_per_host: int = 5  # HTTP connections per host
    
    # CLI request timeout
    cli_request_timeout: float = 5.0  # Timeout for CLI HTTP requests (seconds)


class CancellationConfig(BaseModel):
    """Configuration for the cancellation system"""
    cleanup_timeout: float = 10.0  # Seconds to wait for graceful cleanup before forcing termination
    monitor_interval: float = 1.0  # Seconds between timeout checks


class LoggingConfig(BaseModel):
    """Logging configuration"""
    enabled: bool = True
    level: str = "DEBUG"
    file: str = "logs/agent.log"
    file_cli: Optional[str] = None
    file_api: Optional[str] = None
    cancellation: Optional[CancellationConfig] = None
    
    # Log rotation settings
    rotation_enabled: bool = True  # Enable log rotation
    max_bytes: Union[int, str] = Field(default="10MB")  # Max size per log file (int in bytes or string like "10MB", "100KB", "1GB")
    backup_count: int = 5  # Number of backup files to keep
    
    @field_validator('max_bytes', mode='before')
    @classmethod
    def parse_max_bytes(cls, v: Union[int, str]) -> int:
        """Parse max_bytes from human-readable format (e.g., '10MB') to bytes."""
        if isinstance(v, int):
            return v
        
        if isinstance(v, str):
            # Match number followed by optional unit (KB, MB, GB, case-insensitive)
            match = re.match(r'^(\d+(?:\.\d+)?)\s*(KB|MB|GB|K|M|G)?$', v.strip(), re.IGNORECASE)
            if not match:
                raise ValueError(
                    f"Invalid size format: '{v}'. "
                    "Expected format: number with optional unit (KB/MB/GB), e.g., '10MB', '100KB', '1GB'"
                )
            
            number = float(match.group(1))
            unit = (match.group(2) or '').upper()
            
            # Convert to bytes
            multipliers = {
                '': 1,
                'K': 1024,
                'KB': 1024,
                'M': 1024 * 1024,
                'MB': 1024 * 1024,
                'G': 1024 * 1024 * 1024,
                'GB': 1024 * 1024 * 1024,
            }
            
            return int(number * multipliers[unit])
        
        raise ValueError(f"max_bytes must be int or string, got {type(v).__name__}")
    
    def model_post_init(self, __context) -> None:
        """Post-init hook to parse default values through validator."""
        # Manually trigger validation for max_bytes if it's still a string
        if isinstance(self.max_bytes, str):
            self.max_bytes = self.parse_max_bytes(self.max_bytes)


class ContextConfig(BaseModel):
    """Context information configuration"""
    auto_datetime: bool = True
    timezone: str = "Europe/Berlin"
    location: str = "Germany"


class StatusConfig(BaseModel):
    """Status message system configuration"""
    queue_maxsize: int = 1000  # Max events per queue (prevents memory exhaustion)
    drop_oldest_when_full: bool = True  # Drop oldest events when queue is full
    
    # SSE connection keep-alive settings
    sse_keepalive_interval: float = 15.0  # Interval for SSE keep-alive comments (seconds)
    llm_heartbeat_interval: float = 5.0  # Interval for heartbeat events during LLM calls (seconds)


class SessionPresenceConfig(BaseModel):
    """Which sessions run right now, and waking idle ones (core/session_presence.py)."""
    enabled: bool = False  # Lock files next to the session files in data/sessions
    max_wake_depth: int = 3  # A run woken this deep in a chain wakes nobody; 0 = never wake


class SessionArchiveConfig(BaseModel):
    """Old conversations move out of data/sessions into zips (services/session_archive.py).

    The unit is a whole tree -- a root session and its sub-agent sessions -- and
    it moves only when every session in it is older than ``retention_days`` and
    none of them is running. Changing these values needs a restart.
    """
    enabled: bool = True
    retention_days: int = Field(default=30, ge=1)  # below 1 would archive live work
    sweep_interval_hours: float = Field(default=24.0, gt=0)
    first_sweep_delay_seconds: float = Field(default=300.0, ge=0)  # let the app finish starting
    max_trees_per_sweep: int = Field(default=0, ge=0)  # 0: no cap -- one pass takes
    # everything old enough, which is what "archive what is older than X" asks for.
    # A positive value bounds what ONE pass writes and leaves the rest for the next.
    archive_path: Optional[str] = None  # default: <sessions>/../session_archive


class PathsConfig(BaseModel):
    """Where the system keeps what it writes (agent_system/paths.py).

    ``data_dir`` moves the whole data directory: every relative ``data/...``
    path -- in this configuration, in plugin schema defaults, in the code --
    then lands in it. Relative values are relative to the project; the
    environment variable AGENT_DATA_DIR wins over this. Only the master config
    can set it, and changing it needs a restart.
    """
    data_dir: Optional[str] = None  # default: data (in the project)


class VisionConfig(BaseModel):
    """Vision/image processing configuration"""
    image_warn_size_mb: float = 10.0  # Warn when images exceed this size (MB)
    image_max_size_mb: Optional[float] = None  # Maximum allowed image size (MB), None = no limit


class SkillsSystemConfig(BaseModel):
    """Where packaged skills are discovered (matches the ``skills:`` block).

    Mirrors ``plugins.plugin_dirs``: several roots, each of whose immediate
    subdirectories may be a skill. Relative paths resolve like plugin dirs
    (config-folder first, then repo root). The first root defining a name wins.

    Empty means "use the defaults" (``skills/`` and ``.claude/skills/``, or
    ``$AGENT_SKILL_DIRS``), so an existing config without this block keeps
    working. Note the inverse: a config that DOES list roots replaces the
    defaults entirely — listing only ``skills`` hides ``.claude/skills``.
    """

    skill_dirs: List[str] = Field(default_factory=list)


class HookOrderConfig(BaseModel):
    """Where a hook runs relative to others (names, or the virtual begin/end)."""
    model_config = ConfigDict(extra="forbid")

    before: List[str] = Field(default_factory=list)
    after: List[str] = Field(default_factory=list)


def strip_empty_yaml_keys(section: Dict[str, Any]) -> Dict[str, Any]:
    """Drop what YAML leaves behind when every line under a key is commented
    out: a null, and a mapping that held nothing else. Nothing set -- at any
    depth. Lists are left alone: `before: [null]` is a mistake, not a comment.

    Without it a null reached hook registration as `order: None` (the hook was
    lost without a word), an emptied `p.h:` hid the plugin-wide `p` override,
    and a later file's empty `enabled:` switched hooks back on over an
    earlier file's `enabled: false`.
    """
    kept: Dict[str, Any] = {}
    for key, value in section.items():
        if value is None:
            continue
        if isinstance(value, dict) and value:
            value = strip_empty_yaml_keys(value)
            if not value:
                continue
        kept[key] = value
    return kept


class HookOverrideConfig(BaseModel):
    """Operator override for one hook. Only the fields that are set apply."""
    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    timeout: Optional[float] = Field(default=None, gt=0)
    order: Optional[HookOrderConfig] = None


class GlobalHooksConfig(BaseModel):
    """Global hook settings: the top-level ``hooks:`` section of
    config/plugins.yaml or any included file, merged by load_settings like
    ``plugins:``. Not the per-agent ``agent_config.hooks``."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True  # Master switch for all hooks
    default_timeout: float = Field(default=30.0, gt=0)
    # Keys: plugin_name.hook_name (exact, wins) or plugin_name (all its hooks)
    overrides: Dict[str, HookOverrideConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _drop_empty_keys(cls, data: Any) -> Any:
        if data is None:  # `hooks:` with everything under it commented out
            return {}
        return strip_empty_yaml_keys(data) if isinstance(data, dict) else data


class AgentSystemConfig(BaseModel):
    """Main configuration model for the entire AgentSystem"""
    # Basic metadata
    name: str = "AgentSystem"
    version: str = "0.0.0"
    description: str = "Scarab Flexible AI Agent System"

    # Include references (for documentation purposes)
    includes: Optional[List[str]] = None

    # Core configurations
    paths: PathsConfig = Field(default_factory=PathsConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    status: StatusConfig = Field(default_factory=StatusConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    session_presence: SessionPresenceConfig = Field(default_factory=SessionPresenceConfig)
    session_archive: SessionArchiveConfig = Field(default_factory=SessionArchiveConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    default_agent: str = "basic_agent"
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    # Skill discovery roots. Per-agent selection is
    # AgentConfig.skills; this is only WHERE skills are found.
    skills: SkillsSystemConfig = Field(default_factory=SkillsSystemConfig)

    # Included configurations (will be populated from included files)
    llm_system: Optional[LLMSystemConfig] = None

    # New structure (Epic 0044) - matches YAML keys
    plugins: Optional[PluginsConfig] = None  # From config/plugins.yaml -> plugins:
    external_servers: Optional[MCPServersConfig] = None  # From config/mcp_servers.yaml -> external_servers:
    hooks: GlobalHooksConfig = Field(default_factory=GlobalHooksConfig)  # From config/plugins.yaml -> hooks:

    _source_path: Optional[str] = PrivateAttr(default=None)

    @property
    def source_path(self) -> Optional[str]:
        """The master file load_settings() read this from; None for a config built in code.
        What reads the config files again (the ${VAR} names in them) reads these, not the default."""
        return self._source_path
