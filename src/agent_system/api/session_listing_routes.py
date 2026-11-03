"""The session list and how it is organised: the sidebar's roots (/hierarchy), a node's children, the chat's
``/sessions`` (/listing), a session named by its title (/resolve), the viewer's folders, and the tree of
context vars below one session that the Session Info panel shows.

Routes of session_endpoints' ``session_router``, which registers them with ``add_routes`` ahead of its
``/{session_id}`` routes: FastAPI matches in order, and that one would take "hierarchy" for a session id.
Registered on that router rather than included as one of their own, so its ``routes`` stay one flat list.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from agent_system.api.app_context import registered_agent
from agent_system.api.dependencies import get_session_manager
from agent_system.api.session_folders import SessionFolders, declared_folders, read_folders, write_folders
from agent_system.api.session_http import session_errors_as_http
from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User
from agent_system.auth.session_access import viewer
from agent_system.cli_utils.session_listing import (
    in_chat_selector,
    most_left_out,
    parse_listing,
    split_for_chat,
)

logger = logging.getLogger(__name__)

#: How many files the descendants walk may read at the same time -- session files
#: and sub-indexes alike. The reads run together because they are waiting on disk,
#: not working, but a session file can be megabytes and a tree here can have
#: thousands of nodes: unbounded, one panel's request would decide how much of this
#: process's memory is parsed JSON, and would fill the shared thread pool that every
#: session save also queues in.
_DESCENDANT_READS_AT_ONCE = 16


async def _build_descendants_context_vars(
    session_manager,
    tool_registry,
    user_id: str,
    root_session_id: str,
) -> List[Dict[str, Any]]:
    """Walk the session hierarchy below ``root_session_id`` and collect
    context_vars for every descendant.

    Returns a recursive tree structure with one entry per direct child:

        [
            {
                "session_id": "sub_v5b_story_designer_6850002",
                "agent_name": "v5b_story_designer",
                "context_vars": {...},   # persisted + live-merged from tracker
                "children": [ ... recursive ... ]
            },
            ...
        ]

    For each session the persisted ``context_vars`` are merged with whatever
    the agent's session_tracker currently holds, so live updates show up
    even before the next checkpoint write.

    The topology is walked DOWN from the root, one parent at a time, because a
    sub-index is exactly one parent's children: ``list_child_sessions`` reads
    that one file. It used to come from ``list_sessions``, which merges the main
    index with EVERY per-parent sub-index -- for this user 51 MB across 1303
    files, about a second, to find the handful of ids below one root. Measured
    on that store: 978 ms for a root with two descendants, now under 40 ms.
    """
    if not session_manager:
        return []

    seen: set[str] = {root_session_id}
    reading = asyncio.Semaphore(_DESCENDANT_READS_AT_ONCE)

    async def children_of(parent_id: str) -> List[Dict[str, Any]]:
        """This parent's children -- one sub-index file, not the whole store.

        ``seen`` is not about the tree, which cannot loop: it is about a stale
        sub-index naming a session further up. Walking that would not end.
        """
        try:
            async with reading:
                # Under the same bound as the session files: a sub-index is a file read
                # too, and the biggest one on the user's store is 943 KB -- bigger than
                # most session files. Ungated, a node with a thousand children put a
                # thousand reads into the shared executor in one tick, ahead of whatever
                # a run was trying to save.
                listed = await session_manager.list_child_sessions(
                    user_id, parent_id, annotate_children=False)
        except Exception as err:  # noqa: BLE001 -- one unreadable branch, not a failed load
            # Warning, not debug: this is the only thing between the panel and the
            # truth, and what it hides looks exactly like a session with no children.
            logger.warning("Could not list the sub-sessions of %s: %s", parent_id, err)
            return []
        fresh = []
        for child in listed:
            sid = child.get("session_id")
            if sid and sid not in seen:
                seen.add(sid)
                fresh.append(child)
        return fresh

    async def _load_persisted_vars(sid: str) -> Dict[str, Any]:
        async with reading:
            try:
                data = await session_manager.load_session(user_id, sid)
                cv = data.get("context_vars")
                return cv if isinstance(cv, dict) else {}
            except Exception:
                return {}

    # Cache tool_registry agent lookups so we don't re-resolve per node
    agent_cache: Dict[str, Any] = {}

    def _resolve_agent(name: Optional[str]):
        if not name or not tool_registry:
            return None
        if name in agent_cache:
            return agent_cache[name]
        try:
            agent_cache[name] = registered_agent(tool_registry, name)
        except Exception:
            agent_cache[name] = None
        return agent_cache[name]

    def _live_merge_vars(agent: Any, sid: str, persisted: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(persisted) if isinstance(persisted, dict) else {}
        if agent is None or not hasattr(agent, "_session_tracker"):
            return merged
        try:
            runtime = agent._session_tracker.get_session_template_vars(sid) or {}
        except Exception:
            runtime = {}
        if runtime:
            merged.update(runtime)
        return merged

    async def _walk(parent_id: str) -> List[Dict[str, Any]]:
        children = await children_of(parent_id)
        if not children:
            return []
        # One node's session file and its own children are independent of its
        # siblings', and every one of them is a file read that spends its time
        # waiting. Run them together: a branch then costs its DEPTH in round
        # trips rather than its size.
        async def node(child: Dict[str, Any]) -> Dict[str, Any]:
            # No guard on a missing session_id: children_of only passes on the ones
            # that have it. The old shape needed the guard, because its children came
            # from a list filtered on the PARENT id.
            sid = child["session_id"]
            agent_name = child.get("agent_name")
            agent = _resolve_agent(agent_name)
            persisted, below = await asyncio.gather(_load_persisted_vars(sid), _walk(sid))
            return {
                "session_id": sid,
                "agent_name": agent_name,
                "context_vars": _live_merge_vars(agent, sid, persisted),
                "children": below,
            }

        return list(await asyncio.gather(*(node(c) for c in children)))

    return await _walk(root_session_id)


def _session_node(s: Dict[str, Any]) -> Dict[str, Any]:
    """Index metadata -> sidebar node. ``children`` stays empty: descendants are
    loaded on demand via ``/{session_id}/children``."""
    profile = s.get("llm_profile")
    return {
        "session_id": s.get("session_id"),
        "user_id": s.get("user_id"),
        "title": s.get("title"),
        "agent_name": s.get("agent_name"),
        "llm_profile": profile[0] if isinstance(profile, list) else profile,
        "created_at": s.get("created_at"),
        "updated_at": s.get("updated_at"),
        "message_count": s.get("message_count", 0),
        "last_agent_response": s.get("last_agent_response"),
        "tags": s.get("tags", []),
        "depth": s.get("depth", 1),
        "context_vars": s.get("context_vars", {}),
        "has_children": bool(s.get("has_children")),
        "children": [],
        # A sub-session's runs by the request id each opened with (SessionIndex.row).
        "runs": s.get("runs", []),
        # The list's folders (api/session_folders.py): the one the viewer chose ("" none, absent: by group), and
        # the group the session files by -- its starter's stamp, else its agent's (list_sessions_hierarchy).
        "folder": s.get("folder"),
        "category": s.get("category"),
    }


def _agent_groups(config: Any, names: set) -> Dict[str, Optional[str]]:
    """Each agent's group (``metadata.category``) from the merged config; an agent gone from it has none."""
    from agent_system.config.settings import get_tool_server_config
    groups: Dict[str, Optional[str]] = {}
    for name in names:
        try:
            cfg = get_tool_server_config(name, config)
        except Exception:
            cfg = None
        groups[name] = cfg.metadata.category if cfg is not None and cfg.metadata else None
    return groups


async def list_sessions_hierarchy(
    request: Request,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """Top-level sessions for the sidebar — ROOTS only.

    Each root carries ``has_children`` so the UI can render an expand toggle;
    the sub-sessions themselves are fetched on demand via
    ``/{session_id}/children``. Previously this returned the fully nested tree,
    which meant reading every per-parent sub-index and serialising tens of
    thousands of nodes that stay hidden until a node is expanded.
    """
    user_id = viewer(current_user)
    try:
        roots = await session_manager.list_root_sessions(user_id)
        nodes = [_session_node(s) for s in roots]
        # once per agent, not per session: a config lookup merges the agent's whole inheritance chain
        unstamped = {node["agent_name"] for node in nodes if not node["category"] and node["agent_name"]}
        config = getattr(request.app.state, "config", None)
        if unstamped and config is not None:
            groups = await run_in_threadpool(_agent_groups, config, unstamped)
            for node in nodes:
                node["category"] = node["category"] or groups.get(node["agent_name"])
        return {
            "sessions": nodes,
            "root_count": len(roots),
        }
    except Exception as e:
        logger.exception("Failed to list sessions hierarchy: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


def _declared(request: Request):
    """The folders the plugins declare ([session_folder] in their plugin.toml), under the configured roots."""
    config = getattr(request.app.state, "config", None)
    return declared_folders(config.plugins.plugin_dirs if config is not None and config.plugins else [])


async def get_session_folders(
    request: Request,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """The viewer's session folders: the ones the plugins declare that they kept, then their own."""
    return read_folders(session_manager.storage_path, viewer(current_user), _declared(request))


async def put_session_folders(
    request: Request,
    folders: SessionFolders,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """Make these the viewer's session folders: names, icons and claimed groups; a declared folder left out is
    deleted for them. A session in a folder removed falls back to its group's folder, if any claims it."""
    try:
        return write_folders(session_manager.storage_path, viewer(current_user), folders, _declared(request))
    except ValueError as exc:  # more folders of their own than a user may have
        raise HTTPException(status_code=422, detail=str(exc))


class SessionFolderChoice(BaseModel):
    #: a folder's id; "" out of every folder; null back to the folder its group files it in
    folder: Optional[str] = None


async def put_session_folder(
    request: Request,
    session_id: str,
    choice: SessionFolderChoice,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """Put a session into a folder -- without touching ``updated_at``: moved is not worked in, and the list
    sorts by it."""
    user_id = viewer(current_user)
    shown = read_folders(session_manager.storage_path, user_id, _declared(request)).folders
    if choice.folder and choice.folder not in {f.id for f in shown}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"No folder {choice.folder!r}")
    with session_errors_as_http(session_id):
        await session_manager.update_session_metadata(user_id, session_id, {"folder": choice.folder}, touch=False)
    return {"status": "updated", "session_id": session_id, "folder": choice.folder}


async def list_session_children(
    session_id: str,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """Direct sub-sessions of one parent — reads exactly that parent's sub-index."""
    user_id = viewer(current_user)
    try:
        children = await session_manager.list_child_sessions(user_id, session_id)
        return {"sessions": [_session_node(s) for s in children], "count": len(children)}
    except ValueError as e:  # invalid session id
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception("Failed to list children of %s: %s", session_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


async def resolve_session(
    ref: str = "",
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """The id of the session ``ref`` names -- an id, or a session's TITLE.

    Before "/{session_id}", or that route would take "resolve" for an id.
    The rule itself lives in SessionManager.resolve_session_ref, so the chat
    in the browser finds a session by the name its person gave it exactly as
    the terminal does.
    """
    user_id = viewer(current_user)
    others: List[str] = []
    found = await session_manager.resolve_session_ref(user_id, ref, others=others)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"no session called {ref!r}")
    # A title several sessions share was answered with the newest of them;
    # the others are named so the caller can say that it chose.
    return {"session_id": found, "others": others}


async def list_sessions_for_chat(
    request: Request,
    count: str = "",
    agent: str = "",
    current: str = "",
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """The chat's ``/sessions [count|all]``: top-level sessions, newest first.

    Only those of agents meant for chat -- the rule and the functions are the
    terminal's (cli_utils.session_listing), so both chats list the same.
    ``agent`` is the one the chat runs on and ``current`` the session it is
    in: both stay whatever their agent. The runs left out are counted, with
    the agent most of them ran on; ``count=all`` lists every one.
    """
    limit, everything, complaint = parse_listing(count)
    if complaint is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"count must be a number or 'all', got {complaint!r}")
    user_id = viewer(current_user)
    sessions = await session_manager.list_root_sessions(user_id)
    shown = None if everything else in_chat_selector(
        getattr(request.app.state, "runtime", None), keep=(agent,))
    listable, left_out = split_for_chat(sessions, shown, current or None)
    listed = listable if limit <= 0 else listable[:limit]
    most = most_left_out(left_out)
    return {
        "sessions": [_session_node(s) for s in listed],
        "total": len(listable),
        "left_out": len(left_out),
        "most_left_out": {"agent": most[0], "count": most[1]} if most else None,
    }


def add_routes(router: APIRouter) -> None:
    """Register these routes on *router* (session_endpoints' ``session_router``), in this order."""
    router.get("/hierarchy", response_model=Dict[str, Any])(list_sessions_hierarchy)
    router.get("/folders", response_model=SessionFolders)(get_session_folders)
    router.put("/folders", response_model=SessionFolders)(put_session_folders)
    router.put("/{session_id}/folder", response_model=Dict[str, Any])(put_session_folder)
    router.get("/{session_id}/children", response_model=Dict[str, Any])(list_session_children)
    router.get("/resolve", response_model=Dict[str, Any])(resolve_session)
    router.get("/listing", response_model=Dict[str, Any])(list_sessions_for_chat)
