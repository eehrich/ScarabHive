#!/usr/bin/env python3
"""
JSON Schema Validator for Config-Based Agents

Validates config_agents section in mcp.yaml against the JSON schema.
Can be used standalone or integrated into CI/CD pipelines.

Usage:
    python scripts/validate_config_agents_schema.py [--config path/to/mcp.yaml]
    python scripts/validate_config_agents_schema.py --help

Exit Codes:
    0 - Validation successful
    1 - Validation failed
    2 - Error during validation
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Any, List

try:
    from jsonschema import validate, ValidationError, SchemaError
except ImportError:
    print("Error: jsonschema library not installed", file=sys.stderr)
    print("Install with: pip install jsonschema", file=sys.stderr)
    sys.exit(2)

try:
    import yaml
except ImportError:
    print("Error: PyYAML library not installed", file=sys.stderr)
    print("Install with: pip install PyYAML", file=sys.stderr)
    sys.exit(2)


def load_schema(schema_path: Path) -> Dict[str, Any]:
    """Load JSON schema from file."""
    try:
        with open(schema_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: Schema file not found: {schema_path}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in schema file: {e}", file=sys.stderr)
        sys.exit(2)


def load_config(config_path: Path) -> Dict[str, Any]:
    """Load YAML configuration from file."""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(2)
    except yaml.YAMLError as e:
        print(f"Error: Invalid YAML in config file: {e}", file=sys.stderr)
        sys.exit(2)


def validate_config_agents(
    config_agents: Dict[str, Any],
    schema: Dict[str, Any],
    verbose: bool = False
) -> tuple[bool, List[str]]:
    """
    Validate config_agents against schema.
    
    Returns:
        (success, errors) tuple
    """
    errors = []
    
    if not config_agents:
        errors.append("No config_agents section found or it is empty")
        return False, errors
    
    if verbose:
        print(f"Validating {len(config_agents)} agent(s)...")
    
    # Validate entire config_agents structure
    try:
        validate(instance=config_agents, schema=schema)
        if verbose:
            print("✓ Schema validation passed")
        return True, []
    except ValidationError as e:
        # Format validation error
        error_path = " -> ".join(str(p) for p in e.absolute_path) if e.absolute_path else "root"
        error_msg = f"Validation error at {error_path}: {e.message}"
        errors.append(error_msg)
        
        if verbose:
            print(f"✗ Validation failed: {error_msg}", file=sys.stderr)
            if e.context:
                print("\nAdditional context:", file=sys.stderr)
                for ctx_error in e.context:
                    print(f"  - {ctx_error.message}", file=sys.stderr)
        
        return False, errors
    except SchemaError as e:
        errors.append(f"Schema error: {e.message}")
        return False, errors


def validate_additional_constraints(
    config_agents: Dict[str, Any],
    verbose: bool = False
) -> tuple[bool, List[str]]:
    """
    Validate additional constraints not covered by JSON schema.
    
    Returns:
        (success, warnings) tuple
    """
    warnings = []
    
    for agent_name, agent_def in config_agents.items():
        # Check agent naming convention
        if not agent_name.islower():
            warnings.append(f"Agent '{agent_name}': Name should be lowercase")
        
        if not agent_def.get("enabled", True):
            if verbose:
                print(f"ℹ Agent '{agent_name}' is disabled")
        
        agent_config = agent_def.get("agent_config", {})
        
        # Check max_steps is reasonable
        max_steps = agent_config.get("max_steps", 20)
        if max_steps > 50:
            warnings.append(
                f"Agent '{agent_name}': max_steps={max_steps} is very high. "
                "Consider lower value (typical: 5-30) to avoid long execution times."
            )
        
        # Check both system_prompt and system_template are not provided
        has_prompt = bool(agent_config.get("system_prompt"))
        has_template = bool(agent_config.get("system_template"))
        
        if has_prompt and has_template:
            warnings.append(
                f"Agent '{agent_name}': Both system_prompt and system_template provided. "
                "Only one should be used. Inline prompt will take precedence."
            )
        
        if not has_prompt and not has_template:
            warnings.append(
                f"Agent '{agent_name}': No system prompt specified. "
                "Provide either system_prompt or system_template."
            )
        
        # Check tools configuration
        tools = agent_config.get("tools", {})
        allowed = tools.get("allowed", [])
        blocked = tools.get("blocked", [])
        
        if not allowed and not blocked:
            if verbose:
                print(f"ℹ Agent '{agent_name}': No tools configured")
        
        # Check for conflicting tool patterns
        for allow_pattern in allowed:
            for block_pattern in blocked:
                if allow_pattern == block_pattern:
                    warnings.append(
                        f"Agent '{agent_name}': Tool pattern '{allow_pattern}' "
                        "is both allowed and blocked"
                    )
    
    return len(warnings) == 0, warnings


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Validate config_agents against JSON schema",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate default config
  python scripts/validate_config_agents_schema.py
  
  # Validate specific config file
  python scripts/validate_config_agents_schema.py --config config/mcp.yaml
  
  # Verbose output
  python scripts/validate_config_agents_schema.py --verbose
  
  # Show warnings only
  python scripts/validate_config_agents_schema.py --warnings-only
        """
    )
    
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/mcp.yaml"),
        help="Path to config file (default: config/mcp.yaml)"
    )
    
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("schemas/config-agents.schema.json"),
        help="Path to JSON schema file (default: schemas/config-agents.schema.json)"
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    
    parser.add_argument(
        "--warnings-only",
        action="store_true",
        help="Only check additional constraints, skip schema validation"
    )
    
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors"
    )
    
    args = parser.parse_args()
    
    # Load schema
    if not args.warnings_only:
        schema = load_schema(args.schema)
        if args.verbose:
            print(f"Loaded schema from: {args.schema}")
    
    # Load config
    config = load_config(args.config)
    if args.verbose:
        print(f"Loaded config from: {args.config}")
    
    # Extract config_agents section
    config_agents = config.get("mcp_system", {}).get("config_agents", {})
    
    if not config_agents:
        print("Warning: No config_agents section found in configuration", file=sys.stderr)
        sys.exit(0)
    
    # Validate against schema
    schema_valid = True
    if not args.warnings_only:
        schema_valid, schema_errors = validate_config_agents(
            config_agents,
            schema,
            verbose=args.verbose
        )
        
        if not schema_valid:
            print("\n❌ Schema validation FAILED:", file=sys.stderr)
            for error in schema_errors:
                print(f"  - {error}", file=sys.stderr)
    
    # Validate additional constraints
    constraints_valid, warnings = validate_additional_constraints(
        config_agents,
        verbose=args.verbose
    )
    
    if warnings:
        print("\n⚠️  Validation warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    
    # Summary
    print(f"\n{'='*60}")
    print("Validation Summary:")
    print(f"  Config file: {args.config}")
    print(f"  Agents found: {len(config_agents)}")
    
    if not args.warnings_only:
        print(f"  Schema validation: {'✓ PASSED' if schema_valid else '✗ FAILED'}")
    
    print(f"  Additional checks: {'✓ PASSED' if constraints_valid else f'⚠️  {len(warnings)} warning(s)'}")
    
    # Determine exit code
    if not schema_valid:
        sys.exit(1)
    
    if args.strict and not constraints_valid:
        print("\n❌ Validation FAILED (strict mode: warnings treated as errors)")
        sys.exit(1)
    
    if schema_valid and (constraints_valid or not args.strict):
        print("\n✅ All validations PASSED")
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
