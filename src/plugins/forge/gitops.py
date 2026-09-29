"""The local clone: clone, fetch, switch and push, with the token only where git needs it.

The token reaches git as a credential: ``GIT_CONFIG_*`` environment variables
set a credential helper bound to the platform's own origin that reads it from
the environment -- never from the remote URL, the clone's config or a command
line (visible to every process). A credential, not an ``extraHeader``: git-lfs
adds that header to GitLab's upload URL next to the upload's own
Authorization, and GitLab answers 400 (facts.md F-GL10). Every other helper
-- the user's store, a ``gh`` login -- is reset for these calls.

Fetch and push name the URL the platform gave, not the ``origin`` remote a
shell may have rewritten; an ``insteadOf`` rule that sends it elsewhere finds
no credential, as the helper is bound to the origin. Redirects on the
origin are not followed (a URL-specific config may still turn them on; the
credential stays with the origin), and no hook or fsmonitor runs -- they would
be programs started with the token in their environment.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .http import ForgeError

# A branch name the plugin passes to git: no option lookalike, no ref syntax
# git refuses anyway (checked by git check-ref-format as well).
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")
_AUTH_REFUSED = ("could not read Username", "could not read Password", "Authentication failed",
                 "HTTP Basic: Access denied", "returned error: 401", "returned error: 403")
# The credential helper: shell code git runs through its sh. It names the
# variables, never their values; it answers every action (get, store, erase),
# git reads only the answer to get.
_HELPER = '!f() { echo "username=$FORGE_GIT_USER"; echo "password=$FORGE_GIT_TOKEN"; }; f'


class GitError(ForgeError):
    """git refused or failed; the text is git's own, shortened. ``output`` is
    all of it: what names the kind of failure may sit anywhere in it."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output or message


def valid_branch(name: str) -> bool:
    return bool(_BRANCH.fullmatch(name)) and ".." not in name and "//" not in name and "@{" not in name \
        and not name.endswith((".lock", "/", ".")) and "/." not in name


def same_repo(a: str, b: str) -> bool:
    """Two remote URLs name the same repository: host and path alike, whatever
    the scheme, user, port or ``.git`` -- an SSH origin of the user's own clone
    and the HTTPS URL of the platform count as one."""
    return _repo_key(a) == _repo_key(b) and _repo_key(a) != ("", "")


def _repo_key(url: str) -> tuple[str, str]:
    url = url.strip()
    match = None if "://" in url else _SCP_LIKE.match(url)
    if match:
        host, path = match.group(1), match.group(2)
    else:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
    path = path.strip("/")
    path = path[:-4] if path.lower().endswith(".git") else path
    return host.lower(), path.lower()


def auth_env(clone_url: str, user: str, token: str, ca_bundle: str = "", *, verify: bool = True) -> dict[str, str]:
    """The environment for one git process that talks to the platform.

    Certificates are checked for the platform's origin whatever the user's
    git config says -- a global ``http.sslVerify false`` (found on the
    development machine, review S1) would hand the header to anyone in the
    path. The URL-bound key outranks the global one; ``GIT_SSL_NO_VERIFY``,
    which outranks both, is dropped. Only the operator's ``tls_verify: false``
    for the host turns the check off."""
    parts = urlsplit(clone_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ForgeError(f"the platform's clone URL is not HTTP(S): {clone_url!r}")
    origin = f"{parts.scheme}://{parts.netloc}"
    config = [("credential.helper", ""),                # an empty value clears every helper before it
              (f"credential.{origin}.helper", _HELPER),
              (f"http.{origin}/.sslVerify", "true" if verify else "false"),
              (f"http.{origin}/.followRedirects", "false"),
              # An empty value clears push options a config may set (GitLab
              # reads them as orders: merge_request.merge_when_pipeline_succeeds).
              ("push.pushOption", "")]
    if ca_bundle:
        config.append((f"http.{origin}/.sslCAInfo", ca_bundle))
    env = {key: value for key, value in os.environ.items() if key != "GIT_SSL_NO_VERIFY"}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_CONFIG_COUNT": str(len(config)),
                "FORGE_GIT_USER": user, "FORGE_GIT_TOKEN": token})
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    return env


def _pinned(path: Optional[Path]) -> list[str]:
    no_hooks = (path or Path(".")) / ".git" / "forge-no-hooks"      # never created: no hook runs
    return ["-c", f"core.hooksPath={no_hooks}", "-c", "core.fsmonitor=false"]


def git(path: Optional[Path], *args: str, env: Optional[dict] = None, timeout: float = 120,
        ok: tuple[int, ...] = (0,)) -> str:
    cmd = ["git", *_pinned(path), *(["-C", str(path)] if path is not None else []), *args]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} took longer than {timeout:g} s") from None
    except OSError as exc:
        raise GitError(f"git {args[0]}: {exc}") from None
    if done.returncode not in ok:
        output = _redact(f"{done.stdout}\n{done.stderr}")
        if env is not None and any(sign in output for sign in _AUTH_REFUSED):
            # A refused token makes git ask for a user name, which it may not (F-GL9).
            raise GitError(f"git {args[0]}: the platform refused the token over git -- check that it is valid "
                           f"and may write the repository (GitLab: scope api or write_repository; GitHub: "
                           f"Contents read/write)", output)
        raise GitError(f"git {args[0]}: {_clean(done.stderr or done.stdout)}", output)
    return done.stdout


def _redact(text: str) -> str:
    """Without the header value, should any git version echo its config."""
    return re.sub(r"(?i)(authorization:\s*\w+\s+)\S+", r"\1***", text)


def _clean(text: str) -> str:
    """git's message, shortened: its first lines name the failure, the hints follow."""
    return _redact(text.strip())[:400]


def clone(url: str, path: Path, env: dict, default_branch: str, *, verify: bool = True) -> None:
    """A clone as init, check, fetch and checkout: the config check runs inside
    the new repository, where an ``includeIf "gitdir:..."`` section applies
    -- checked from its parent it would not, while ``git clone`` applies it
    (review of the fix round). A clone that stops half way is fetched into
    again by the next checkout."""
    path.mkdir(parents=True, exist_ok=True)
    git(None, "init", "--quiet", "-b", default_branch, str(path))
    git(path, "remote", "add", "origin", url)
    refuse_rewrites(url, env, path, verify=verify)
    fetch(path, url, env)
    if has_ref(path, f"refs/remotes/origin/{default_branch}"):     # an empty project has none
        adopt(path, default_branch, env)


def adopt(path: Path, branch: str, env: dict) -> None:
    """The platform's ``branch`` checked out as the local one, tracking it --
    the last step of a clone."""
    git(path, "checkout", "--quiet", "-B", branch, "--track", f"refs/remotes/origin/{branch}", env=env, timeout=900)


def origin_url(path: Path) -> str:
    """The origin as configured -- ``remote get-url`` would answer with it
    already rewritten by an insteadOf rule, which refuse_rewrites names."""
    return git(path, "config", "--get", "remote.origin.url", ok=(0, 1)).strip()


def fetch(path: Path, url: str, env: dict) -> None:
    git(path, "fetch", "--quiet", "--prune", "--", url, "+refs/heads/*:refs/remotes/origin/*", env=env, timeout=600)


def current_branch(path: Path) -> str:
    return git(path, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1)).strip()


def has_ref(path: Path, ref: str) -> bool:
    return bool(git(path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", ok=(0, 1)).strip())


def rev(path: Path, ref: str) -> str:
    return git(path, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def dirty(path: Path) -> list[str]:
    """Tracked files with changes not committed; untracked files do not block a switch."""
    return [line for line in git(path, "status", "--porcelain", "--untracked-files=no").splitlines() if line]


def switch(path: Path, branch: str, start: Optional[str] = None, env: Optional[dict] = None) -> None:
    """``env``: a checkout runs git-lfs's smudge filter, which downloads with
    the credential it is given -- without it, it asks the user's helpers."""
    if start is None:
        git(path, "switch", "--quiet", branch, env=env, timeout=900)
    else:
        git(path, "switch", "--quiet", "--no-track", "-c", branch, start, env=env, timeout=900)


def is_ancestor(path: Path, older: str, newer: str) -> bool:
    try:
        git(path, "merge-base", "--is-ancestor", older, newer)
    except GitError:
        return False
    return True


def sync(path: Path, ref: str, env: Optional[dict] = None) -> str:
    """Brings the checked-out branch up to ``ref`` where that loses nothing:
    ``up to date``, ``ahead`` (local commits not pushed yet), ``fast-forwarded``
    or ``diverged`` (both sides have commits -- left as it is)."""
    if rev(path, "HEAD") == rev(path, ref):
        return "up to date"
    if is_ancestor(path, ref, "HEAD"):
        return "ahead"
    if is_ancestor(path, "HEAD", ref):
        git(path, "merge", "--ff-only", "--quiet", ref, env=env, timeout=900)
        return "fast-forwarded"
    return "diverged"


def rewrites(url: str, env: Optional[dict], path: Optional[Path] = None, *, verify: bool = True) -> list[str]:
    """Git config that would send ``url`` elsewhere or unchecked: an
    ``url.<x>.insteadOf``/``pushInsteadOf`` rule -- e.g. to SSH, where git
    talks with the user's own identity instead of the bot's token (fix-round
    review); a remote named by the URL itself, whose ``url``/``pushurl`` git
    takes instead; and, while certificates are to be checked, whatever makes
    git skip the check for the URL or a path below it (git-lfs asks
    ``<url>/info/lfs/...``). Which ``sslVerify`` applies to the URL git decides
    itself -- its rules for specificity, wildcards and boolean spellings are
    its own (review of the fix round)."""
    found = []
    listing = git(path, "config", "-z", "--get-regexp",
                  r"^(url\..*\.(pushinsteadof|insteadof)|remote\..*\.(url|pushurl))$", env=env, ok=(0, 1))
    for record in listing.split("\0"):
        key, _, value = record.partition("\n")          # -z: a key may hold spaces, a value newlines
        name = key.lower()
        if name.startswith("url.") and url.lower().startswith(value.lower()):   # "" rewrites every URL
            found.append(f"{key} = {value}")
        elif name.startswith("remote.") and key[len("remote."):key.rindex(".")] == url:
            found.append(f"{key} = {value}")
    if verify and url.lower().startswith("https://"):
        applies = git(path, "config", "--type=bool", "--get-urlmatch", "http.sslverify", url, env=env,
                      ok=(0, 1)).strip()
        if applies == "false":
            found.append(f"http.<...>.sslVerify = false for {url}")
        # A key below the URL is asked about at its own place, git ranking it
        # against forge's key; keys elsewhere are never read as booleans, so a
        # malformed one of another host cannot stop forge (review of the fix round).
        below = url.rstrip("/").lower() + "/"
        for record in git(path, "config", "-z", "--get-regexp", r"^http\..+\.sslverify$", env=env,
                          ok=(0, 1)).split("\0"):
            place = record.partition("\n")[0][len("http."):-len(".sslverify")]
            if place.lower().startswith(below) and git(
                    path, "config", "--type=bool", "--get-urlmatch", "http.sslverify", place, env=env,
                    ok=(0, 1)).strip() == "false":
                found.append(f"http.{place}.sslVerify = false")
    return found


def refuse_rewrites(url: str, env: Optional[dict], path: Optional[Path] = None, *, verify: bool = True) -> None:
    found = rewrites(url, env, path, verify=verify)
    if found:
        raise ForgeError(f"the git config redirects {url} or turns off its certificate check ({'; '.join(found)}) "
                         f"-- forge would reach another place, with another identity or unchecked; remove that "
                         f"setting (to talk to a host without checking, set tls_verify: false on it in forge's "
                         f"config)")


def uses_lfs(path: Path, ref: str) -> bool:
    """The branch tracks files with Git LFS: a .gitattributes anywhere in it names the filter."""
    return bool(git(path, "grep", "-l", "-F", "filter=lfs", ref, "--", "*.gitattributes", ok=(0, 1)).strip())


def push(path: Path, url: str, local: str, remote: str, env: dict) -> None:
    """A plain push of exactly one branch: git refuses a non-fast-forward, and
    nothing here forces it. No tags and no submodules ride along, whatever
    the config says (followTags, recurseSubmodules would push outside the
    prefix). Hooks are off, so git-lfs's pre-push never runs: its objects go
    up first, explicitly, under the same header."""
    if uses_lfs(path, f"refs/heads/{local}"):
        try:
            git(path, "lfs", "push", url, f"refs/heads/{local}", env=env, timeout=1800)
        except GitError as exc:
            if "is not a git command" in exc.output:
                raise GitError("this repository keeps files in Git LFS, and git-lfs is not installed -- "
                               "install it where ScarabHive runs", exc.output) from None
            raise
    try:
        git(path, "push", "--quiet", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", "--", url,
            f"refs/heads/{local}:refs/heads/{remote}", env=env, timeout=600)
    except GitError as exc:
        text = exc.output
        if "non-fast-forward" in text or "fetch first" in text or "[rejected]" in text:
            raise GitError(f"the remote branch {remote} has commits that {local} does not: checkout the "
                           f"branch again (it fetches) and bring them in, then push -- nothing is forced") from None
        if "remote rejected" in text or "pre-receive hook declined" in text:
            # The platform's own reason is on its "remote:" lines (F-GL8).
            said = " ".join(ln[len("remote:"):].strip() for ln in text.splitlines()
                            if ln.startswith("remote:") and ln[len("remote:"):].strip())
            raise GitError(f"the platform refused the push to {remote}: {said[:300] or _clean(text)}", text) from None
        raise
