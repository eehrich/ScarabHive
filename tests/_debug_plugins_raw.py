import sys
from pathlib import Path
from agent_system import cli
from agent_system.config.models import AgentConfig, MCPConfig
import shutil
import tempfile

# create temp plugins dir
pdir = Path(tempfile.mkdtemp()) / "plugins"
pdir.mkdir()
plugin_dir = pdir / "raw_example"
plugin_dir.mkdir()
(plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "raw_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')
(plugin_dir / "plugin.yaml").write_text('description: "Raw plugin"\nversion: "0.0"\n')

# build config
cfg = AgentConfig()
cfg.mcp = MCPConfig(plugin_dirs=[str(pdir)])
# monkeypatch load_settings
cli.load_settings = lambda path=None: cfg
# set argv like test
sys.argv = ["agent-cli", "plugins", "info", "raw_example", "--raw", "--format", "json"]
# run
cli.main()
print('\n-- EXIT --')
