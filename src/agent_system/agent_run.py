#!/usr/bin/env python3
"""
Simple Agent Runner - agent-run

A lightweight CLI tool to execute agent requests quickly.

Usage:
    agent-run "Your request here"
    
This tool:
1. Loads the configuration from config files
2. Discovers and loads plugins 
3. Starts the default agent defined in config
4. Executes the request and returns the result
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config.settings import load_settings
from .mcp.base import MCPRegistry
from .mcp.status import status_bus
from .servers.agent.server import Agent
from .cli_utils.common import (
    set_color_mode,
    status_subscriber, format_result_output, format_error
)


logger = logging.getLogger(__name__)


def setup_basic_logging(verbose: bool = False) -> None:
    """Setup basic logging for the agent runner.
    
    Args:
        verbose: If True, set level to DEBUG. Otherwise WARNING.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format='%(levelname)s: %(message)s' if not verbose else '%(levelname)s: %(name)s: %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )


async def initialize_system(config):
    """Initialize the MCP registry and load plugins using the same bootstrap process as CLI."""
    # Create MCP registry
    registry = MCPRegistry()
    
    # Use the same bootstrap process as CLI
    from .servers.bootstrap import bootstrap_servers
    
    try:
        # Bootstrap all servers and plugins
        bootstrap_servers(config, registry)
        logger.info(f"Bootstrap completed. Registry has {len(registry.list())} servers: {registry.list()}")
    except Exception as e:
        logger.warning(f"Bootstrap failed: {e}", exc_info=True)
        # Continue with empty registry - agent can still work without plugins
    
    return registry


async def create_agent(config, registry, agent_name: str):
    """Create and initialize the specified agent.
    
    This function is a wrapper around the shared cli_utils.agent_runner.create_and_register_agent
    to maintain backward compatibility with existing code.
    """
    from .cli_utils.agent_runner import create_and_register_agent
    return await create_and_register_agent(config, registry, agent_name)


async def run_agent_request(agent: Agent, request: str, llm_override=None, llm_profile_info: str | None = None) -> dict:
    """Execute a request with the agent and return the result.
    
    Args:
        agent: The agent instance to execute the request with
        request: The user's request/question
        llm_override: Optional LLM client to override agent's default
        llm_profile_info: Optional profile info string for status display
    """
    try:
        logger.info(f"Executing request: {request[:100]}{'...' if len(request) > 100 else ''}")
        
        # Use the same pattern as CLI - collect final result from run_events
        from .servers.agent.result_utils import collect_final_result
        result = await collect_final_result(
            agent, 
            request, 
            llm_override=llm_override,
            llm_profile_info_override=llm_profile_info
        )
        
        return result
    
    except Exception as e:
        logger.error(f"Failed to execute agent request: {e}", exc_info=True)
        raise


async def main_async(
    request: str, 
    agent_name: str | None = None, 
    llm_profile: str | None = None,
    show_status: bool = True
) -> None:
    """Main async function to execute the agent request.
    
    Args:
        request: The user's request/question
        agent_name: Optional agent name to use (defaults to config.default_agent)
        llm_profile: Optional LLM profile to override agent's default
        show_status: Whether to display status messages (default: True)
    """
    try:
        # Load configuration
        logger.info("Loading configuration...")
        config = load_settings()
        logger.info(f"Loaded config for {config.name} v{config.version}")
        
        # Initialize system (plugins, registry)
        logger.info("Initializing system...")
        registry = await initialize_system(config)
        
        # Get agent name from argument or use default
        if agent_name is None:
            agent_name = config.default_agent
        logger.info(f"Using agent: {agent_name}")
        
        # Create and initialize agent
        logger.info("Creating agent...")
        agent = await create_agent(config, registry, agent_name)
        
        # Create LLM override if profile specified
        llm_override = None
        llm_profile_info = None
        if llm_profile and config.llm_system and config.llm_system.profiles:
            if llm_profile not in config.llm_system.profiles:
                error_msg = f"LLM profile '{llm_profile}' not found in configuration."
                available_profiles = sorted(config.llm_system.profiles.keys())
                if available_profiles:
                    error_msg += "\n\nAvailable profiles:\n  " + "\n  ".join(available_profiles)
                raise ValueError(error_msg)
            
            try:
                # Resolve profile to model config using the factory
                from .llm.factory import resolve_llm_config_for_agent
                from .config.models import AgentConfig
                
                # Create temporary agent config with override profile
                temp_agent_config = AgentConfig(llm_profile=llm_profile)
                llm_kwargs = resolve_llm_config_for_agent(config, temp_agent_config)
                
                # Create new LLM with resolved config
                from .llm.clients import make_llm
                llm_override = make_llm(**llm_kwargs)
                
                # Build profile info string for status display
                model = llm_kwargs.get('model', 'unknown')
                provider = llm_kwargs.get('provider', 'unknown')
                llm_profile_info = f"{llm_profile}:{provider}/{model}"
                
                logger.info(f"Using LLM override: {llm_profile_info}")
            except Exception as e:
                logger.error(f"Failed to create LLM override: {e}", exc_info=True)
                raise ValueError(f"Failed to apply LLM profile '{llm_profile}': {str(e)}")
        
        # Subscribe to status events if enabled
        status_queue = None
        status_task = None
        if show_status:
            status_queue = await status_bus.subscribe()
            status_task = asyncio.create_task(status_subscriber(status_queue))
        
        # Execute the request
        logger.info("Executing request...")
        try:
            result = await run_agent_request(agent, request, llm_override, llm_profile_info)
        finally:
            # Cancel status subscriber
            if status_task:
                status_task.cancel()
                try:
                    await status_task
                except asyncio.CancelledError:
                    pass
            if status_queue:
                status_bus.unsubscribe(status_queue)  # Not async!
        
        # Print the result
        print(format_result_output(result))
        
    except ValueError as e:
        # User-friendly error for common issues (agent not found, etc.)
        print(format_error(str(e)), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Agent execution failed: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


def main() -> None:
    """Main entry point for the agent-run CLI tool."""
    parser = argparse.ArgumentParser(
        description="Simple Agent Runner - Execute requests with the default agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    agent-run "What's the weather like today?"
    agent-run "Search for information about Python MCP protocol"
    agent-run "Help me analyze this data: [1, 2, 3, 4, 5]"
    agent-run --agent sysadmin_agent "List all SSH servers"
    agent-run --llm fast "Quick question about Python"
    agent-run --agent financial_analyst_agent --llm smart "Analyze AAPL stock"
    agent-run --no-status "What time is it?"
    agent-run --no-color "Tell me a joke"
        """
    )
    
    parser.add_argument(
        "request",
        help="The request/question to send to the agent"
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging (DEBUG level)"
    )
    
    parser.add_argument(
        "--agent",
        help="Override the default agent (use agent name from config)"
    )
    
    parser.add_argument(
        "--llm",
        dest="llm_profile",
        help="Override the LLM profile (use profile name from llm.yaml)"
    )
    
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="always",
        help="Control color output (default: always)"
    )
    
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable color output (same as --color=never)"
    )
    
    parser.add_argument(
        "--no-status",
        action="store_true",
        help="Disable status messages during execution"
    )
    
    args = parser.parse_args()
    
    # Set color mode globally
    if args.no_color:
        set_color_mode("never")
    else:
        set_color_mode(args.color)
    
    # Initialize colorama on Windows for ANSI color support
    try:
        if args.color != "never" and sys.stdout.isatty():
            import colorama
            colorama.init()
    except Exception as e:
        logger.debug(f"Failed to initialize colorama: {e}")
    
    # Setup logging
    setup_basic_logging(verbose=args.verbose)
    
    # Determine whether to show status
    show_status = not args.no_status
    
    # Run the async main function
    try:
        asyncio.run(main_async(args.request, args.agent, args.llm_profile, show_status))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()