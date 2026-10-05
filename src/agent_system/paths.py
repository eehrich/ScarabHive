"""Where the project is, and where the person stood when they typed the command.

Those used to be one and the same. Every entry point was started from the
repository root, so ``Path.cwd()`` answered both questions at once and nothing
had to tell them apart. Started from anywhere else they come apart, and the
code silently gets the wrong one of the two: a plugin config that says
``data/writer/books.db`` is resolved by whoever opens it, so the database is
created wherever the person happened to stand -- empty, and every query
against it reads zero rows without ever failing.

There is no list of keys that would fix that. The paths live as relative
strings in forty plugin configs and are resolved at open time, invisible to
any search. So the process moves instead: `enter_project` puts it where those
strings have always been resolved from, and remembers the directory the person
came from for the things that mean THEM -- today that is every path they type
on the command line.

What this does NOT do is let the agent WORK where the person stands: the
terminal's shell and file_ops' allowlist still start in the project. Their
directories are configured, and opening them up is a question about the
sandbox, not about where a config file lives.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The repository, anchored to this file instead of to the working directory.
#: The same value thirteen places in the core compute for themselves today
#: (``parents[2]``/``parents[3]``, depending on how deep the module sits).
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    """The config an entry point loads when none is named: AGENT_CONFIG_PATH, else the project's
    config/config.yaml -- not the working directory's: a module run from elsewhere must find the
    same file the API loads."""
    return Path(os.environ.get("AGENT_CONFIG_PATH") or PROJECT_ROOT / "config" / "config.yaml")

#: Where the process started, once it has moved away from there. None means
#: it never did -- a test, the API, anything embedding this -- and then the
#: working directory still IS where the person is, so nothing here may
#: override it. Remembering an import-time directory instead made the chat's
#: /export write its transcript into the repository from a test that had
#: chdir'd somewhere else entirely.
_launch_dir: Path | None = None


def launch_dir() -> Path:
    """The directory the command was started in -- what the person means by ".".

    After `enter_project` that is no longer the working directory, which is
    the repository. Before it, and in every process that never calls it, the
    two are the same and the working directory is the honest answer.
    """
    return _launch_dir if _launch_dir is not None else Path.cwd()


def user_path(value: str | os.PathLike[str]) -> Path:
    """Resolve a path the PERSON gave against the directory they gave it in.

    An attachment or a config named on the command line is typed where they
    stand, not where the process ended up. Absolute paths and ``~`` keep
    meaning what they say.

    ``os.path.expanduser``, not the pathlib one: that raises
    RuntimeError("Could not determine home directory") for a name it cannot
    resolve, and ``~$notes.md`` -- the lock file Word leaves beside a document
    -- is such a name whenever USERNAME differs from the profile directory. A
    file name is not allowed to end the process that was handed it.
    """
    path = Path(os.path.expanduser(str(value)))
    # The branch says what happens to an absolute path; pathlib would do the
    # same thing silently (a `/` with an absolute right side drops the left),
    # and a rule nobody can see is a rule nobody keeps. No test can tell the
    # two apart -- that is why it is written down here instead.
    return path if path.is_absolute() else launch_dir() / path


def enter_project() -> Path:
    """Run the rest of the process from the repository; return where it began.

    Safe for everything that resolves a relative path today, and provably so:
    until now every entry point ran from the repository root, so this puts the
    process where those paths were already being resolved from. What changes
    is only that it now holds when the person starts somewhere else.

    Being called again does not overwrite the remembered directory: the test
    is where the process stands, not a flag. Standing in the project already
    means either a second call or a person who started there -- both answer
    with the same directory -- while a test that runs one entry point after
    another from different directories gets each one right.
    """
    global _launch_dir
    if Path.cwd() != PROJECT_ROOT:
        _launch_dir = Path.cwd()
    # A config named relatively is named where the person stands, and
    # agent-run has no --config at all, so this variable is its only way to
    # name one. Rewritten before the move, and inherited by every sub-process.
    env_config = os.environ.get("AGENT_CONFIG_PATH")
    if env_config and not Path(env_config).is_absolute():
        os.environ["AGENT_CONFIG_PATH"] = str(user_path(env_config))
    if not (PROJECT_ROOT / "config").is_dir():
        # Installed rather than checked out: parents[2] then points into the
        # environment, and moving there would recreate the very shadow tree
        # this exists to prevent -- inside site-packages. Stay put; that is
        # exactly the behaviour of every release before this one.
        logger.warning("No project at %s -- running from %s, as before",
                       PROJECT_ROOT, launch_dir())
        return launch_dir()
    started_in = launch_dir()
    os.chdir(PROJECT_ROOT)
    return started_in


# --- The data directory -------------------------------------------------------
#
# Everything the system keeps -- sessions, databases, caches, the writer's
# books -- lives under one directory, written as "data/..." in code and
# configuration alike. One setting moves all of it:
#
#   AGENT_DATA_DIR (environment)  >  paths.data_dir (config.yaml)  >  data
#
# A relative setting is relative to the project, like every relative path in
# the configuration. With nothing set, nothing changes: a data path stays the
# relative "data/..." it always was, resolved against the working directory
# (tests that chdir into a temporary directory rely on exactly that).

#: The environment variable that names the data directory. It wins over the
#: configuration, as a machine's own setting should: a server, a second
#: instance on the same checkout, a test run.
DATA_DIR_ENV = "AGENT_DATA_DIR"

_UNREAD = object()
#: ``paths.data_dir`` of the configuration this process runs with. Set by the
#: first load_settings; a process that never loads settings reads the master
#: config on first use instead (provisionally -- a load still overrides it).
_config_data_dir: object = _UNREAD
#: Whether a load_settings has recorded the setting. Only the first one does.
_settled = False


def set_config_data_dir(value: str | None) -> None:
    """Record ``paths.data_dir`` of the configuration the process runs with.

    The first load decides; later ones leave it alone. A helper that calls
    ``load_settings()`` bare reads the default config, not the one the process
    was started with (``--config``, ``build_app("config_x/...")``), and a hot
    reload of a changed setting would move half the process and leave the
    other half -- the data directory needs a restart to change.
    """
    global _config_data_dir, _settled
    if _settled:
        return
    text = "" if value is None else str(value).strip()
    _config_data_dir = text or None
    _settled = True


def configured_data_dir() -> Path | None:
    """The data directory if one is configured; None means the default, ``data``.

    Read on every call rather than at import: a module-level path would freeze
    whatever was known when the module happened to be imported -- before
    ``--config`` was parsed, before a test set the variable.
    """
    global _config_data_dir
    value = os.environ.get(DATA_DIR_ENV, "").strip()
    if not value:
        if _config_data_dir is _UNREAD:
            # Deferred: settings imports the config models, and this module
            # is imported by every entry point long before they are needed.
            # Provisional, not settled: the load that follows may name another
            # config (--config).
            from agent_system.config.settings import master_data_dir
            _config_data_dir = (master_data_dir() or "").strip() or None
        value = _config_data_dir  # type: ignore[assignment]
    if not value:
        return None
    path = Path(os.path.expanduser(str(value)))
    return path if path.is_absolute() else PROJECT_ROOT / path


def data_path(*parts: str) -> Path:
    """A path in the data directory: ``data_path("writer", "books.db")``.

    Relative when nothing is configured -- ``data/writer/books.db``, exactly
    the literal it replaces. A site that must not depend on the working
    directory joins it onto PROJECT_ROOT: a configured directory is absolute
    and wins that join, the default gets anchored.
    """
    root = configured_data_dir()
    return (root if root is not None else Path("data")).joinpath(*parts)


def _names_data(value: str) -> bool:
    """Is ``value`` a relative path whose first part is ``data``?"""
    if "\n" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and path.parts[:1] == ("data",)


def resolve_data_path(value: str | os.PathLike[str]) -> Path:
    """A path from configuration, the environment or a database row.

    ``data/...`` lands in the data directory; anything else -- absolute, or
    relative to somewhere else -- is returned as it is. Applying it twice
    changes nothing, so a value the loader already moved can pass through
    it again.
    """
    path = Path(value)
    if _names_data(str(value)):
        return data_path(*path.parts[1:])
    return path


def relocate_data_paths(value: Any) -> Any:
    """``value`` with every relative ``data/...`` string moved into the data directory.

    For configuration read from YAML, whose paths are written against the
    default location. Nothing configured, nothing changes. A moved path is
    written with forward slashes, as the YAML wrote it, and keeps a trailing
    slash: an allowlist entry ``data/workspace/`` must not start admitting
    ``workspace2`` because pathlib dropped the separator.
    """
    if configured_data_dir() is None:
        return value
    return _relocate(value)


def _relocate(value: Any) -> Any:
    if isinstance(value, str):
        if not _names_data(value):
            return value
        moved = resolve_data_path(value).as_posix()
        return moved + "/" if value.endswith(("/", "\\")) else moved
    if isinstance(value, dict):
        return {key: _relocate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_relocate(item) for item in value]
    return value
