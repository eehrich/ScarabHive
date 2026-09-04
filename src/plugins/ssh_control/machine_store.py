"""Where machines added at runtime survive a restart.

``add_machine(persistent=true)`` used to write ``config/mcp.yaml``. Three
things were wrong with that, and they compounded:

1. ``config/config.yaml`` does not include that file (``includes:`` lists
   llm, plugins, mcp_servers and the agent YAMLs), so **nothing ever read it
   back**. Every machine reported as "saved" was gone at the next start.
2. It looked like configuration while being runtime state, and it was the
   plugin itself that created the file -- so the operator got a config file
   no human had written and no loader knew about.
3. The read-modify-write block existed FOUR times (the add and remove halves
   of the tool, and of the web panel). That is how the halves came to
   disagree about the nesting: add wrote ``plugins.servers.ssh_control``
   while remove read ``servers.ssh_control``, so removal silently did
   nothing and reported success.

One place now, one shape, under ``data/`` where runtime state belongs:

.. code-block:: yaml

    # data/ssh_control/machines.<instance>.yaml
    machines:
      - name: NewBox
        host: 192.0.2.9
        port: 22
        username: root
        auth_method: key

The instance name is in the FILE name, not the directory, so the noun leads
and the path does not stutter (the instance is usually called ``ssh_control``
too). Keyed per instance because two ``ssh_control`` instances -- two agents
with different machine sets -- would otherwise overwrite each other.

The declarative machines stay in ``config/agents/*.yaml`` and **win** on a
name collision -- a stored entry that shadows a configured one is dropped
with a warning. The other way round, a stale runtime entry would silently
override what an admin wrote in config, which is the kind of invisible drift
that costs an afternoon to find.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from agent_system.utils import yaml_io

logger = logging.getLogger(__name__)

#: Relative like every other plugin's data path (see
#: ``mcp_client/connection.py`` and ``context_engineer/server.py``). Tests
#: point this at a tmp_path.
STORE_DIR = Path("data/ssh_control")

#: What ssh_control wrote before 2026-09-03, and nothing ever read.
LEGACY_PATH = Path("config/mcp.yaml")


def store_path(instance: str) -> Path:
    """The store file for one plugin instance."""
    return STORE_DIR / f"machines.{instance}.yaml"


def load(instance: str) -> list[dict[str, Any]]:
    """The stored machines, or ``[]``.

    A missing store is the normal case, not an error: most instances never
    add a machine at runtime. A broken store is logged and treated as empty
    -- a plugin that refuses to start because a convenience file is corrupt
    would take the configured machines down with it.
    """
    path = store_path(instance)
    if not path.exists():
        return []
    try:
        data = yaml_io.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.error("Could not read the machine store %s: %s", path, e)
        return []
    machines = data.get("machines") if isinstance(data, dict) else None
    if not isinstance(machines, list):
        return []
    return [m for m in machines if isinstance(m, dict) and m.get("name")]


def unrestorable_reason(machine: dict[str, Any]) -> str | None:
    """Why this machine could not be brought back, or None if it can.

    Storing an entry that cannot connect is worse than not storing it: it is
    read back at the next start, occupies the name (add_machine rejects a
    duplicate), and fails on first use with "Key path required" or "Password
    required" from ``auth.py``. The caller has to be told at ADD time, while
    the operator is still there -- not at the next restart.

    Checked here rather than in the two call sites so a third one cannot get
    it wrong; four copies of this plugin's persistence logic is what made the
    store necessary in the first place.
    """
    method = machine.get("auth_method", "key")
    if method == "password":
        # Deliberately not solved by storing it: the store is a plain file.
        return ("a password machine cannot be stored -- the password would "
                "have to be written to disk; use key or agent authentication")
    if method == "key" and not machine.get("key_path"):
        return "key authentication needs a key_path to be restorable"
    return None


def add(instance: str, machine: dict[str, Any]) -> Path:
    """Store one machine, replacing any entry of the same name.

    Returns the file written, so the caller can name it in its status line.
    Raises ValueError for a machine that could not be restored.
    """
    reason = unrestorable_reason(machine)
    if reason:
        raise ValueError(reason)
    kept = [m for m in load(instance) if m.get("name") != machine.get("name")]
    kept.append(machine)
    return _write(instance, kept)


def remove(instance: str, name: str) -> bool:
    """Drop one machine. ``False`` when it was not in the store."""
    machines = load(instance)
    kept = [m for m in machines if m.get("name") != name]
    if len(kept) == len(machines):
        return False
    _write(instance, kept)
    return True


def _write(instance: str, machines: list[dict[str, Any]]) -> Path:
    """Write the whole store, atomically.

    Temp file plus ``os.replace`` rather than ``write_text``: a truncating
    write that is interrupted leaves either a file that fails to parse (and
    ``load`` then silently reports NO machines) or -- worse, because nothing
    logs it -- a valid YAML document that lost its tail.
    """
    path = store_path(instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        yaml.safe_dump({"machines": machines}, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def merge_into(instance: str, configured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``configured`` plus the stored machines it does not already name.

    Called once at startup. Config wins on a collision -- see the module
    docstring for why that direction and not the other.
    """
    known = {m.get("name") for m in configured if isinstance(m, dict)}
    extra = []
    for machine in load(instance):
        if machine["name"] in known:
            logger.warning(
                "Machine '%s' is in %s AND in the configuration -- keeping the "
                "configured one, ignoring the stored entry",
                machine["name"], store_path(instance),
            )
            continue
        extra.append(machine)
    if extra:
        logger.info(
            "Restored %d machine(s) from %s: %s",
            len(extra), store_path(instance),
            ", ".join(sorted(m["name"] for m in extra)),
        )
    return [*configured, *extra]


def legacy_machine_count() -> int:
    """How many machines sit in the old, never-read ``config/mcp.yaml``.

    Reported once at startup so an operator who "persisted" machines there
    learns why they never came back. Deliberately NOT migrated: those entries
    have been inert since they were written, and silently resurrecting hosts
    at some later restart is worse than a warning that says where they are.
    """
    if not LEGACY_PATH.exists():
        return 0
    try:
        data = yaml_io.safe_load(LEGACY_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return 0
    if not isinstance(data, dict):
        return 0
    section = ((data.get("plugins") or {}).get("servers") or {}).get("ssh_control") or {}
    machines = section.get("machines")
    return len(machines) if isinstance(machines, list) else 0
