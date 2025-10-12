"""Configuration-based agent management commands for the CLI."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

try:
    from tabulate import tabulate  # optional dependency for pretty tables
except Exception:
    tabulate = None

logger = logging.getLogger(__name__)


def _config_agents_list(config: Any, args: Any) -> None:
    """List all configuration-based agents and their status."""
    from agent_system.plugins.config_agent_discovery import list_config_agents
    
    agents = list_config_agents(config.agents)
    
    if args.out_format == "json":
        print(json.dumps(agents, indent=2, ensure_ascii=False))
    else:
        # Table format
        if not agents:
            print("No configuration-based agents found.")
            return

        rows = []
        for agent in agents:
            name = agent.get('name', 'unknown')
            enabled = agent.get('enabled', False)
            llm_profile = agent.get('llm_profile', 'N/A')
            max_steps = agent.get('max_steps', 'N/A')
            description = agent.get('description', '')
            status = "Enabled" if enabled else "Disabled"
            
            rows.append((name, llm_profile, max_steps, status, description))

        headers = ["NAME", "LLM", "STEPS", "STATUS", "DESCRIPTION"]
        if tabulate:
            print(tabulate(rows, headers=headers, tablefmt="github"))
        else:
            # Fallback if tabulate not available
            print(f"{headers[0]:<20} {headers[1]:<10} {headers[2]:<8} {headers[3]:<10} {headers[4]}")
            print("-" * 80)
            for row in rows:
                print(f"{row[0]:<20} {row[1]:<10} {str(row[2]):<8} {row[3]:<10} {row[4][:30]}")


def _config_agents_show(config: Any, args: Any) -> None:
    """Show detailed information about a specific configuration-based agent."""
    from agent_system.plugins.config_agent_discovery import get_config_agent_info
    
    agent_name = args.agent_name
    
    try:
        info = get_config_agent_info(agent_name, config.agents)
    except KeyError:
        print(f"Error: Config agent '{agent_name}' not found", file=sys.stderr)
        sys.exit(1)
    
    if args.out_format == "json":
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        # Human-readable format
        print(f"\n{'=' * 60}")
        print(f"Config Agent: {info['name']}")
        print(f"{'=' * 60}")
        print(f"Enabled:      {info['enabled']}")
        print(f"Description:  {info.get('description', 'N/A')}")
        print(f"Base Type:    {info['base_type']}")
        print(f"LLM Profile:  {info['llm_profile']}")
        print(f"Max Steps:    {info['max_steps']}")
        
        if info.get('system_template'):
            print(f"Template:     {info['system_template']}")
        if info.get('has_inline_prompt'):
            print("Prompt:       [Inline prompt defined]")
        
        if info.get('tools'):
            print("\nTools:")
            allowed = info['tools'].get('allowed', [])
            blocked = info['tools'].get('blocked', [])
            if allowed:
                print(f"  Allowed:  {', '.join(allowed)}")
            else:
                print("  Allowed:  (none)")
            if blocked:
                print(f"  Blocked:  {', '.join(blocked)}")
        
        if info.get('context_management'):
            ctx = info['context_management']
            print("\nContext Management:")
            print(f"  Enabled:   {ctx.get('enabled', False)}")
            if ctx.get('enabled'):
                print(f"  Strategy:  {ctx.get('strategy', 'N/A')}")
                print(f"  Preserve:  {ctx.get('preserve_recent_messages', 'N/A')} messages")
        
        if info.get('metadata'):
            meta = info['metadata']
            if meta:
                print("\nMetadata:")
                for key, value in meta.items():
                    if isinstance(value, list):
                        print(f"  {key}: {', '.join(value)}")
                    else:
                        print(f"  {key}: {value}")
        
        print(f"{'=' * 60}\n")


def _config_agents_validate(config: Any, args: Any) -> None:
    """Validate configuration-based agents."""
    from agent_system.plugins.config_agent_validation import (
        validate_all_config_agents,
        validate_config_agent,
        get_validation_summary
    )
    
    # Get LLM profiles for validation
    llm_profiles = list(config.llm_system.profiles.keys()) if config.llm_system else None
    
    if args.agent_name:
        # Validate single agent
        agent_name = args.agent_name
        
        if not config.agents or agent_name not in config.agents:
            print(f"Error: Config agent '{agent_name}' not found", file=sys.stderr)
            sys.exit(1)
        
        definition = config.agents[agent_name]
        errors = validate_config_agent(agent_name, definition, llm_profiles)
        
        if args.out_format == "json":
            print(json.dumps({
                "agent_name": agent_name,
                "valid": len(errors) == 0,
                "errors": errors
            }, indent=2))
        else:
            if not errors:
                print(f"✅ Config agent '{agent_name}' is valid")
            else:
                print(f"❌ Config agent '{agent_name}' has {len(errors)} error(s):")
                for i, error in enumerate(errors, 1):
                    print(f"  {i}. {error}")
                sys.exit(1)
    else:
        # Validate all agents
        results = validate_all_config_agents(config.agents, llm_profiles)
        
        if args.out_format == "json":
            output = {
                "total": len(results),
                "passed": sum(1 for errors in results.values() if not errors),
                "failed": sum(1 for errors in results.values() if errors),
                "results": [
                    {"agent_name": name, "valid": len(errors) == 0, "errors": errors}
                    for name, errors in results.items()
                ]
            }
            print(json.dumps(output, indent=2))
        else:
            summary = get_validation_summary(results)
            print(summary)
            
            # Exit with error if any agent failed
            if any(errors for errors in results.values()):
                sys.exit(1)


def register_commands(subparsers: Any) -> None:
    """Register config-agents commands with the CLI parser."""
    
    # Main config-agents command group
    config_agents_parser = subparsers.add_parser(
        "config-agents",
        help="Manage configuration-based agents"
    )
    config_agents_subparsers = config_agents_parser.add_subparsers(dest="config_agents_cmd")
    
    # config-agents list
    list_parser = config_agents_subparsers.add_parser(
        "list",
        help="List all configuration-based agents"
    )
    list_parser.add_argument(
        "--format",
        dest="out_format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)"
    )
    list_parser.set_defaults(func=_config_agents_list)
    
    # config-agents show
    show_parser = config_agents_subparsers.add_parser(
        "show",
        help="Show detailed information about a config agent"
    )
    show_parser.add_argument(
        "agent_name",
        help="Name of the configuration-based agent"
    )
    show_parser.add_argument(
        "--format",
        dest="out_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)"
    )
    show_parser.set_defaults(func=_config_agents_show)
    
    # config-agents validate
    validate_parser = config_agents_subparsers.add_parser(
        "validate",
        help="Validate configuration-based agents"
    )
    validate_parser.add_argument(
        "agent_name",
        nargs="?",
        help="Name of the agent to validate (if not specified, validates all)"
    )
    validate_parser.add_argument(
        "--format",
        dest="out_format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)"
    )
    validate_parser.set_defaults(func=_config_agents_validate)
