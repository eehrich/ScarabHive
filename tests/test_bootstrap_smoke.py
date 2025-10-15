from agent_system.app import build_app
from agent_system.config.settings import load_settings
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.bootstrap import bootstrap_servers


def test_build_app():
  app = build_app()
  assert app is not None


def test_bootstrap_and_list():
  cfg = load_settings('config/config.yaml')  # Use new unified config
  reg = MCPRegistry()
  bootstrap_servers(cfg, reg)
  assert len(reg.list()) >= 1
