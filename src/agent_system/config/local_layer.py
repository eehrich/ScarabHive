"""This machine's own layer, written: a credential into config/local.env, the signing key named in config/local.yaml.

Neither file is in the repository (.gitignore), so a `git pull` never meets what is written here. The Setup panel
and the install scripts write through this module (docs/einrichtung_konzept.md); the loader reads both
(settings.LOCAL_SECRETS, settings.LOCAL_CONFIG).

The install scripts run it as a module:  python -m agent_system.config.local_layer signing-key [config/config.yaml]
"""
from __future__ import annotations

import re
import secrets
import sys
import threading
from pathlib import Path
from typing import Collection, Optional

import yaml

from agent_system.utils import yaml_io
from agent_system.utils.io import atomic_write_text

from .settings import (LOCAL_CONFIG, LOCAL_SECRETS, _read_secrets_file, environment_at_restart, expand_env,
                       local_text, master_section, set_by_the_environment)

SIGNING_KEY_VARIABLE = "AUTH_SECRET_KEY"
SIGNING_KEY_REFERENCE = "${%s}" % SIGNING_KEY_VARIABLE

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: One write at a time: two saves read the file, and the later write would drop the other's line (two tabs of the
#: panel, each save on a thread of its own). Reentrant: ensure_signing_key writes through write_secret.
WRITING = threading.RLock()


def _text(path: Path) -> str:
    """A local file as text, "" where there is none. Raises ValueError where it is no text: rewriting it would
    garble what it holds."""
    if not path.is_file():
        return ""
    try:
        return local_text(path)
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not UTF-8 text: fix it by hand") from None


def write_secret(cfg_path: Path, name: str, value: str) -> Path:
    """Set *name* to *value* in the local secrets file beside *cfg_path*: its line replaced, or appended. Returns
    the file. Raises ValueError for what a secrets file cannot hold, OSError where it cannot be written."""
    value = value.strip()
    if not _NAME.match(name):
        raise ValueError(f"{name!r} is no variable name")
    if not value:
        raise ValueError("the value is empty")
    # Every break splitlines() knows (U+2028, U+0085, a form feed ...) would start a line of its own on reading: a
    # value could set another variable. An API key is one line of printable characters.
    if len(value.splitlines()) != 1 or not value.isprintable():
        raise ValueError("the value holds a line break or a character that is not printable")
    if value[0] in "\"'" or value[-1] in "\"'":
        raise ValueError("the value starts or ends with a quote, which the loader strips")
    path = cfg_path.parent / LOCAL_SECRETS
    with WRITING:
        lines, written = [], False
        for line in _text(path).splitlines():
            if line.strip().partition("=")[0].strip() == name and not line.strip().startswith("#"):
                if written:
                    continue  # a later line of the name: the first one counts, and it is this one now
                line, written = f"{name}={value}", True
            lines.append(line)
        if not written:
            lines.append(f"{name}={value}")
        atomic_write_text(path, "\n".join(lines) + "\n")
    return path


def signing_key_at_restart(cfg_path: Path) -> Optional[str]:
    """``auth.secret_key`` as a start would take it now: the master's and the local layer's auth section, ``${VAR}``
    expanded from the environment a start would have (secrets files as they read now). None where it would not
    load."""
    from .models import AuthConfig
    section = master_section(cfg_path, "auth", default={})
    return AuthConfig(**expand_env(section, environ=environment_at_restart(str(cfg_path)))).secret_key


def _own(key: Optional[str], known: Collection[str]) -> bool:
    from agent_system.auth.security import WeakSecretKeyError, check_secret_key
    if key is None or key in known:
        return False
    try:
        check_secret_key(key, reject_public=True)
    except WeakSecretKeyError:
        return False
    return True


def ensure_signing_key(cfg_path: Path, known: Collection[str] = ()) -> str:
    """Give this installation its own signing key, unless a start would already sign with one: a random value in the
    local secrets file, named by auth.secret_key in the local layer. *known* are further keys that count as public.
    Returns what was done, in a sentence. Raises ValueError where it cannot be done without touching a file by hand.

    A restart applies it, and every login signed with the old key ends then.
    """
    try:
        current = signing_key_at_restart(cfg_path)
    except Exception:  # noqa: BLE001 -- a broken auth section: the start fails on it, not on the key
        raise ValueError("the auth section of the configuration does not load: fix it first") from None
    if _own(current, known):
        return "this installation already has its own signing key"
    if set_by_the_environment(SIGNING_KEY_VARIABLE):  # before any file is touched: nothing here could apply
        raise ValueError(f"{SIGNING_KEY_VARIABLE} is set in the environment of this process, which wins over "
                         f"{LOCAL_SECRETS}: set an own key there")
    with WRITING:
        return _make_signing_key(cfg_path, known)


def _make_signing_key(cfg_path: Path, known: Collection[str]) -> str:
    _point_at_the_variable(cfg_path.parent / LOCAL_CONFIG)
    local_env = cfg_path.parent / LOCAL_SECRETS
    held = _read_secrets_file(local_env).get(SIGNING_KEY_VARIABLE) if local_env.is_file() else None
    if held is None or not _own(held, known):
        write_secret(cfg_path, SIGNING_KEY_VARIABLE, secrets.token_hex(32))
    if not _own(signing_key_at_restart(cfg_path), known):
        # another file still wins: a real variable, or auth set by hand somewhere the local layer does not reach
        raise ValueError(f"the key was written to {LOCAL_SECRETS}, but a start would still sign with another one")
    return (f"a new signing key is in {LOCAL_SECRETS}, named in {LOCAL_CONFIG}: a restart applies it, and everyone "
            "logs in again then")


def _point_at_the_variable(path: Path) -> None:
    """auth.secret_key in the local layer names SIGNING_KEY_VARIABLE -- added where it names nothing, left where it
    already does. ValueError where it names something else, or the file does not load: that is a hand's to change."""
    text = _text(path)
    try:
        data = yaml_io.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as error:
        raise ValueError(f"{path} does not load: {error}") from None
    data = {} if data is None else data
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a mapping: fix it by hand")
    auth = data.get("auth")
    if isinstance(auth, dict) and "secret_key" in auth:
        if auth["secret_key"] == SIGNING_KEY_REFERENCE:
            return
        raise ValueError(f"{path} sets auth.secret_key itself: change it there, or set it to "
                         f'"{SIGNING_KEY_REFERENCE}"')
    entry = f'secret_key: "{SIGNING_KEY_REFERENCE}"'
    lines = text.splitlines()
    if auth is None and "auth" not in data:
        lines += ["", "# This installation's own signing key, in local.env (python -m agent_system.config.local_layer)",
                  "auth:", f"  {entry}"]
    else:
        start = next((i for i, line in enumerate(lines) if re.match(r"auth:\s*(#.*)?$", line)), None)
        if start is None:
            raise ValueError(f"the auth section of {path} is not in block style: add {entry} to it by hand")
        indent = next((re.match(r"\s*", line).group() for line in lines[start + 1:]
                       if line.strip() and not line.lstrip().startswith("#")), "  ") or "  "
        lines.insert(start + 1, indent + entry)
    new = "\n".join(lines) + "\n"
    check = yaml_io.safe_load(new)
    if not (isinstance(check, dict) and isinstance(check.get("auth"), dict)
            and check["auth"].get("secret_key") == SIGNING_KEY_REFERENCE):
        raise ValueError(f"auth.secret_key could not be added to {path}: add {entry} under auth by hand")
    atomic_write_text(path, new)


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "signing-key" or len(argv) > 2:
        print("usage: python -m agent_system.config.local_layer signing-key [config/config.yaml]", file=sys.stderr)
        return 2
    from agent_system.paths import default_config_path
    cfg_path = Path(argv[1]) if len(argv) > 1 else default_config_path()  # the one the API loads
    if not cfg_path.is_file():
        print(f"no configuration at {cfg_path}", file=sys.stderr)
        return 1
    try:
        print(ensure_signing_key(cfg_path))
    except (ValueError, OSError) as error:
        print(f"signing key not set: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
