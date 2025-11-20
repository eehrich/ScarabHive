#!/usr/bin/env python3
"""
Tool Schema Validation Script

Validates that all tool schemas in plugin schema.yaml files are in the correct format.
This script checks:
1. OpenAI format: {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
2. MCP format: {"name": "...", "description": "...", "inputSchema": {...}}

Usage:
    python src/scripts/validate_all_tool_schemas.py
    python src/scripts/validate_all_tool_schemas.py --verbose
    python src/scripts/validate_all_tool_schemas.py --plugin basic_operations
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


class ToolSchemaValidator:
    """Validates tool schemas in plugin schema.yaml files."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.all_schemas: list[tuple[str, dict, str]] = []  # (plugin_name, tool_schema, format)

    def validate_plugin(self, plugin_path: Path) -> bool:
        """
        Validate a single plugin's schema.yaml for tool format issues.

        Returns:
            True if validation passes, False otherwise
        """
        schema_yaml_path = plugin_path / "schema.yaml"

        if not schema_yaml_path.exists():
            logger.debug(f"No schema.yaml found for {plugin_path.name}")
            return True  # Not an error - some plugins don't have schemas

        # Load schema.yaml with Jinja2 template handling
        try:
            with open(schema_yaml_path, "r", encoding="utf-8") as f:
                schema_content = f.read()

            # Replace template variables with placeholders
            schema_content = re.sub(r'\{\{\s*name\s*\}\}', 'plugin_name', schema_content)
            schema_content = re.sub(r'\{%.*?%\}', '', schema_content, flags=re.DOTALL)
            schema_content = re.sub(r'\{\{[^}]+\}\}', 'TEMPLATE_VALUE', schema_content)

            schema_data = yaml.safe_load(schema_content)
        except Exception as e:
            self.errors.append(f"{plugin_path.name}: Failed to load schema.yaml: {e}")
            return False

        if not schema_data:
            logger.debug(f"{plugin_path.name}: Empty schema.yaml")
            return True

        # Check for tools section
        tools = schema_data.get("tools")
        if not tools:
            logger.debug(f"{plugin_path.name}: No tools section in schema.yaml")
            return True

        if not isinstance(tools, list):
            self.errors.append(f"{plugin_path.name}: tools must be a list")
            return False

        # Validate each tool
        plugin_passed = True
        for idx, tool in enumerate(tools):
            if not isinstance(tool, dict):
                self.errors.append(f"{plugin_path.name}[{idx}]: Tool must be an object")
                plugin_passed = False
                continue

            validation_result = self._validate_tool_format(plugin_path.name, idx, tool)
            if not validation_result:
                plugin_passed = False

        return plugin_passed

    def _validate_tool_format(self, plugin_name: str, idx: int, tool: dict) -> bool:
        """
        Validate a single tool schema format.

        Returns:
            True if tool format is valid, False otherwise
        """
        # Check for OpenAI format
        has_openai_format = (
            "type" in tool and
            tool["type"] == "function" and
            "function" in tool
        )

        # Check for MCP format
        has_mcp_format = (
            "name" in tool and
            "inputSchema" in tool
        )

        # Check for partial OpenAI format (missing type field)
        has_partial_openai = (
            "function" in tool and
            "type" not in tool
        )

        # Validate format
        if has_openai_format:
            # Valid OpenAI format
            tool_name = tool.get("function", {}).get("name", "unknown")
            self.all_schemas.append((plugin_name, tool, "OpenAI"))
            logger.debug(f"{plugin_name}[{idx}]: '{tool_name}' - OpenAI format ✓")
            return True

        elif has_mcp_format:
            # Valid MCP format
            tool_name = tool.get("name", "unknown")
            self.all_schemas.append((plugin_name, tool, "MCP"))
            logger.debug(f"{plugin_name}[{idx}]: '{tool_name}' - MCP format ✓")
            return True

        elif has_partial_openai:
            # Missing type field
            tool_name = tool.get("function", {}).get("name", "unknown")
            self.errors.append(
                f"{plugin_name}[{idx}]: Tool '{tool_name}' is missing 'type' field "
                f"(should be 'type': 'function')"
            )
            return False

        else:
            # Invalid format
            self.errors.append(
                f"{plugin_name}[{idx}]: Tool has invalid format. Must be either:\n"
                f"  - OpenAI format: {{\"type\": \"function\", \"function\": {{\"name\": \"...\", ...}}}}\n"
                f"  - MCP format: {{\"name\": \"...\", \"inputSchema\": {{...}}}}\n"
                f"  Found keys: {list(tool.keys())}"
            )
            return False

    def report_results(self, plugins_validated: int) -> bool:
        """
        Print validation results.

        Returns:
            True if all validations passed, False otherwise
        """
        print(f"\n{'='*70}")
        print("Tool Schema Validation Results")
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
            print("[OK] All tool schemas are valid!")
            print()
        elif not self.errors:
            print("[OK] No errors found (warnings only)")
            print()
        else:
            print("[X] Validation failed with errors")
            print()

        # Print summary statistics
        print(f"Plugins validated: {plugins_validated}")
        print(f"Total tools found: {len(self.all_schemas)}")

        # Count by format
        openai_count = sum(1 for _, _, fmt in self.all_schemas if fmt == "OpenAI")
        mcp_count = sum(1 for _, _, fmt in self.all_schemas if fmt == "MCP")

        print(f"  - OpenAI format: {openai_count}")
        print(f"  - MCP format: {mcp_count}")
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
            if item.is_dir():
                # Check if it's a plugin (has schema.yaml or plugin.yaml)
                if (item / "schema.yaml").exists() or (item / "plugin.yaml").exists():
                    plugin_dirs.append(item)

    return plugin_dirs


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Validate tool schemas in plugin schema.yaml files"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Validate all plugins (default behavior)"
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
    validator = ToolSchemaValidator()
    all_passed = True

    for plugin_path in sorted(plugins_to_validate):
        plugin_passed = validator.validate_plugin(plugin_path)
        all_passed = all_passed and plugin_passed

    # Report results
    success = validator.report_results(len(plugins_to_validate))

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
