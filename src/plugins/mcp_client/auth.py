"""HTTP auth headers for an outbound MCP connection.

Lived in ``agent_system.tools.security`` next to a security manager nobody
called. The headers are this plugin's business: it is the one that opens
connections to foreign MCP servers. ``${VAR}`` placeholders are already
resolved -- the config loader expands them while it reads the YAML.
"""

from __future__ import annotations

import base64
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent_system.config.models import MCPAuthConfig


def build_auth_headers(auth_config: Optional[MCPAuthConfig]) -> Dict[str, str]:
    """Headers for bearer / api_key / basic auth, empty for none or no config."""
    if not auth_config:
        return {}

    headers: Dict[str, str] = {}

    if auth_config.type == "bearer" and auth_config.bearer_token:
        headers["Authorization"] = f"Bearer {auth_config.bearer_token}"

    elif auth_config.type == "api_key" and auth_config.api_key:
        headers[auth_config.api_key_header] = auth_config.api_key

    elif auth_config.type == "basic" and auth_config.username and auth_config.password:
        credentials = f"{auth_config.username}:{auth_config.password}"
        headers["Authorization"] = f"Basic {base64.b64encode(credentials.encode()).decode()}"

    return headers
