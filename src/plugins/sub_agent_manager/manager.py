"""Sub-Agent Manager - Core lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import random
import weakref
from agent_system.services.session_manager import SessionNotFoundError
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from agent_system.tools.base import ToolServerRegistry
    from agent_system.services.session_service import SessionService

logger = logging.getLogger(__name__)

#: Stored statuses of a sub-agent that is still there to use: "active" (running or
#: idle) and "interrupted" (its session is present, its last run ended unexpectedly).
OPEN_STATUSES = ("active", "interrupted")

#: One lock per parent session, shared by every manager in this process (the server builds one
#: per call). A sub-agent's entry is written whole -- update_session_metadata merges one
#: level -- so a write that had read it before another landed wrote that one away; and a limit is
#: a count that a spawn reads and then fills. Measured without it: an archive beside an activity
#: update was lost, and a fan-out of six creates passed a limit of three.
#: ponytail: per process -- another process writing the same parent is not held off by it.
_parent_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _parent_lock(parent_session_id: str) -> asyncio.Lock:
    lock = _parent_locks.get(parent_session_id)
    if lock is None:
        lock = _parent_locks[parent_session_id] = asyncio.Lock()
    return lock


def _first_counter() -> int:
    """Where this process starts counting instance ids: at random, so two processes land on one id only
    by chance, and a restarted one does not count through the ids of the one before (an id already on
    disk is skipped besides). It started at the time of day -- processes started in the same second,
    the parallel agent-cli runs of a batch, counted through the same ids, and one's session file could
    replace the other's between the existence check and the write."""
    return random.randrange(10_000_000)


async def message_counts(sessions, user_id: str, parent_id: str) -> dict[str, int]:
    """The message count of each sub-session of ``parent_id``, from the parent's sub-index: one small
    file, rewritten at every save of a sub-session with the transcript's length as of that save -- no
    transcript read for it. The count in the parent's entry is no substitute: it lags a run behind and
    was measured wrong besides (0, 2 or 9 where the transcripts held 4 to 27)."""
    try:
        rows = await sessions.list_child_sessions(user_id, parent_id, annotate_children=False)
    except Exception as error:  # a figure must not fail the listing it stands in
        logger.debug("No sub-index for %s: %s", parent_id, error)
        return {}
    return {row["session_id"]: int(row.get("message_count") or 0) for row in rows if row.get("session_id")}


class CallerMistake(ValueError):
    """A call naming a sub-agent that is not there, not the caller's, or busy, or an agent that
    cannot be spawned. The model is answered and can correct it; the manager did nothing wrong.
    Logged as an error, with a traceback, it read like a crash in the server log."""


class SubAgentLimitReached(ValueError):
    """No room for one more active sub-agent: a limit is reached, and nothing was archived to make
    some. A ValueError still, for every caller that catches those."""


class SubAgentManager:
    """Manages lifecycle of sub-agent instances.

    This manager handles:
    - Creating sub-sessions via SessionManager
    - Linking sub-sessions to parent sessions
    - Listing and querying sub-sessions
    - Generating unique instance IDs

    CRITICAL: Does NOT extend SessionTracker. Uses SessionManager directly
    for all persistence operations.
    """
    
    # Class-level counter shared by every manager of the process, started by `_first_counter` at the
    # first id the process makes.
    _class_counter: Optional[int] = None

    def __init__(
        self, 
        session_service: SessionService, 
        registry: ToolServerRegistry, 
        max_nesting_depth: int = 5,
        max_sub_agents_per_type: int = 3,
        max_sub_agents_per_session: int = 10,
        auto_archive_on_limit: bool = False,
    ):
        """Initialize SubAgentManager.

        Args:
            session_service: Session service for persistence
            registry: tool registry for agent lookup
            max_nesting_depth: Levels of sub-agents this manager grants below the
                calling session. Sub-sessions inherit the remaining budget, and a
                manager further down can only lower it, never raise it.
            max_sub_agents_per_type: Maximum number of active sub-agents per type per session
            max_sub_agents_per_session: Maximum total number of active sub-agents per session
            auto_archive_on_limit: If True, automatically archive the oldest sub-agent when
                a limit is reached instead of returning limit_reached error.
        """
        self._session_service = session_service
        self._registry = registry
        self.max_nesting_depth = max_nesting_depth
        self.max_sub_agents_per_type = max_sub_agents_per_type
        self.max_sub_agents_per_session = max_sub_agents_per_session
        self.auto_archive_on_limit = auto_archive_on_limit

        # Activity update tracking: only save when activity actually changes
        # to avoid excessive session saves during streaming
        self._last_activity: dict[str, str | None] = {}  # sub_session_id -> last activity

        logger.info(
            f"SubAgentManager initialized ("
            f"max_nesting_depth={max_nesting_depth}, "
            f"max_sub_agents_per_type={max_sub_agents_per_type}, "
            f"max_sub_agents_per_session={max_sub_agents_per_session}, "
            f"auto_archive_on_limit={auto_archive_on_limit})"
        )

    async def create_sub_session(
        self,
        parent_session_id: str,
        agent_type: str,
        initial_message: str,
        instance_label: Optional[str] = None,
        params: Optional[dict] = None
    ) -> str:
        """Create sub-session file via SessionManager.

        Args:
            parent_session_id: Parent coordinator session ID
            agent_type: Type of agent to create (e.g., "research_agent")
            initial_message: Initial task/message for sub-agent
            instance_label: Optional human-readable label
            params: Optional tool call params with injected _user_id

        Returns:
            Sub-session ID (format: sub_{agent_type}_{global_counter})

        Raises:
            ValueError: If parent session not found, agent type invalid, or max depth exceeded
        """
        # The count and the entry that takes the place it found, in one piece: every create of a
        # fan-out read the same count otherwise.
        async with _parent_lock(parent_session_id):
            return await self._create_sub_session(
                parent_session_id, agent_type, initial_message, instance_label, params)

    async def _create_sub_session(
        self,
        parent_session_id: str,
        agent_type: str,
        initial_message: str,
        instance_label: Optional[str],
        params: Optional[dict],
    ) -> str:
        """create_sub_session, under the parent's lock."""
        # Extract user_id from params or session file
        user_id = self._extract_user_id(parent_session_id, params)
        session_manager = self._session_service.session_manager

        # Load parent session to check depth
        # If parent session doesn't exist yet (e.g., CLI ephemeral session),
        # create it automatically with minimal metadata
        try:
            parent_data = await session_manager.load_session(user_id, parent_session_id)
        except SessionNotFoundError:
            logger.info(
                f"Parent session {parent_session_id} not found in storage, creating it now"
            )
            # Extract agent_name and llm_profile from params (injected by ToolExecutionManager)
            parent_agent = params.get("_agent") if params else None
            if not parent_agent:
                raise ValueError(
                    f"Cannot create parent session {parent_session_id}: _agent not found in params. "
                    "Session metadata not set correctly."
                )

            parent_agent_name = parent_agent.name if hasattr(parent_agent, 'name') else str(parent_agent)
            parent_llm_profile = parent_agent.agent_config.default_llm_profile if hasattr(parent_agent, 'agent_config') else None

            if not parent_llm_profile:
                raise ValueError(
                    f"Cannot create parent session {parent_session_id}: agent.agent_config.llm_profile not found"
                )

            # Create parent session with actual metadata from agent
            # This handles CLI/ephemeral sessions that haven't been saved yet
            # Race condition guard: parallel sub-agent creates may all find
            # the parent missing and try to create it simultaneously.  The
            # first one wins; the rest just load the already-created session.
            try:
                await session_manager.create_session(
                    user_id=user_id,
                    session_id=parent_session_id,
                    title="Coordinator Session",
                    agent_name=parent_agent_name,
                    llm_profile=parent_llm_profile
                )
            except ValueError as exc:
                if "already exists" not in str(exc):
                    raise
                logger.debug(
                    f"Parent session {parent_session_id} was created concurrently, loading it"
                )
            parent_data = await session_manager.load_session(user_id, parent_session_id)

        # Check nesting depth.
        #
        # `depth` counts from the root of the whole tree; it is reported, not
        # enforced.  What bounds a tree is the budget: how many further levels
        # may be created below this session.  `max_nesting_depth` is what THIS
        # manager grants below its caller, the inherited budget is what an
        # ancestor already granted, and the smaller of the two wins.  So a
        # strict manager keeps bounding its whole subtree even when a looser
        # one is called further down, and it still works when its own agent
        # runs nested -- neither of which a limit on the absolute depth can do.
        parent_depth = parent_data.get("depth", 1)
        child_depth = parent_depth + 1

        inherited = parent_data.get("depth_budget")
        budget = self.max_nesting_depth if inherited is None else min(inherited, self.max_nesting_depth)

        if budget < 1:
            raise CallerMistake(  # a limit's refusal, not a fault
                f"Maximum nesting depth exceeded: session {parent_session_id} "
                f"(depth {parent_depth}) has no levels left below it "
                f"(max_nesting_depth={self.max_nesting_depth}, inherited budget={inherited})"
            )

        # The agent before the room for it: making room archives the oldest sub-agent, and a
        # misspelled type archived one the caller may be waiting on, then failed.
        try:
            agent = self._registry.get(agent_type)
        except KeyError:  # the registry raises for a name it does not know
            agent = None
        if not agent:
            raise CallerMistake(f"Agent type '{agent_type}' not found in registry")

        agent_llm_profile = agent.agent_config.default_llm_profile if hasattr(agent, 'agent_config') else None
        if not agent_llm_profile:
            raise ValueError(f"Agent '{agent_type}' has no agent_config.llm_profile")

        await self._make_room(parent_session_id,
                              parent_data.get("metadata", {}).get("sub_agents", {}), agent_type)

        # Generate unique instance ID + create session.
        #
        # Race condition guard for multi-process scenarios (e.g. 5 parallel
        # agent-cli runs): each Python process has its OWN ``_class_counter``,
        # started at random, so two land on one id only by chance.
        #
        # ``_generate_instance_id`` checks uniqueness against the filesystem,
        # but between that check and ``create_session`` (which runs the same
        # check + write atomically) another process can race in. So we wrap
        # the create in a retry loop: on collision, generate a NEW id and
        # try again. The class_counter advances on each retry, so we won't
        # hit the same id twice from this process.
        max_id_collision_retries = 10
        title = (
            initial_message[:100]
            if len(initial_message) <= 100
            else f"{initial_message[:97]}..."
        )
        sub_session_id: str = ""
        for attempt in range(max_id_collision_retries):
            sub_session_id = await self._generate_instance_id(agent_type, instance_label)
            try:
                await session_manager.create_session(
                    user_id=user_id,
                    session_id=sub_session_id,
                    title=title,
                    agent_name=agent_type,
                    llm_profile=agent_llm_profile,
                    parent_session_id=parent_session_id,
                )
                break  # success
            except ValueError as exc:
                msg = str(exc)
                if "already exists" not in msg:
                    raise
                # Cross-process collision — another agent-cli process raced and
                # wrote this exact session_id. Generate a fresh id (counter is
                # already incremented in our own class state) and retry.
                if attempt < max_id_collision_retries - 1:
                    logger.info(
                        "Sub-session id collision (cross-process race) on %s — "
                        "regenerating id (attempt %d/%d)",
                        sub_session_id, attempt + 1, max_id_collision_retries,
                    )
                    continue
                # Final attempt failed — re-raise so the caller's normal retry
                # path can react.
                raise

        logger.info(
            f"Created sub-session: {sub_session_id} (parent: {parent_session_id}, depth: {child_depth})"
        )

        # Add parent link and depth to sub-session metadata
        session_data = await session_manager.load_session(user_id, sub_session_id)

        session_data["depth"] = child_depth
        session_data["depth_budget"] = budget - 1  # what the child may still grant

        if "parent_session" not in session_data:
            session_data["parent_session"] = {}

        session_data["parent_session"]["session_id"] = parent_session_id
        session_data["parent_session"]["created_at"] = datetime.now(UTC).isoformat()

        # Initialize sub_agents dict for potential children
        if "metadata" not in session_data:
            session_data["metadata"] = {}
        if "sub_agents" not in session_data["metadata"]:
            session_data["metadata"]["sub_agents"] = {}

        # Inherit context_vars from parent (can be overridden by sub-agent's set_context).
        #
        # Two sources are merged, with in-memory taking precedence on conflict:
        #
        # 1. DISK: parent_data.get("context_vars", {}) — last checkpointed snapshot.
        # 2. IN-MEMORY: parent_agent._session_tracker.get_session_template_vars(parent_session_id)
        #    — live state that set_session_template_vars writes to first. The
        #    periodic checkpoint loop eventually flushes this to disk, but its
        #    interval (default ~10s) can lag when the parent agent is blocked
        #    inside a long-running tool — the first sub-agent of such a tool
        #    would otherwise inherit a stale disk snapshot (often empty for
        #    fresh sessions, missing late-set vars on resume). Symptom on v4:
        #    scene_planner gets ``book_id`` missing → batch_scene validator
        #    rejects ("book_id context not set"). See Fix #510.
        parent_context_vars = dict(parent_data.get("context_vars") or {})
        parent_agent = params.get("_agent") if params else None
        if parent_agent is not None and hasattr(parent_agent, "_session_tracker"):
            try:
                live_vars = parent_agent._session_tracker.get_session_template_vars(
                    parent_session_id,
                )
                if live_vars:
                    # In-memory wins on key collision — the tracker is the
                    # write-side source of truth; disk is its derivative.
                    parent_context_vars.update(live_vars)
            except Exception as e:
                logger.debug(
                    "Could not read parent in-memory tracker vars for "
                    "sub-session inheritance (%s): %s",
                    parent_session_id, e,
                )

        if parent_context_vars:
            session_data["context_vars"] = parent_context_vars
            # Provenance snapshot: remembers WHICH values came from the parent,
            # so a later continue can tell "the sub-agent changed this itself"
            # from "this is a stale copy of a parent value". See
            # merge_parent_context_vars().
            session_data["context_vars_inherited"] = dict(parent_context_vars)
            logger.debug(
                f"Inherited context_vars from parent: {list(parent_context_vars.keys())}"
            )

        await session_manager.save_session(session_data)

        logger.debug(f"Linked sub-session {sub_session_id} to parent {parent_session_id}, depth={child_depth}")

        # Update parent session with sub-agent metadata
        # creator_plugin is passed via params from the server
        creator_plugin = params.get("_creator_plugin") if params else None
        await self._link_sub_to_parent(
            user_id, parent_session_id, sub_session_id, agent_type, initial_message, child_depth,
            creator_plugin=creator_plugin
        )

        return sub_session_id

    @staticmethod
    def merge_parent_context_vars(
        sub_session_data: dict,
        parent_live_vars: Optional[dict],
    ) -> tuple[dict, dict]:
        """Refresh a sub-session's context_vars from the parent's LIVE vars.

        A sub-agent inherits the parent's context_vars only ONCE, when its
        session is created. Without this refresh, a ``continue`` would render
        the sub-agent's prompt from a frozen snapshot: after the coordinator
        calls ``set_context(aufgabe=World)`` and continues an existing writer,
        ``{{ aufgabe }}`` would still say "Idee" while the task text says
        "World" — two contradicting instructions in one prompt.

        Provenance rule: a key the sub-agent set ITSELF (its value differs from
        the snapshot it inherited) wins; every other key follows the parent,
        whose tracker is the live source of truth. Keys only the sub-agent has
        are kept.

        Legacy sessions have no ``context_vars_inherited``; treating their vars
        as fully inherited lets the parent's current values through, which is
        the whole point of the refresh.

        The new snapshot ACCUMULATES (old snapshot updated by the parent's
        current values) instead of replacing it. Replacing would erase the
        provenance of keys the parent no longer carries — an unreadable or
        empty parent tracker would then make every sub var look self-set, and
        the parent could never update it again.

        Returns:
            (merged_vars, new_inherited_snapshot)
        """
        sub_vars = dict(sub_session_data.get("context_vars") or {})
        parent_live = dict(parent_live_vars or {})

        if "context_vars_inherited" in sub_session_data:
            inherited = dict(sub_session_data.get("context_vars_inherited") or {})
        else:
            inherited = dict(sub_vars)  # legacy: assume nothing was self-set

        own = {
            key: value for key, value in sub_vars.items()
            if key not in inherited or inherited[key] != value
        }
        merged = {**sub_vars, **parent_live, **own}
        new_inherited = {**inherited, **parent_live}
        return merged, new_inherited

    async def refresh_sub_context_vars(
        self,
        user_id: str,
        sub_session_id: str,
        sub_session_data: dict,
        parent_agent: Any,
        parent_session_id: str,
    ) -> dict:
        """Merge the parent's live context_vars into a sub-session and persist.

        Called before continuing a sub-agent so its prompt renders from the
        CURRENT orchestration state. Mutates ``sub_session_data`` in place and
        returns the merged vars (empty dict if nothing to apply).
        """
        parent_live: dict = {}
        if parent_agent is not None and hasattr(parent_agent, "_session_tracker"):
            try:
                parent_live = parent_agent._session_tracker.get_session_template_vars(
                    parent_session_id,
                ) or {}
            except Exception as e:
                logger.debug(
                    "Could not read parent live context_vars for %s: %s",
                    parent_session_id, e,
                )

        # Merged into the record as it is now, one write at a time (SessionService.save_lock):
        # the caller's copy is from before a rename, a /vars or a save since, which it would undo
        session_manager = self._session_service.session_manager
        async with self._session_service.save_lock(sub_session_id):
            record = await session_manager.load_session(user_id, sub_session_id)
            merged, new_inherited = self.merge_parent_context_vars(record, parent_live)
            changed = (
                merged != (record.get("context_vars") or {})
                or new_inherited != (record.get("context_vars_inherited") or {})
            )
            if changed:
                record["context_vars"] = merged
                record["context_vars_inherited"] = new_inherited
                await session_manager.save_session(record)
        sub_session_data["context_vars"] = merged
        sub_session_data["context_vars_inherited"] = new_inherited
        if changed:
            logger.debug(
                "Refreshed context_vars for sub-session %s from parent %s: %s",
                sub_session_id, parent_session_id, sorted(merged),
            )
        return merged

    async def _link_sub_to_parent(
        self,
        user_id: str,
        parent_session_id: str,
        sub_session_id: str,
        agent_type: str,
        task_summary: str,
        child_depth: int,
        creator_plugin: Optional[str] = None
    ) -> None:
        """Add sub-agent metadata to parent session.

        Args:
            user_id: User ID
            parent_session_id: Parent session ID
            sub_session_id: Sub-session ID
            agent_type: Agent type
            task_summary: Task summary (truncated to 100 chars)
            child_depth: Nesting depth of child
            creator_plugin: Name of the sub_agent_manager plugin that created this sub-agent
        """
        session_manager = self._session_service.session_manager
        
        # Use atomic metadata update to avoid "lost update" race condition
        # This only updates metadata.sub_agents, not the whole session
        sub_agent_info = {
            sub_session_id: {
                "instance_id": sub_session_id,
                "agent_type": agent_type,
                "created_at": datetime.now(UTC).isoformat(),
                "last_used": datetime.now(UTC).isoformat(),
                "status": "active",
                "task_summary": task_summary[:100],
                "depth": child_depth,
                "message_count": 0,  # Will be updated after first LLM interaction
                "creator_plugin": creator_plugin  # Track which sub_agent_manager created this
            }
        }
        
        await session_manager.update_session_metadata(
            user_id, 
            parent_session_id, 
            {"sub_agents": sub_agent_info}
        )

        logger.debug(f"Updated parent {parent_session_id} with sub-agent {sub_session_id} (depth={child_depth})")

    async def list_sub_sessions(
        self,
        parent_session_id: str,
        include_completed: bool = False,
        creator_plugin: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """List sub-sessions by loading parent metadata.

        Args:
            parent_session_id: Parent session ID
            include_completed: Include archived/completed sessions
            creator_plugin: If set, only return sub-agents created by this plugin instance

        Returns:
            List of sub-agent metadata dicts
        """
        user_id = self._extract_user_id(parent_session_id)
        session_manager = self._session_service.session_manager

        try:
            parent_data = await session_manager.load_session(user_id, parent_session_id)
        except (FileNotFoundError, Exception) as e:
            # Parent session doesn't exist yet (new session) or other session error - normal during initialization
            if "not found" in str(e).lower():
                logger.debug(f"Parent session {parent_session_id} not found (likely new session)")
                return []
            raise

        sub_agents = parent_data.get("metadata", {}).get("sub_agents", {})

        result = []
        for instance_id, metadata in sub_agents.items():
            if not include_completed and metadata.get("status") not in OPEN_STATUSES:
                continue
            # Filter by creator_plugin if specified
            if creator_plugin and metadata.get("creator_plugin") != creator_plugin:
                continue
            result.append(metadata)

        # Sort by last_used (most recent first)
        result.sort(key=lambda x: x.get("last_used", ""), reverse=True)

        logger.debug(f"Listed {len(result)} sub-sessions for parent {parent_session_id}")

        return result

    async def _make_room(
        self, parent_session_id: str, existing_sub_agents: dict[str, Any], agent_type: str
    ) -> None:
        """Room for one more active sub-agent of `agent_type` among the parent's
        `existing_sub_agents`: under both limits, or -- with auto_archive_on_limit -- once the
        oldest active one is archived. SubAgentLimitReached otherwise. The caller holds the
        parent's lock from its count to the entry that takes the room."""
        active_sub_agents = [
            sub_id for sub_id, sub_meta in existing_sub_agents.items()
            if sub_meta.get("status") == "active"
        ]

        # The type's limit first: archiving the oldest of that type makes room under both. The
        # session's first archived the oldest of any type, and with the type still full, one more.
        active_agents_of_type = [
            sub_id for sub_id in active_sub_agents
            if existing_sub_agents[sub_id].get("agent_type") == agent_type
        ]

        if len(active_agents_of_type) >= self.max_sub_agents_per_type:
            if self.auto_archive_on_limit:
                oldest_id = self._find_oldest_sub_agent(existing_sub_agents, active_agents_of_type)
                if not await self._archive_sub_agent(parent_session_id, oldest_id):
                    raise SubAgentLimitReached(
                        f"Maximum number of active sub-agents of type '{agent_type}' "
                        f"({self.max_sub_agents_per_type}) reached, and the oldest "
                        f"({oldest_id}) could not be archived to make room."
                    )
                active_sub_agents = [sid for sid in active_sub_agents if sid != oldest_id]
                logger.info(
                    f"Auto-archived sub-agent {oldest_id} (type limit: {agent_type}) "
                    f"to make room in parent {parent_session_id}"
                )
            else:
                raise SubAgentLimitReached(
                    f"Maximum number of active sub-agents of type '{agent_type}' "
                    f"({self.max_sub_agents_per_type}) reached. "
                    f"Active sub-agents: {active_agents_of_type}"
                )

        # Check total session limit
        if len(active_sub_agents) >= self.max_sub_agents_per_session:
            if self.auto_archive_on_limit:
                oldest_id = self._find_oldest_sub_agent(existing_sub_agents, active_sub_agents)
                if not await self._archive_sub_agent(parent_session_id, oldest_id):
                    # Nothing was archived, so no room was made. Spawning anyway puts the
                    # session over the limit it asked for, quietly and for good.
                    raise SubAgentLimitReached(
                        f"Maximum number of active sub-agents per session "
                        f"({self.max_sub_agents_per_session}) reached, and the oldest "
                        f"({oldest_id}) could not be archived to make room."
                    )
                logger.info(
                    f"Auto-archived sub-agent {oldest_id} (session limit) "
                    f"to make room in parent {parent_session_id}"
                )
            else:
                raise SubAgentLimitReached(
                    f"Maximum number of active sub-agents per session "
                    f"({self.max_sub_agents_per_session}) reached. "
                    f"Active sub-agents: {len(active_sub_agents)}"
                )

    async def reopen_sub_session(self, parent_session_id: str, instance_id: str) -> None:
        """Open an instance for a new run: status "active", and no activity left from an earlier
        one -- an activity from a run that never recorded its end reads as a crash to every
        process until the new run takes its lock.

        One the limits do not count -- anything but active: failed, cancelled, interrupted,
        archived -- takes a place like a new one: the limits apply, and with auto_archive_on_limit
        the oldest active one makes room.
        A continue used to reopen past them, and the session stayed over its limit for good.
        It is the one write that opens an archived instance again (`_write_sub_agent`).
        """
        user_id = self._extract_user_id(parent_session_id)
        async with _parent_lock(parent_session_id):
            try:
                parent_data = await self._session_service.session_manager.load_session(user_id, parent_session_id)
            except Exception as error:
                # Nothing to count against -- and the write below fails the same way and says so.
                logger.warning(f"Could not load parent session {parent_session_id}: {error}")
                parent_data = {}
            sub_agents = parent_data.get("metadata", {}).get("sub_agents", {})
            metadata = sub_agents.get(instance_id)
            if metadata is not None and metadata.get("status") != "active":
                # the limits count active ones only, so it does not count itself
                await self._make_room(parent_session_id, sub_agents, metadata.get("agent_type", ""))
            await self._write_sub_agent(
                parent_session_id=parent_session_id,
                sub_session_id=instance_id,
                reopen=True,
                status="active",
                current_activity=None,
                activity_updated_at=None,
                error=None,  # an earlier run's: a later failure that stores none would report it
                ending_unread=None,  # continued, the caller has it: the bell of the job's process stops
            )

    async def update_sub_session_metadata(
        self,
        parent_session_id: str,
        sub_session_id: str,
        *,
        expect: Optional[dict[str, Any]] = None,
        **updates: Any
    ) -> bool:
        """Update sub-session metadata in parent session. False when it wrote nothing.

        Args:
            parent_session_id: Parent session ID
            sub_session_id: Sub-session ID
            expect: Write only while the stored entry still holds these values -- for a decision
                taken on an earlier read of it.
            **updates: Metadata fields to update (e.g., last_used, status)
        """
        async with _parent_lock(parent_session_id):
            return await self._write_sub_agent(
                parent_session_id=parent_session_id, sub_session_id=sub_session_id, expect=expect,
                **updates)

    async def _write_sub_agent(
        self, parent_session_id: str, sub_session_id: str, *, reopen: bool = False,
        expect: Optional[dict[str, Any]] = None, **updates: Any
    ) -> bool:
        """update_sub_session_metadata, for a caller that holds the parent's lock.

        An open status (OPEN_STATUSES) leaves an archived instance archived: a run that ended
        cleanly, a list that found its activity orphaned -- the archive may have landed while it
        ran, and undone it would take back the room it made. Only `reopen` opens one again, and
        reopen_sub_session applies the limits first. failed and cancelled are a run's verdict and
        are written.
        """
        user_id = self._extract_user_id(parent_session_id)
        session_manager = self._session_service.session_manager

        # First check if sub-session exists in parent metadata (via cache if available)
        try:
            parent_data = await session_manager.load_session(user_id, parent_session_id)
        except Exception as e:
            logger.warning(f"Could not load parent session {parent_session_id}: {e}")
            return False
            
        if "metadata" not in parent_data or "sub_agents" not in parent_data.get("metadata", {}):
            logger.warning(f"Parent session {parent_session_id} has no sub_agents metadata")
            return False
            
        if sub_session_id not in parent_data["metadata"]["sub_agents"]:
            logger.warning(f"Sub-session {sub_session_id} not found in parent metadata")
            return False

        # Build update for the specific sub-agent
        current_sub_agent = parent_data["metadata"]["sub_agents"][sub_session_id].copy()
        if expect and any(current_sub_agent.get(key) != value for key, value in expect.items()):
            logger.debug(f"Sub-session {sub_session_id} changed since it was read; not writing {updates}")
            return False
        if (not reopen and current_sub_agent.get("status") == "archived"
                and updates.get("status") in OPEN_STATUSES):
            updates = {key: value for key, value in updates.items() if key != "status"}
        current_sub_agent.update(updates)
        
        # Use atomic metadata update
        await session_manager.update_session_metadata(
            user_id,
            parent_session_id,
            {"sub_agents": {sub_session_id: current_sub_agent}}
        )

        logger.debug(f"Updated metadata for sub-session {sub_session_id}: {updates}")
        return True

    async def update_sub_agent_activity(
        self,
        parent_session_id: str,
        sub_session_id: str,
        activity: str | None,
        force: bool = False
    ) -> None:
        """Update current activity status for sub-agent (for live status display).
        
        Only saves when activity actually changes to avoid excessive session saves
        during streaming (e.g., repeated "Thinking..." events).

        Args:
            parent_session_id: Parent session ID
            sub_session_id: Sub-session ID
            activity: Activity description (e.g., "Thinking...", "Running tool: xyz") or None to clear
            force: If True, always save even if activity unchanged
        """
        last_activity = self._last_activity.get(sub_session_id)
        
        # Skip if activity hasn't changed (unless forced)
        if not force and activity == last_activity:
            return
        
        # Activity changed - update tracking and persist
        self._last_activity[sub_session_id] = activity
        
        # Clean up tracking when activity is cleared (session ended)
        if activity is None:
            self._last_activity.pop(sub_session_id, None)
        
        await self.update_sub_session_metadata(
            parent_session_id=parent_session_id,
            sub_session_id=sub_session_id,
            current_activity=activity,
            activity_updated_at=datetime.now(UTC).isoformat() if activity else None
        )
        logger.debug(f"Updated activity for {sub_session_id}: {activity}")

    def _find_oldest_sub_agent(self, sub_agents: dict, candidate_ids: list[str]) -> str:
        """Find the oldest sub-agent by created_at timestamp.

        Args:
            sub_agents: Dict of sub-agent metadata keyed by instance_id
            candidate_ids: IDs to search within

        Returns:
            Instance ID of the oldest sub-agent
        """
        candidates = [
            (sid, sub_agents[sid].get("created_at", ""))
            for sid in candidate_ids
        ]
        candidates.sort(key=lambda x: x[1])  # ascending = oldest first
        return candidates[0][0]

    async def _archive_sub_agent(self, parent_session_id: str, sub_session_id: str) -> bool:
        """Archive a sub-agent by setting its status to 'archived' in parent metadata. The caller
        holds the parent's lock (_make_room).

        Args:
            parent_session_id: Parent session ID
            sub_session_id: Sub-agent instance ID to archive
        """
        written = await self._write_sub_agent(
            parent_session_id=parent_session_id,
            sub_session_id=sub_session_id,
            status="archived",
            archived_at=datetime.now(UTC).isoformat()
        )
        if not written:
            # It says archived nowhere, so nothing is: telling anyone would have them tidy up
            # after an instance that is still listed, still counted and still pollable.
            logger.warning(f"Could not archive sub-agent {sub_session_id} in parent {parent_session_id}")
            return False
        logger.debug(f"Archived sub-agent {sub_session_id} in parent {parent_session_id}")
        return True

    async def _generate_instance_id(
        self,
        agent_type: str,
        instance_label: Optional[str]
    ) -> str:
        """Generate unique instance ID: sub_{label}_{counter}.

        Uses a class-level counter shared across ALL SubAgentManager instances
        to ensure uniqueness even when multiple parent sessions run in parallel.

        Args:
            agent_type: Agent type
            instance_label: Optional custom label

        Returns:
            Unique instance ID (short format, no parent ID)
        """
        import re

        # No await below, so nothing else of this loop runs in between: the counter needs no lock.
        session_manager = self._session_service.session_manager

        # Keep incrementing counter until we find a unique ID
        max_attempts = 1000
        for _ in range(max_attempts):
            # Increment class-level counter (shared across all instances)
            counter = SubAgentManager._class_counter
            counter = (_first_counter() if counter is None else counter) + 1
            SubAgentManager._class_counter = counter

            # Use custom label or auto-generate
            if instance_label:
                # Sanitize label (alphanumeric, underscore, hyphen only)
                sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', instance_label)
                instance_id = f"sub_{sanitized}_{counter:04d}"
            else:
                # Auto-generate: sub_{agent_type}_{counter}
                instance_id = f"sub_{agent_type}_{counter:04d}"

            # Check if this ID is already taken
            if not session_manager._session_id_exists_globally(instance_id):
                logger.debug(f"Generated unique instance ID: {instance_id} (class counter: {counter})")
                return instance_id
            else:
                logger.debug(f"Instance ID {instance_id} already exists, trying next counter...")

        # Fallback if we somehow can't find a unique ID
        raise ValueError(f"Failed to generate unique instance ID after {max_attempts} attempts")


    def _extract_user_id(self, session_id: str, params: Optional[dict] = None) -> str:
        """Extract user_id from injected params or session file location.

        Priority:
        1. Use _user_id from injected params (from ToolExecutionManager)
        2. Search session file in user directories
        3. Use 'anonymous' as fallback (NOT 'admin' for security)

        Args:
            session_id: Session ID to search for
            params: Optional tool call params with injected _user_id

        Returns:
            User ID (from params, directory name, or 'anonymous')
        """
        # Priority 1: Use injected user_id from tool params — but only if it
        # is a non-empty string. ``None`` or empty values must fall through to
        # the directory search; otherwise load_session(None, ...) ends up in
        # ``data/sessions/None/`` (which never exists), and the subsequent
        # cross-user check raises SessionPermissionError because the real
        # session lives under the actual user_id (e.g. ``cli_user``).
        if params and "_user_id" in params:
            raw = params["_user_id"]
            if raw and isinstance(raw, str) and raw.strip():
                user_id = raw.strip()
                logger.debug(f"Using user_id '{user_id}' from injected params")
                return user_id
            logger.debug(
                f"_user_id in params but value is empty/None ({raw!r}) — "
                "falling through to directory search"
            )

        # Priority 2: Search all user directories for this session_id
        storage_path = self._session_service.session_manager.storage_path

        for user_dir in storage_path.iterdir():
            if user_dir.is_dir() and not user_dir.name.startswith('.'):
                session_file = user_dir / f"{session_id}.json"
                if session_file.exists():
                    logger.debug(
                        f"Found session {session_id} in user directory: {user_dir.name}"
                    )
                    return user_dir.name

        # Priority 3: Use 'anonymous' as safe fallback (NOT 'admin' for security)
        # This happens when session doesn't exist yet (e.g., CLI ephemeral sessions)
        logger.info(
            f"No user_id found for session {session_id}, using 'anonymous'"
        )
        return "anonymous"
