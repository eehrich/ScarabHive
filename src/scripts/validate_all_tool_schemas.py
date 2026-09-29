#!/usr/bin/env python3
"""
Tool Schema Validation Script

Validates that all tool schemas in plugin schema.yaml files are in the correct format.
This script checks:
1. OpenAI format: {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
2. MCP format: {"name": "...", "description": "...", "inputSchema": {...}}
3. Every tool routes to a method the plugin defines (warning otherwise)

Usage:
    python src/scripts/validate_all_tool_schemas.py
    python src/scripts/validate_all_tool_schemas.py --verbose
    python src/scripts/validate_all_tool_schemas.py --plugin basic_operations
"""

import argparse
import ast
import json
import logging
import sys
from pathlib import Path
from typing import Any

from jinja2 import Environment, meta

from agent_system.plugins.schema_loader import load_schema_from_dir

# One definition of where plugins live, shared with validate_plugin.py rather
# than repeated here. A second copy of that list is how this script came to
# walk two roots while the other walked four: 9 plugin directories out of
# reach, 2 of them carrying a schema.yaml this validator exists to check --
# and "Plugins validated: 64" reads like a complete sweep either way.
from scripts.validate_plugin import plugin_roots

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)


class ToolSchemaValidator:
    """Validates tool schemas in plugin schema.yaml files."""

    #: Stand-in for a variable the plugin would supply at runtime: truthy,
    #: iterable AND sized, because the shipped schemas ask all three of it
    #: (``{% if x %}``, ``x|length``, ``x|tojson``). A bare ``True`` breaks
    #: basic_agent ("object of type 'bool' has no len()") and llm_router.
    PRESENT = ["TEMPLATE_VALUE"]

    #: What a ``call`` override invokes when it only hands the tool on
    #: (``self.server.call``, ``super().call``, ``call_with_status``).
    HAND_ON = {"call", "call_tool", "call_with_status"}

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.all_schemas: list[tuple[str, dict, str]] = []  # (plugin_name, tool_schema, format)
        self.two_state: list[str] = []  # schemas validated in both branch states
        self.one_state: list[str] = []  # ... and those where the second render failed
        self.methods_checked = 0  # tools whose handler method was looked up
        self.own_routing: list[str] = []  # plugins that route by their own rule

    def _render(self, plugin_path: Path) -> list[dict]:
        """The schema in every shape production can build it.

        Through the loader the RUNTIME uses -- and validate_plugin.py since
        2181390d -- not a second renderer beside it. This script used to delete
        the ``{% ... %}`` tags with a regex and keep what stood between them, so
        both branches of an if/else landed in one document: tavily_search read
        as ``tools: []`` followed by a sequence and was reported broken.

        Twice, because one render is one branch. ``{% if api_key_configured %}``
        hides two tools until the flag is set, ``{% if not read_only %}`` hides
        two until it is unset -- neither document alone holds them all, and
        both are shapes the runtime really builds.
        """
        text = (plugin_path / "schema.yaml").read_text(encoding="utf-8")
        variables = meta.find_undeclared_variables(Environment().parse(text))
        documents = [load_schema_from_dir(plugin_path, {"name": plugin_path.name})]
        if variables - {"name"}:
            present = {**{v: self.PRESENT for v in variables},
                       "name": plugin_path.name}
            try:
                documents.append(load_schema_from_dir(plugin_path, present))
                self.two_state.append(plugin_path.name)
            except Exception as e:
                # One stand-in cannot be the right SHAPE for every variable a
                # schema might use (`{% for k, v in cfg.items() %}` wants a
                # mapping). The plugin's own default render stands; naming the
                # state that stayed unseen is the honest answer -- calling a
                # healthy schema broken is the false alarm this file just lost.
                self.one_state.append(f"{plugin_path.name} ({type(e).__name__})")
        return documents

    @staticmethod
    def _tool_key(tool: Any) -> str:
        """What makes two entries the SAME tool across the two renders.

        Its name, not its content: where a variable sits INSIDE a tool
        (basic_agent writes the profile list into its parameters), the two
        documents describe that tool differently, and keying on the text
        counted six plugins' tools twice -- 170 reported for 164 in the tree.
        Anything without a usable name falls back to its content, so a
        malformed entry is still reported once.
        """
        if isinstance(tool, dict):
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            name = name or tool.get("name")
            if isinstance(name, str) and name:
                return name
        return json.dumps(tool, sort_keys=True, default=str)

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

        try:
            documents = self._render(plugin_path)
        except Exception as e:
            self.errors.append(f"{plugin_path.name}: Failed to load schema.yaml: {e}")
            return False

        tools = []
        seen = set()
        for schema_data in documents:
            found = (schema_data or {}).get("tools")
            if not found:
                continue
            if not isinstance(found, list):
                self.errors.append(f"{plugin_path.name}: tools must be a list")
                return False
            for tool in found:
                key = self._tool_key(tool)
                if key not in seen:
                    seen.add(key)
                    tools.append(tool)

        if not tools:
            logger.debug(f"{plugin_path.name}: No tools section in schema.yaml")
            return True

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

        self._check_tool_methods(plugin_path, tools)
        return plugin_passed

    @staticmethod
    def _method_name(plugin_name: str, tool_name: str) -> str:
        """The routing of ``SchemaBasedToolMixin._get_method_name``."""
        if tool_name == plugin_name:
            return "execute"
        if tool_name.startswith(f"{plugin_name}_"):
            return tool_name[len(plugin_name) + 1:]
        return tool_name

    def _check_tool_methods(self, plugin_path: Path, tools: list) -> None:
        """Warn about a tool whose handler method exists nowhere in the plugin.

        Searched across the whole plugin package, not only server.py: a
        handler may live in a mixin module (writer_issues keeps execute_task in
        repair_pipeline.py).

        Skipped: a plugin that routes by its own rule -- overriding
        ``_get_method_name``, or a ``call`` that hands on to no other ``call``
        (log_viewer dispatches with if/elif). Defining ``call`` or
        ``call_tool`` alone is no such sign: most plugin.py wrappers define
        them only to hand on to the server's mixin, where this routing applies.

        Only methods count -- functions defined directly in a class body -- so
        a same-named module-level or nested function (a route handler called
        ``call``) neither stands in for a missing handler nor switches the
        check off.

        ponytail: methods are pooled across every class in the package, so a
        same-named method on an unrelated class satisfies the lookup. A handler
        inherited from a class outside the package, and the mixin's Agent
        special case (tool == agent name goes to Agent.call), would read as
        missing. None of it occurs in the tree today; resolve the server class
        hierarchy if it does.
        """
        defined: set[str] = set()
        own_call = False
        for py_file in plugin_path.rglob("*.py"):
            if "tests" in py_file.relative_to(plugin_path).parts:
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError) as e:
                self.warnings.append(f"{plugin_path.name}: cannot parse {py_file.name}: {e}")
                continue
            methods = [node for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef)
                       for node in cls.body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for node in methods:
                defined.add(node.name)
                if node.name == "call" and not any(
                        isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in self.HAND_ON
                        for sub in ast.walk(node)):
                    own_call = True
        if own_call or "_get_method_name" in defined:
            self.own_routing.append(plugin_path.name)
            return
        for tool in tools:
            name = self._tool_key(tool)
            if not isinstance(tool, dict) or name.startswith("{"):
                continue
            self.methods_checked += 1
            method = self._method_name(plugin_path.name, name)
            if method not in defined:
                self.warnings.append(
                    f"{plugin_path.name}: tool '{name}' routes to method "
                    f"'{method}()', which the plugin does not define")

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

        # Say how the count came about: a schema that takes plugin variables
        # was rendered twice, with them absent and present, and the tools of
        # both documents are in the number above. One render would silently
        # drop a whole branch -- three shipped tools live in one.
        if self.two_state:
            print(f"\n{len(self.two_state)} schema(s) take plugin variables and "
                  f"were validated in both states: "
                  f"{', '.join(sorted(self.two_state))}")
        if self.one_state:
            print(f"\n{len(self.one_state)} schema(s) could only be rendered with "
                  f"their variables ABSENT -- tools behind a flag are unchecked "
                  f"there: {', '.join(sorted(self.one_state))}")
        print(f"\nHandler methods checked for {self.methods_checked} tool(s)")
        if self.own_routing:
            print(f"{len(self.own_routing)} plugin(s) route by their own rule, "
                  f"handlers unchecked: {', '.join(sorted(self.own_routing))}")
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
                # Check if it's a plugin (has schema.yaml or plugin.toml)
                if (item / "schema.yaml").exists() or (item / "plugin.toml").exists():
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
        for root in plugin_roots(project_root):
            location = root / plugin_name
            if location.exists():
                plugins_to_validate.append(location)
                break
        else:
            logger.error(f"Plugin not found: {plugin_name}")
            sys.exit(1)

    else:
        # Validate all plugins (default)
        plugins_to_validate = find_plugin_directories(plugin_roots(project_root))

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
