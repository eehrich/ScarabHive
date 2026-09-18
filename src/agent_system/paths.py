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

logger = logging.getLogger(__name__)

#: The repository, anchored to this file instead of to the working directory.
#: The same value thirteen places in the core compute for themselves today
#: (``parents[2]``/``parents[3]``, depending on how deep the module sits).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

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
