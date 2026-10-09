"""The files a configuration is read from and how they stack: the master config, the includes it
names (by path or by glob), and this machine's local layer over both -- which sections each may set,
how a file's sections go over what came before, and where a relative path written in them points.

read_layers gives load_settings the stacked result; ${VAR} expansion, the data directory,
inheritance and validation are its own steps (settings.py). The pieces that read one section or
list the files without a full load (master_section, config_files) serve the processes and panels
that need only that.
"""
from __future__ import annotations

import glob as glob_module
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from agent_system.utils import yaml_io

from .environment import text_encoding
from .merging import deep_merge, overlay_section
from .models import strip_empty_yaml_keys

# The loader's one logger, the name it reported under before it was split into modules: a filter
# on load_settings.__module__ (the agent editor's quiet_loader) must keep catching all of it.
logger = logging.getLogger("agent_system.config.settings")


def master_path(config_path: Optional[str] = None) -> Path:
    """The master config a load reads: *config_path*, else AGENT_CONFIG_PATH, else config/config.yaml
    relative to the working directory."""
    return Path(config_path or os.environ.get("AGENT_CONFIG_PATH") or "config/config.yaml")


def read_master(cfg_path: Path) -> Any:
    """The master config as parsed, {} where it is empty. Raises where it does not read or parse."""
    return yaml_io.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def _include_entries(master: dict) -> list:
    """The master's `includes` (or `files`) as written; a single string is a list of one."""
    includes = master.get("includes") or master.get("files") or []
    return [includes] if isinstance(includes, str) else includes


def _resolve_relative_paths(data: dict, base_dir: Path) -> dict:
    """Resolve relative paths (starting with ./) in agent config values.

    Walks the config tree and resolves ``system_template`` values that
    start with ``./`` relative to *base_dir* (the directory of the YAML
    file that declared them).  This allows agent configs co-located with
    plugins to use ``system_template: ./prompts/foo.md`` instead of
    hard-coding a project-relative path.
    """
    PATH_KEYS = {"system_template"}

    def _walk(obj: object) -> object:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in PATH_KEYS and isinstance(value, str) and (value.startswith("./") or value.startswith("../")):
                    resolved = str((base_dir / value).resolve())
                    obj[key] = resolved
                else:
                    _walk(value)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
        return obj

    return _walk(data)  # type: ignore[return-value]


def _expand_includes(master: dict, cfg_path: Path) -> list[str]:
    """The master config's `includes` (or `files`), globs expanded, relative to the config directory.

    A pattern that matches nothing stays in the list, for the error report of the loader.
    """
    expanded: list[str] = []
    for inc in _include_entries(master):
        if '*' in inc or '?' in inc or '[' in inc:
            pattern = inc if Path(inc).is_absolute() else str(cfg_path.parent / inc)
            matched_files = sorted(glob_module.glob(pattern))
            if matched_files:
                expanded.extend(str(Path(matched).relative_to(cfg_path.parent)) for matched in matched_files)
            else:
                expanded.append(inc)
        else:
            expanded.append(inc)
    return expanded


#: What an include cannot set: a process reads its data directory from the
#: master and the local layer alone (master_data_dir), the setup panel the
#: signing key a restart applies (plugins/setup/status.py), and an include's
#: includes are not followed.
MASTER_ONLY_SECTIONS = ("paths", "auth", "includes", "files")

#: What of auth an include may set after all: the route rules (config/security.yaml). Only an include the master
#: names by its own path, never one a glob matched -- the globs take in every plugin's agents/*.yaml, and a plugin
#: must not open routes by shipping a file. The rest of auth (enabled, the signing key, ...) stays the master's.
AUTH_RULE_SECTIONS = ("endpoint_security", "llm_security", "plugin_security")

#: This machine's own layer beside the master config, never in the repository (.gitignore). Read after every
#: include, so it wins, and -- unlike an include -- it may set auth and paths too: the Setup panel and the install
#: scripts point auth.secret_key at the machine's own key here, never in a tracked file.
LOCAL_CONFIG = "local.yaml"


def local_layer(cfg_path: Path) -> dict:
    """The local layer beside *cfg_path*; {} where there is none. Raises ValueError where it is there and does not
    load: it names this machine's own signing key, and a start without it would sign with the public one."""
    path = cfg_path.parent / LOCAL_CONFIG
    if not path.is_file():
        return {}
    try:
        part = yaml_io.safe_load(local_text(path)) or {}
    except (OSError, ValueError, yaml.YAMLError) as error:  # ValueError: no text
        raise ValueError(f"Config file {path} does not load: {error}") from error
    if not isinstance(part, dict):
        raise ValueError(f"Config file {path} does not load: not a mapping")
    # A section with every line commented out sets nothing; in hooks, an empty key neither (as in an include).
    return {key: strip_empty_yaml_keys(value) if key == "hooks" and isinstance(value, dict) else value
            for key, value in part.items() if value is not None}


def local_text(path: Path) -> str:
    """A file of the local layer as text: UTF-8, with a BOM or without, or UTF-16 with one -- what PowerShell 5.1's
    `>` writes, which the install guide has the reader do. Raises OSError, or UnicodeDecodeError (a ValueError)."""
    data = read_config_bytes(path)
    return data.decode(text_encoding(data))


def read_config_bytes(path: Path) -> bytes:
    """A config file's bytes. Windows refuses a read while another process replaces the file (os.replace: the Agent
    Editor saving, the Setup panel writing): tried again for up to a second before it counts as unreadable -- a file
    the master names fails the start then. A denial that lasts (POSIX rights) costs that second."""
    for attempt in range(20):
        try:
            return path.read_bytes()
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")


def master_section(cfg_path: Path, name: str, default: Any = None) -> Any:
    """Section *name* (auth, paths) as a start takes it: the master's, the local layer's over it; *default* where
    neither names it. A null in the master stays null, which fails the start as it fails this caller. Raises where
    the master does not load or is no mapping."""
    master = read_master(cfg_path)
    if not isinstance(master, dict):
        raise ValueError(f"{cfg_path} is not a mapping")
    local = local_layer(cfg_path)  # a null section there sets nothing
    if name not in local:
        return master.get(name, default)
    return overlay_section(master.get(name), local[name])


def _named_includes(master: dict) -> set[str]:
    """The includes the master names by their own path, not by a glob."""
    return {inc for inc in _include_entries(master)
            if isinstance(inc, str) and not any(char in inc for char in "*?[")}


def _merge_auth_rules(data: dict, section: dict, source: str, master_name: str) -> None:
    """The route rules of an include the master names, over the master's (AUTH_RULE_SECTIONS); every other key of
    its auth section is refused by name."""
    refused = sorted(str(key) for key in section if key not in AUTH_RULE_SECTIONS)
    if refused:
        logger.warning("%s: auth.%s is read from %s only -- ignored here (an include sets only %s)", source,
                       ", auth.".join(refused), master_name, ", ".join(AUTH_RULE_SECTIONS))
    auth = data.get("auth")
    auth = dict(auth) if isinstance(auth, dict) else {}
    for key in AUTH_RULE_SECTIONS:
        if section.get(key) is None:
            continue  # absent, or every line commented out: sets nothing
        auth[key] = overlay_section(auth.get(key), section[key])
    data["auth"] = auth


def config_files(config_path: Optional[str] = None) -> list[Path]:
    """The files `load_settings` reads, in its order: the master config, every include that exists, the local layer."""
    cfg_path = master_path(config_path)
    if not cfg_path.exists():
        return []
    master = read_master(cfg_path)
    local = cfg_path.parent / LOCAL_CONFIG
    included = (Path(inc) if Path(inc).is_absolute() else cfg_path.parent / inc
                for inc in _expand_includes(master, cfg_path))
    return [cfg_path, *(path for path in included if path.exists() and not _is_local(path, local)),
            *([local] if local.is_file() else [])]


def _is_local(path: Path, local: Path) -> bool:
    return path.resolve() == local.resolve()


def read_layers(cfg_path: Path) -> dict:
    """The master config at *cfg_path* (which exists), every include over it and the local layer over
    those, each section merged as the files stack: what load_settings goes on to expand and validate.
    Raises where the master, a file it names by path, or the local layer does not load."""
    try:
        master = read_master(cfg_path)
    except yaml.YAMLError as e:
        logger.error(f"YAML syntax error in config file '{cfg_path}': {e}")
        raise ValueError(f"Failed to parse configuration file '{cfg_path}': {e}") from e
    except Exception as e:
        logger.error(f"Failed to read config file '{cfg_path}': {e}")
        raise

    # Start with the master config as base
    data = dict(master)

    includes = _expand_includes(master, cfg_path)
    logger.debug(f"Config includes {len(includes)} files from glob patterns")
    named = _named_includes(master)

    local_path = cfg_path.parent / LOCAL_CONFIG

    # Load each included file and merge into specific sections
    for inc in includes:
        inc_path = Path(inc)
        if not inc_path.is_absolute():
            inc_path = cfg_path.parent.joinpath(inc_path)
        if inc_path.exists() and _is_local(inc_path, local_path):
            continue  # a master that still names the local layer: it comes last, below
        if inc_path.exists():
            try:
                part = yaml_io.safe_load(read_config_bytes(inc_path).decode("utf-8")) or {}
                if isinstance(part, dict):
                    # A section with every line commented out sets nothing: a
                    # null `plugins:` failed the whole file in deep_merge.
                    part = {key: value for key, value in part.items() if value is not None}

                # Resolve relative paths (./prompts/...) relative to include file dir
                _resolve_relative_paths(part, inc_path.parent)

                _merge_include(data, part, inc, inc_path, named, cfg_path.name)
            except yaml.YAMLError as e:
                if inc in named:
                    raise ValueError(f"Failed to parse included configuration file '{inc_path}': {e}") from e
                # Log YAML syntax errors and continue (allows other configs to load)
                logger.error(f"YAML syntax error in included config '{inc_path}': {e}")
                logger.warning(f"Skipping malformed config file: {inc_path}")
            except Exception as e:
                if inc in named:
                    raise ValueError(f"Failed to load included configuration file '{inc_path}': {e}") from e
                # Log other parsing errors but continue loading
                logger.error(f"Failed to load included config '{inc_path}': {e}", exc_info=True)
                logger.warning(f"Skipping problematic config file: {inc_path}")
        elif inc in named and not _is_local(inc_path, local_path):  # the local layer is optional, named or not
            # The master names this file by its path: the route rules (security.yaml) among them. Skipped, the
            # start would go on with the models' defaults -- every plugin panel open to any user.
            raise ValueError(f"Included configuration file '{inc_path}' is named in {cfg_path.name} but missing")

    # This machine's own layer, over everything: every section but another include list.
    local = local_layer(cfg_path)
    _resolve_relative_paths(local, cfg_path.parent)
    for key, value in local.items():
        if key in ("includes", "files"):
            logger.warning("%s: '%s' is read from %s only -- ignored here", LOCAL_CONFIG, key, cfg_path.name)
            continue
        data[key] = overlay_section(data.get(key), value)
    return data


def _merge_include(data: dict, part: Any, inc: str, inc_path: Path, named: set[str], master_name: str) -> None:
    """One include's sections over *data*, in place: llm_system, plugins and hooks merge into what came
    before, external_servers replaces it, the master's own sections are refused by name (auth's route
    rules excepted, in a file the master names by path), and every other section goes over what came
    before. Raises on a part that is no mapping; read_layers skips the file then, or fails the start."""
    # Merge based on included file structure
    # Deep merge llm_system to allow multiple files to contribute models/profiles
    if "llm_system" in part:
        if "llm_system" in data:
            data["llm_system"] = deep_merge(data["llm_system"], part["llm_system"])
        else:
            data["llm_system"] = part["llm_system"]
        # Log models being added
        if "models" in part.get("llm_system", {}):
            model_names = list(part["llm_system"]["models"].keys())
            logger.debug(f"Added LLM models from {inc_path.name}: {model_names}")
    elif inc.endswith("llm.yaml"):
        # If llm.yaml contains the configuration directly (legacy format)
        data["llm_system"] = part

    # New split structure (Epic 0044)
    # Deep merge plugins to allow multiple files to contribute agents/servers
    if "plugins" in part:
        if "plugins" in data:
            data["plugins"] = deep_merge(data["plugins"], part["plugins"])
        else:
            data["plugins"] = part["plugins"]

    if "external_servers" in part:
        # mcp_servers.yaml uses "external_servers" key
        data["external_servers"] = part["external_servers"]

    # Global hook settings (plugins.yaml). Dropped here, they
    # were re-read from config/plugins.yaml relative to the
    # working directory -- ignoring --config.
    if "hooks" in part:
        section, current = part["hooks"], data.get("hooks")
        if isinstance(section, dict):
            # Before the merge: an empty key sets nothing, it
            # must not overwrite an earlier file's value with
            # null (which the model then turns into a default)
            section = strip_empty_yaml_keys(section)
        if section is None:
            pass  # every line commented out: nothing set here
        elif isinstance(section, dict) and isinstance(current, dict):
            data["hooks"] = deep_merge(current, section)
        elif current is None or isinstance(current, dict):
            # The first section -- or a wrong shape (`hooks: []`),
            # kept as it is so GlobalHooksConfig rejects it by
            # name. deep_merge on it raised inside the per-file
            # except and logged the whole file as skipped.
            data["hooks"] = section

    # Every other section too, over what came before.
    for key, value in part.items():
        if key in ("llm_system", "plugins", "external_servers", "hooks"):
            continue  # merged above
        if key == "auth" and inc in named and isinstance(value, dict):
            _merge_auth_rules(data, value, inc_path.name, master_name)
            continue
        if key in MASTER_ONLY_SECTIONS:
            logger.warning("%s: '%s' is read from %s only -- ignored here", inc_path.name, key,
                           master_name)
            continue
        data[key] = overlay_section(data.get(key), value)


def resolve_discovery_dirs(data: dict, cfg_path: Path) -> None:
    """plugins.plugin_dirs and skills.skill_dirs as absolute paths, in place (_resolve_dir_list).
    *cfg_path* exists."""
    base_dir = cfg_path.parent
    # repository root is assumed to be parent of the config dir
    repo_root = cfg_path.parent.parent if cfg_path.parent.parent.exists() else base_dir
    try:
        _resolve_dir_list(data, "plugins", "plugin_dirs", base_dir, repo_root, expand=True)   # plugins.plugin_dirs
        _resolve_dir_list(data, "skills", "skill_dirs", base_dir, repo_root)     # skills.skill_dirs
    except Exception:
        # Conservative: if resolution fails for any reason, keep original values --
        # but say so: an unexpanded "src/plugins*" discovers no plugin at all.
        logger.warning("Resolving plugin_dirs/skill_dirs failed; kept as written", exc_info=True)


def _resolve_dir_list(data: dict, block_key: str, list_key: str, base_dir: Path, repo_root: Path,
                      expand: bool = False) -> None:
    """Resolve <block_key>.<list_key> entries to absolute paths in place.

    Shared by plugin_dirs and skill_dirs so both behave identically —
    the alternative was duplicating this resolution per discovery root.
    With ``expand`` a wildcard entry becomes the directories it matches
    (sorted, each a valid package name) instead of the pattern: every
    reader of plugin_dirs takes its entries as package roots, and
    ``src/plugins*`` then names roots a checkout may or may not carry.
    """
    block = data.get(block_key) if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return
    entries = block.get(list_key)
    if not isinstance(entries, list):
        return
    def package_dirs(pattern: str) -> list[str]:
        matches = [m for m in sorted(glob_module.glob(pattern))
                   if Path(m).is_dir() and Path(m).name.isidentifier()]
        if not matches:
            logger.warning(f"{block_key}.{list_key}: {pattern} matches no directory")
        return matches

    resolved = []
    for p in entries:
        if isinstance(p, str) and p:
            ppath = Path(p)
            wildcard = any(ch in p for ch in "*?[")
            if wildcard and ppath.is_absolute() and expand:
                resolved.extend(package_dirs(p))
                continue
            if not ppath.is_absolute() and wildcard:
                # A wildcard entry (e.g. "skills/*/") cannot be tested
                # with exists() — the literal path never exists, so the
                # plain branch below would silently root it at the
                # config folder. Ask glob which base actually matches;
                # expansion itself happens at discovery.
                # Resolve the BASE, not the pattern: Path.resolve() on
                # "skills/*" would keep the star as a literal component.
                # Absolute matters here — a relative pattern would move
                # with the working directory at discovery time.
                base_pattern = str(base_dir.resolve().joinpath(ppath))
                repo_pattern = str(repo_root.resolve().joinpath(ppath))
                if glob_module.glob(base_pattern, recursive=True):
                    p = base_pattern
                elif glob_module.glob(repo_pattern, recursive=True):
                    p = repo_pattern
                else:
                    # No match either way: keep it repo-rooted, which is
                    # where a skills/... pattern is meant, so the
                    # "matched nothing" warning names a sane path.
                    p = repo_pattern
                if expand:
                    resolved.extend(package_dirs(p))
                else:
                    resolved.append(p)
                continue
            if not ppath.is_absolute():
                # Try config-folder-relative first
                try:
                    candidate = (base_dir.joinpath(ppath)).resolve()
                except Exception:
                    candidate = base_dir.joinpath(ppath)
                # If that candidate doesn't exist but an equivalent
                # path exists relative to the repo root, prefer the
                # repo-root-relative path (handles `src/...`).
                if not candidate.exists():
                    try:
                        repo_candidate = (repo_root.joinpath(ppath)).resolve()
                    except Exception:
                        repo_candidate = repo_root.joinpath(ppath)
                    if repo_candidate.exists():
                        p = str(repo_candidate)
                    else:
                        p = str(candidate)
                else:
                    p = str(candidate)
        resolved.append(p)
    data[block_key][list_key] = resolved
