"""Agent execution commands for the CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from typing import Any

logger = logging.getLogger(__name__)


async def _agent_run(args: Any) -> None:
    """Run the agent with the specified configuration."""
    from agent_system.servers.agent.server import Agent
    from agent_system.config import load_settings

    try:
        # Load configuration
        config = load_settings(getattr(args, 'config', None))

        # Create and run agent
        agent = Agent(config)

        # Set up signal handlers for graceful shutdown
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, shutting down agent...")
            asyncio.create_task(agent.stop())

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Run the agent
        logger.info("Starting agent...")
        await agent.run()

    except KeyboardInterrupt:
        logger.info("Agent interrupted by user")
    except Exception as e:
        logger.error(f"Failed to run agent: {e}")
        print(json.dumps({"error": f"Failed to run agent: {str(e)}"}, ensure_ascii=False))
        sys.exit(1)


async def _agent_status(args: Any) -> None:
    """Show the current status of the agent."""
    # This would check if an agent is currently running
    # For now, just return a placeholder
    print(json.dumps({"status": "not_running", "message": "Agent status checking not yet implemented"}, ensure_ascii=False))


async def _agent_stop(args: Any) -> None:
    """Stop a running agent."""
    # This would send a stop signal to a running agent
    # For now, just return a placeholder
    print(json.dumps({"message": "Agent stop command not yet implemented"}, ensure_ascii=False))


async def handle_agent_command(args: Any) -> None:
    """Handle agent subcommands."""
    action = getattr(args, 'action', 'run')

    if action == "run":
        await _agent_run(args)
    elif action == "status":
        await _agent_status(args)
    elif action == "stop":
        await _agent_stop(args)
    else:
        print(json.dumps({"error": f"Unknown agent action: {action}"}, ensure_ascii=False))