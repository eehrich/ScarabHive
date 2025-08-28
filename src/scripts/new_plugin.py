#!/usr/bin/env python3
"""Scaffold a new plugin folder under plugins/"""
import sys
from pathlib import Path

TEMPLATE_PLUGIN = '''PLUGIN_NAME = "{name}"

class {class_name}:
    def __init__(self, name, cfg=None, ssl_verify=True):
        self.name = name
        self.cfg = cfg or {{}}
        self.ssl_verify = ssl_verify

    async def call(self, *args, **kwargs):
        return {{"status": "ok", "name": self.name}}

PLUGIN_FACTORY = {class_name}
'''

TEMPLATE_META = '''
# Optional plugin metadata
name: {name}
description: "A plugin called {name}"
version: 0.0.1
'''


def main():
    if len(sys.argv) < 2:
        print('Usage: new_plugin.py <plugin_name>')
        sys.exit(2)
    name = sys.argv[1]
    folder = Path('plugins') / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / '__init__.py').write_text('# package')
    (folder / 'plugin.py').write_text(TEMPLATE_PLUGIN.format(name=name, class_name=name.capitalize()))
    (folder / 'plugin.yaml').write_text(TEMPLATE_META.format(name=name))
    print('Created plugin at', folder)


if __name__ == '__main__':
    main()
