#!/usr/bin/env python3
"""
Tool Method Mapping Validation Script

Validates that all plugins with tool names matching the plugin name ({{name}})
have the correct execute() method implementation.

This ensures compatibility with the SchemaBasedToolMixin routing logic:
- Tool name = plugin name → routes to execute()
- Tool name = plugin_name_suffix → routes to suffix()

Usage:
    python src/scripts/validate_tool_method_mapping.py
    python src/scripts/validate_tool_method_mapping.py --verbose
"""

import argparse
import logging
import re
import sys
from pathlib import Path

import yaml

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)


class ToolMethodMappingValidator:
    """Validates tool name → method name mapping in plugins."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.validated_plugins: int = 0

    def validate_plugin(self, plugin_path: Path) -> bool:
        """
        Validate a single plugin's tool → method mapping.

        Returns:
            True if validation passes, False otherwise
        """
        schema_yaml_path = plugin_path / "schema.yaml"
        server_py_path = plugin_path / "server.py"

        if not schema_yaml_path.exists():
            logger.debug(f"No schema.yaml found for {plugin_path.name}")
            return True  # Not an error

        if not server_py_path.exists():
            logger.debug(f"No server.py found for {plugin_path.name}")
            return True  # Not an error

        # Load schema.yaml
        try:
            with open(schema_yaml_path, "r", encoding="utf-8") as f:
                schema_content = f.read()

            # Replace template variables
            schema_content = re.sub(r'\{\{\s*name\s*\}\}', plugin_path.name, schema_content)
            schema_content = re.sub(r'\{%.*?%\}', '', schema_content, flags=re.DOTALL)
            schema_content = re.sub(r'\{\{[^}]+\}\}', 'TEMPLATE_VALUE', schema_content)

            schema_data = yaml.safe_load(schema_content)
        except Exception as e:
            self.errors.append(f"{plugin_path.name}: Failed to load schema.yaml: {e}")
            return False

        if not schema_data or "tools" not in schema_data:
            logger.debug(f"{plugin_path.name}: No tools section in schema.yaml")
            return True

        # Load server.py
        try:
            with open(server_py_path, "r", encoding="utf-8") as f:
                server_content = f.read()
        except Exception as e:
            self.errors.append(f"{plugin_path.name}: Failed to load server.py: {e}")
            return False

        # Check each tool
        tools = schema_data["tools"]
        if not isinstance(tools, list):
            self.errors.append(f"{plugin_path.name}: tools must be a list")
            return False

        plugin_passed = True
        for idx, tool in enumerate(tools):
            if not isinstance(tool, dict):
                continue

            # Extract tool name
            tool_name = None
            if "function" in tool:
                tool_name = tool["function"].get("name")
            elif "name" in tool:
                tool_name = tool["name"]

            if not tool_name:
                continue

            # Check if tool name exactly matches plugin name
            if tool_name == plugin_path.name:
                # Tool name matches plugin name → must have execute() method
                if not re.search(r'async def execute\(self, params', server_content):
                    self.errors.append(
                        f"{plugin_path.name}: Tool '{tool_name}' matches plugin name "
                        f"but server.py has no execute() method. "
                        f"Tool name '{{{{name}}}}' → method 'execute()'"
                    )
                    plugin_passed = False
                else:
                    logger.debug(f"{plugin_path.name}: Tool '{tool_name}' → execute() ✓")

            # Check if tool name has plugin prefix (e.g., "plugin_name_method")
            elif tool_name.startswith(f"{plugin_path.name}_"):
                suffix = tool_name[len(plugin_path.name) + 1:]
                if not re.search(rf'async def {suffix}\(self, params', server_content):
                    self.warnings.append(
                        f"{plugin_path.name}: Tool '{tool_name}' expects method '{suffix}()' "
                        f"but it may not exist in server.py"
                    )
                else:
                    logger.debug(f"{plugin_path.name}: Tool '{tool_name}' → {suffix}() ✓")

        self.validated_plugins += 1
        return plugin_passed

    def report_results(self) -> bool:
        """
        Print validation results.

        Returns:
            True if all validations passed, False otherwise
        """
        print(f"\n{'='*70}")
        print("Tool → Method Mapping Validation Results")
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
            print("[OK] All tool → method mappings are correct!")
            print()
        elif not self.errors:
            print("[OK] No errors found (warnings only)")
            print()
        else:
            print("[X] Validation failed with errors")
            print()

        print(f"Plugins validated: {self.validated_plugins}")
        print()

        return len(self.errors) == 0


def find_plugin_directories(base_dirs: list[Path]) -> list[Path]:
    """Find all plugin directories in base directories."""
    plugin_dirs = []

    for base_dir in base_dirs:
        if not base_dir.exists():
            logger.warning(f"Plugin directory not found: {base_dir}")
            continue

        for item in base_dir.iterdir():
            if item.is_dir() and (item / "schema.yaml").exists():
                plugin_dirs.append(item)

    return plugin_dirs


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Validate tool name → method name mapping in plugins"
    )
    parser.add_argument(
        "--plugin",
        "-p",
        help="Plugin name to validate (e.g., cognitive_stack)"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output"
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Determine project root
    script_path = Path(__file__).resolve()
    project_root = script_path.parent.parent.parent

    # Determine which plugins to validate
    plugins_to_validate: list[Path] = []

    if args.plugin:
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

    else:
        # Validate all plugins (default)
        base_dirs = [
            project_root / "src" / "plugins",
            project_root / "src" / "plugins_writer"
        ]
        plugins_to_validate = find_plugin_directories(base_dirs)

    # Validate plugins
    validator = ToolMethodMappingValidator()
    all_passed = True

    for plugin_path in sorted(plugins_to_validate):
        plugin_passed = validator.validate_plugin(plugin_path)
        all_passed = all_passed and plugin_passed

    # Report results
    success = validator.report_results()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
