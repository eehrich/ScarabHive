#!/usr/bin/env python3
"""
Plugin Validation Script

Validates plugin conformity by checking:
- plugin.yaml structure and required fields
- schema.yaml structure and tool definitions
- File structure and required files
- Schema compliance with JSON schemas
- Cross-references between files
- Hook configuration (if applicable)
- Template variable usage

Usage:
    python src/scripts/validate_plugin.py <plugin_path>
    python src/scripts/validate_plugin.py --all
    python src/scripts/validate_plugin.py --plugin basic_operations
    python src/scripts/validate_plugin.py --all --fix
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import ValidationError, validate as json_validate

# Import config analyzer (only used with --extract-config)
sys.path.insert(0, str(Path(__file__).parent))
try:
    from analyze_plugin_config import analyze_plugin
except ImportError:
    analyze_plugin = None

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)


class PluginValidator:
    """Validates plugin conformity against system requirements."""

    def __init__(self, plugin_path: Path, schemas_dir: Path):
        self.plugin_path = plugin_path
        self.schemas_dir = schemas_dir
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.plugin_yaml: dict[str, Any] | None = None
        self.schema_yaml: dict[str, Any] | None = None

    def validate(self) -> bool:
        """
        Run all validation checks.

        Returns:
            True if all validations pass, False otherwise
        """
        logger.info(f"Validating plugin: {self.plugin_path.name}")

        # Check basic file structure
        if not self._check_file_structure():
            return False

        # Load configuration files
        if not self._load_configs():
            return False

        # Validate plugin.yaml against JSON schema
        self._validate_plugin_yaml_schema()

        # Validate schema.yaml structure
        self._validate_schema_yaml_structure()

        # Cross-validate configurations
        self._cross_validate_configs()

        # Validate template variables
        self._validate_template_variables()

        # Validate hook configurations (if present)
        self._validate_hooks()

        # Validate entrypoint
        self._validate_entrypoint()

        # Report results
        return self._report_results()

    def _check_file_structure(self) -> bool:
        """Check that required files exist."""
        required_files = ["plugin.yaml"]

        for filename in required_files:
            file_path = self.plugin_path / filename
            if not file_path.exists():
                self.errors.append(f"Missing required file: {filename}")
                return False

        # Check for either schema.yaml or plugin.py (entrypoint)
        has_schema = (self.plugin_path / "schema.yaml").exists()
        has_plugin_py = (self.plugin_path / "plugin.py").exists()
        has_server_py = (self.plugin_path / "server.py").exists()

        if not has_schema:
            self.warnings.append(
                "No schema.yaml found - plugin may be config-only or have programmatic tools"
            )

        if not has_plugin_py and not has_server_py:
            self.errors.append(
                "Missing plugin.py or server.py - no entrypoint module found"
            )
            return False

        return True

    def _load_configs(self) -> bool:
        """Load and parse YAML configuration files."""
        try:
            # Load plugin.yaml
            plugin_yaml_path = self.plugin_path / "plugin.yaml"
            with open(plugin_yaml_path, "r", encoding="utf-8") as f:
                self.plugin_yaml = yaml.safe_load(f)

            # Load schema.yaml (optional) - handle Jinja2 templates
            schema_yaml_path = self.plugin_path / "schema.yaml"
            if schema_yaml_path.exists():
                with open(schema_yaml_path, "r", encoding="utf-8") as f:
                    schema_content = f.read()

                # Replace template variables with placeholder values for validation
                import re
                # Replace {{ name }} with placeholder
                schema_content = re.sub(r'\{\{\s*name\s*\}\}', 'plugin_name', schema_content)
                # Remove Jinja2 control structures {% ... %}
                schema_content = re.sub(r'\{%.*?%\}', '', schema_content, flags=re.DOTALL)
                # Replace other {{ var | filter }} or {{ var }} with simple placeholder (no quotes)
                schema_content = re.sub(r'\{\{[^}]+\}\}', 'TEMPLATE_VALUE', schema_content)

                try:
                    self.schema_yaml = yaml.safe_load(schema_content)
                except yaml.YAMLError as e:
                    self.errors.append(f"YAML parsing error in schema.yaml: {e}")
                    return False

            return True

        except yaml.YAMLError as e:
            self.errors.append(f"YAML parsing error: {e}")
            return False
        except Exception as e:
            self.errors.append(f"Error loading configs: {e}")
            return False

    def _validate_plugin_yaml_schema(self) -> None:
        """Validate plugin.yaml against JSON schema."""
        schema_path = self.schemas_dir / "plugin-config.schema.json"

        if not schema_path.exists():
            self.warnings.append(
                f"JSON schema not found: {schema_path} - skipping schema validation"
            )
            return

        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)

            # Remove enum restriction from category field if present
            if "properties" in schema and "category" in schema["properties"]:
                if "enum" in schema["properties"]["category"]:
                    del schema["properties"]["category"]["enum"]
                # Ensure it's just a string type
                schema["properties"]["category"]["type"] = "string"

            json_validate(instance=self.plugin_yaml, schema=schema)
            logger.debug("plugin.yaml passed JSON schema validation")

        except ValidationError as e:
            self.errors.append(
                f"plugin.yaml schema validation failed: {e.message} at {list(e.path)}"
            )
        except Exception as e:
            self.errors.append(f"Error validating plugin.yaml schema: {e}")

    def _validate_schema_yaml_structure(self) -> None:
        """Validate schema.yaml structure and required fields."""
        if not self.schema_yaml:
            return  # schema.yaml is optional

        # Check for tools section (if MCP plugin)
        plugin_type = self.plugin_yaml.get("type", "mcp_only")

        if "mcp" in plugin_type or plugin_type == "hybrid":
            if "tools" not in self.schema_yaml:
                self.errors.append(
                    f"MCP plugin type '{plugin_type}' requires 'tools' section in schema.yaml"
                )
            else:
                self._validate_tools_section()

        # Check for hooks section (if hooks plugin)
        if "hooks" in plugin_type or plugin_type == "hooks_only":
            if "hooks" not in self.schema_yaml:
                self.errors.append(
                    f"Hooks plugin type '{plugin_type}' requires 'hooks' section in schema.yaml"
                )
            else:
                self._validate_hooks_section()

        # Check for web_ui section (if web or hybrid plugin)
        if plugin_type in ["web_only", "hybrid", "web_with_hooks", "hybrid_with_hooks"]:
            if "web_ui" in self.schema_yaml:
                self._validate_web_ui_section()
            else:
                self.warnings.append(
                    f"Web plugin type '{plugin_type}' should have 'web_ui' section in schema.yaml"
                )

    def _validate_tools_section(self) -> None:
        """Validate tools section in schema.yaml."""
        tools = self.schema_yaml.get("tools", [])

        if not isinstance(tools, list):
            self.errors.append("'tools' must be a list")
            return

        if len(tools) == 0:
            self.warnings.append("'tools' list is empty - plugin provides no tools")

        seen_tool_names: set[str] = set()

        for idx, tool in enumerate(tools):
            if not isinstance(tool, dict):
                self.errors.append(f"Tool at index {idx} must be an object")
                continue

            # Check required fields
            if "type" not in tool:
                self.errors.append(f"Tool at index {idx} missing 'type' field")
                continue

            if tool["type"] != "function":
                self.errors.append(
                    f"Tool at index {idx} has invalid type: {tool['type']} (expected 'function')"
                )
                continue

            if "function" not in tool:
                self.errors.append(f"Tool at index {idx} missing 'function' field")
                continue

            function = tool["function"]

            # Validate function object
            if "name" not in function:
                self.errors.append(f"Tool at index {idx} missing 'name' in function")
                continue

            tool_name = function["name"]

            # Check for duplicate tool names
            if tool_name in seen_tool_names and "{{" not in tool_name:
                self.errors.append(f"Duplicate tool name: {tool_name}")
            seen_tool_names.add(tool_name)

            if "description" not in function:
                self.warnings.append(
                    f"Tool '{tool_name}' missing description - highly recommended"
                )

            if "parameters" not in function:
                self.warnings.append(
                    f"Tool '{tool_name}' missing parameters - tools should define parameters"
                )
            else:
                self._validate_tool_parameters(tool_name, function["parameters"])

    def _validate_tool_parameters(self, tool_name: str, parameters: dict[str, Any]) -> None:
        """Validate tool parameter schema."""
        if not isinstance(parameters, dict):
            self.errors.append(f"Tool '{tool_name}' parameters must be an object")
            return

        if parameters.get("type") != "object":
            self.warnings.append(
                f"Tool '{tool_name}' parameters.type should be 'object'"
            )

        if "properties" not in parameters:
            self.warnings.append(
                f"Tool '{tool_name}' has no properties defined - may be intentional"
            )

        # Check required fields
        required = parameters.get("required", [])
        properties = parameters.get("properties", {})

        for req_field in required:
            if req_field not in properties:
                self.errors.append(
                    f"Tool '{tool_name}' requires field '{req_field}' not in properties"
                )

        # Check property definitions
        for prop_name, prop_def in properties.items():
            if not isinstance(prop_def, dict):
                self.errors.append(
                    f"Tool '{tool_name}' property '{prop_name}' must be an object"
                )
                continue

            if "type" not in prop_def:
                self.warnings.append(
                    f"Tool '{tool_name}' property '{prop_name}' missing type"
                )

            if "description" not in prop_def:
                self.warnings.append(
                    f"Tool '{tool_name}' property '{prop_name}' missing description"
                )

        # Check for additionalProperties
        if "additionalProperties" not in parameters:
            self.warnings.append(
                f"Tool '{tool_name}' should explicitly set 'additionalProperties' (recommend false)"
            )

    def _validate_hooks_section(self) -> None:
        """Validate hooks section in schema.yaml."""
        hooks = self.schema_yaml.get("hooks", [])

        if not isinstance(hooks, list):
            self.errors.append("'hooks' must be a list")
            return

        if len(hooks) == 0:
            self.warnings.append("'hooks' list is empty")

        seen_hook_names: set[str] = set()
        valid_hook_types = [
            "pre_llm_call",
            "post_llm_call",
            "pre_tool_call",
            "post_tool_call",
            "format_output",
            "session_start",
            "session_end"
        ]

        for idx, hook in enumerate(hooks):
            if not isinstance(hook, dict):
                self.errors.append(f"Hook at index {idx} must be an object")
                continue

            # Check required fields
            if "name" not in hook:
                self.errors.append(f"Hook at index {idx} missing 'name' field")
                continue

            hook_name = hook["name"]

            if hook_name in seen_hook_names:
                self.errors.append(f"Duplicate hook name: {hook_name}")
            seen_hook_names.add(hook_name)

            if "type" not in hook:
                self.errors.append(f"Hook '{hook_name}' missing 'type' field")
                continue

            hook_type = hook["type"]
            if hook_type not in valid_hook_types:
                self.errors.append(
                    f"Hook '{hook_name}' has invalid type: {hook_type}. "
                    f"Valid types: {', '.join(valid_hook_types)}"
                )

            if "enabled" not in hook:
                self.warnings.append(
                    f"Hook '{hook_name}' missing 'enabled' field - defaults to true"
                )

            if "description" not in hook:
                self.warnings.append(
                    f"Hook '{hook_name}' missing description - highly recommended"
                )

            # Validate order if present
            if "order" in hook:
                self._validate_hook_order(hook_name, hook["order"])

    def _validate_hook_order(self, hook_name: str, order: dict[str, Any]) -> None:
        """Validate hook ordering specification."""
        if not isinstance(order, dict):
            self.errors.append(f"Hook '{hook_name}' order must be an object")
            return

        for key in order:
            if key not in ["before", "after"]:
                self.warnings.append(
                    f"Hook '{hook_name}' order has unknown key: {key}"
                )

        for order_type in ["before", "after"]:
            if order_type in order:
                order_list = order[order_type]
                if not isinstance(order_list, list):
                    self.errors.append(
                        f"Hook '{hook_name}' order.{order_type} must be a list"
                    )
                    continue

                for item in order_list:
                    if not isinstance(item, str):
                        self.errors.append(
                            f"Hook '{hook_name}' order.{order_type} items must be strings"
                        )

    def _validate_web_ui_section(self) -> None:
        """Validate web_ui section in schema.yaml."""
        web_ui = self.schema_yaml.get("web_ui", {})

        if not isinstance(web_ui, dict):
            self.errors.append("'web_ui' must be an object")
            return

        # Check recommended fields
        recommended_fields = [
            "enabled",
            "button_text",
            "button_icon",
            "panel_title",
            "panel_endpoint",
            "panel_type"
        ]

        for field in recommended_fields:
            if field not in web_ui:
                self.warnings.append(
                    f"web_ui missing recommended field: {field}"
                )

        # Validate panel_type if present
        if "panel_type" in web_ui:
            panel_type = web_ui["panel_type"]
            if panel_type not in ["fetch", "iframe"]:
                self.errors.append(
                    f"web_ui.panel_type must be 'fetch' or 'iframe', got: {panel_type}"
                )

        # Validate panels section if present
        if "panels" in web_ui:
            panels = web_ui["panels"]
            if not isinstance(panels, list):
                self.errors.append("web_ui.panels must be a list")
            else:
                self._validate_panels(panels)

    def _validate_panels(self, panels: list[dict[str, Any]]) -> None:
        """Validate panel definitions."""
        for idx, panel in enumerate(panels):
            if not isinstance(panel, dict):
                self.errors.append(f"Panel at index {idx} must be an object")
                continue

            required_fields = ["id", "title", "url"]
            for field in required_fields:
                if field not in panel:
                    self.errors.append(
                        f"Panel at index {idx} missing required field: {field}"
                    )

    def _cross_validate_configs(self) -> None:
        """Cross-validate plugin.yaml and schema.yaml."""
        if not self.plugin_yaml or not self.schema_yaml:
            return

        plugin_type = self.plugin_yaml.get("type", "mcp_only")

        # Check consistency between plugin type and schema content
        has_tools = "tools" in self.schema_yaml
        has_hooks = "hooks" in self.schema_yaml

        if "mcp" in plugin_type and not has_tools:
            self.warnings.append(
                f"Plugin type '{plugin_type}' suggests MCP tools but schema.yaml has no 'tools' section"
            )

        if plugin_type == "hooks_only" and has_tools:
            self.warnings.append(
                "Plugin type 'hooks_only' but schema.yaml defines tools"
            )

        if "hooks" in plugin_type and not has_hooks:
            self.warnings.append(
                f"Plugin type '{plugin_type}' suggests hooks but schema.yaml has no 'hooks' section"
            )

    def _validate_template_variables(self) -> None:
        """Validate template variable usage in schema.yaml."""
        if not self.schema_yaml:
            return

        import re

        schema_str = yaml.dump(self.schema_yaml)

        # Find all template variables {{ ... }}
        template_vars = re.findall(r'\{\{\s*(\w+)\s*\}\}', schema_str)

        valid_vars = {"name"}  # Standard template vars

        for var in template_vars:
            if var not in valid_vars:
                self.warnings.append(
                    f"Schema uses custom template variable: '{var}' - "
                    f"ensure server provides this via get_template_vars()"
                )

    def _validate_hooks(self) -> None:
        """Validate hook configuration consistency."""
        if not self.plugin_yaml:
            return

        # Check if plugin.yaml declares hooks
        plugin_hooks = self.plugin_yaml.get("hooks", [])

        if plugin_hooks:
            self.warnings.append(
                "plugin.yaml contains 'hooks' section - hooks should be defined in schema.yaml"
            )

    def _validate_entrypoint(self) -> None:
        """Validate entrypoint module and factory."""
        if not self.plugin_yaml:
            return

        entrypoint = self.plugin_yaml.get("entrypoint")
        if not entrypoint:
            self.errors.append("Missing 'entrypoint' in plugin.yaml")
            return

        # Parse entrypoint format: "module:FACTORY"
        if ":" not in entrypoint:
            self.errors.append(
                f"Invalid entrypoint format: '{entrypoint}' (expected 'module:FACTORY')"
            )
            return

        module_name, factory_name = entrypoint.split(":", 1)

        # Check if module file exists
        module_file = self.plugin_path / f"{module_name}.py"
        if not module_file.exists():
            self.errors.append(
                f"Entrypoint module not found: {module_name}.py"
            )

        # Check common mistakes
        if module_name == "server" and not (self.plugin_path / "plugin.py").exists():
            self.warnings.append(
                "Entrypoint uses 'server:' but no plugin.py found. "
                "Ensure PLUGIN_FACTORY is exported from server.py or create plugin.py"
            )

        # Validate factory name convention
        if factory_name != "PLUGIN_FACTORY" and not factory_name.endswith("Server"):
            self.warnings.append(
                f"Factory name '{factory_name}' doesn't follow conventions "
                "(PLUGIN_FACTORY or *Server)"
            )

    def _report_results(self) -> bool:
        """Report validation results and return success status."""
        print(f"\n{'='*70}")
        print(f"Validation Results: {self.plugin_path.name}")
        print(f"{'='*70}\n")

        if self.errors:
            print(f"[X] ERRORS ({len(self.errors)}):")
            for error in self.errors:
                print(f"   * {error}")
            print()

        if self.warnings:
            print(f"[!] WARNINGS ({len(self.warnings)}):")
            for warning in self.warnings:
                print(f"   * {warning}")
            print()

        if not self.errors and not self.warnings:
            print("[OK] All checks passed!")
            print()
            return True

        if not self.errors:
            print("[OK] No errors found (warnings only)")
            print()
            return True

        print("[X] Validation failed with errors")
        print()
        return False

    def apply_fixes(self) -> bool:
        """
        Apply automatic fixes to plugin.yaml based on code analysis.

        Returns:
            True if fixes were applied successfully
        """
        if not self.auto_fix:
            return False

        plugin_name = self.plugin_path.name
        plugin_yaml_path = self.plugin_path / "plugin.yaml"

        try:
            # Analyze plugin code to extract config parameters
            logger.debug(f"Analyzing {plugin_name} for config parameters...")
            discovered_config = analyze_plugin(self.plugin_path)

            if not discovered_config:
                logger.debug(f"No config parameters found for {plugin_name}")
                return False

            # Convert flat keys with dots to nested structure
            structured_config = self._structure_config(discovered_config)

            # Reload current plugin.yaml
            with open(plugin_yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)

            # Check if config needs updating
            current_config = data.get("config", {})

            # Only update if config is empty or missing parameters
            needs_update = False
            if not current_config or current_config == {}:
                needs_update = True
            else:
                # Check if any discovered params are missing
                for key in discovered_config.keys():
                    if key not in current_config:
                        needs_update = True
                        break

            if needs_update:
                data["config"] = structured_config

                # Write back
                with open(plugin_yaml_path, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

                param_count = self._count_params(structured_config)
                self.fixes_applied.append(
                    f"Updated config with {param_count} parameter(s) from code analysis"
                )
                logger.info(f"Applied config fix for {plugin_name}")
                return True

        except Exception as e:
            logger.error(f"Failed to apply fixes: {e}")
            return False

        return False

    def _structure_config(self, flat_config: dict) -> dict:
        """
        Convert flat config with dot-notation keys to nested structure.

        Example:
            {'security.audit_log': True, 'machines': []}
            -> {'security': {'audit_log': True}, 'machines': []}

        If both 'security.audit_log' and 'audit_log' exist, prefer the nested version.
        """
        structured = {}
        nested_keys = set()  # Track which keys have nested versions (e.g., 'audit_log' has 'security.audit_log')

        # First pass: identify which keys have nested versions
        for key in flat_config.keys():
            if '.' in key:
                # e.g., 'security.audit_log' -> add 'audit_log' to nested_keys
                parts = key.split('.')
                for i in range(1, len(parts) + 1):
                    child_key = '.'.join(parts[i:])
                    if child_key:  # Don't add empty string
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
                    # e.g., 'a.b.c' -> structured['a']['b']['c']
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

    def _count_params(self, config: dict) -> int:
        """Count total parameters in config."""
        count = 0
        for value in config.values():
            if isinstance(value, dict):
                count += self._count_params(value)
            else:
                count += 1
        return count


def find_plugin_directories(base_dirs: list[Path]) -> list[Path]:
    """Find all plugin directories in base directories."""
    plugin_dirs = []

    for base_dir in base_dirs:
        if not base_dir.exists():
            logger.warning(f"Plugin directory not found: {base_dir}")
            continue

        for item in base_dir.iterdir():
            if item.is_dir() and (item / "plugin.yaml").exists():
                plugin_dirs.append(item)

    return plugin_dirs


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Validate plugin conformity with schema requirements"
    )
    parser.add_argument(
        "plugin_path",
        nargs="?",
        help="Path to plugin directory (e.g., src/plugins/basic_operations)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Validate all plugins in src/plugins/ and src/plugins_writer/"
    )
    parser.add_argument(
        "--plugin",
        "-p",
        help="Plugin name to validate (e.g., basic_operations)"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output"
    )
    parser.add_argument(
        "--extract-config",
        action="store_true",
        help="Analyze plugin code and display found config parameters (does not modify files)"
    )
    parser.add_argument(
        "--merge-config",
        action="store_true",
        help="Merge extracted config parameters into schema.yaml (adds missing keys only, preserves existing)"
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Determine project root
    script_path = Path(__file__).resolve()
    project_root = script_path.parent.parent.parent
    schemas_dir = project_root / "schemas"

    if not schemas_dir.exists():
        logger.error(f"Schemas directory not found: {schemas_dir}")
        sys.exit(1)

    # Determine which plugins to validate
    plugins_to_validate: list[Path] = []

    if args.all:
        # Validate all plugins
        base_dirs = [
            project_root / "src" / "plugins",
            project_root / "src" / "plugins_writer"
        ]
        plugins_to_validate = find_plugin_directories(base_dirs)

    elif args.plugin:
        # Validate specific plugin by name
        plugin_name = args.plugin
        possible_locations = [
            project_root / "src" / "plugins" / plugin_name,
            project_root / "src" / "plugins_writer" / plugin_name
        ]

        for location in possible_locations:
            if location.exists():
                plugins_to_validate.append(location)
                break
        else:
            logger.error(f"Plugin not found: {plugin_name}")
            sys.exit(1)

    elif args.plugin_path:
        # Validate specific path
        plugin_path = Path(args.plugin_path)
        if not plugin_path.exists():
            logger.error(f"Path not found: {plugin_path}")
            sys.exit(1)
        plugins_to_validate.append(plugin_path)

    else:
        parser.print_help()
        sys.exit(1)

    # Handle --extract-config option (display only)
    if args.extract_config:
        if analyze_plugin is None:
            logger.error("Config extraction requires analyze_plugin_config.py module")
            sys.exit(1)

        logger.info("Analyzing plugin code for config parameters (display only)...\n")

        for plugin_path in sorted(plugins_to_validate):
            plugin_name = plugin_path.name

            try:
                # Analyze plugin code
                discovered_config = analyze_plugin(plugin_path)

                if not discovered_config:
                    print(f"  {plugin_name}: No config parameters found")
                    continue

                print(f"  {plugin_name}: Found {len(discovered_config)} config parameter(s):")
                for key, value in discovered_config.items():
                    print(f"    {key}: {value}")
                print()

            except Exception as e:
                logger.error(f"  {plugin_name}: Failed to analyze - {e}")

        logger.info("Use --merge-config to add missing parameters to schema.yaml")
        sys.exit(0)

    # Handle --merge-config option (write to schema.yaml)
    if args.merge_config:
        if analyze_plugin is None:
            logger.error("Config merging requires analyze_plugin_config.py module")
            sys.exit(1)

        logger.info("Merging config parameters into schema.yaml...\n")

        for plugin_path in sorted(plugins_to_validate):
            plugin_name = plugin_path.name
            schema_yaml_path = plugin_path / "schema.yaml"

            try:
                # Analyze plugin code
                discovered_config = analyze_plugin(plugin_path)

                if not discovered_config:
                    logger.info(f"  {plugin_name}: No config parameters found")
                    continue

                # Convert flat keys to nested structure
                def structure_config(flat_config):
                    structured = {}
                    for key, value in flat_config.items():
                        if "." in key:
                            parts = key.split(".")
                            current = structured
                            for part in parts[:-1]:
                                if part not in current:
                                    current[part] = {}
                                current = current[part]
                            current[parts[-1]] = value
                        else:
                            structured[key] = value
                    return structured

                structured_config = structure_config(discovered_config)

                # Load existing config from schema.yaml to check what's missing
                existing_config = {}
                if schema_yaml_path.exists():
                    try:
                        with open(schema_yaml_path, "r", encoding="utf-8") as f:
                            import re
                            content = f.read()
                            # Try to parse despite Jinja2 templates
                            temp_content = re.sub(r'\{\{.*?\}\}', 'TEMPLATE', content)
                            temp_content = re.sub(r'\{%.*?%\}', '', temp_content, flags=re.DOTALL)
                            existing_data = yaml.safe_load(temp_content) or {}
                            existing_config = existing_data.get('config', {})
                    except Exception:
                        pass  # If parsing fails, treat as no existing config

                # Flatten existing config for comparison
                def flatten_dict(d, parent_key=''):
                    items = []
                    for k, v in d.items():
                        new_key = f"{parent_key}.{k}" if parent_key else k
                        if isinstance(v, dict):
                            items.extend(flatten_dict(v, new_key).items())
                        else:
                            items.append((new_key, v))
                    return dict(items)

                flat_existing = flatten_dict(existing_config)

                # discovered_config is already flat with dots
                # Find missing keys
                missing_keys = {k: v for k, v in discovered_config.items() if k not in flat_existing}

                if not missing_keys:
                    logger.info(f"  {plugin_name}: All config parameters already present")
                    continue

                # Structure missing keys
                missing_structured = structure_config(missing_keys)

                # Append missing keys to config section
                if schema_yaml_path.exists():
                    with open(schema_yaml_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()

                    # Check if config section exists
                    has_config = any(line.strip().startswith('config:') for line in lines)

                    if has_config:
                        # Config exists - skip to avoid duplicates (user should merge manually)
                        logger.info(f"  {plugin_name}: Config section exists, skipping (use --extract-config to see missing params)")
                        continue
                    else:
                        # No config section exists, append at end
                        config_lines = ["\nconfig:\n"]
                        def add_config_lines(cfg, indent=1):
                            for key, value in cfg.items():
                                if isinstance(value, dict):
                                    config_lines.append(f"{'  ' * indent}{key}:\n")
                                    add_config_lines(value, indent + 1)
                                elif isinstance(value, list):
                                    # Format lists as readable multi-line
                                    config_lines.append(f"{'  ' * indent}{key}:\n")
                                    for item in value:
                                        if isinstance(item, str):
                                            # Quote strings only if they contain YAML special characters
                                            needs_quote = any(c in item for c in ['*', '&', '!', '|', '>', '@', '`', ':', '#', '[', ']', '{', '}'])
                                            if needs_quote or (item and item[0] == '-'):
                                                config_lines.append(f"{'  ' * (indent + 1)}- '{item}'\n")
                                            else:
                                                config_lines.append(f"{'  ' * (indent + 1)}- {item}\n")
                                        else:
                                            config_lines.append(f"{'  ' * (indent + 1)}- {item}\n")
                                else:
                                    if isinstance(value, bool):
                                        val_str = str(value).lower()
                                    elif isinstance(value, str):
                                        val_str = value
                                    elif value is None:
                                        val_str = 'null'
                                    else:
                                        val_str = str(value)
                                    config_lines.append(f"{'  ' * indent}{key}: {val_str}\n")

                        add_config_lines(missing_structured)

                        with open(schema_yaml_path, "a", encoding="utf-8") as f:
                            f.writelines(config_lines)
                else:
                    # Create new schema.yaml with config
                    config_lines = ["config:\n"]
                    def add_config_lines(cfg, indent=1):
                        for key, value in cfg.items():
                            if isinstance(value, dict):
                                config_lines.append(f"{'  ' * indent}{key}:\n")
                                add_config_lines(value, indent + 1)
                            elif isinstance(value, list):
                                # Format lists as readable multi-line
                                config_lines.append(f"{'  ' * indent}{key}:\n")
                                for item in value:
                                    if isinstance(item, str):
                                        # Quote strings only if they contain YAML special characters
                                        needs_quote = any(c in item for c in ['*', '&', '!', '|', '>', '@', '`', ':', '#', '[', ']', '{', '}'])
                                        if needs_quote or (item and item[0] == '-'):
                                            config_lines.append(f"{'  ' * (indent + 1)}- '{item}'\n")
                                        else:
                                            config_lines.append(f"{'  ' * (indent + 1)}- {item}\n")
                                    else:
                                        config_lines.append(f"{'  ' * (indent + 1)}- {item}\n")
                            else:
                                if isinstance(value, bool):
                                    val_str = str(value).lower()
                                elif isinstance(value, str):
                                    val_str = value
                                elif value is None:
                                    val_str = 'null'
                                else:
                                    val_str = str(value)
                                config_lines.append(f"{'  ' * indent}{key}: {val_str}\n")

                    add_config_lines(missing_structured)

                    with open(schema_yaml_path, "w", encoding="utf-8") as f:
                        f.writelines(config_lines)

                logger.info(f"  {plugin_name}: Added {len(missing_keys)} missing config parameter(s) to schema.yaml")

            except Exception as e:
                logger.error(f"  {plugin_name}: Failed to merge config - {e}")

        logger.info("\nConfig merge complete")
        sys.exit(0)

    # Validate plugins
    all_passed = True
    results: list[tuple[str, bool]] = []

    for plugin_path in sorted(plugins_to_validate):
        validator = PluginValidator(plugin_path, schemas_dir)
        passed = validator.validate()
        results.append((plugin_path.name, passed))
        all_passed = all_passed and passed

    # Print summary if validating multiple plugins
    if len(plugins_to_validate) > 1:
        print(f"\n{'='*70}")
        print("SUMMARY")
        print(f"{'='*70}\n")

        passed_count = sum(1 for _, passed in results if passed)
        failed_count = len(results) - passed_count

        for plugin_name, passed in results:
            status = "[OK] PASS" if passed else "[X] FAIL"
            print(f"  {status}  {plugin_name}")

        print(f"\nTotal: {len(results)} plugins")
        print(f"Passed: {passed_count}")
        print(f"Failed: {failed_count}\n")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
