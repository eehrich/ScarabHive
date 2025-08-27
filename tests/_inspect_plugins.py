from pathlib import Path
import tempfile
import sys
from agent_system.mcp.plugins import discover_all_plugins

pdir = Path(tempfile.mkdtemp()) / 'plugins'
pdir.mkdir()
(plugin_dir := pdir / 'raw_example').mkdir()
(plugin_dir / 'plugin.py').write_text('PLUGIN_NAME = "raw_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')
(plugin_dir / 'plugin.yaml').write_text('description: "Raw plugin"\nversion: "0.0"\n')

pls = discover_all_plugins([pdir])
print('plugins keys:', list(pls.keys()))
for k,f in pls.items():
    print('name:', k, 'type:', type(f), 'repr:', repr(f), 'module:', getattr(f,'__module__', None))
