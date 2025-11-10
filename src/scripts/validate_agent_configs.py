#!/usr/bin/env python3
"""
Validate agent YAML configuration files for syntax and schema errors.

This script validates agent configuration files against:
1. YAML syntax (proper formatting)
2. Schema structure (plugins.servers hierarchy)
3. Pydantic models (MCPConfig from agent_system.config.models)

Usage:
    python src/scripts/validate_agent_configs.py <yaml_file1> [yaml_file2] ...

Arguments:
    yaml_file: Full path(s) to YAML file(s) to validate

Examples:
    # Validate all agent configs (using shell glob)
    python src/scripts/validate_agent_configs.py config/agents_writer/*.yaml

    # Validate specific files
    python src/scripts/validate_agent_configs.py config/agents_writer/book_architect.yaml config/agents_writer/scene_writer.yaml

    # Use in CI/CD
    python src/scripts/validate_agent_configs.py config/agents_writer/*.yaml || exit 1
"""

import sys
from pathlib import Path
from typing import List, Tuple

import yaml
from pydantic import ValidationError

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_system.config.models import MCPConfig


def validate_yaml_file(file_path: Path) -> Tuple[bool, str]:
    """
    Validate a single YAML file for both syntax and schema.

    Args:
        file_path: Path to the YAML file

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        # Step 1: Parse YAML syntax
        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Step 2: Validate schema structure
        if not isinstance(data, dict):
            return False, "Root element must be a dictionary"

        if "plugins" not in data:
            return False, "Missing required 'plugins' key"

        if "servers" not in data["plugins"]:
            return False, "Missing required 'plugins.servers' key"

        servers = data["plugins"]["servers"]
        if not isinstance(servers, dict):
            return False, "'plugins.servers' must be a dictionary"

        # Step 3: Validate each server config against Pydantic model
        for server_name, server_config in servers.items():
            try:
                MCPConfig(**server_config)
            except ValidationError as e:
                error_lines = []
                for error in e.errors():
                    loc = ".".join(str(x) for x in error["loc"])
                    error_lines.append(f"  {loc}: {error['msg']}")
                return False, f"Schema validation failed for server '{server_name}':\n" + "\n".join(error_lines)

        return True, ""

    except yaml.YAMLError as e:
        return False, f"YAML parsing error: {e}"
    except UnicodeDecodeError as e:
        return False, f"Encoding error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Error: No YAML files specified")
        print("\nUsage:")
        print("  validate-agents <yaml_file1> [yaml_file2] ...")
        print("\nExamples:")
        print("  validate-agents config/agents_writer/*.yaml")
        print("  validate-agents config/agents_writer/book_architect.yaml")
        return 1

    # Get all YAML files from arguments
    yaml_files = [Path(arg).resolve() for arg in sys.argv[1:]]

    # Validate each file exists
    missing_files = [f for f in yaml_files if not f.exists()]
    if missing_files:
        print("❌ Error: The following files do not exist:")
        for f in missing_files:
            print(f"  - {f}")
        return 1

    print(f"Validating {len(yaml_files)} agent configuration files...\n")

    valid_files: List[Path] = []
    invalid_files: List[Tuple[Path, str]] = []

    for yaml_file in sorted(yaml_files):
        is_valid, error_msg = validate_yaml_file(yaml_file)

        if is_valid:
            valid_files.append(yaml_file)
            print(f"✓ {yaml_file.name:30s} OK")
        else:
            invalid_files.append((yaml_file, error_msg))
            print(f"✗ {yaml_file.name:30s} FAILED")

    # Print summary
    print("\n" + "=" * 70)
    print(f"Results: {len(valid_files)} valid, {len(invalid_files)} invalid")
    print("=" * 70)

    if invalid_files:
        print("\nValidation errors:\n")
        for file_path, error_msg in invalid_files:
            print(f"  {file_path.name}:")
            print(f"    {error_msg}\n")
        return 1

    print("\n✅ All agent configuration files are valid!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
