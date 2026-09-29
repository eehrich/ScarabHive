#!/usr/bin/env python3
"""
Plugin Validation Script

Validates plugin conformity by checking:
- plugin.toml structure and required fields
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
    python src/scripts/validate_plugin.py --all --merge-config
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml
# jsonschema is imported where it is used (_validate_manifest_schema), not here:
# it is not in requirements/core.txt -- it arrives with a plugin's own deps --
# and validate_all_tool_schemas.py imports this module only for PLUGIN_ROOTS.
# A module-level import would make that script need a package it never calls.

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

#: A plugin is a directory carrying this. ONE definition, because the last
#: time the two places disagreed, ``_check_file_structure`` was fixed and
#: ``find_plugin_directories`` was not: ``--all`` then found 0 of 64 plugins
#: and exited 0, so the pre-commit hook, the CI step and the make target all
#: passed on an empty list.
MANIFEST_NAME = "plugin.toml"

#: Every root that actually holds plugins. Measured 2026-09-20: 62 + 12 + 2
#: manifests. ``--all`` and ``--plugin`` used to name only the first two, so
#: plugins_trading was unreachable by either; the LLM providers had their own
#: root as well until they moved into ``plugins``.
PLUGIN_ROOTS = ("plugins", "plugins_writer", "plugins_trading")


def has_manifest(path: Path) -> bool:
    """Does this directory look like a plugin?"""
    return (path / MANIFEST_NAME).exists()


def plugin_roots(project_root: Path) -> list[Path]:
    """The plugin roots under a checkout, in a fixed order."""
    return [project_root / "src" / name for name in PLUGIN_ROOTS]


class PluginValidator:
    """Validates plugin conformity against system requirements."""

    def __init__(self, plugin_path: Path, schemas_dir: Path):
        self.plugin_path = plugin_path
        self.schemas_dir = schemas_dir
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.manifest: dict[str, Any] | None = None
        self.schema_yaml: dict[str, Any] | None = None

    def validate(self) -> bool:
        """
        Run all validation checks.

        Returns:
            True if all validations pass, False otherwise
        """
        logger.info(f"Validating plugin: {self.plugin_path.name}")

        # Check basic file structure, then load the configuration files. Either
        # failing ends the run -- but with the report: the errors are already
        # collected, and an exit 1 with nothing printed tells nobody why.
        if not self._check_file_structure() or not self._load_configs():
            self._report_results()
            return False

        # Validate the manifest against its JSON schema
        self._validate_manifest_schema()

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

    #: Types that ship no plugin-registry entrypoint module. A "library"
    #: plugin (amiga, coder, research, writer_publish) is agents, skills and
    #: prompts -- config, no code; an "llm-provider" is found by the LLM
    #: registry through provider.py, not through PLUGIN_FACTORY. Demanding
    #: plugin.py or an `entrypoint` from either was this validator refusing a
    #: shape the runtime supports.
    CODELESS_TYPES = frozenset({"library", "llm-provider"})

    #: The type was called "tool-server" until 17.09.2026. A plugin from outside
    #: this repo still says so, and its meaning has not changed.
    LEGACY_TYPES = {"tool-server": "tool-server"}

    def declared_types(self) -> list[str]:
        """The manifest's `type` list, old string form and old names converted."""
        declared = (self.manifest or {}).get("type", ["tool-server"])
        if isinstance(declared, str):
            declared = self._convert_old_type_format(declared)
        return [self.LEGACY_TYPES.get(name, name) for name in (declared or [])]

    def _is_codeless(self) -> bool:
        return bool(self.CODELESS_TYPES & set(self.declared_types()))

    def _check_file_structure(self) -> bool:
        """Check that required files exist."""
        # Demanding plugin.yaml here made this validator refuse EVERY plugin
        # in the tree at its first check: measured 2026-09-05, 73 plugin.toml
        # and 0 plugin.yaml, so nothing behind this line had run in a long
        # time. plugin.yaml is gone, not deprecated -- there is no fallback.
        if not has_manifest(self.plugin_path):
            self.errors.append(f"Missing required file: {MANIFEST_NAME}")
            return False

        # The manifest decides what else has to be there -- so read it first.
        # (_load_configs runs after this method and does it again; here we only
        # need the type, and a manifest that cannot be read is reported there.)
        from agent_system.plugins.plugin_manifest import load_plugin_metadata
        self.manifest = load_plugin_metadata(self.plugin_path)

        if not (self.plugin_path / "schema.yaml").exists() and not self._is_codeless():
            self.warnings.append(
                "No schema.yaml found - plugin may be config-only or have programmatic tools"
            )

        # No entrypoint check here on purpose: _validate_entrypoint does it
        # properly, against the module the manifest actually NAMES, and it
        # runs to the end instead of aborting the whole validation at the
        # first finding. Measured: with this block gone, a plugin whose
        # plugin.py is missing is still refused -- by that check.
        return True

    def _load_configs(self) -> bool:
        """Load and parse the plugin manifest + schema."""
        try:
            # Load the plugin manifest through the shared loader, which
            # returns the [plugin] table of plugin.toml.
            from agent_system.plugins.plugin_manifest import load_plugin_metadata
            self.manifest = load_plugin_metadata(self.plugin_path)

            # Render schema.yaml through the RUNTIME's loader, not through a
            # regex. Stripping `{% ... %}` leaves BOTH branches of a
            # conditional standing: tavily_search declares `tools: []` without
            # an API key and a block sequence with one, and the stripped result
            # was neither -- "expected <block end>" for a file the runtime
            # renders fine. Measured 2026-09-05: 8 of the 62 schemas carry
            # Jinja logic, and the real loader parses all 62.
            schema_yaml_path = self.plugin_path / "schema.yaml"
            if schema_yaml_path.exists():
                from agent_system.plugins.schema_loader import load_schema_from_dir
                try:
                    self.schema_yaml = load_schema_from_dir(
                        self.plugin_path, {"name": self.plugin_path.name})
                except Exception as e:
                    self.errors.append(f"schema.yaml does not render or parse: {e}")
                    return False

            return True

        except yaml.YAMLError as e:
            self.errors.append(f"YAML parsing error: {e}")
            return False
        except Exception as e:
            self.errors.append(f"Error loading configs: {e}")
            return False

    def _validate_manifest_schema(self) -> None:
        """Validate the manifest against its JSON schema."""
        from jsonschema import ValidationError, validate as json_validate

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

            json_validate(instance=self.manifest, schema=schema)
            logger.debug("plugin.toml passed JSON schema validation")

        except ValidationError as e:
            self.errors.append(
                f"plugin.toml schema validation failed: {e.message} at {list(e.path)}"
            )
        except Exception as e:
            self.errors.append(f"Error validating plugin.toml schema: {e}")

    def _validate_schema_yaml_structure(self) -> None:
        """Validate schema.yaml structure and required fields."""
        if not self.schema_yaml:
            return  # schema.yaml is optional

        # Check for tools section (if plugin)
        raw_types = self.manifest.get("type", ["tool-server"])

        # Handle both old string format and new list format for backward compatibility
        if isinstance(raw_types, str):
            self.warnings.append(
                f"Plugin type is using deprecated string format: '{raw_types}'. "
                "Please update to list format (e.g., ['tool-server'], ['web'], ['tool-server', 'web'])"
            )
        plugin_types = self.declared_types()

        if "tool-server" in plugin_types:
            if "tools" not in self.schema_yaml:
                self.errors.append(
                    f"plugin type {plugin_types} requires 'tools' section in schema.yaml"
                )
            else:
                self._validate_tools_section()

        # Check for hooks section (if hooks plugin)
        if "hooks" in plugin_types:
            if "hooks" not in self.schema_yaml:
                self.errors.append(
                    f"Hooks plugin type {plugin_types} requires 'hooks' section in schema.yaml"
                )
            else:
                self._validate_hooks_section()

        # A web_ui section is checked wherever it is: plugins typed tool-server
        # (comfyui, ssh_control) serve panels too.
        if "web_ui" in self.schema_yaml:
            self._validate_web_ui_section()
        elif "web" in plugin_types:
            self.warnings.append(
                f"Web plugin type {plugin_types} should have 'web_ui' section in schema.yaml"
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

            # Check which format the tool is in (OpenAI or MCP)
            has_openai_format = "type" in tool and tool["type"] == "function" and "function" in tool
            has_mcp_format = "name" in tool and "inputSchema" in tool
            has_partial_openai = "function" in tool and "type" not in tool

            if not has_openai_format and not has_mcp_format and not has_partial_openai:
                self.errors.append(
                    f"Tool at index {idx} must be in OpenAI format (type=function, function={{...}}) "
                    f"or MCP format (name, inputSchema). Found keys: {list(tool.keys())}"
                )
                continue

            # Extract tool name for duplicate checking
            if has_openai_format or has_partial_openai:
                tool_name = tool.get("function", {}).get("name")
            elif has_mcp_format:
                tool_name = tool.get("name")
            else:
                tool_name = None

            # Check for duplicate tool names
            if tool_name and tool_name in seen_tool_names and "{{" not in tool_name:
                self.errors.append(f"Duplicate tool name: {tool_name}")
            if tool_name:
                seen_tool_names.add(tool_name)

            # Validate based on format
            if has_openai_format:
                self._validate_openai_tool(idx, tool)
            elif has_mcp_format:
                self._validate_mcp_tool(idx, tool)
            elif has_partial_openai:
                # Missing type field
                self.errors.append(f"Tool at index {idx} missing 'type' field (should be 'function')")
                self._validate_openai_tool(idx, tool, skip_type_check=True)

    def _validate_openai_tool(self, idx: int, tool: dict, skip_type_check: bool = False) -> None:
        """Validate tool in OpenAI format."""
        if not skip_type_check and tool.get("type") != "function":
            self.errors.append(
                f"Tool at index {idx} has invalid type: {tool['type']} (expected 'function')"
            )
            return

        if "function" not in tool:
            self.errors.append(f"Tool at index {idx} missing 'function' field")
            return

        function = tool["function"]

        # Validate function object
        if "name" not in function:
            self.errors.append(f"Tool at index {idx} missing 'name' in function")
            return

        tool_name = function["name"]

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

    def _validate_mcp_tool(self, idx: int, tool: dict) -> None:
        """Validate tool in MCP format (name + inputSchema)."""
        tool_name = tool["name"]

        if "description" not in tool:
            self.warnings.append(
                f"tool '{tool_name}' missing description - highly recommended"
            )

        if "inputSchema" not in tool:
            self.warnings.append(
                f"tool '{tool_name}' missing inputSchema - tools should define parameters"
            )
        else:
            # Validate inputSchema as parameters
            self._validate_tool_parameters(tool_name, tool["inputSchema"])

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
        # From the enum, not from a copy: this list was missing
        # pre_llm_request and post_llm_response (HookType has had them since
        # the LLM-client-level hooks landed), so message_debugger -- a shipped,
        # working plugin -- was reported as broken.
        from agent_system.hooks.plugin_hook import HookType
        valid_hook_types = [h.value for h in HookType]

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

            # SchemaBasedHookPlugin does `hook.get("type", "").upper()`, so
            # `PRE_LLM_CALL` and `pre_llm_call` are the same hook to the
            # runtime -- and the docstring of that class writes it upper case.
            hook_type = str(hook["type"]).lower()
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

            if "on_error" in hook and (hook["on_error"] != "block" or hook_type != "pre_tool_call"):
                self.errors.append(
                    f"Hook '{hook_name}' on_error must be 'block', and only on a pre_tool_call "
                    f"hook -- anything else lets a failing hook's call run"
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
        """Validate web_ui: the panel's catalogue entry and the schema-routed endpoints.

        The panel goes through the catalogue's own parser, so a panel this
        accepts is one the launcher shows.
        """
        from agent_system.ui.catalog import PanelSpecError, plugin_panel
        from agent_system.ui.resources import sprite_icons

        web_ui = self.schema_yaml.get("web_ui", {})
        if not isinstance(web_ui, dict):
            self.errors.append("'web_ui' must be an object")
            return
        # YAML reads keys like `on:` as booleans: named as text, so they sort with the rest
        unknown = sorted(str(key) for key in set(web_ui) - {"panel", "endpoints"})
        if unknown:
            self.errors.append(f"web_ui has unknown keys {unknown}; known: endpoints, panel")
        if not isinstance(web_ui.get("endpoints", []), list):
            self.errors.append("web_ui.endpoints must be a list")
        if "panel" in web_ui:
            try:
                plugin_panel(self.plugin_path.name, web_ui["panel"], sprite_icons())
            except PanelSpecError as error:
                self.errors.append(str(error))

    def _cross_validate_configs(self) -> None:
        """Cross-validate plugin.toml and schema.yaml."""
        if not self.manifest or not self.schema_yaml:
            return

        plugin_types = self.declared_types()

        # Check consistency between plugin type and schema content
        has_tools = "tools" in self.schema_yaml
        has_hooks = "hooks" in self.schema_yaml

        if "tool-server" in plugin_types and not has_tools:
            self.warnings.append(
                f"Plugin type {plugin_types} suggests tools but schema.yaml has no 'tools' section"
            )

        if plugin_types == ["hooks"] and has_tools:
            self.warnings.append(
                "Plugin type ['hooks'] but schema.yaml defines tools"
            )

        if "hooks" in plugin_types and not has_hooks:
            self.warnings.append(
                f"Plugin type {plugin_types} suggests hooks but schema.yaml has no 'hooks' section"
            )

    def _convert_old_type_format(self, old_type: str) -> list[str]:
        """Convert old string type format to new list format.

        Mapping:
        - mcp_only -> ["tool-server"]
        - web_only -> ["web"]
        - hooks_only -> ["hooks"]
        - hybrid -> ["tool-server", "web"]
        - mcp_with_hooks -> ["tool-server", "hooks"]
        - web_with_hooks -> ["web", "hooks"]
        - hybrid_with_hooks -> ["tool-server", "web", "hooks"]
        """
        mapping = {
            "mcp_only": ["tool-server"],
            "web_only": ["web"],
            "hooks_only": ["hooks"],
            "hybrid": ["tool-server", "web"],
            "mcp_with_hooks": ["tool-server", "hooks"],
            "web_with_hooks": ["web", "hooks"],
            "hybrid_with_hooks": ["tool-server", "web", "hooks"],
        }
        return mapping.get(old_type, ["tool-server"])

    def _validate_template_variables(self) -> None:
        """Validate template variable usage in schema.yaml."""
        if not self.schema_yaml:
            return

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
        if not self.manifest:
            return

        # Check if the manifest declares hooks
        plugin_hooks = self.manifest.get("hooks", [])

        if plugin_hooks:
            self.warnings.append(
                "plugin.toml contains a hooks section - hooks belong in schema.yaml"
            )

    def _validate_entrypoint(self) -> None:
        """Validate entrypoint module and factory."""
        if not self.manifest:
            return

        entrypoint = self.manifest.get("entrypoint")
        if not entrypoint:
            if not self._is_codeless():
                self.errors.append("Missing 'entrypoint' in the manifest")
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
            if item.is_dir() and has_manifest(item):
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
        help="Validate every plugin under the roots in PLUGIN_ROOTS"
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
        plugins_to_validate = find_plugin_directories(plugin_roots(project_root))

    elif args.plugin:
        # Validate specific plugin by name
        plugin_name = args.plugin
        possible_locations = [root / plugin_name for root in plugin_roots(project_root)]

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

                _ = structure_config(discovered_config)  # noqa: F841

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
