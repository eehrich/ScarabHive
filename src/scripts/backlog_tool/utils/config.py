"""Configuration utilities for backlog CLI."""

import configparser
from pathlib import Path
from typing import Dict, Union


def load_config() -> Dict[str, Union[str, int, bool]]:
    """Load configuration from .backlogrc file if it exists.

    Returns a dictionary of configuration values that can be used as defaults
    for command line arguments.
    """
    config: Dict[str, Union[str, int, bool]] = {}

    # Look for .backlogrc in current directory first, then home directory
    config_paths = [
        Path.cwd() / ".backlogrc",
        Path.home() / ".backlogrc"
    ]

    config_file = None
    for path in config_paths:
        if path.exists():
            config_file = path
            break

    if config_file is None:
        return config

    try:
        parser = configparser.ConfigParser()
        parser.read(config_file)

        if 'backlog' in parser:
            section = parser['backlog']

            # Map config keys to command line argument names
            config_mappings = {
                'default_file': 'file',
                'default_color': 'color',
                'backup_dir': 'backup_dir',
                'max_backups': 'max_backups'
            }

            for config_key, arg_name in config_mappings.items():
                if config_key in section and section[config_key]:
                    value = section[config_key]

                    # Handle boolean values for color
                    if arg_name == 'color':
                        if value.lower() in ('true', '1', 'yes', 'on'):
                            config[arg_name] = True
                        elif value.lower() in ('false', '0', 'no', 'off'):
                            config[arg_name] = False
                        else:
                            # Keep as string for auto/default values
                            config[arg_name] = value
                    elif arg_name == 'max_backups':
                        # Convert to int for max_backups
                        try:
                            config[arg_name] = int(value)
                        except ValueError:
                            # Keep as string if not a valid int
                            config[arg_name] = value
                    else:
                        config[arg_name] = value

    except Exception:
        # If config file is malformed, just ignore it
        pass

    return config
