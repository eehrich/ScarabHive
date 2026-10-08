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
from .paths import enter_project
from .tools.status import status_bus
from .servers.agent.server import Agent
from .services.session_manager import SessionPermissionError
from .cli_utils.session_defaults import (
    choose_agent_name,
    choose_llm_profile,
    load_session_llm_params,
    profile_for_record,
    session_defaults,
)
from .cli_utils.attachments import greedy_attach_hint, sort_attachments
from .cli_utils.session_listing import DEFAULT_LIMIT, parse_listing, print_sessions
from .cli_utils.common import (
    set_color_mode,
    status_subscriber,
    print_agent_response,
    format_error
)
from .llm.batch.initialization import init_batch_system, shutdown_batch_system
from .tools.integration import shutdown_tools

if TYPE_CHECKING:
    from .llm.models import ChatMessage


logger = logging.getLogger(__name__)


def setup_basic_logging(verbose: bool = False) -> None:
    """Setup basic logging for the agent runner.

    Args:
        verbose: If True, set level to DEBUG. Otherwise WARNING.
    """
    from .utils.logging import LibraryDebugFilter

    level = logging.DEBUG if verbose else logging.WARNING
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(LibraryDebugFilter())
    logging.basicConfig(
        level=level,
        format='%(levelname)s: %(message)s' if not verbose else '%(levelname)s: %(name)s: %(message)s',
        handlers=[handler]
    )


async def initialize_system(config):
    """Initialize the tool registry and load plugins using InitializationService."""
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
        from .tools.base import ToolServerRegistry
        return ToolServerRegistry(), None


async def create_agent(config, registry, agent_name: str, session_service=None):
    """Create and initialize the specified agent.

    This function is a wrapper around the shared cli_utils.agent_runner.create_and_register_agent
    to maintain backward compatibility with existing code.

    Args:
        config: System configuration
        registry: tool registry
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
    from .utils.id import short_id

    request_id = short_id()
    try:
        # Log request info (handle both string and ChatMessage)
        request_preview = request if isinstance(request, str) else f"<multimodal message with {len(request.content)} parts>"
        logger.info(f"Executing request: {str(request_preview)[:100]}{'...' if len(str(request_preview)) > 100 else ''}")

        # Use the same pattern as CLI - collect final result from run_events
        from .servers.agent.result_utils import collect_final_result
        result = await collect_final_result(
            agent,
            request,
            request_id=request_id,
            session_id=session_id,  # Pass session_id for conversation history
            llm_override=llm_override,
            llm_profile_info_override=llm_profile_info
        )

        return result

    except Exception as e:
        logger.error(f"Failed to execute agent request: {e}", exc_info=True)
        raise
    finally:
        # Named here so it can be let go of: what the run registered under it
        # (a tool call, a preloaded tool, a sub-agent) goes with the run, as the
        # API lets go of its request tree when the request ends.
        from .core.request_context import release_request_user_tree
        release_request_user_tree(request_id)


async def main_async(request: str, agent_name: str | None = None, llm_profile: str | None = None, show_status: bool = True,
                     session_id: str | None = None, session_user: str = "cli_user",
                     list_sessions: str | None = None, session_title: str | None = None,
                     attachments: list[str] | None = None, force: bool = False) -> None:
    """Async main function to run agent request with session support.

    Args:
        request: The request to send to the agent
        agent_name: Override default agent (optional)
        llm_profile: Override LLM profile (optional)
        show_status: Whether to display status messages
        session_id: Session ID to continue (optional)
        session_user: User ID for session storage
        list_sessions: Count of sessions to list instead of running (0 = all,
            "" for the default); None runs the request
        session_title: Title for new session (optional)
        attachments: Files to attach; the kind of each is detected, not declared
        force: Run the session even though another process holds it
    """
    presence = None
    stopped = False   # Ctrl-C: its user stopped the run (core/session_presence.py)
    try:
        # Handle --list-sessions flag (needs session_manager only)
        if list_sessions is not None:
            from .core.session_presence import sessions_dir
            from .services.session_manager import SessionManager

            # The store every run uses (AGENT_SESSION_STORAGE_PATH, else the
            # data directory's sessions): this listing used to ignore the
            # variable and read the checkout's sessions.
            session_manager = SessionManager(storage_path=str(sessions_dir()))

            # Every session, pipeline runs included: which agents the chat
            # offers is in the configuration, and this listing deliberately
            # reads none -- a config that does not load must not hide it.
            # `all` is taken without a complaint, it is what this lists anyway.
            limit, _, complaint = parse_listing(list_sessions)
            if complaint:
                print(f"Ignoring '{complaint}': --list-sessions takes a count or 'all'.")
            await print_sessions(
                session_manager, session_user,
                limit=limit,
                current_session_id=session_id,
                more_hint="--list-sessions <count>, --list-sessions 0 for no limit",
                # <id>: agent-run takes --session as it is, titles are agent-cli's
                footer="Continue one with: --session <id>",
            )
            return

        # Load configuration
        logger.info("Loading configuration...")
        config = load_settings()
        logger.info(f"Loaded config for {config.name} v{config.version}")

        # Initialize system (plugins, registry) - now returns session_service too
        logger.info("Initializing system...")
        registry, session_service = await initialize_system(config)

        # What this session was started with, when one is being continued.
        # Same rules as agent-cli (cli_utils.session_defaults) on purpose: the
        # two entry points share a session, and answering "which agent, which
        # model" differently made every agent-run overwrite what agent-cli had
        # stored there.
        # getattr, not a dot: initialize_system returns session_service=None on
        # its degraded path ("Continue with minimal registry - agent can still
        # work"), and reaching through it here would raise BEFORE the agent is
        # built -- turning a run that used to answer into an exit 1. The None
        # lands on the guard in load_session_settings.
        stored_agent, stored_llm = await session_defaults(
            getattr(session_service, "session_manager", None),
            session_user, session_id, config)

        # Get agent name from argument, from the session, or use the default
        agent_name = choose_agent_name(agent_name, stored_agent, config.default_agent)
        logger.info(f"Using agent: {agent_name}")

        # Create and initialize agent
        logger.info("Creating agent...")
        agent = await create_agent(config, registry, agent_name, session_service=session_service)

        # Generate or use provided session ID
        from .utils.id import short_id
        actual_session_id = session_id or short_id()

        # Session presence (core/session_presence.py): the session is held
        # BEFORE it is loaded -- a run that reads the file first can be
        # overtaken by the process holding it and would write its own copy back
        # over that run. Held through the save after the run (see the finally).
        from .core.session_presence import SessionBusy, presence_for
        presence = presence_for(config)
        if presence:
            try:
                presence.hold(actual_session_id, session_user, agent_name)
            except SessionBusy as busy:
                if not force:
                    print(f"Error: {busy}.", file=sys.stderr)
                    print("Wait for it to finish, or pass --force if its lock is a leftover.",
                          file=sys.stderr)
                    sys.exit(1)
                print(f"Warning: {busy}; running it anyway (--force).", file=sys.stderr)

        # Continue on the model the session was started with (see
        # choose_llm_profile for what that does and does not outrank).
        llm_profile = choose_llm_profile(
            llm_profile, stored_llm, stored_agent, agent_name,
            agent.agent_config.default_llm_profile)
        record_profile = profile_for_record(llm_profile, agent.agent_config.default_llm_profile)
        # And its llm_params (a chat's /think, the web chat's thinking level), for the agent they were set for
        llm_params = (await load_session_llm_params(
            getattr(session_service, "session_manager", None), session_user, session_id)
            if stored_agent and stored_agent == agent_name else None) or {}
        llm_choice = {"profile": llm_profile, "params": llm_params}

        # Open the session the way the API and agent-cli do.
        if session_service is not None:
            try:
                session_exists = await session_service.open_for_run(
                    agent, session_user, actual_session_id, record_profile, llm_choice=llm_choice)
            except SessionPermissionError as e:
                # Exit 1, not return: a refused run is not a finished one.
                logger.error(f"Permission denied for session {actual_session_id}: {e}")
                print(f"Error: {e}", file=sys.stderr)
                print("This session belongs to a different user. Use a different session ID.", file=sys.stderr)
                sys.exit(1)
        elif session_id:
            # The degraded bootstrap (initialize_system) has no store: a session
            # named to continue would run without its history and not be saved.
            print(f"Error: cannot continue session '{session_id}' -- the session store did not "
                  f"start (see the log).", file=sys.stderr)
            sys.exit(1)
        else:
            # The degraded bootstrap without a session to continue: the run
            # still answers, and still names its user to the tools.
            session_exists = False
            agent._session_tracker.set_session_metadata(actual_session_id, {
                "user_id": session_user, "agent_name": agent.name, "llm_profile": record_profile})
        was_new_session = not session_exists
        if session_exists:
            count = len(agent._session_tracker.get_session_messages(actual_session_id) or [])
            logger.info(f"Loaded session {actual_session_id} with {count} messages")
            print(f"Continuing session '{actual_session_id}' ({count} messages)")
        elif session_id:
            logger.info(f"Session '{session_id}' not found, creating new session with this ID")
            print(f"Creating new session '{session_id}'")

        # Create LLM override if profile specified
        llm_override = None
        llm_profile_info = None
        if (llm_profile or llm_params) and config.llm_system and config.llm_system.profiles:
            from .llm.factory import UnknownLLMProfile, override_for_profile
            try:
                # params alone apply to the agent's own primary, as --llm-params do in agent-cli
                llm_override, llm_profile_info = override_for_profile(
                    config, agent.agent_config, llm_profile or agent.agent_config.default_llm_profile,
                    llm_params or None)
                logger.info(f"Using LLM override: {llm_profile_info}")
            except UnknownLLMProfile:
                raise   # a ValueError whose message lists the profiles there are
            except Exception as e:
                logger.error(f"Failed to create LLM override: {e}", exc_info=True)
                raise ValueError(f"Failed to apply LLM profile '{llm_profile}': {str(e)}")

        # Process multimodal attachments (images, audio, text files)
        from typing import Union
        task_input: Union[str, ChatMessage] = request
        
        sorted_attachments, attachment_problems = sort_attachments(attachments or [])
        if attachment_problems:
            raise ValueError("; ".join(attachment_problems))
        if any(sorted_attachments.values()):
            # Checked against the model this run will use -- the --llm override
            # wins. AttachmentRejected is a ValueError: the handler below says
            # it and exits 1 -- publish_pipeline and the writer runners read
            # the code.
            from .utils.multimodal_processor import message_with_attachments
            task_input = message_with_attachments(request, sorted_attachments, llm_override, agent)

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
        if result.get("cancelled", False):
            # Its task was cancelled, not the run: the run cannot tell, this hold
            # can, and notes it for the run it held around.
            stopped = True

        # Save session after successful request execution (skip if cancelled, or refused before it ran:
        # the agent's role gate, another user's session, another run's lock -- it ran nothing, and a save
        # only rewrote the record with this entry agent and profile, its updated_at moved)
        if not result.get("cancelled", False) and not result.get("refused"):
            try:
                # Use the actual agent name that was requested (from parameter or config.default_agent)
                # instead of agent.agent_name which may not exist or be "default"
                agent_name_used = agent_name  # From the argument, the session, or the default
                llm_profile_used = profile_for_record(
                    llm_profile, agent.agent_config.default_llm_profile)

                # Save the session
                success = await session_service.save_session(
                    agent=agent,
                    user_id=session_user,
                    session_id=actual_session_id,
                    agent_name=agent_name_used,
                    llm_profile=llm_profile_used,
                    was_new_session=was_new_session,
                    title=session_title,
                    llm_choice=llm_choice,
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
                print_agent_response(summary)
            else:
                import json
                # default=str: a non-JSON tool value must not fail a finished run.
                print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    except (asyncio.CancelledError, KeyboardInterrupt):
        stopped = True   # before or around the run: its user stopped it all the same
        raise
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
        if presence:
            presence.release(actual_session_id, session_user, stopped=stopped)
        # Shutdown batch queue manager if it was started
        await shutdown_batch_system()
        # The tool integration is this PROCESS's, not the agent's: the agent
        # only set up the module-level singleton because it asked first. So
        # the entry point takes it down, as agent-cli and the app do -- this
        # stops every plugin (a terminal's background processes, an SSH
        # channel, file_ops' indexer) instead of leaving them to whatever the
        # interpreter's exit happens to reach. A wake run is this process too.
        try:
            await shutdown_tools()
        except Exception as e:  # noqa: BLE001 - the run is over; say it, don't fail it
            logger.warning("Failed to shut down the tool integration: %s", e)


def main() -> None:
    """The agent-run entry point: a local process, run by whoever operates the
    installation -- so the agent role gate takes its default user, cli_user,
    for the local operator (auth/agent_access.local_operator_trusted). The
    API process never does."""
    from .auth.agent_access import local_operator_trusted

    with local_operator_trusted():
        _main()


def _main() -> None:
    """Main entry point for the agent-run CLI tool."""
    # Same as agent-cli: run from the repository whatever directory this was
    # started in, and keep where the person started for the paths they typed.
    enter_project()
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

    # nargs="?" without type=int on purpose: argparse fills an optional's slot
    # from the next token BEFORE converting it, so `--list-sessions "what is
    # going on"` would die on int() instead of listing -- which is what the
    # store_true version did. parse_limit sorts the count from the text after.
    parser.add_argument(
        "--list-sessions",
        dest="list_sessions",
        nargs="?",
        const="",
        default=None,
        metavar="COUNT|all",
        help=f"List this user's sessions, one line each (default {DEFAULT_LIMIT}, 0 = no limit; "
             "'all' is taken, this lists everything anyway). "
             "Sub-agent sessions are not listed."
    )

    parser.add_argument(
        "--session-title",
        dest="session_title",
        help="Title for the new session (auto-generated from request if not provided)"
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Run the session even though another process holds it "
             "(for a lock left behind by a process that hangs)"
    )

    # One flag for every kind of file, like `/attach` in the chat: the kind is
    # read from the file, not from which flag was typed (cli_utils.attachments).
    # The three old flags still work for anyone's shell history but are out of
    # the help; they land in the same list and are sorted the same way.
    parser.add_argument(
        "--attach",
        dest="attachments",
        nargs="+",
        metavar="PATH",
        action="extend",
        default=None,
        help="File(s) to attach to the request -- images, audio or text; "
             "the kind is detected per file"
    )

    parser.add_argument(
        "--images", "--audio", "--text", "--files",
        dest="attachments",
        nargs="+",
        metavar="PATH",
        action="extend",
        default=None,
        help=argparse.SUPPRESS
    )

    args = parser.parse_args()

    # Validate that either --list-sessions or request is provided
    if args.list_sessions is None and not args.request:
        # Say WHY there is no request when --attach swallowed it, instead of
        # sending someone to the usage line for a command they typed in full.
        hint = greedy_attach_hint(args.attachments, command="agent-run")
        parser.error(hint or "Either 'request' or --list-sessions must be provided")

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
            list_sessions=getattr(args, "list_sessions", None),
            session_title=getattr(args, "session_title", None),
            attachments=getattr(args, "attachments", None),
            force=getattr(args, "force", False)
        ))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()