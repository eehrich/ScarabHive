"""`agent-cli run` and `agent-cli chat`: from the loaded config to the answer.

The one command that starts the whole system. ``run_agent_command`` takes its
steps in the order docs/_arch_cli_architecture.md (section 4) describes --
logging, bootstrap, the session's own defaults, the entry agent, --max-steps,
the session-store commands, the LLM override, the attachments, the session
hold and open, the run, the save, the shutdown -- and each step is a function
of what it needs. They were one function's body, and its closures read twenty
of its locals; spelled out, a step says what it reads and what it hands on.

``chat`` shares every step up to the run and then hands over to the REPL
(cli_utils/chat/), on the same event loop. The one-shot run itself -- its
stop, its stream on the terminal, the result printed after -- is one_shot.py.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Union

from ...config.models import AgentSystemConfig
from ...core.session_presence import SessionBusy, presence_for
from ...llm.batch.initialization import init_batch_system, shutdown_batch_system
from ...llm.models import ChatMessage
from ...servers.agent.entry import NotAnAgent, entry_agent
from ...services.session_manager import SessionPermissionError
from ...tools.integration import initialize_tools, shutdown_tools
from ...utils.logging import ColorizedFormatter, setup_role_logging
from ..agent_runner import exit_unless_forced, wake_message
from ..attachments import greedy_attach_hint, sort_attachments
from ..common import supports_color
from ..event_loop import get_cli_loop, run_async
from ..session_archive_cli import build_archive, print_archived, run_restore, run_sweep
from ..session_defaults import (
    choose_agent_name,
    choose_llm_profile,
    load_session_llm_params,
    load_session_settings,
    profile_for_record,
    usable_session_defaults,
)
from ..session_listing import in_chat_selector, newest_of, parse_limit, parse_listing, print_sessions
from .one_shot import RunControl, print_result, run_one_shot

if TYPE_CHECKING:
    from ...servers.agent.server import Agent

logger = logging.getLogger(__name__)

#: Prints a progress line with -v, nothing without (agent_cli builds it).
VPrint = Callable[[str], None]


@dataclass
class Bootstrapped:
    """What the bootstrap hands the run: InitializationService's registry,
    session service and session store, and the service itself -- its runtime
    says which agents the chat offers."""

    init_service: Any
    registry: Any
    session_service: Any
    session_manager: Any


@dataclass
class LLMChoice:
    """The model this run goes out on."""

    #: The profile forced on the agent; None leaves it on its own.
    profile: Optional[str] = None
    #: llm_params laid over it (--llm-params, or the session's own).
    params: Optional[Dict[str, Any]] = None
    #: The client built for them; None runs the agent's own.
    client: Any = None
    #: How the status line names it.
    info: Optional[str] = None


def stored_session_settings(session_manager: Any, session_user: str,
                            session_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """load_session_settings on the CLI's own loop (see cli_utils.session_defaults)."""
    return run_async(load_session_settings(session_manager, session_user, session_id))


def colour_console_logs() -> None:
    """On a terminal, log lines follow --color as the rest of agent-cli's output does."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler.formatter, ColorizedFormatter):
            handler.formatter.use_colors = supports_color()


def setup_cli_logging(config: AgentSystemConfig, verbose: bool) -> None:
    """Setup logging from config -- the CLI's own file (logs/agent.log becomes
    logs/agent-cli.log), so it and the API do not write into one. Console
    level is adjusted below."""
    log_file = setup_role_logging(config.logging, "cli")
    colour_console_logs()
    # If verbose not set, reduce console output to WARNING to avoid noisy logs on stdout
    if not verbose:
        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            # FileHandler is a subclass of StreamHandler — exclude it
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.WARNING)
    if log_file:
        logger.info("Logging initialized, file=%s", log_file)


def bootstrap(config: AgentSystemConfig, vprint: VPrint) -> Bootstrapped:
    """Registry, session service and session store, built by InitializationService."""
    from ...services.initialization_service import apply_ssl_verify_to_environment
    apply_ssl_verify_to_environment(config)
    vprint("[cli] bootstrapping servers...")
    logger.info("Bootstrapping servers")

    # Store config for lazy batch queue manager initialization
    # This allows LLMFactory to create the manager when first needed
    from ...llm.factory import set_batch_config
    set_batch_config(config)

    # Use InitializationService for consistent bootstrap + injection
    from ...services.initialization_service import InitializationService
    init_service = InitializationService(config)
    registry, session_service = init_service.initialize_for_cli()

    # Keep references to session_manager for CLI use
    return Bootstrapped(init_service, registry, session_service, init_service.session_manager)


def resolve_session_ref(session_manager: Any, session_user: str, typed: Optional[str]) -> Optional[str]:
    """The session --session names: its id, also when it was typed as a title.

    What --session names is an id or the TITLE of a session: ids are
    machine-made and cannot be renamed (SessionManager.resolve_session_ref).
    Resolved right after the bootstrap, before anything reads it -- the
    session's own agent and LLM profile are looked up by it a few lines down,
    and presence holds it before the session is ever loaded. Resolved later,
    the run would take the config's default agent, hold a lock under the typed
    name and write that agent over the record of the session it then loaded.
    """
    if typed and session_manager is not None:
        others: list = []
        named = run_async(session_manager.resolve_session_ref(session_user, typed, others=others))
        if named and named != typed:
            # stderr: stdout carries the task result
            print(f"Session '{typed}': {named}{newest_of(others, '--list-sessions all')}",
                  file=sys.stderr)
            return named
    return typed


def start_runtime(config: AgentSystemConfig, registry: Any, vprint: VPrint) -> None:
    """The tool integration and the batch system; a failure of either is a warning."""
    vprint(f"[cli] servers registered: {', '.join(registry.list())}")
    logger.info("Servers registered: %s", ", ".join(registry.list()))

    # Initialize global tool integration to enable tool sharing across agents
    vprint("[cli] initializing tool integration...")
    logger.info("Initializing tool integration")
    try:
        run_async(initialize_tools(config))
        vprint("[cli] tool integration initialized")
        logger.info("tool integration initialized successfully")
    except Exception as e:
        logger.warning("Failed to initialize tool integration: %s", e)
        vprint(f"[cli] Warning: tool integration failed: {e}")

    # Initialize batch queue manager if any LLM models have batch enabled
    vprint("[cli] initializing batch queue manager...")
    try:
        run_async(init_batch_system(config))
        vprint("[cli] batch queue manager initialized")
        logger.info("Batch queue manager initialized successfully")
    except Exception as e:
        logger.warning("Failed to initialize batch queue manager: %s", e)
        vprint(f"[cli] Warning: batch queue manager failed: {e}")

    # Note: SessionManager, SessionService, and dependency injection
    # are now handled by InitializationService.initialize_for_cli() above


def shut_down_runtime(vprint: VPrint) -> None:
    """Batch system and tool integration, however this run ends. It is up from the
    bootstrap on: returning without this leaks stdio child processes and
    aiohttp sessions until the interpreter exits."""
    try:
        run_async(shutdown_batch_system())
        vprint("[cli] batch queue manager shut down")
        logger.info("Batch queue manager shut down successfully")
    except Exception as e:
        logger.warning("Failed to shutdown batch queue manager: %s", e)
    try:
        run_async(shutdown_tools())
        vprint("[cli] tool integration shut down")
        logger.info("tool integration shut down successfully")
    except Exception as e:
        logger.warning("Failed to shutdown tool integration: %s", e)


def build_entry_agent(entry_name: str, config: AgentSystemConfig, boot: Bootstrapped,
                      vprint: VPrint) -> Agent:
    """Get or create the agent -- the one factory the API and /agent use too.

    A name that is no agent: the list of agents, exit 1."""
    was_registered = entry_name in boot.registry.list()
    try:
        agent = entry_agent(entry_name, config, boot.registry, boot.session_service)
    except NotAnAgent as e:
        logger.error("No agent '%s': %s", entry_name, e)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not was_registered:
        vprint(f"[cli] created agent: {entry_name}")
    return agent


def apply_max_steps(agent: Agent, max_steps_override: Optional[int], vprint: VPrint) -> None:
    """--max-steps: the budget for THIS process, not a config change.

    Copied onto the instance rather than threaded through the run: both
    readers take it from agent_config (server.py's run loop and the final
    answer), and the copy keeps the loaded config untouched. Safe here in a
    way it would not be in the API, where one agent instance serves every
    request -- which is why /run has no such override.
    """
    if max_steps_override is not None:
        agent.agent_config = agent.agent_config.model_copy(
            update={"max_steps": max_steps_override})
        vprint(f"[cli] max_steps override: {max_steps_override}")


def run_session_store_command(args: Any, config: AgentSystemConfig, boot: Bootstrapped,
                              entry_name: str, vprint: VPrint) -> bool:
    """--list-sessions and the archive commands. True when one of them ran:
    the run ends there."""
    # --list-sessions reads the session store and leaves. It comes before the
    # LLM override and the attachments and needs neither: a profile that
    # cannot be built (a missing key, a typo) must not hide the listing.
    list_sessions = getattr(args, "list_sessions", None)
    if list_sessions is not None:
        session_user = getattr(args, "session_user", "cli_user")
        vprint(f"[cli] listing sessions for user: {session_user}")
        limit, everything, complaint = parse_listing(list_sessions)
        if complaint:
            print(f"Ignoring '{complaint}': --list-sessions takes a count or 'all'.")
        run_async(print_sessions(
            boot.session_manager, session_user,
            limit=limit,
            current_session_id=getattr(args, "session_id", None),
            more_hint="--list-sessions <count>, --list-sessions 0 for no limit",
            footer="Continue one with: --session <id or title>",
            shown=None if everything else in_chat_selector(boot.init_service.runtime,
                                                           keep=(entry_name,)),
            everything_hint="--list-sessions all",
        ))
        return True

    # The archive commands read and write the session store and leave, for the
    # same reason as the listing above: none of them needs an LLM profile.
    list_archived = getattr(args, "list_archived", None)
    restore_session = getattr(args, "restore_session", None)
    archive_sessions = getattr(args, "archive_sessions", None)
    if list_archived is not None or restore_session or archive_sessions is not None:
        session_user = getattr(args, "session_user", "cli_user")
        archive = build_archive(boot.session_manager, config)
        if list_archived is not None:
            limit, complaint = parse_limit(list_archived)
            if complaint:
                print(f"Ignoring '{complaint}': --list-archived takes a count.")
            run_async(print_archived(archive, session_user, limit=limit))
        if restore_session:
            run_async(run_restore(archive, session_user, restore_session))
        if archive_sessions is not None:
            days, complaint = parse_limit(archive_sessions, default=0)
            if complaint:
                print(f"Ignoring '{complaint}': --archive-sessions takes a number of days.")
            run_async(run_sweep(
                archive, session_user,
                retention_days=days or None,
                dry_run=getattr(args, "archive_dry_run", False),
            ))
        return True
    return False


def choose_llm(*, requested_profile: Optional[str], llm_params_override: Optional[Dict[str, Any]],
               config: AgentSystemConfig, agent: Agent, entry_name: str,
               stored_agent: Optional[str], stored_llm: Optional[str],
               session_manager: Any, session_user: str, session_id: Optional[str],
               vprint: VPrint) -> LLMChoice:
    """The LLM override (--llm and/or --llm-params), or the session's own.

    Built before the attachments: the capability check must see the model this
    run will actually use, and a bad profile stops the run before a session
    exists.
    """
    llm_override = None
    llm_profile_info = None
    # Continue on the model the session was started with (see
    # choose_llm_profile for what that does and does not outrank).
    llm_profile_override = choose_llm_profile(
        requested_profile, stored_llm, stored_agent, entry_name,
        agent.agent_config.default_llm_profile)
    if llm_profile_override and not requested_profile:
        vprint(f"[cli] continuing session with its own LLM profile: "
               f"{llm_profile_override}")
    # And with its own llm_params (a chat's /think, the web chat's thinking level), on the same
    # terms as the profile: --llm-params wins, and only for the agent they were set for.
    if llm_params_override is None and stored_agent and stored_agent == entry_name:
        llm_params_override = run_async(load_session_llm_params(
            session_manager, session_user, session_id)) or None
        if llm_params_override:
            vprint(f"[cli] continuing session with its own llm_params: {llm_params_override}")
    # Exit 1 on the profile errors below, not 0: a caller checking the code
    # took a refused run for a finished one. Same for a session that cannot
    # be loaded further down.
    if llm_profile_override or llm_params_override:
        # --llm-params without --llm: apply them to the agent's default profile.
        effective_profile = (
            llm_profile_override or agent.agent_config.default_llm_profile
        )
        if config.llm_system and config.llm_system.profiles:
            from ...llm.factory import UnknownLLMProfile, override_for_profile

            try:
                llm_override, llm_profile_info = override_for_profile(
                    config, agent.agent_config, effective_profile, llm_params_override)
                logger.info(f"Using LLM override: {llm_profile_info}")
                vprint(f"[cli] Using LLM profile: {llm_profile_info}")
            except UnknownLLMProfile as e:
                # An explicit --llm was checked before the bootstrap already;
                # this is the agent's own default profile.
                print(f"ERROR: {e}", file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                logger.error(f"Failed to create LLM override: {e}", exc_info=True)
                print(f"ERROR: Failed to apply LLM profile '{effective_profile}': {str(e)}", file=sys.stderr)
                sys.exit(1)
    return LLMChoice(llm_profile_override, llm_params_override, llm_override, llm_profile_info)


def build_task_input(args: Any, agent: Agent, llm_override: Any,
                     vprint: VPrint) -> tuple[Union[str, ChatMessage], Dict[str, list]]:
    """The task, with the attachments in it; and the attachments by kind.

    Process multimodal attachments -- the kind comes from the file, not from
    which flag was typed (cli_utils.attachments), same as /attach in the chat.
    A file that cannot be sent: exit 1.
    """
    # A woken run says so in the ROLE, not only in the words: the sentence is
    # the same, but a `user` turn claims a person typed it (agent_runner).
    task_input: Union[str, ChatMessage] = (
        wake_message() if getattr(args, "woken", False) else args.task)
    sorted_attachments, attachment_problems = sort_attachments(
        getattr(args, "attachments", None) or [])
    for problem in attachment_problems:
        print(f"Error: {problem}", file=sys.stderr)
    if attachment_problems:
        # `run` defaults its task, so a swallowed request does not even look
        # like a missing one here -- it looks like a file that is not there.
        hint = greedy_attach_hint(getattr(args, "attachments", None),
                                  command=f"agent-cli {args.subcommand}")
        if hint:
            print(f"Error: {hint}", file=sys.stderr)
        sys.exit(1)
    # Chat sends them with its first message, through the same path /attach
    # takes -- there may be no message yet.
    if any(sorted_attachments.values()) and args.subcommand != "chat":
        from ...utils.multimodal_processor import AttachmentRejected, message_with_attachments

        # Checked against the model this run will use -- the --llm override
        # wins over the agent's default, one rule with the HTTP API and the
        # chat. sys.exit(1), not return: a caller that checks the code -- the
        # writer runners do -- read a refused run as a finished one with empty
        # output.
        try:
            task_input = message_with_attachments(args.task, sorted_attachments, llm_override, agent)
        except AttachmentRejected as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        vprint("[cli] created multimodal message")
    return task_input, sorted_attachments


def hold_session(presence: Any, *, session_id: str, session_user: str, entry_name: str,
                 woken: bool, force: bool, vprint: VPrint) -> bool:
    """Hold the session before it is loaded. False: a woken run steps aside
    (the runtime is shut down already); busy without --force exits 1.

    Session presence (core/session_presence.py): the session is held BEFORE
    it is loaded. A run that reads the file first can be overtaken by the
    process that holds it, and would then write its own copy over that run.
    Chat takes the hold over and lets go of it itself (run_chat_loop).
    """
    try:
        presence.hold(session_id, session_user, entry_name)
    except SessionBusy as busy:
        if woken:
            logger.info("%s; that process hands the waiting input over itself", busy)
            shut_down_runtime(vprint)
            return False
        exit_unless_forced(busy, force)
    else:
        if woken and not presence.pending(session_id, session_user):
            # A run in between took the input with it; waking the session
            # for nothing would cost an LLM call and say nothing.
            logger.info("Woken run of %s: the input was taken already", session_id)
            presence.release(session_id, session_user)
            shut_down_runtime(vprint)
            return False
        if woken:
            # This run is told that input waits: what rang for it is
            # delivered. Taken with the stamp here, not on the first LLM
            # step: a ringer that saw the session held rang on into a
            # wake run per ring, up to max_wake_depth, and a run that
            # never reaches an LLM call left the mark and woke itself.
            presence.take_for_wake(session_id, session_user)
    return True


def _cli_vars(agent: Agent, session_id: str, cli_vars_raw: Optional[list]) -> Dict[str, str]:
    """Merge CLI --vars overrides into session template_vars; returns what was applied."""
    cli_vars: dict[str, str] = {}
    if cli_vars_raw and hasattr(agent, '_session_tracker'):
        for item in cli_vars_raw:
            if '=' not in item:
                print(f"Warning: ignoring invalid --vars entry (expected KEY=VALUE): {item}", file=sys.stderr)
                continue
            key, _, value = item.partition('=')
            cli_vars[key.strip()] = value.strip()
        if cli_vars:
            agent._session_tracker.set_session_template_vars(session_id, cli_vars)
            logger.debug(f"[cli] Applied CLI template_vars overrides: {list(cli_vars.keys())}")
    return cli_vars


async def open_session(session_service: Any, agent: Agent, *, session_user: str,
                       actual_session_id: str, typed_session_id: Optional[str],
                       record_profile: Optional[str], cli_vars_raw: Optional[list],
                       vprint: VPrint) -> tuple[bool, bool, Dict[str, str]]:
    """Open the session for this run the way the API does
    (SessionService.open_for_run), then lay --vars over its template vars.
    Returns (continue, was_new_session, the --vars applied)."""
    try:
        exists = await session_service.open_for_run(
            agent, session_user, actual_session_id, record_profile)
    except SessionPermissionError as e:
        logger.error(f"Permission denied for session {actual_session_id}: {e}")
        print(f"Error: {e}", file=sys.stderr)
        print("This session belongs to a different user. Use a different session ID.", file=sys.stderr)
        return False, False, {}
    except Exception as e:
        logger.error(f"Failed to load session {actual_session_id}: {e}", exc_info=True)
        print(f"Error loading session: {e}", file=sys.stderr)
        return False, False, {}
    was_new_session = not exists
    if exists:
        count = len(agent._session_tracker.get_session_messages(actual_session_id) or [])
        vprint(f"[cli] loaded session with {count} messages")
        logger.info(f"Loaded session {actual_session_id} with {count} messages")
    elif typed_session_id:
        logger.info(f"Session '{typed_session_id}' not found, creating new session with this ID")
        # stderr: stdout carries the task result (redirects stay clean)
        print(f"Creating new session '{typed_session_id}'", file=sys.stderr)

    # Chat's /new creates further sessions and has to re-apply the --vars.
    return True, was_new_session, _cli_vars(agent, actual_session_id, cli_vars_raw)  # Continue with task execution


async def save_session_after_run(session_service: Any, agent: Agent, *, entry_name: str,
                                 session_user: str, typed_session_id: Optional[str],
                                 actual_session_id: str, was_new_session: bool,
                                 title: Optional[str], llm: LLMChoice, vprint: VPrint) -> None:
    """Save session after successful task execution."""
    try:
        # Use the actual agent name that was requested (entry_name from args)
        # instead of agent.agent_name which may not exist or be "default"
        agent_name_used = entry_name  # The agent name determined from args.agent_override or config.default_agent
        # The profile actually USED, not the raw flag: session_service
        # overwrites the record unconditionally, so reading the flag
        # here wrote the agent's default over the session's own choice
        # on every bare --session resume -- the choice then survived
        # exactly one continuation.
        llm_profile_used = profile_for_record(
            llm.profile, agent.agent_config.default_llm_profile)

        # Save the session
        success = await session_service.save_session(
            agent=agent,
            user_id=session_user,
            session_id=actual_session_id,
            agent_name=agent_name_used,
            llm_profile=llm_profile_used,
            was_new_session=was_new_session,
            title=title,
            # what this run was given for its model: the next bare --session starts on it
            llm_choice={"profile": llm.profile, "params": llm.params or {}},
        )

        if success:
            if typed_session_id:
                vprint(f"[cli] updated session: {typed_session_id}")
                logger.info(f"Updated session {typed_session_id}")
            else:
                vprint(f"[cli] created new session: {actual_session_id}")
                logger.info(f"Created new session {actual_session_id}")
                # stderr: stdout traegt das Ergebnis (`>out.json` darf
                # keine Meta-Zeilen einsammeln); im Terminal weiter sichtbar.
                print(f"\nSession saved: {actual_session_id}", file=sys.stderr)
        else:
            logger.warning("Session save returned False")
    except Exception as e:
        logger.error(f"Failed to save session: {e}", exc_info=True)
        print(f"Warning: Failed to save session: {e}", file=sys.stderr)


def run_in_session(args: Any, config: AgentSystemConfig, boot: Bootstrapped, agent: Agent,
                   entry_name: str, llm: LLMChoice, task_input: Union[str, ChatMessage],
                   sorted_attachments: Dict[str, list], vprint: VPrint) -> None:
    """Hold and open the session, run the task (or the chat) in it, save it,
    let it go and shut the runtime down; then print the result."""
    # Initialize session management
    session_id = getattr(args, "session_id", None)
    session_user = getattr(args, "session_user", "cli_user")

    # Generate or use provided session ID
    from ...utils.id import short_id
    actual_session_id = session_id or short_id()

    # SessionManager and SessionService already initialized earlier (before Agent creation)
    # to enable passing session_service to Agent constructor
    record_profile = profile_for_record(llm.profile, agent.agent_config.default_llm_profile)

    is_chat = args.subcommand == "chat"
    presence = presence_for(config)
    if presence and not hold_session(
            presence, session_id=actual_session_id, session_user=session_user,
            entry_name=entry_name, woken=getattr(args, "woken", False),
            force=getattr(args, "force", False), vprint=vprint):
        return

    # Run session operations
    try:
        should_continue, was_new_session, parsed_cli_vars = run_async(open_session(
            boot.session_service, agent, session_user=session_user,
            actual_session_id=actual_session_id, typed_session_id=session_id,
            record_profile=record_profile, cli_vars_raw=getattr(args, 'template_vars', None),
            vprint=vprint))
    except KeyboardInterrupt:
        # Held already: let go marked, or the input waiting wakes it as this exits.
        if presence:
            presence.release(actual_session_id, session_user, stopped=True)
        shut_down_runtime(vprint)
        raise
    if not should_continue:
        # Chat as well: it takes the hold over only once its REPL runs.
        if presence:
            presence.release(actual_session_id, session_user)
        shut_down_runtime(vprint)
        sys.exit(1)

    control = RunControl(short_id())

    # Execute with new status-aware streaming
    show_tools = getattr(args, "show_tools", False)
    show_status = not getattr(args, "no_status", False)

    # Chat mode: hand over to the REPL instead of the one-shot execution.
    # Everything above (bootstrap, agent, session ops, LLM override) is shared.
    # Inside the same try/finally as the one-shot path so the tools and the batch
    # system get shut down the same way -- returning early leaked stdio child
    # processes and aiohttp sessions until interpreter exit.
    try:
        if is_chat:
            from ..chat import run_chat_loop
            run_chat_loop(
                # The SHARED loop, not a private one: bootstrap connected the
                # external MCP servers on it, and their tasks only make
                # progress while this very loop runs the turns.
                loop=get_cli_loop(),
                agent=agent,
                entry_name=entry_name,
                session_service=boot.session_service,
                session_manager=boot.session_manager,
                session_user=session_user,
                session_id=actual_session_id,
                was_new_session=was_new_session,
                llm_profile=record_profile,
                llm_override=llm.client,
                llm_profile_info=llm.info,
                show_status=show_status,
                initial_task=getattr(args, "task", None),
                template_vars=parsed_cli_vars,
                llm_params=llm.params,
                session_title=getattr(args, "session_title", None),
                attachments=[path for group in sorted_attachments.values()
                             for path in group],
                runtime=boot.init_service.runtime,
            )
            # Chat saved its own sessions per turn and prints its own output.
            return
        result = run_one_shot(
            agent, task_input, control, actual_session_id,
            raw=getattr(args, "raw", False), verbose=args.verbose,
            show_tools=show_tools, show_status=show_status,
            llm_override=llm.client, llm_profile_info=llm.info)

        # Check if request was cancelled
        if result.get("cancelled", False):
            vprint("[cli] cancelled by user")
            logger.info("Request cancelled by user")
        else:
            vprint("[cli] done")
            logger.info("Task completed")

        # Only save session if not cancelled -- nor refused before it ran (the agent's role gate, another
        # user's session, another run's lock): it ran nothing, and a save only rewrote the record with this
        # entry agent and profile, its updated_at moved.
        if not result.get("cancelled", False) and not result.get("refused"):
            run_async(save_session_after_run(
                boot.session_service, agent, entry_name=entry_name, session_user=session_user,
                typed_session_id=session_id, actual_session_id=actual_session_id,
                was_new_session=was_new_session, title=getattr(args, "session_title", None),
                llm=llm, vprint=vprint))

    finally:
        # Chat lets go of the session it has open itself: after /new or
        # /resume that is no longer the one this run started with. Not a
        # second time here: a turn takes its OWN hold for its request
        # (server.py _presence_hold), and a turn abandoned by a third
        # Ctrl-C never gave it back. The chat released only its own, so
        # releasing again would drop the abandoned turn's -- waking a
        # session whose run is still unwinding.
        if presence and not is_chat:
            presence.release(actual_session_id, session_user, stopped=control.stopped)
        # What the run registered under its request id goes with it, as the
        # API lets go of its request tree when the request ends.
        from ...core.request_context import release_request_user_tree
        release_request_user_tree(control.request_id)
        shut_down_runtime(vprint)

    print_result(result, raw=getattr(args, "raw", False), show_tools=show_tools,
                 verbose=getattr(args, "verbose", False), streamed_errors=control.streamed_errors)


def run_agent_command(args: Any, config: AgentSystemConfig,
                      llm_params_override: Optional[Dict[str, Any]], vprint: VPrint) -> None:
    """`run` and `chat`: *llm_params_override* is --llm-params as the parser
    checked it, *vprint* agent_cli's -v printer."""
    setup_cli_logging(config, args.verbose)
    boot = bootstrap(config, vprint)
    args.session_id = resolve_session_ref(
        boot.session_manager, getattr(args, "session_user", "cli_user"),
        getattr(args, "session_id", None))
    start_runtime(config, boot.registry, vprint)

    # What this session was started with, when one is being continued. An
    # explicit --agent/--llm still wins; the stored values only replace the
    # config defaults, which are the wrong answer for a session that was
    # begun with something else.
    stored_agent, stored_llm = usable_session_defaults(
        *stored_session_settings(
            boot.session_manager, getattr(args, "session_user", "cli_user"),
            getattr(args, "session_id", None)),
        config)

    # Determine CLI agent name from config (can be overridden with --agent)
    entry_name = choose_agent_name(
        getattr(args, "agent_override", None), stored_agent, config.default_agent)
    if stored_agent and entry_name == stored_agent:
        vprint(f"[cli] continuing session with its own agent: {entry_name}")

    agent = build_entry_agent(entry_name, config, boot, vprint)
    apply_max_steps(agent, getattr(args, "max_steps", None), vprint)

    if run_session_store_command(args, config, boot, entry_name, vprint):
        return

    llm = choose_llm(
        requested_profile=getattr(args, "llm_profile_override", None),
        llm_params_override=llm_params_override, config=config, agent=agent,
        entry_name=entry_name, stored_agent=stored_agent, stored_llm=stored_llm,
        session_manager=boot.session_manager,
        session_user=getattr(args, "session_user", "cli_user"),
        session_id=getattr(args, "session_id", None), vprint=vprint)
    task_input, sorted_attachments = build_task_input(args, agent, llm.client, vprint)

    vprint(f"[cli] running task: {args.task}")
    logger.info("Running task: %s", args.task)

    run_in_session(args, config, boot, agent, entry_name, llm, task_input, sorted_attachments, vprint)
