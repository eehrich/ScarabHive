"""The process environment as the configuration takes it: the secrets files beside the master
config read into it, which of its values came from such a file, and ``${VAR}`` expansion from it.

Its own module because this is the process's state, not one load's: what a secrets file put into
the environment is remembered -- and handed on to the processes this one starts -- for as long as
the process runs, whichever config it loads next. The Setup panel, the terminal plugin and
local_layer.py ask it what a restart would see.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

# The loader's one logger, the name it reported under before it was split into modules: a filter
# on load_settings.__module__ (the agent editor's quiet_loader) must keep catching all of it.
logger = logging.getLogger("agent_system.config.settings")


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


def text_encoding(data: bytes) -> str:
    """How a file written by hand is decoded: UTF-16 where it starts with that BOM -- what PowerShell 5.1's `>`
    writes -- else UTF-8, with a BOM or without (utf-8-sig drops one). Secrets files and the local layer alike."""
    return "utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"


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
    encoding = text_encoding(data)
    utf16 = encoding == "utf-16"
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
