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
from typing import TYPE_CHECKING

from .config.settings import load_settings
from .mcp.status import status_bus
from .servers.agent.server import Agent
from .services.session_manager import SessionPermissionError
from .cli_utils.common import (
    set_color_mode,
    status_subscriber,
    format_output_with_hooks,
    print_agent_response,
    format_error
)
from .llm.batch.initialization import init_batch_system, shutdown_batch_system

if TYPE_CHECKING:
    from .llm.models import ChatMessage


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
    """Initialize the MCP registry and load plugins using InitializationService."""
    # Use centralized initialization service
    from .services.initialization_service import InitializationService
    from .llm.factory import set_batch_config

    try:
        # Store config for lazy batch queue manager initialization
        # This allows LLMFactory to create the manager when first needed
        set_batch_config(config)
        
        init_service = InitializationService(config)
        registry, session_service = init_service.initialize_for_cli()
        logger.info(f"Initialization completed. Registry has {len(registry.list())} servers: {registry.list()}")
        
        # Start batch queue manager async tasks (if it was created)
        await init_batch_system(config)
        
        return registry, session_service
    except Exception as e:
        logger.warning(f"Initialization failed: {e}", exc_info=True)
        # Continue with minimal registry - agent can still work
        from .mcp.base import MCPRegistry
        return MCPRegistry(), None


async def create_agent(config, registry, agent_name: str, session_service=None):
    """Create and initialize the specified agent.

    This function is a wrapper around the shared cli_utils.agent_runner.create_and_register_agent
    to maintain backward compatibility with existing code.

    Args:
        config: System configuration
        registry: MCP registry
        agent_name: Name of agent to create
        session_service: Optional SessionService to inject into agent
    """
    from .cli_utils.agent_runner import create_and_register_agent
    return await create_and_register_agent(config, registry, agent_name, session_service=session_service)


async def run_agent_request(agent: Agent, request: str | "ChatMessage", session_id: str, llm_override=None, llm_profile_info: str | None = None) -> dict:
    """Execute a request with the agent and return the result.

    Args:
        agent: The agent instance to execute the request with
        request: The user's request/question (string or ChatMessage for multimodal)
        session_id: Session ID for conversation history
        llm_override: Optional LLM client to override agent's default
        llm_profile_info: Optional profile info string for status display
    """
    try:
        # Log request info (handle both string and ChatMessage)
        request_preview = request if isinstance(request, str) else f"<multimodal message with {len(request.content)} parts>"
        logger.info(f"Executing request: {str(request_preview)[:100]}{'...' if len(str(request_preview)) > 100 else ''}")

        # Use the same pattern as CLI - collect final result from run_events
        from .servers.agent.result_utils import collect_final_result
        result = await collect_final_result(
            agent,
            request,
            session_id=session_id,  # Pass session_id for conversation history
            llm_override=llm_override,
            llm_profile_info_override=llm_profile_info
        )

        return result

    except Exception as e:
        logger.error(f"Failed to execute agent request: {e}", exc_info=True)
        raise


async def main_async(request: str, agent_name: str | None = None, llm_profile: str | None = None, show_status: bool = True,
                     session_id: str | None = None, session_user: str = "cli_user",
                     list_sessions: bool = False, session_title: str | None = None,
                     image_paths: list[str] | None = None, audio_paths: list[str] | None = None,
                     text_file_paths: list[str] | None = None) -> None:
    """Async main function to run agent request with session support.

    Args:
        request: The request to send to the agent
        agent_name: Override default agent (optional)
        llm_profile: Override LLM profile (optional)
        show_status: Whether to display status messages
        session_id: Session ID to continue (optional)
        session_user: User ID for session storage
        list_sessions: List all sessions for user
        session_title: Title for new session (optional)
        image_paths: List of image file paths to attach (optional)
        audio_paths: List of audio file paths to attach (optional)
        text_file_paths: List of text file paths to attach (optional)
    """
    try:
        # Handle --list-sessions flag (needs session_manager only)
        if list_sessions:
            from pathlib import Path as PathLib
            from .services.session_manager import SessionManager

            storage_path = PathLib(__file__).parents[2] / "data" / "sessions"
            session_manager = SessionManager(storage_path=str(storage_path))

            sessions = await session_manager.list_sessions(session_user)

            if not sessions:
                print(f"No sessions found for user '{session_user}'")
                return

            print(f"\nSessions for user '{session_user}':")
            print("-" * 80)
            for sess in sessions:
                sess_id = sess.get("session_id", "unknown")
                title = sess.get("title", "Untitled")
                agent = sess.get("agent_name", "unknown")
                llm = sess.get("llm_profile", "unknown")
                created = sess.get("created_at", "unknown")
                msg_count = sess.get("message_count", len(sess.get("messages", [])))  # Use message_count from metadata

                print(f"ID: {sess_id}")
                print(f"  Title: {title}")
                print(f"  Agent: {agent}, LLM: {llm}")
                print(f"  Messages: {msg_count}, Created: {created}")
                print()
            return

        # Load configuration
        logger.info("Loading configuration...")
        config = load_settings()
        logger.info(f"Loaded config for {config.name} v{config.version}")

        # Initialize system (plugins, registry) - now returns session_service too
        logger.info("Initializing system...")
        registry, session_service = await initialize_system(config)

        # Get agent name from argument or use default
        if agent_name is None:
            agent_name = config.default_agent
        logger.info(f"Using agent: {agent_name}")

        # Create and initialize agent
        logger.info("Creating agent...")
        agent = await create_agent(config, registry, agent_name, session_service=session_service)

        # Generate or use provided session ID
        from .utils.id import short_id
        actual_session_id = session_id or short_id()

        # Load existing session if --session provided, otherwise initialize empty
        session_exists = False
        was_new_session = False

        if session_id:
            logger.info(f"Loading session: {session_id}")
            try:
                session_exists, msg_count = await session_service.load_and_restore_session(
                    agent, session_user, session_id
                )
                if session_exists:
                    logger.info(f"Loaded session {session_id} with {msg_count} messages")
                    print(f"Continuing session '{session_id}' ({msg_count} messages)")
                    was_new_session = False
                else:
                    # Session ID provided but doesn't exist - create it
                    logger.info(f"Session '{session_id}' not found, creating new session with this ID")
                    print(f"Creating new session '{session_id}'")
                    agent._session_tracker.set_session_messages(actual_session_id, [])
                    was_new_session = True  # Will be saved at end
            except SessionPermissionError as e:
                # User trying to access session they don't own
                logger.error(f"Permission denied for session {session_id}: {e}")
                print(f"Error: {e}", file=sys.stderr)
                print("This session belongs to a different user. Use a different session ID.", file=sys.stderr)
                return
            except Exception as e:
                logger.error(f"Failed to load session {session_id}: {e}", exc_info=True)
                print(f"Error loading session: {e}", file=sys.stderr)
                return
        else:
            # No session ID provided - create new one with auto-generated ID
            logger.debug(f"Creating new session: {actual_session_id}")
            agent._session_tracker.set_session_messages(actual_session_id, [])
            was_new_session = True

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
                # Use factory function that properly handles batch mode
                from .llm.factory import create_llm_from_profile, resolve_llm_config_for_agent
                from .config.models import AgentConfig

                llm_override = create_llm_from_profile(
                    config=config,
                    llm_profile=llm_profile,
                )

                # Get profile info for status display
                temp_agent_config = AgentConfig(llm_profile=llm_profile)
                llm_kwargs = resolve_llm_config_for_agent(config, temp_agent_config)
                model = llm_kwargs.get('model', 'unknown')
                provider = llm_kwargs.get('provider', 'unknown')
                llm_profile_info = f"{llm_profile}:{provider}/{model}"

                logger.info(f"Using LLM override: {llm_profile_info}")
            except Exception as e:
                logger.error(f"Failed to create LLM override: {e}", exc_info=True)
                raise ValueError(f"Failed to apply LLM profile '{llm_profile}': {str(e)}")

        # Set session metadata for tool execution context (AFTER LLM override logic)
        # This ensures user_id is available when tools are called
        effective_llm_profile = llm_profile or agent.agent_config.default_llm_profile
        agent._session_tracker.set_session_metadata(actual_session_id, {
            "user_id": session_user,
            "agent_name": agent.name,
            "llm_profile": effective_llm_profile
        })

        # Process multimodal attachments (images, audio, text files)
        from typing import Union
        task_input: Union[str, ChatMessage] = request
        
        if image_paths or audio_paths or text_file_paths:
            attachment_counts = []
            if image_paths:
                attachment_counts.append(f"{len(image_paths)} image(s)")
            if audio_paths:
                attachment_counts.append(f"{len(audio_paths)} audio(s)")
            if text_file_paths:
                attachment_counts.append(f"{len(text_file_paths)} text file(s)")
            logger.info(f"Processing attachments: {', '.join(attachment_counts)}")
            
            try:
                from pathlib import Path as PathLib
                from .utils.multimodal_processor import (
                    create_multimodal_message_extended,
                    ImageProcessingError,
                    AudioProcessingError,
                    TextFileProcessingError
                )

                # Convert string paths to Path objects
                images = [PathLib(p) for p in image_paths] if image_paths else None
                audios = [PathLib(p) for p in audio_paths] if audio_paths else None
                texts = [PathLib(p) for p in text_file_paths] if text_file_paths else None

                # Create multimodal message with all attachment types
                task_input = create_multimodal_message_extended(
                    text=request,
                    image_paths=images,
                    audio_paths=audios,
                    text_file_paths=texts,
                )

                logger.info("Created multimodal message")

            except ImageProcessingError as e:
                print(f"Error processing image: {e}", file=sys.stderr)
                return
            except AudioProcessingError as e:
                print(f"Error processing audio: {e}", file=sys.stderr)
                return
            except TextFileProcessingError as e:
                print(f"Error processing text file: {e}", file=sys.stderr)
                return
            except Exception as e:
                print(f"Error processing attachments: {e}", file=sys.stderr)
                logger.exception("Unexpected error in multimodal processing")
                return

        # Subscribe to status events if enabled
        status_queue = None
        status_task = None
        if show_status:
            status_queue = await status_bus.subscribe()
            status_task = asyncio.create_task(status_subscriber(status_queue))

        # Execute the request
        logger.info("Executing request...")
        try:
            result = await run_agent_request(agent, task_input, actual_session_id, llm_override, llm_profile_info)
            # Check if agent was cancelled and print message
            if result.get("cancelled", False):
                msg = "\n✋ Cancelled by user"
                from .cli_utils.common import supports_color, colorize
                if supports_color():
                    msg = colorize(msg, "33")  # yellow
                print(msg)
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Direct Ctrl-C (rare, usually caught by agent)
            msg = "\n✋ Cancelled by user"
            from .cli_utils.common import supports_color, colorize
            if supports_color():
                msg = colorize(msg, "33")  # yellow
            print(msg)
            result = {"task": request, "cancelled": True, "summary": ""}
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

        # Save session after successful request execution (skip if cancelled)
        if not result.get("cancelled", False):
            try:
                # Use the actual agent name that was requested (from parameter or config.default_agent)
                # instead of agent.agent_name which may not exist or be "default"
                agent_name_used = agent_name  # Already determined from args or config.default_agent at line 172-174
                llm_profile_used = llm_profile or agent.agent_config.default_llm_profile

                # Save the session
                success = await session_service.save_session(
                    agent=agent,
                    user_id=session_user,
                    session_id=actual_session_id,
                    agent_name=agent_name_used,
                    llm_profile=llm_profile_used,
                    was_new_session=was_new_session,
                    title=session_title
                )

                if success:
                    if session_id:
                        logger.info(f"Updated session {session_id}")
                    else:
                        logger.info(f"Created new session {actual_session_id}")
                        print(f"\nSession saved: {actual_session_id}")
                else:
                    logger.warning("Session save returned False")
            except Exception as e:
                logger.error(f"Failed to save session: {e}", exc_info=True)
                print(f"Warning: Failed to save session: {e}", file=sys.stderr)

        # Print the result with formatting based on --color setting (skip if cancelled)
        if not result.get("cancelled", False):
            # Extract summary from result
            summary = result.get("summary", "") if isinstance(result, dict) else str(result)

            if summary:
                formatted_summary, content_format = await format_output_with_hooks(
                    output=summary,
                    agent_instance=agent,
                    session_id=actual_session_id,
                    request_id="agent_run",
                    output_format='ansi'  # Request ANSI format for terminal display
                )

                print_agent_response(formatted_summary, content_format)
            else:
                import json
                print(json.dumps(result, indent=2, ensure_ascii=False))

    except ValueError as e:
        # User-friendly error for common issues (agent not found, etc.)
        print(format_error(str(e)), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Agent execution failed: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Shutdown batch queue manager if it was started
        await shutdown_batch_system()


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
        nargs="?",  # Make request optional
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
        choices=["auto", "always", "never", "ansi", "html", "text"],
        # Default "auto", not "always": "always" emitted escape sequences into
        # redirected output (agent_cli.py fixed this first).
        default="auto",
        help="Output format: auto=ANSI if TTY, always/ansi=ANSI colors, html=HTML, never/text=plain text"
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

    parser.add_argument(
        "--session",
        dest="session_id",
        help="Continue an existing session by ID"
    )

    parser.add_argument(
        "--session-user",
        dest="session_user",
        default="cli_user",
        help="User ID for session storage (default: cli_user)"
    )

    parser.add_argument(
        "--list-sessions",
        dest="list_sessions",
        action="store_true",
        help="List all sessions for the current user"
    )

    parser.add_argument(
        "--session-title",
        dest="session_title",
        help="Title for the new session (auto-generated from request if not provided)"
    )

    parser.add_argument(
        "--images", "--attach",
        dest="images",
        nargs="+",
        metavar="PATH",
        help="Path(s) to image file(s) to attach to the request"
    )

    parser.add_argument(
        "--audio",
        dest="audio",
        nargs="+",
        metavar="PATH",
        help="Path(s) to audio file(s) to attach to the request (mp3, wav, ogg, etc.)"
    )

    parser.add_argument(
        "--text", "--files",
        dest="text_files",
        nargs="+",
        metavar="PATH",
        help="Path(s) to text file(s) to attach to the request (txt, md, py, json, etc.)"
    )

    args = parser.parse_args()

    # Validate that either --list-sessions or request is provided
    if not args.list_sessions and not args.request:
        parser.error("Either 'request' or --list-sessions must be provided")

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
        asyncio.run(main_async(
            request=args.request,
            agent_name=args.agent,
            llm_profile=args.llm_profile,
            show_status=show_status,
            session_id=getattr(args, "session_id", None),
            session_user=getattr(args, "session_user", "cli_user"),
            list_sessions=getattr(args, "list_sessions", False),
            session_title=getattr(args, "session_title", None),
            image_paths=getattr(args, "images", None),
            audio_paths=getattr(args, "audio", None),
            text_file_paths=getattr(args, "text_files", None)
        ))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()