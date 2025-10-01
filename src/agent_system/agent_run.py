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

from .config.settings import load_settings, get_mcp_config_by_name
from .mcp.base import MCPRegistry
from .servers.agent.server import Agent


logger = logging.getLogger(__name__)


def setup_basic_logging() -> None:
    """Setup basic logging for the agent runner."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s',
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
        logger.warning(f"Bootstrap failed: {e}")
        # Continue with empty registry - agent can still work without plugins
    
    return registry


async def create_agent(config, registry, agent_name: str):
    """Create and initialize the specified agent."""
    # Get the agent configuration with inheritance
    mcp_config = get_mcp_config_by_name(agent_name, config)
    
    if not mcp_config:
        raise ValueError(f"Agent '{agent_name}' not found in configuration")
    
    if not mcp_config.agent_config:
        raise ValueError(f"Agent '{agent_name}' has no agent_config section")
    
    # Create the agent using the new signature:
    # Agent(name, system_config, mcp_config, registry)
    agent = Agent(agent_name, config, mcp_config, registry)
    
    # Make agent public so it shows up in tool lists if needed
    agent._mcp_public = True
    
    # Register the agent in the registry
    registry.register(agent_name, agent)
    
    logger.info(f"Created agent '{agent_name}' with LLM profile '{mcp_config.agent_config.llm_profile}'")
    return agent


async def run_agent_request(agent: Agent, request: str) -> dict:
    """Execute a request with the agent and return the result."""
    try:
        logger.info(f"Executing request: {request[:100]}{'...' if len(request) > 100 else ''}")
        
        # Use the same pattern as CLI - collect final result from run_events
        from .servers.agent.result_utils import collect_final_result
        result = await collect_final_result(agent, request)
        
        return result
    
    except Exception as e:
        logger.error(f"Failed to execute request: {e}")
        raise


async def main_async(request: str, agent_name: str | None = None) -> None:
    """Main async function to execute the agent request."""
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
        
        # Execute the request
        logger.info("Executing request...")
        result = await run_agent_request(agent, request)
        
        # Print the result
        print("\n" + "="*50)
        print("AGENT RESPONSE:")
        print("="*50)
        
        # Extract summary or response from result
        if isinstance(result, dict):
            summary = result.get("summary", "")
            if summary:
                print(summary)
            else:
                # Print the whole result if no summary
                import json
                print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(str(result))
            
        print("="*50)
        
    except Exception as e:
        logger.error(f"Agent execution failed: {e}")
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
        """
    )
    
    parser.add_argument(
        "request",
        help="The request/question to send to the agent"
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    
    parser.add_argument(
        "--agent",
        help="Override the default agent (use agent name from config)"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(levelname)s: %(name)s: %(message)s',
            handlers=[logging.StreamHandler(sys.stdout)]
        )
    else:
        setup_basic_logging()
    
    # Run the async main function
    try:
        asyncio.run(main_async(args.request, args.agent))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()