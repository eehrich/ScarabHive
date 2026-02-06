#!/usr/bin/env python3
"""
Migrate config documentation from plugin.yaml to schema.yaml.

This script:
1. Extracts config parameters from plugin code
2. Adds them to schema.yaml as a 'config' section
3. Removes 'config' from plugin.yaml (replaced with empty {})
"""

import sys
from pathlib import Path
import yaml
import re

# Add scripts directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from analyze_plugin_config import analyze_plugin


def extract_config_to_schema(plugin_path: Path, force: bool = False) -> bool:
    """
    Extract config from code and add to schema.yaml.

    Args:
        plugin_path: Path to plugin directory
        force: Overwrite existing config section

    Returns:
        True if extraction was successful
    """
    plugin_name = plugin_path.name
    schema_file = plugin_path / "schema.yaml"

    if not schema_file.exists():
        print(f"  [SKIP] {plugin_name}: No schema.yaml")
        return False

    try:
        # 1. Extract config from code
        discovered_config = analyze_plugin(plugin_path)

        # Convert flat keys with dots to nested structure
        structured_config = structure_config(discovered_config)

        if not structured_config:
            print(f"  [OK] {plugin_name}: No config parameters found")
            return True

        # 2. Load schema.yaml
        with open(schema_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # Check if config section already exists
        if re.search(r'^config:', content, re.MULTILINE):
            if not force:
                print(f"  [SKIP] {plugin_name}: config already exists (use --force to overwrite)")
                return False
            else:
                # Remove existing config section
                content = re.sub(r'\nconfig:.*?(?=\n[a-z_]+:|$)', '', content, flags=re.DOTALL)

        # Add config section to schema.yaml
        config_yaml = yaml.dump({'config': structured_config}, default_flow_style=False, allow_unicode=True, sort_keys=False)

        if '\ntools:' in content:
            # Insert before tools section
            content = content.replace('\ntools:', f'\n{config_yaml}\ntools:')
        else:
            # Append to end
            content += f'\n{config_yaml}'

        with open(schema_file, 'w', encoding='utf-8') as f:
            f.write(content)

        param_count = count_params(structured_config)
        print(f"  [OK] {plugin_name}: Added {param_count} config param(s) to schema.yaml")

        return True

    except Exception as e:
        print(f"  [ERROR] {plugin_name}: {e}")
        import traceback
        traceback.print_exc()
        return False


def remove_config_from_plugin(plugin_path: Path) -> bool:
    """
    Remove config section from plugin.yaml (set to empty {}).

    Args:
        plugin_path: Path to plugin directory

    Returns:
        True if removal was successful
    """
    plugin_name = plugin_path.name
    plugin_file = plugin_path / "plugin.yaml"

    if not plugin_file.exists():
        print(f"  [SKIP] {plugin_name}: No plugin.yaml")
        return False

    try:
        with open(plugin_file, 'r', encoding='utf-8') as f:
            plugin_data = yaml.safe_load(f)

        if 'config' not in plugin_data:
            print(f"  [SKIP] {plugin_name}: No config in plugin.yaml")
            return True

        if plugin_data['config'] == {}:
            print(f"  [SKIP] {plugin_name}: config already empty")
            return True

        # Set config to empty dict
        plugin_data['config'] = {}

        with open(plugin_file, 'w', encoding='utf-8') as f:
            yaml.dump(plugin_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        print(f"  [OK] {plugin_name}: Removed config from plugin.yaml")
        return True

    except Exception as e:
        print(f"  [ERROR] {plugin_name}: {e}")
        return False


def migrate_plugin(plugin_path: Path) -> bool:
    """
    Migrate config from plugin.yaml to schema.yaml for a single plugin.

    Args:
        plugin_path: Path to plugin directory

    Returns:
        True if migration was successful
    """
    plugin_name = plugin_path.name
    schema_file = plugin_path / "schema.yaml"
    plugin_file = plugin_path / "plugin.yaml"

    if not schema_file.exists():
        print(f"  [SKIP] {plugin_name}: No schema.yaml")
        return False

    if not plugin_file.exists():
        print(f"  [SKIP] {plugin_name}: No plugin.yaml")
        return False

    try:
        # 1. Extract config from code
        discovered_config = analyze_plugin(plugin_path)

        # Convert flat keys with dots to nested structure
        structured_config = structure_config(discovered_config)

        if not structured_config:
            print(f"  [OK] {plugin_name}: No config parameters found")
            # Still remove config from plugin.yaml if it exists
            with open(plugin_file, 'r', encoding='utf-8') as f:
                plugin_data = yaml.safe_load(f)

            if 'config' in plugin_data and plugin_data['config']:
                plugin_data['config'] = {}
                with open(plugin_file, 'w', encoding='utf-8') as f:
                    yaml.dump(plugin_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                print("       - Removed config from plugin.yaml")
            return True

        # 2. Load and update schema.yaml
        with open(schema_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # Check if config section already exists
        if re.search(r'^config:', content, re.MULTILINE):
            print(f"  [SKIP] {plugin_name}: config already exists in schema.yaml")
            return False

        # Add config section to schema.yaml
        # Insert before 'tools:' section if it exists, otherwise at the end
        config_yaml = yaml.dump({'config': structured_config}, default_flow_style=False, allow_unicode=True, sort_keys=False)

        if '\ntools:' in content:
            # Insert before tools section
            content = content.replace('\ntools:', f'\n{config_yaml}\ntools:')
        else:
            # Append to end
            content += f'\n{config_yaml}'

        with open(schema_file, 'w', encoding='utf-8') as f:
            f.write(content)

        param_count = count_params(structured_config)
        print(f"  [OK] {plugin_name}: Added {param_count} config param(s) to schema.yaml")

        # 3. Remove config from plugin.yaml
        with open(plugin_file, 'r', encoding='utf-8') as f:
            plugin_data = yaml.safe_load(f)

        if 'config' in plugin_data:
            plugin_data['config'] = {}
            with open(plugin_file, 'w', encoding='utf-8') as f:
                yaml.dump(plugin_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            print("       - Removed config from plugin.yaml")

        return True

    except Exception as e:
        print(f"  [ERROR] {plugin_name}: {e}")
        import traceback
        traceback.print_exc()
        return False


def structure_config(flat_config: dict) -> dict:
    """
    Convert flat config with dot-notation keys to nested structure.

    Example:
        {'security.audit_log': True, 'machines': []}
        -> {'security': {'audit_log': True}, 'machines': []}
    """
    structured = {}
    nested_keys = set()

    # First pass: identify which keys have nested versions
    for key in flat_config.keys():
        if '.' in key:
            parts = key.split('.')
            for i in range(1, len(parts) + 1):
                child_key = '.'.join(parts[i:])
                if child_key:
                    nested_keys.add(child_key)

    # Second pass: build structure
    for key, value in flat_config.items():
        if '.' in key:
            # Nested key like 'security.audit_log'
            parts = key.split('.')
            parent = parts[0]
            child = '.'.join(parts[1:])

            if parent not in structured:
                structured[parent] = {}

            # Recursively handle deeper nesting
            if '.' in child:
                current = structured[parent]
                child_parts = child.split('.')
                for part in child_parts[:-1]:
                    if part not in current:
                        current[part] = {}
                    current = current[part]
                current[child_parts[-1]] = value
            else:
                structured[parent][child] = value
        else:
            # Flat key - only add if there's no nested version of this key
            if key not in nested_keys:
                structured[key] = value

    return structured


def count_params(config: dict) -> int:
    """Count total parameters in config."""
    count = 0
    for value in config.values():
        if isinstance(value, dict):
            count += count_params(value)
        else:
            count += 1
    return count


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Migrate config from plugin.yaml to schema.yaml")
    parser.add_argument('--plugin', help='Specific plugin name to migrate')
    parser.add_argument('--all', action='store_true', help='Migrate all plugins')
    parser.add_argument('--extract-config', action='store_true',
                       help='Extract config from code and add to schema.yaml')
    parser.add_argument('--remove-config-from-plugin', action='store_true',
                       help='Remove config from plugin.yaml files (set to empty {})')
    parser.add_argument('--force', action='store_true',
                       help='Overwrite existing config in schema.yaml')
    args = parser.parse_args()

    if not args.plugin and not args.all:
        parser.print_help()
        return 1

    if not args.extract_config and not args.remove_config_from_plugin:
        print("ERROR: Specify at least one action: --extract-config or --remove-config-from-plugin")
        return 1

    # Find plugins to migrate
    plugin_dirs = []

    if args.plugin:
        # Single plugin
        for base_dir in [Path('src/plugins'), Path('src/plugins_writer')]:
            plugin_path = base_dir / args.plugin
            if plugin_path.exists():
                plugin_dirs.append(plugin_path)
                break
        else:
            print(f"Plugin '{args.plugin}' not found")
            return 1
    else:
        # All plugins
        for base_dir in [Path('src/plugins'), Path('src/plugins_writer')]:
            if base_dir.exists():
                for plugin_dir in sorted(base_dir.iterdir()):
                    if plugin_dir.is_dir():
                        plugin_dirs.append(plugin_dir)

    print(f"Processing {len(plugin_dirs)} plugin(s)...\n")

    success_count = 0
    for plugin_path in plugin_dirs:
        if args.extract_config:
            if extract_config_to_schema(plugin_path, force=args.force):
                success_count += 1

        if args.remove_config_from_plugin:
            if remove_config_from_plugin(plugin_path):
                if not args.extract_config:
                    success_count += 1

    print(f"\n{'='*70}")
    print(f"Processing complete: {success_count}/{len(plugin_dirs)} successful")

    return 0
if __name__ == '__main__':
    sys.exit(main())
