"""Who may run an agent: the per-agent role gate (``AgentMetadata.min_role``).

One decision for every way a run starts -- POST /run and /events, a chat
command, a session created for an agent, a sub-agent the SAM spawns, an agent
called as another agent's tool, a stategraph machine, a woken agent-cli run --
so that no entry answers the question differently. The HTTP entries refuse
early with a 403; ``Agent.run_events`` asks again before it touches anything,
as the backstop for every path that reaches an agent without an endpoint.

No FastAPI in here on purpose: the core (``Agent.run_events``) and plugins that
serve their own routes (openai_api) call it with whatever identity they hold.

The caller (``who``) comes in the shapes the system carries:

* an account object with a ``role`` -- ``User``/``UserInDB`` from a token,
  ``AnonymousUser`` from the endpoint enforcer;
* a user NAME, as sessions and the request-ownership map store it: the local
  operator ``cli_user`` (only in agent-cli and agent-run, see
  ``local_operator_trusted``), ``anonymous``, or an account name, whose
  CURRENT role is read from the user store -- a run woken hours after its
  session was created answers to the role the account has now;
* None: nobody could be named, which is refused. A run without any identity
  cannot start a gated agent.

Fail closed: an unknown required role, an unknown account role, a missing or
inactive account, a user store that cannot be read -- each is "not enough".
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

#: The identity agent-cli and agent-run run under by default (``--session-user``).
#: Both are local processes started by whoever operates the installation, not
#: remote callers, so the gate trusts them -- there, and only there: a process
#: opts in with :func:`local_operator_trusted` (agent_cli.main, agent_run.main).
#: In the API process the name is a name like any other, and no account holds it.
#:
#: The name is reserved: no account can be registered under it
#: (UserDatabase.RESERVED_USERNAMES) -- but only since the reservation exists,
#: and it is checked at registration, not at login. So even where it is trusted,
#: the name counts as the operator only while the user store holds no account of
#: it; an older account under it answers with its own role, like any other.
LOCAL_OPERATOR = "cli_user"

#: Whether this process takes LOCAL_OPERATOR for the local operator. Off unless
#: an entry point of a local process switches it on.
_local_operator_trusted = False

#: _stored_account's answer when the user store exists but cannot be read.
_UNREADABLE = object()

#: The identity of an unauthenticated web caller, as sessions store it.
ANONYMOUS = "anonymous"


def _rank(role: Any) -> int:
    """The role's rank in ROLE_HIERARCHY; 0 for anything that is not a known role."""
    from agent_system.auth.enforcement import ROLE_HIERARCHY

    value = getattr(role, "value", role)  # UserRole is a str enum; str() of it is "UserRole.ADMIN"
    if not isinstance(value, str):
        return 0
    return ROLE_HIERARCHY.get(value.strip().lower(), 0)


def _role_text(role: Any) -> str:
    value = getattr(role, "value", role)
    return str(value)


def _stored_account(username: str, auth: Any) -> Any:
    """(role, is_active) of *username* in the user store; None when there is no
    such account (or no store at all); _UNREADABLE when the store cannot be read.

    The process's own store when the app set one up (``database._db``). Otherwise
    the configured file, opened read-only and only if it exists: ``get_db()``
    would create a database -- in the working directory of whatever process asks,
    agent-cli included -- and an empty store answers "no such account" anyway.
    """
    from agent_system.auth import database

    store = database._db
    if store is not None:
        try:
            user = store.get_user_by_username(username)
        except Exception:  # noqa: BLE001 - a broken row (unknown role) or store is a refusal, not a crash
            logger.warning("agent gate: could not read account %r from the user store", username, exc_info=True)
            return _UNREADABLE
        return (user.role, bool(user.is_active)) if user is not None else None

    path = Path(getattr(auth, "database_path", None) or "")
    if not str(path) or not path.is_file():
        return None
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute("SELECT role, is_active FROM users WHERE username = ?",
                               (username,)).fetchone()
    except sqlite3.Error:
        logger.warning("agent gate: could not read account %r from %s", username, path, exc_info=True)
        return _UNREADABLE
    return (row[0], bool(row[1])) if row is not None else None


@contextmanager
def local_operator_trusted() -> Iterator[None]:
    """While inside, this process takes LOCAL_OPERATOR for the local operator.

    For the entry points of local processes -- agent-cli, agent-run -- around
    everything they run. Scoped rather than switched on for good: a test that
    drives one of those entry points in the test process must not leave the
    trust behind for the next one, which may stand in for the API.
    """
    global _local_operator_trusted
    before, _local_operator_trusted = _local_operator_trusted, True
    try:
        yield
    finally:
        _local_operator_trusted = before


def agent_run_denial(min_role: Optional[str], who: Any, auth: Optional[Any], *,
                     local_operator: Optional[bool] = None) -> Optional[str]:
    """Why *who* may not run an agent that requires *min_role*, or None if it may.

    Args:
        min_role: the agent's ``metadata.min_role`` -- None means no gate.
        who: the caller: an account object with ``role`` (User, UserInDB,
            AnonymousUser), a user name (``cli_user``, ``anonymous``, an
            account name), or None.
        auth: the ``AuthConfig`` in force; None or disabled means no gate --
            without accounts there is no role to compare.
        local_operator: whether ``cli_user`` may count as the local operator;
            None is this process's own setting (:func:`local_operator_trusted`).
            True is for judging a run ANOTHER process will start -- a wake
            spawns agent-cli, which trusts it.

    Returns:
        A human-readable reason for the refusal, or None.
    """
    if min_role is None:
        return None
    if auth is None or not getattr(auth, "enabled", False):
        return None

    required = _rank(min_role)
    if required == 0:
        return f"it requires the role '{_role_text(min_role)}', which is no known role"
    needs = f"it requires the role '{_role_text(min_role)}'"

    if who is None or (isinstance(who, str) and not who.strip()):
        return f"{needs} and the caller is not identified"

    if hasattr(who, "role") and not isinstance(who, str):
        name = getattr(who, "username", None) or "the caller"
        if getattr(who, "is_active", True) is False:
            return f"{needs} and the account '{name}' is inactive"
        role = who.role
    elif isinstance(who, str):
        name = who
        if who == ANONYMOUS:
            anonymous = getattr(auth, "anonymous_access", None)
            if not getattr(anonymous, "enabled", False):
                return f"{needs} and anonymous access is disabled"
            role = getattr(anonymous, "role", None)
        else:
            account = _stored_account(who, auth)
            if account is _UNREADABLE:
                return f"{needs} and the user store could not be read"
            if account is None:
                trusted = _local_operator_trusted if local_operator is None else local_operator
                if who == LOCAL_OPERATOR and trusted:
                    return None  # a local process, and no account holds the name
                return f"{needs} and there is no account '{who}'"
            role, active = account
            if not active:
                return f"{needs} and the account '{who}' is inactive"
    else:
        return f"{needs} and the caller cannot be identified ({type(who).__name__})"

    if _rank(role) < required:
        return f"{needs}; '{name}' has the role '{_role_text(role)}'"
    return None


def may_run_agent(min_role: Optional[str], who: Any, auth: Optional[Any]) -> bool:
    """True when *who* may run an agent that requires *min_role* (see agent_run_denial)."""
    return agent_run_denial(min_role, who, auth) is None


def agent_min_role(registry: Any, name: str) -> Optional[str]:
    """The ``min_role`` of the registered agent *name*, or None (no gate, no such agent, no agent).

    Asked the way the other walkers ask: the registry's view first -- it answers
    from the instance when there is one and from the declaration of a lazy agent
    that has not been built, so asking builds nothing -- and the instance only for
    a registry the view cannot answer for. A server that is not an ``Agent``
    carries no gate: it cannot be run.
    """
    from agent_system.runtime import ServerView

    describe = getattr(registry, "describe", None)
    if callable(describe):
        try:
            view = describe(name)
        except Exception:  # noqa: BLE001 - the fallback below asks the instance
            view = None
        # isinstance, not "is not None": a Mock registry answers with a Mock.
        if isinstance(view, ServerView):
            return view.min_role
    try:
        instance = registry.get(name)
    except Exception:  # noqa: BLE001 - an unknown name is no agent, so no gate
        return None
    from agent_system.servers.agent.server import Agent

    if isinstance(instance, Agent):
        return getattr(instance, "min_role", None)
    return None
