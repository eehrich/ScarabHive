from setuptools import setup, find_packages

setup(
    name='test_plugin_real',
    version='0.0.1',
    packages=find_packages(),
    entry_points={
        'agent_system.tool_plugins': [
            'real_example = test_plugin_pkg.plugin:factory'
        ]
    }
)
