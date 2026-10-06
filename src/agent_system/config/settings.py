"""
Settings loader using pydantic BaseSettings semantics.

This provides a single, typed configuration entrypoint that prefers
environment variables and falls back to YAML file values. It keeps
behavior explicit and testable.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
import time
from fnmatch import fnmatchcase
from typing import Any, Optional
from pathlib import Path
import os
import yaml
from agent_system.utils import yaml_io
from agent_system.paths import relocate_data_paths, set_config_data_dir
import glob as glob_module

from .models import AgentSystemConfig, ToolServerConfig, strip_empty_yaml_keys

logger = logging.getLogger(__name__)

#: Loaded at most once per process — the file is read on every load_settings()
#: call otherwise (config reload, tests, every CLI subcommand).
_secrets_loaded: set[str] = set()

#: What this process took from a secrets file: name -> a fingerprint of the value. The rest of its environment
#: is the real one, which a process started now would get again (environment_at_restart). Handed on to the
#: processes it starts in the environment, which carries the values on as well (a woken run, spawn_wake): theirs
#: are no real ones either -- unless the starter set the name itself, to another value (a terminal's env_vars).
SECRETS_FROM_FILE_ENV = "HIVE_SECRETS_FROM_FILE"


def _env_name(name: str) -> str:
    """A variable's name as os.environ compares it: blind to case on Windows."""
    return name.upper() if os.name == "nt" else name


def _fingerprint(value: str) -> str:
    # surrogatepass: a variable of undecodable bytes arrives as lone surrogates on POSIX
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _handed_on() -> dict[str, str]:
    entries = (item.rpartition(":") for item in os.environ.get(SECRETS_FROM_FILE_ENV, "").split(",") if item)
    return {_env_name(name): fingerprint for name, _, fingerprint in entries if name}


_secrets_from_file: dict[str, str] = _handed_on()


def _secrets_file_entries(path: Path, skipped: Optional[list[str]] = None) -> list[tuple[str, str]]:
    """Every ``KEY=value`` line of a secrets file, in order -- a name may come twice. Raises OSError.

    UTF-8, with a BOM or without, or UTF-16 with one (PowerShell 5.1's `>`). A line that does not decode, holds
    no KEY=value, or a NUL no environment takes, is left out and named in *skipped* -- never its value. One
    umlaut saved in another encoding, or a line appended in another, must not take every key with it: a signing
    key given by ${VAR} would be "" then.

    Quotes are stripped so `KEY="v"` and `KEY=v` behave the same; values are used verbatim otherwise (no escape
    processing -- an API key is an opaque string).
    """
    data = path.read_bytes()
    utf16 = data[:2] in (b"\xff\xfe", b"\xfe\xff")
    encoding = "utf-16" if utf16 else "utf-8-sig"  # utf-8-sig drops a BOM
    try:
        lines, broken = data.decode(encoding).splitlines(), False
    except UnicodeDecodeError:
        # Split as the text would be: the lines that decode keep their numbers and ends. UTF-8 keeps a byte that
        # does not decode as a lone surrogate, so a U+FFFD in a value is not taken for one; UTF-16 has no such way.
        lines, broken = data.decode(encoding, "replace" if utf16 else "surrogateescape").splitlines(), True
    entries: list[tuple[str, str]] = []
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if broken and _undecoded(line, utf16):
            reason = f"not {'UTF-16' if utf16 else 'UTF-8'} text"
        elif not (sep and name):
            reason = "no KEY=value"
        elif "\0" in line:
            reason = "a NUL character"
        else:
            entries.append((name, value.strip().strip('"').strip("'")))
            continue
        if skipped is not None:
            skipped.append(f"line {number}: {reason}")
    return entries


def _undecoded(line: str, utf16: bool) -> bool:
    if utf16:
        return chr(0xFFFD) in line
    return any(0xDC80 <= ord(char) <= 0xDCFF for char in line)


def _read_secrets_file(path: Path, skipped: Optional[list[str]] = None) -> dict[str, str]:
    """The secrets file as the loader takes it: the first line of a name winning. Raises OSError."""
    found: dict[str, str] = {}
    for name, value in _secrets_file_entries(path, skipped):
        found.setdefault(name, value)
    return found


#: This machine's own credentials beside the master config, never in the repository (.gitignore). Read before
#: secrets.env, so a name it holds wins over that file; the Setup panel and the install scripts write here.
LOCAL_SECRETS = "local.env"


def secrets_files(cfg_path: Path) -> list[Path]:
    """The credential files beside *cfg_path*, in the order a start reads them: the first to name a variable wins."""
    return [cfg_path.parent / LOCAL_SECRETS, cfg_path.parent / "secrets.env"]


def environment_at_restart(config_path: str) -> dict[str, str]:
    """The environment a process started now would expand *config_path* with.

    This one's, less what it took from the secrets files beside the config, and
    those files as they read now -- where the real environment still wins, as in
    _load_secrets_file. The real environment is taken as the next start gets it
    again.
    """
    env = {name: value for name, value in os.environ.items() if not _came_from_a_file(name, value)}
    for path in secrets_files(Path(config_path)):
        try:
            found = _read_secrets_file(path) if path.is_file() else {}
        except OSError:  # a start goes on without it (_load_secrets_file), and says so then
            found = {}
        for name, value in found.items():
            if environment_takes(name, value):
                env.setdefault(_env_name(name), value)
    return env


def _came_from_a_file(name: str, value: str) -> bool:
    return _secrets_from_file.get(_env_name(name)) == _fingerprint(value)


def set_by_the_environment(name: str) -> bool:
    """Whether *name* is set by the real environment -- not taken from a secrets file. A file cannot change it then:
    the environment wins at every start."""
    value = os.environ.get(name)
    return value is not None and not _came_from_a_file(name, value)


def take_secret(name: str, value: str) -> None:
    """Put a credential just written to a secrets file into this process's environment, as a start would have taken
    it -- remembered as the file's, and handed on to the processes this one starts. Raises ValueError/OSError where
    the environment does not take it."""
    os.environ[name] = value
    _secrets_from_file[_env_name(name)] = _fingerprint(value)
    _hand_on("a credential written at runtime")


def _hand_on(source: str) -> None:
    try:
        os.environ[SECRETS_FROM_FILE_ENV] = ",".join(f"{name}:{fingerprint}"
                                                     for name, fingerprint in sorted(_secrets_from_file.items()))
    except (ValueError, OSError):  # more names than a Windows variable holds
        logger.warning("The processes this one starts are not told which of %d credentials came from %s",
                       len(_secrets_from_file), source)


def environment_takes(name: str, value: str) -> bool:
    """What a start can put into its environment: Windows takes `name=value` up to 32767 UTF-16 units (os.putenv
    raises past it, measured 28.09.2026; a character past U+FFFF is two), POSIX any length."""
    return os.name != "nt" or len(f"{name}={value}".encode("utf-16-le", "surrogatepass")) // 2 <= 32767


def _load_secrets_file(path: Path) -> None:
    """Read ``KEY=value`` lines into the environment, without overriding it.

    The real environment WINS on purpose: the server sets its credentials
    through systemd/CI, and a stale developer file on the same machine must
    not quietly replace them. That also makes the file optional — a deployment
    that has no secrets.env is fully configured through the environment.

    Never raises: an unreadable or malformed credentials file must degrade to
    "no credentials from here" (and say so), not stop the process from
    starting.
    """
    key = str(path.resolve())  # one file under two spellings (relative, absolute) is read once
    if key in _secrets_loaded:
        return
    _secrets_loaded.add(key)
    if not path.is_file():
        return
    skipped: list[str] = []
    try:
        found = _read_secrets_file(path, skipped)
    except OSError as e:
        logger.warning("Could not read %s: %s", path, e)
        return
    loaded = 0
    for name, value in found.items():
        try:
            if name in os.environ:
                continue
            os.environ[name] = value
        except (ValueError, OSError):  # longer than Windows lets a variable be; a setenv that fails
            skipped.append(f"{name}: not taken by the environment")
            continue
        _secrets_from_file[_env_name(name)] = _fingerprint(value)
        loaded += 1
    if skipped:
        logger.warning("Left out of %s: %s", path, "; ".join(skipped))
    if loaded:
        _hand_on(str(path))
    # Count only — never the names' values.
    logger.info("Loaded %d credential(s) from %s", loaded, path)


def deep_merge(base: dict, overlay: dict) -> dict:
    """Deep merge two dictionaries. Overlay values override base values.
    
    For nested dicts, merges recursively. For lists and other types, overlay replaces base.
    
    Args:
        base: Base dictionary
        overlay: Dictionary to merge on top of base
        
    Returns:
        Merged dictionary
    """
    result = dict(base)
    
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Recursively merge nested dicts
            result[key] = deep_merge(result[key], value)
        else:
            # Replace value (including lists, primitives, etc.)
            result[key] = value
    
    return result


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


_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def expand_env(value: Any, missing: Optional[set[str]] = None, environ: Optional[dict[str, str]] = None) -> Any:
    """`value` with every ${VAR} replaced from the environment, as the loader reads the YAML.

    An unset variable becomes "" and its name goes into `missing` -- collected, not silently blanked: an unset key
    used to surface hours later as an opaque 401 from a provider; the operator needs the VARIABLE NAME.
    `environ` stands in for this process's environment (environment_at_restart).
    """
    env = os.environ if environ is None else environ
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            name = match.group(1)
            if name not in env:
                if missing is not None:
                    missing.add(name)
                return ""
            return env[name]
        return _ENV_PLACEHOLDER.sub(repl, value)
    if isinstance(value, dict):
        return {k: expand_env(v, missing, environ) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, missing, environ) for v in value]
    return value


def _expand_includes(master: dict, cfg_path: Path) -> list[str]:
    """The master config's `includes` (or `files`), globs expanded, relative to the config directory.

    A pattern that matches nothing stays in the list, for the error report of the loader.
    """
    includes = master.get("includes") or master.get("files") or []
    if isinstance(includes, str):
        includes = [includes]
    expanded: list[str] = []
    for inc in includes:
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
    return data.decode("utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig")


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
    master = yaml_io.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(master, dict):
        raise ValueError(f"{cfg_path} is not a mapping")
    local = local_layer(cfg_path)  # a null section there sets nothing
    if name not in local:
        return master.get(name, default)
    section = master.get(name)
    return deep_merge(section, local[name]) if isinstance(section, dict) and isinstance(local[name], dict) \
        else local[name]


def _named_includes(master: dict) -> set[str]:
    """The includes the master names by their own path, not by a glob."""
    includes = master.get("includes") or master.get("files") or []
    return {inc for inc in ([includes] if isinstance(includes, str) else includes)
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
        current = auth.get(key)
        auth[key] = deep_merge(current, section[key]) if isinstance(current, dict) and isinstance(section[key], dict) \
            else section[key]
    data["auth"] = auth


def config_files(config_path: Optional[str] = None) -> list[Path]:
    """The files `load_settings` reads, in its order: the master config, every include that exists, the local layer."""
    cfg_path = Path(config_path or os.environ.get("AGENT_CONFIG_PATH") or "config/config.yaml")
    if not cfg_path.exists():
        return []
    master = yaml_io.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    local = cfg_path.parent / LOCAL_CONFIG
    included = (Path(inc) if Path(inc).is_absolute() else cfg_path.parent / inc
                for inc in _expand_includes(master, cfg_path))
    return [cfg_path, *(path for path in included if path.exists() and not _is_local(path, local)),
            *([local] if local.is_file() else [])]


def _is_local(path: Path, local: Path) -> bool:
    return path.resolve() == local.resolve()


def master_data_dir(config_path: Optional[str] = None) -> Optional[str]:
    """``paths.data_dir`` as a start takes it, for a process that never loads settings.

    Only the master and the local layer can set it: the loader takes this
    section from those alone. Credentials are loaded and ``${VAR}`` expanded
    first, so the value is what load_settings would see.
    """
    cfg_path = Path(config_path or os.environ.get("AGENT_CONFIG_PATH") or "config/config.yaml")
    if not cfg_path.exists():
        return None
    for path in secrets_files(cfg_path):
        _load_secrets_file(path)
    try:
        section = master_section(cfg_path, "paths")
    except ValueError:
        return None
    value = section.get("data_dir") if isinstance(section, dict) else None
    return expand_env(str(value)) if value is not None else None


#: In the environment of a run a process that enforces authentication woke (core/session_presence.spawn_wake), and
#: of every run that run wakes in turn. The run loads its config with auth on, whatever the disk says by now: it acts
#: for a user of that API and judges them as the API does -- which enforces what it started with until a restart.
#: Tighten only: whoever sets it makes a process stricter, never looser.
AUTH_REQUIRED_ENV = "HIVE_AUTH_REQUIRED"
#: Whether this process is such a run. Taken out of the environment on import: what the run starts otherwise -- a
#: terminal command, a test run, an API of its own -- loads its config as the disk says; its wakes get it back.
AUTH_REQUIRED_BY_WAKER = os.environ.pop(AUTH_REQUIRED_ENV, None) == "1"


def load_settings(config_path: Optional[str] = None) -> AgentSystemConfig:
    """Load and return an `AgentSystemConfig` using environment variables and
    optional YAML config file. Environment variables take precedence for
    any ${VAR} placeholders inside the YAML but do not override explicit
    keys unless the YAML uses that placeholder.

    Args:
        config_path: optional path to YAML config; if omitted, uses
                     the `config/config.yaml` file if it exists or the
                     path from the AGENT_CONFIG_PATH environment variable.
    """
    # Clear caches when reloading settings
    global _plugins_cache
    _plugins_cache = None
    # A reload is a fresh verdict: what the operator just fixed must be able to
    # report again if it is still broken.
    _reported_stale_llm_params.clear()
    
    # Allow overriding default config file via env var
    env_cfg = os.environ.get("AGENT_CONFIG_PATH")
    cfg_path = Path(config_path or env_cfg or "config/config.yaml")

    # Credentials come from a file the repository never sees, so that the
    # YAML can be shared and versioned. Loaded BEFORE the ${VAR} expansion
    # below, which is what actually consumes them.
    for secrets_path in secrets_files(cfg_path):
        _load_secrets_file(secrets_path)

    data: dict = {}

    # If the master config exists, load it and then load any included files
    if cfg_path.exists():
        try:
            master = yaml_io.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
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
                    logger.debug(f"Loaded included config: {inc_path.name}")
                    
                    # Resolve relative paths (./prompts/...) relative to include file dir
                    _resolve_relative_paths(part, inc_path.parent)
                    
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
                        # Log servers being added
                        if "servers" in part.get("plugins", {}):
                            server_names = list(part["plugins"]["servers"].keys())
                            logger.debug(f"Added servers from {inc_path.name}: {server_names}")
                    
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
                            _merge_auth_rules(data, value, inc_path.name, cfg_path.name)
                            continue
                        if key in MASTER_ONLY_SECTIONS:
                            logger.warning("%s: '%s' is read from %s only -- ignored here", inc_path.name, key,
                                           cfg_path.name)
                            continue
                        current = data.get(key)
                        data[key] = (deep_merge(current, value) if isinstance(current, dict) and isinstance(value, dict)
                                     else value)

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
            current = data.get(key)
            data[key] = deep_merge(current, value) if isinstance(current, dict) and isinstance(value, dict) else value

    # Apply simple env-variable expansion for ${VAR} patterns (keep existing loader behavior)
    _missing_vars: set[str] = set()
    data = expand_env(data, _missing_vars)

    if _missing_vars:
        logger.warning(
            "Config references %d unset variable(s): %s — the values are empty. "
            "Enter them in the Setup panel, or in %s (template: secrets.env.example), "
            "or set them in the environment.",
            len(_missing_vars), ", ".join(sorted(_missing_vars)),
            cfg_path.parent / LOCAL_SECRETS,
        )

    # One data directory for every path the configuration names under data/
    # (paths.py). Recorded first, since the move resolves against it. The
    # paths section itself stays as written: `data_dir: data/x` would
    # otherwise be read as a path inside itself.
    section = data.get("paths")
    set_config_data_dir(section.get("data_dir") if isinstance(section, dict) else None)
    data = {key: value if key == "paths" else relocate_data_paths(value)
            for key, value in data.items()}

    # Resolve plugin_dirs to absolute paths
    # Paths are resolved relative to the configuration file directory first,
    # then relative to repository root if the config-relative path doesn't exist.
    # This allows entries like `src/plugins` in included files to refer to the
    # repository `src` tree while keeping resolution predictable.
    if cfg_path.exists():
        base_dir = cfg_path.parent
        # repository root is assumed to be parent of the config dir
        repo_root = cfg_path.parent.parent if cfg_path.parent.parent.exists() else base_dir

        def _resolve_dir_list(block_key: str, list_key: str, expand: bool = False) -> None:
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

        try:
            _resolve_dir_list("plugins", "plugin_dirs", expand=True)   # plugins.plugin_dirs
            _resolve_dir_list("skills", "skill_dirs")     # skills.skill_dirs
        except Exception:
            # Conservative: if resolution fails for any reason, keep original values --
            # but say so: an unexpanded "src/plugins*" discovers no plugin at all.
            logger.warning("Resolving plugin_dirs/skill_dirs failed; kept as written", exc_info=True)

    _resolve_model_inheritance(data)

    # Validate configuration with Pydantic
    try:
        cfg = AgentSystemConfig.model_validate(
            data, context={"drop_stale_llm_params": True})
    except Exception as e:
        logger.error(f"Configuration validation failed: {e}")
        # Log the data structure that failed validation for debugging
        logger.debug(f"Failed configuration data: {data}")
        raise
    cfg._source_path = str(cfg_path)
    if AUTH_REQUIRED_BY_WAKER:
        cfg.auth.enabled = True  # a woken run judges its user as the API that woke it does
    _report_dropped_llm_params(data, cfg)
    _report_unknown_llm_profiles(cfg)
    return cfg


def _resolve_model_inheritance(data: dict) -> None:
    """Resolve ``extends`` on llm_system.models, in place.

    A model entry inherits from another model entry — variants (``-unlimited``,
    ``-nostream``) from theirs, family members from the one that carries the
    shared knobs. Same idea as the agent ``type:`` chains, and the same merge:
    field-wise deep, lists via +item/!pattern.

    Runs before validation: LLMModelConfig has no ``extends`` field, and with
    extra="forbid" an unresolved one is a loud error instead of a silent
    fallback to the defaults.
    """
    llm_system = data.get("llm_system")
    if not isinstance(llm_system, dict):
        return
    models = llm_system.get("models")
    if not isinstance(models, dict) or not models:
        return

    sources = {k: v for k, v in models.items() if isinstance(v, dict)}
    resolved: dict = {}

    def resolve(name: str, chain: tuple) -> dict:
        if name in resolved:
            return resolved[name]
        if name in chain:
            raise ValueError(
                f"llm_system.models: extends cycle {' -> '.join(chain + (name,))}")
        src = sources.get(name)
        if src is None:
            raise ValueError(
                f"llm_system.models: '{chain[-1] if chain else name}' extends "
                f"'{name}', which is not a model entry")
        own = {k: v for k, v in src.items() if k != "extends"}
        parent = src.get("extends")
        out = (_deep_merge_dict(copy.deepcopy(resolve(parent, chain + (name,))),
                                own, name, explicit_none=True)
               if parent else copy.deepcopy(own))
        resolved[name] = out
        return out

    for name in list(sources):
        models[name] = copy.deepcopy(resolve(name, ()))


#: Already-reported (agent, keys) pairs. get_tool_server_config re-validates on
#: every agent creation; the operator needs the line once, not per spawn.
_reported_stale_llm_params: set = set()

#: Errors raised before any log handler existed. Every entry point loads the
#: config FIRST and configures logging after, so these would only ever reach
#: stderr — not the logfile the operator actually reads. setup_logging replays
#: them once a handler is there.
_deferred_config_errors: list = []


def flush_deferred_config_errors() -> int:
    """Re-emit config errors that were raised before logging was configured."""
    pending, _deferred_config_errors[:] = list(_deferred_config_errors), []
    for fmt, args in pending:
        logger.error(fmt, *args)
    return len(pending)


def _warn_stale_llm_params(name: str, raw: Any, agent_cfg: Any) -> None:
    """Name the agent whose profile-keyed llm_params were dropped.

    The validator sees the params but not which agent they belong to, so the
    comparison happens at the call sites, where both are at hand. Loud on
    purpose: the values silently stop applying, which is exactly what the
    strict check was built to prevent. It just must not cost the start.
    """
    if agent_cfg is None or not isinstance(raw, dict):
        return
    dropped = set(raw) - set(getattr(agent_cfg, "llm_params", None) or {})
    if not dropped:
        return
    marker = (name, frozenset(dropped))
    if marker in _reported_stale_llm_params:
        return
    _reported_stale_llm_params.add(marker)
    # available_llm_profiles instead of list(llm_profile): the field may be a
    # plain STRING, and list("turbo") spells the chain out letter by letter in
    # the error message the operator has to act on.
    chain = list(getattr(agent_cfg, "available_llm_profiles", None) or [])
    fmt = ("Agent '%s': llm_params for %s have NO effect — those profiles are in "
           "none of its LLM chains %s. Values: %s")
    args = (name, sorted(dropped), chain, {k: raw[k] for k in sorted(dropped)})
    logger.error(fmt, *args)
    if not logging.getLogger().handlers:
        _deferred_config_errors.append((fmt, args))


def _report_dropped_llm_params(data: dict, cfg: AgentSystemConfig) -> None:
    """Whole-config pass over the raw server dicts (load_settings).

    Except where the load could not judge. An entry that inherits -- its
    ``type`` names another server -- and does not set both chains itself was
    validated against chains that are not its own: the model default stands
    where the parent's chain will be once _resolve_server_inheritance has run.
    Its profile-keyed params were dropped for a mismatch that does not exist,
    and the merge after that had nothing left to keep. Measured on
    v4_beat_scorer: `llm_writer_simple: {max_tokens: 120000}` in the child,
    the chain inherited, and the key gone from get_tool_server_config with
    "in none of its LLM chains ['normal']".

    So those keys go back, and the judgement happens where the chain is known:
    get_tool_server_config validates the merged config the same way, and drops
    and names what is still stale there.
    """
    raw_servers = ((data.get("plugins") or {}).get("servers") or {})
    for name, server in (getattr(cfg.plugins, "servers", None) or {}).items():
        raw_server = raw_servers.get(name) or {}
        raw_agent = raw_server.get("agent_config") or {}
        raw = raw_agent.get("llm_params")
        agent_cfg = getattr(server, "agent_config", None)
        inherits = raw_server.get("type") in raw_servers
        if inherits and not {"llm_profile", "llm_profile_advanced"} <= set(raw_agent):
            _restore_llm_params(raw, agent_cfg)
            continue
        _warn_stale_llm_params(name, raw, agent_cfg)


def _restore_llm_params(raw: Any, agent_cfg: Any) -> None:
    """Put back the keys the load dropped. In place: assigning the field would
    run the validator again, without the context that makes it tolerant."""
    kept = getattr(agent_cfg, "llm_params", None)
    if not isinstance(raw, dict) or not isinstance(kept, dict):
        return
    for key in set(raw) - set(kept):
        kept[key] = copy.deepcopy(raw[key])


def _profile_names(entries: Any) -> list[str]:
    """The profiles a chain names: in a list, ``"+x"`` adds x and a ``"!x"`` entry
    removes one and names none; a string is one name as written (it merges nothing)."""
    if not isinstance(entries, list):
        return [str(entries)]
    return [e[1:] if e.startswith("+") else e for e in map(str, entries) if not e.startswith("!")]


def _report_unknown_llm_profiles(cfg: AgentSystemConfig) -> None:
    """Name chain members that do not exist as profiles.

    Only the PRIMARY profile is resolved at startup; fallback links are built
    lazily, in the failure path. A typo in one therefore stays invisible until
    the primary model rate-limits — the exact moment the fallback was
    configured for. Then it dies with "Profile 'x' not found" and the
    configured resilience turns out to never have existed.

    Loud, but not fatal — same rule as the stale llm_params next door: one
    typo in ONE agent must not keep every server down, and the primary
    profile of every OTHER agent still works.
    """
    profiles = set(getattr(cfg.llm_system, "profiles", None) or {})
    if not profiles:
        return  # nothing to compare against (partial config / tests)
    for name, server in (getattr(cfg.plugins, "servers", None) or {}).items():
        agent_cfg = getattr(server, "agent_config", None)
        if agent_cfg is None:
            continue
        # Only the chains the entry sets itself: an unset llm_profile is the model default "normal", not a choice;
        # the inherited chain is checked on the entry that sets it.
        own = agent_cfg.model_fields_set
        chain: list[str] = []
        if "llm_profile" in own:
            chain += _profile_names(agent_cfg.llm_profile)
        if "llm_profile_advanced" in own:
            chain += _profile_names(agent_cfg.llm_profile_advanced or [])
        chain = list(dict.fromkeys(chain))
        unknown = [p for p in chain if p not in profiles]
        if not unknown:
            continue
        # a merging list's first entry carries its prefix, so an inherited primary is never "unknown" here
        primary = agent_cfg.default_llm_profile if "llm_profile" in own else None
        fmt = ("Agent '%s': LLM profiles %s are in its chain but in no "
               "llm_system.profiles — %s. Chain: %s")
        args = (name, sorted(unknown),
                "THE AGENT WILL NOT START" if primary in unknown
                else "the fallback dies in the incident it exists for",
                list(chain))
        logger.error(fmt, *args)
        if not logging.getLogger().handlers:
            _deferred_config_errors.append((fmt, args))


# Cache for plugin discovery (avoid repeated calls)
_plugins_cache: frozenset[str] | None = None
# No cache for the resolved inheritance: it was keyed by server name alone, so a second config object (a fresh load
# from disk next to the live one) got the first one's result. Resolving all 215 servers takes 8 ms without it
# (measured 2026-09-16), the same as with it.


def _known_plugin_types() -> frozenset[str]:
    """The plugin type names, for the membership tests below.

    Read from the manifests, not by discovering: this used to import every
    plugin module in the process just to answer "is this type a plugin?"
    (measured 2026-09-04 in a fresh process: 0.85 s, 187 plugin modules in
    sys.modules). The catalog looks in the same directory discovery would use
    without arguments -- ONE directory, the importable ``plugins`` package,
    not the three in config.plugins.plugin_dirs. Widening that here is not a
    free cleanup: `repair_pipeline` (type: writer_issues) inherits its
    ``write_key`` from the sibling server because ``writer_issues`` is NOT in
    this set, and would lose it (measured over all 219 servers: that one).
    """
    global _plugins_cache
    if _plugins_cache is None:
        from ..plugins.catalog import PluginCatalog
        from ..plugins.discovery import default_plugin_dirs
        _plugins_cache = PluginCatalog(default_plugin_dirs()).types()
    return _plugins_cache


def _resolve_server_inheritance(
    server_name: str,
    config: "AgentSystemConfig",
    visited: set[str] | None = None
) -> tuple[str, dict]:
    """Resolve server config inheritance chain.
    
    If a server's type refers to another server (not a plugin), this function
    resolves the inheritance chain and merges configurations.
    
    Args:
        server_name: Name of the server to resolve
        config: AgentSystemConfig instance
        visited: Set of already visited servers (for cycle detection)
        
    Returns:
        Tuple of (final_plugin_type, merged_config_dict)
        
    Raises:
        ValueError: If circular inheritance detected
    """
    
    if visited is None:
        visited = set()
    
    if server_name in visited:
        raise ValueError(f"Circular inheritance detected: {' -> '.join(visited)} -> {server_name}")
    
    visited.add(server_name)
    
    # Get server config
    server_config = config.plugins.servers.get(server_name)
    if server_config is None:
        return (server_name, {})  # Not found, return as-is
    
    # Use exclude_unset=True to only export explicitly set values, not Pydantic defaults
    # This prevents defaults from overriding parent values during inheritance
    server_dict = server_config.model_dump(exclude_unset=True)
    typ = server_dict.get("type", "basic_agent")
    
    # Check if server references itself (e.g., writer_content with type: writer_content)
    # This is not real inheritance, treat it as if type is a plugin
    if typ == server_name:
        # Check if it's a known plugin
        plugins = _known_plugin_types()
        if typ in plugins:
            # Type is a real plugin, return as-is
            return (typ, server_dict)
        else:
            # Self-reference but not a plugin - return as-is (will fail later in bootstrap)
            return (typ, server_dict)
    
    # Check if type is a known plugin (use cached plugins)
    plugins = _known_plugin_types()
    
    if typ in plugins:
        # Type is a real plugin, no further inheritance needed
        return (typ, server_dict)
    
    # Type might be another server - check if it exists
    parent_server = config.plugins.servers.get(typ)
    if parent_server is None:
        # Not a server either, return as-is (will fail later in bootstrap)
        return (typ, server_dict)
    
    # Recursively resolve parent
    parent_type, parent_dict = _resolve_server_inheritance(typ, config, visited)
    
    # Merge: parent config first, then child overrides
    merged = _deep_merge_dict(parent_dict, server_dict, server_name)
    # The final type comes from the resolved parent chain
    merged["type"] = parent_type
    
    return (parent_type, merged)


def get_tool_server_config(server_name: str, config: Optional[AgentSystemConfig] = None) -> Optional[ToolServerConfig]:
    """Get a ToolServerConfig by name with inheritance from default_config and parent servers.
    
    This function creates a final ToolServerConfig by:
    1. Starting with the default_config (from plugins or legacy mcp_system)
    2. Resolving inheritance if type refers to another server (e.g., type: writer_agent)
    3. Overlaying/merging the specific server configuration
    
    Server inheritance example:
        writer_agent:
          type: basic_agent
          agent_config:
            hooks: {enabled: true}
            
        character_designer:
          type: writer_agent  # Inherits from writer_agent, resolves to basic_agent
          agent_config:
            max_steps: 50     # Override specific values
    
    Args:
        server_name: Name of the server configuration to retrieve
        config: Optional AgentSystemConfig instance. If None, loads from default path.
        
    Returns:
        ToolServerConfig instance with inherited values, or None if server not found
    """
    from .models import ToolServerConfig
    
    if config is None:
        config = load_settings()
    
    # Use new structure (plugins)
    if not config.plugins:
        return None
    
    server_config = config.plugins.servers.get(server_name)
    if server_config is None:
        return None
    
    # Use exclude_unset for default_config too - but this one we want WITH defaults
    # because it's the base layer. So use regular model_dump() here.
    default_config_dict = config.plugins.default_config.model_dump()
    # ...except its metadata: with the defaults dumped, visibility "private" would read
    # as named on every server and hide the plugin manifest's (ServerDecl.visibility)
    if config.plugins.default_config.metadata is not None:
        default_config_dict["metadata"] = config.plugins.default_config.metadata.model_dump(exclude_unset=True)
    
    # Resolve inheritance chain (type: writer_agent -> type: basic_agent)
    try:
        final_type, resolved_config_dict = _resolve_server_inheritance(server_name, config)
        logger.debug(
            "Resolved server '%s': type '%s' -> '%s'",
            server_name, server_config.type, final_type
        )
    except ValueError as e:
        logger.error("Failed to resolve inheritance for '%s': %s", server_name, e)
        # Fall back to direct config without inheritance (what it sets: the defaults come next)
        resolved_config_dict = server_config.model_dump(exclude_unset=True)
    
    # Start with default config, then merge resolved (inherited) config
    merged_config = default_config_dict.copy()
    merged_config = _deep_merge_dict(merged_config, resolved_config_dict, server_name)
    
    # Create and return final ToolServerConfig instance. Same tolerance as the config
    # load: a stale profile key that only appears AFTER inheritance (child
    # overrides the chains, inherits the keyed params) would otherwise raise
    # here — moving the abort from startup to agent creation, not removing it.
    final = ToolServerConfig.model_validate(
        merged_config, context={"drop_stale_llm_params": True})
    _warn_stale_llm_params(
        server_name, (merged_config.get("agent_config") or {}).get("llm_params"),
        getattr(final, "agent_config", None))
    return final


def _deep_merge_dict(base: dict, override: dict, _path: str = "",
                     *, explicit_none: bool = False) -> dict:
    """Deep merge two dictionaries, with override values taking precedence.

    A list is either MERGED into the inherited one or REPLACES it, never both:

    - `+item` appends to the inherited list
    - `!pattern` removes matching items from it (supports wildcards)
    - a list with no prefixes at all replaces the inherited one wholesale

    Examples:
        # Replace the entire list (no +/! anywhere)
        tools:
          allowed: ["new_tool/*"]

        # Merge with the inherited list
        tools:
          allowed:
            - "+new_tool/*"     # add
            - "!old_tool/*"     # remove

    Mixing the two forms raises — see :func:`_merge_lists_with_syntax`.

    Args:
        base: Base dictionary (default values)
        override: Override dictionary (specific values that override base)
        _path: Dotted key path, used only to make error messages locatable.
        explicit_none: What ``key: null`` in the override means. Default False —
            "nothing said", the inherited value stays; that is what an agent
            yaml with an empty key needs. True means "no value", the inherited
            one is removed: a model entry that inherits ``max_tokens: 16384``
            has no other way to say it wants the provider default.

    Returns:
        New dictionary with merged values
    """
    result = base.copy()

    for key, value in override.items():
        path = f"{_path}.{key}" if _path else str(key)
        if isinstance(value, dict) and (result.get(key) is None or isinstance(result[key], dict)):
            # Recursively merge nested dictionaries -- also into a value the parent
            # lacks or leaves None, or a nested "+x" would survive as a literal.
            # Such a dict is taken over whole otherwise: its Nones stay.
            fresh = not isinstance(result.get(key), dict)
            result[key] = _deep_merge_dict(result.get(key) or {}, value, path,
                                           explicit_none=explicit_none or fresh)
        elif isinstance(value, list):
            # Also for a key the parent does not have: without this, a "+x"
            # would survive into the value as a literal and match nothing.
            parent = result.get(key)
            # an inherited string ("llm_profile: normal") is a list of one: "+x" adds to it
            base_list = parent if isinstance(parent, list) else [parent] if isinstance(parent, str) else []
            result[key] = _merge_lists_with_syntax(base_list, value, path)
        elif value is not None or explicit_none:
            result[key] = value

    return result


def _merge_lists_with_syntax(parent_list: list, child_list: list,
                             path: str = "list") -> list:
    """Merge a child list into the inherited one, or let it replace it.

    Two mutually exclusive intents:

    - **merge** — every string carries ``+`` (add) or ``!`` (remove pattern)
    - **replace** — no string carries a prefix; the child list wins wholesale

    Mixing them raises ``ValueError``. The mix is almost always a forgotten
    ``+``, and it is unrecoverable by guessing: read as "replace", the prefixed
    entries would be the only survivors; read as "merge", the bare one silently
    joins the inherited list. Both readings are defensible, which is exactly why
    the config must say which one it means.

    Args:
        parent_list: The inherited list
        child_list: The override list from the child config
        path: Dotted key path, for the error message

    Returns:
        Merged or replaced list

    Raises:
        ValueError: if the child list mixes prefixed and bare string entries.
    """
    strings = [item for item in child_list if isinstance(item, str)]
    prefixed = [s for s in strings if s.startswith(('+', '!'))]
    bare = [s for s in strings if not s.startswith(('+', '!'))]

    if prefixed and bare:
        raise ValueError(
            f"Config '{path}' mixes list merge syntax with replacement entries. "
            f"Prefixed: {prefixed} — these add to / remove from the inherited "
            f"list. Without a prefix: {bare} — a list of those REPLACES the "
            f"inherited list entirely. One list cannot mean both. "
            f"Most likely a '+' was forgotten on {bare}; add it, or drop every "
            f"prefix to replace the list instead."
        )

    if not prefixed:
        # No merge syntax - complete replacement (original behavior)
        return child_list

    # The inherited list may itself still carry prefixes: a server's own "+x" is
    # only stripped when it is merged against default_config, and that happens
    # AFTER inheritance is resolved. Normalise it against an empty base first —
    # otherwise the "+" rides along into the child's result and later looks like
    # an authoring error that nobody made.
    return _apply_list_ops(_apply_list_ops([], parent_list), child_list)


def _apply_list_ops(base: list, items: list) -> list:
    """Apply ``+``/``!``/bare entries of *items* onto *base*.

    Deliberately permissive — mixing is rejected by the caller, which is the
    only place that knows whether the list was author-written or already merged.
    """
    result = list(base)

    for item in items:
        if not isinstance(item, str):
            # Non-string items are added as-is
            if item not in result:
                result.append(item)
            continue

        if item.startswith('!'):
            # Remove pattern from result
            pattern = item[1:]  # Strip ! prefix
            result = [r for r in result if not _matches_pattern(r, pattern)]
        else:
            clean_item = item[1:] if item.startswith('+') else item
            if clean_item not in result:
                result.append(clean_item)

    return result


def _matches_pattern(value: str, pattern: str) -> bool:
    """Whether a list entry matches a ``!pattern`` -- fnmatch, case-sensitive;
    ``"plugin/*"`` also matches the bare ``"plugin"``.

    A wildcard in the middle (``"*_sam/*"``) used to match nothing, silently.
    """
    if not isinstance(value, str):
        return False
    return fnmatchcase(value, pattern) or (pattern.endswith("/*") and value == pattern[:-2])
