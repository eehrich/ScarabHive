from agent_system.app import build_app
from agent_system.config.settings import load_settings
from agent_system.tools.base import ToolServerRegistry
from agent_system.servers.bootstrap import bootstrap_servers


def test_build_app():
  app = build_app()
  assert app is not None


def test_bootstrap_and_list():
  cfg = load_settings('config/config.yaml')  # Use new unified config
  reg = ToolServerRegistry()
  bootstrap_servers(cfg, reg)
  assert len(reg.list()) >= 1
