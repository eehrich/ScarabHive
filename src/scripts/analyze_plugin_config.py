#!/usr/bin/env python3
"""
Plugin Config Analyzer

Analyzes plugin source code to automatically extract config parameters.
Searches for patterns like:
- getattr(mcp_config, 'param', default)
- config.get('param', default)
- config['param']
- Reads from config/plugins.yaml for production values
"""

import ast
import re
from pathlib import Path
from typing import Any, Dict
import yaml


class ConfigExtractor:
    """Extracts config parameters from Python code via AST analysis."""

    def __init__(self, plugin_path: Path):
        self.plugin_path = plugin_path
        self.config_params: Dict[str, Any] = {}

    def extract(self) -> Dict[str, Any]:
        """Extract all config parameters from plugin code."""
        # 1. Analyze Python files first - prioritize core files, skip web UI
        priority_files = ['server.py', 'mcp_server.py', 'plugin.py', '__init__.py', 'connection_manager.py']
        other_files = []

        for py_file in self.plugin_path.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue

            # Skip web UI and test files for config
            if 'web_endpoint' in py_file.name or 'test' in py_file.name:
                continue

            # Prioritize core files
            if py_file.name in priority_files:
                self._analyze_python_file(py_file)
            else:
                other_files.append(py_file)

        # Then analyze other files
        for py_file in other_files:
            self._analyze_python_file(py_file)

        # 2. Check schema.yaml for config definitions
        self._analyze_schema_yaml()

        # 3. Check config/plugins.yaml for production values (after code analysis)
        # This way we can avoid duplicates - only add production values that
        # don't conflict with nested structures from code
        self._analyze_production_config()

        return self.config_params

    def _analyze_python_file(self, file_path: Path):
        """Analyze a Python file using AST and regex."""
        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read()

            # Try AST parsing first (most reliable)
            try:
                tree = ast.parse(content)
                self._extract_from_ast(tree)
            except SyntaxError:
                pass  # Fall back to regex

            # Regex patterns as fallback/supplement
            self._extract_from_regex(content)

        except Exception as e:
            print(f"  Warning: Could not analyze {file_path.name}: {e}")

    def _extract_from_ast(self, tree: ast.AST):
        """Extract config params using AST."""
        for node in ast.walk(tree):
            # Pattern: getattr(mcp_config, 'key', default)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == 'getattr':
                    if len(node.args) >= 2:
                        # Check if first arg is mcp_config or similar
                        if isinstance(node.args[0], ast.Name):
                            obj_name = node.args[0].id
                            if 'config' in obj_name.lower():
                                # Second arg is the key
                                if isinstance(node.args[1], ast.Constant):
                                    key = node.args[1].value
                                    default = self._extract_default(node.args[2]) if len(node.args) > 2 else None
                                    # Skip empty dicts
                                    if default != {}:
                                        self._add_param(key, default)

            # Pattern: config.get('key', default)
            elif isinstance(node, ast.Attribute):
                if node.attr == 'get' and isinstance(node.value, ast.Name):
                    if 'config' in node.value.id.lower():
                        # This is tricky, we'd need parent info - regex is better here
                        pass

    def _extract_from_regex(self, content: str):
        """Extract config params using regex patterns."""
        patterns = [
            # getattr(mcp_config, 'key', default)
            r'getattr\(\s*(?:mcp_config|config|self\.config)\s*,\s*["\'](\w+)["\']\s*,\s*(.+?)\)',
            # config.get('key', default) - also matches security_config, defaults, etc.
            r'(?:\w+_config|config|defaults|self\.config)\.get\(\s*["\'](\w+)["\']\s*,\s*(.+?)\)',
            # config['key'] (no default)
            r'(?:mcp_config|config|self\.config)\[["\'](\w+)["\']\]',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, content):
                key = match.group(1)
                default = match.group(2).strip() if match.lastindex > 1 else None

                if default:
                    # Clean up default value
                    default = default.rstrip(')')
                    # Try to evaluate simple literals
                    default = self._parse_default_value(default)

                    # Skip empty dict placeholders - these are nested section containers
                    if default == {} or default == '{}':
                        continue

                    self._add_param(key, default)
                else:
                    # No default value
                    self._add_param(key, default)

        # Extract nested config structures like: security_config = config.get('security', {})
        # Then look for: security_config.get('audit_log', True)
        nested_pattern = r'(\w+_config)\s*=\s*config\.get\(\s*["\'](\w+)["\']\s*,\s*\{\}\s*\)'
        nested_sections = {}
        for match in re.finditer(nested_pattern, content):
            var_name = match.group(1)  # e.g., 'security_config'
            section_name = match.group(2)  # e.g., 'security'
            nested_sections[var_name] = section_name

        # Now find nested keys: security_config.get('audit_log', True)
        for var_name, section_name in nested_sections.items():
            nested_get_pattern = rf'{re.escape(var_name)}\.get\(\s*["\'](\w+)["\']\s*(?:,\s*(.+?))?\s*\)'
            for match in re.finditer(nested_get_pattern, content):
                child_key = match.group(1)
                default = match.group(2)
                if default:
                    default = default.strip().rstrip(')')
                    default = self._parse_default_value(default)

                # Store as nested structure
                full_key = f"{section_name}.{child_key}"
                self._add_param(full_key, default)

    def _extract_default(self, node: ast.AST) -> Any:
        """Extract default value from AST node."""
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.List):
            return [self._extract_default(elt) for elt in node.elts]
        elif isinstance(node, ast.Dict):
            return {
                self._extract_default(k): self._extract_default(v)
                for k, v in zip(node.keys, node.values)
            }
        elif isinstance(node, ast.Name):
            if node.id == 'True':
                return True
            elif node.id == 'False':
                return False
            elif node.id == 'None':
                return None
        return None

    def _parse_default_value(self, value_str: str) -> Any:
        """Parse default value from string."""
        value_str = value_str.strip()

        # Booleans
        if value_str == 'True':
            return True
        elif value_str == 'False':
            return False
        elif value_str == 'None':
            return None

        # Numbers
        if value_str.isdigit():
            return int(value_str)

        try:
            return float(value_str)
        except ValueError:
            pass

        # Strings (remove quotes)
        if (value_str.startswith('"') and value_str.endswith('"')) or \
           (value_str.startswith("'") and value_str.endswith("'")):
            return value_str[1:-1]

        # Lists (simple)
        if value_str.startswith('[') and value_str.endswith(']'):
            try:
                return eval(value_str)
            except Exception:
                pass

        # Keep as string if can't parse
        return value_str

    def _add_param(self, key: str, default: Any):
        """Add parameter to config dict."""
        if key and key not in self.config_params:
            self.config_params[key] = default

    def _analyze_schema_yaml(self):
        """Extract config from schema.yaml if it exists."""
        schema_file = self.plugin_path / "schema.yaml"
        if not schema_file.exists():
            return

        try:
            with open(schema_file, encoding="utf-8") as f:
                content = f.read()
                # Remove Jinja2 templates for parsing
                content = re.sub(r'\{\{.*?\}\}', '""', content)
                content = re.sub(r'\{%.*?%\}', '', content, flags=re.DOTALL)
                schema = yaml.safe_load(content)

            if not schema:
                return

            # Check for config section
            config_schema = schema.get('config', {})
            for key, spec in config_schema.items():
                if isinstance(spec, dict):
                    default = spec.get('default')
                    if key not in self.config_params and default is not None:
                        self.config_params[key] = default

        except Exception as e:
            print(f"  Warning: Could not parse schema.yaml: {e}")

    def _analyze_production_config(self):
        """
        Check config/plugins.yaml for production config values.

        Only add values that don't conflict with nested structures from code.
        E.g., if code has 'security.audit_log', don't add flat 'security: {}'.
        """
        plugin_name = self.plugin_path.name
        config_file = Path("config/plugins.yaml")

        if not config_file.exists():
            return

        try:
            with open(config_file, encoding="utf-8") as f:
                config = yaml.safe_load(f)

            servers = config.get('plugins', {}).get('servers', {})
            plugin_config = servers.get(plugin_name, {})

            # Check which top-level keys already have nested versions from code
            nested_parents = set()
            for key in self.config_params.keys():
                if '.' in key:
                    parent = key.split('.')[0]
                    nested_parents.add(parent)

            # Merge production values
            for key, value in plugin_config.items():
                # Skip special keys
                if key in ['type', 'enabled', 'agent_config', 'metadata']:
                    continue

                # If this key already has nested versions from code, flatten the production value
                if key in nested_parents and isinstance(value, dict):
                    self._flatten_dict(value, prefix=key)
                # If value is a nested dict (but not special lists), flatten it
                elif isinstance(value, dict) and key not in ['machines', 'defaults', 'config']:
                    self._flatten_dict(value, prefix=key)
                else:
                    # Add simple value only if not already covered by nested keys
                    if key not in nested_parents:
                        self.config_params[key] = value

        except Exception as e:
            print(f"  Warning: Could not read config/plugins.yaml: {e}")

    def _flatten_dict(self, d: dict, prefix: str = ''):
        """Recursively flatten nested dict with dot notation."""
        for key, value in d.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                self._flatten_dict(value, prefix=full_key)
            else:
                self.config_params[full_key] = value


def analyze_plugin(plugin_path: Path | str) -> Dict[str, Any]:
    """Analyze a plugin and return its config parameters."""
    if isinstance(plugin_path, str):
        plugin_path = Path(plugin_path)
    extractor = ConfigExtractor(plugin_path)
    return extractor.extract()


def main():
    """Main entry point for testing."""
    import argparse

    parser = argparse.ArgumentParser(description="Analyze plugin config parameters")
    parser.add_argument("plugin_path", help="Path to plugin directory")
    args = parser.parse_args()

    plugin_path = Path(args.plugin_path)
    if not plugin_path.exists():
        print(f"Error: Plugin path not found: {plugin_path}")
        return 1

    print(f"Analyzing plugin: {plugin_path.name}")
    print("=" * 70)

    config = analyze_plugin(plugin_path)

    if config:
        print(f"\nFound {len(config)} config parameter(s):")
        print()
        for key, value in sorted(config.items()):
            print(f"  {key}: {value!r}".encode('utf-8', errors='replace').decode('utf-8'))
    else:
        print("\nNo config parameters found (plugin needs no configuration)")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
