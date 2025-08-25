from agent_system.agent.interface_api import build_app
from agent_system.config.loader import load_config
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.bootstrap import bootstrap_servers


def test_build_app():
  app = build_app()
  assert app is not None


def test_bootstrap_and_list():
  cfg = load_config('config/agent.yaml')
  reg = MCPRegistry()
  bootstrap_servers(cfg, reg)
  assert len(reg.list()) >= 1
