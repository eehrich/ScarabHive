"""Claude Code as a child process: its command line, its environment, its
stream, and the git worktree it works in (docs/coding_cli_plugin_konzept.md).

Every flag here was measured on Claude Code 2.1.257 (concept §2, M-CC-*). The
process runs detached and writes its stream into a file, so a run outlives
the ScarabHive process that started it -- an API restart, a one-shot
agent-cli run -- and whoever looks next finds its end on disk.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional, Sequence

import psutil
import yaml

from agent_system.config.settings import _secrets_file_entries
from agent_system.core.session_presence import alive
from agent_system.utils import yaml_io

# Planning reads; editing also writes. --tools is always explicit: under
# --restricted the list still held Artifact, SendMessage, PushNotification and
# more (M-CC-6), and naming exactly these left exactly these (M-CC-9).
PLAN_TOOLS = ("Read", "Glob", "Grep")
EDIT_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
# The shell tool may not run these itself, whatever allowed_commands says. A
# program an allowed command starts is no tool call and is not checked.
DENIED_COMMANDS = ("git push", "git remote", "git config", "git worktree")
# What Claude Code needs to start and find its login. Everything else stays
# out, above all the keys config/secrets.env put into this process: an
# ANTHROPIC_API_KEY would even switch it from the subscription to the API.
ENV_NAMES = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "TEMP", "TMP", "TMPDIR", "USERNAME",
    "USERDOMAIN", "COMPUTERNAME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "TZ", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PSMODULEPATH", "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"})
CAP_ACTION = 100
# Secret values an excluded file holds: a .env line's value, a YAML key named like one.
_SECRET_KEY = re.compile(r"(?i)secret|password|passwd|token|api[_-]?key|private[_-]?key")
MIN_SECRET_CHARS = 8


class GitError(RuntimeError):
    pass


def find_claude(command: str) -> Optional[list[str]]:
    """The executable to start, or None. npm's claude.cmd would run it through
    cmd.exe, which parses the arguments by its own rules; the executable it
    calls sits beside it."""
    found = shutil.which(command or "claude")
    if found and os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        exe = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        return [str(exe)] if exe.is_file() else None
    return [found] if found else None


def shell_tool() -> str:
    """Claude Code's shell tool here: on Windows it offers PowerShell, and a
    Bash named in --tools does not appear (M-CC-9)."""
    return "PowerShell" if os.name == "nt" else "Bash"


def build_command(exe: list[str], *, mode: str, mcp_config: Path, allowed_commands: Iterable[str] = (),
                  model: str = "", resume: str = "", rules: Optional[Path] = None) -> list[str]:
    """The whole command line. The task goes in on stdin, so nothing the model
    wrote ever becomes an argument; resume is a session id this plugin read."""
    tools = list(PLAN_TOOLS if mode == "plan" else EDIT_TOOLS)
    cmd = [*exe, "-p", "--output-format", "stream-json", "--verbose", "--restricted",
           "--strict-mcp-config", "--mcp-config", str(mcp_config),
           "--permission-mode", "plan" if mode == "plan" else "acceptEdits"]
    commands = [c for c in allowed_commands if c] if mode != "plan" else []
    if commands:
        # Headless, a command nobody approved is refused (M-CC-9): the shell
        # runs exactly these, never the denied ones.
        shell = shell_tool()
        tools.append(shell)
        cmd += ["--allowedTools", *(f"{shell}({c})" for c in commands),
                "--disallowedTools", *(f"{shell}({c}:*)" for c in DENIED_COMMANDS)]
    cmd += ["--tools", ",".join(tools)]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    if rules is not None:
        # --restricted leaves the project's CLAUDE.md unread; appended, it is read (M-CC-9).
        cmd += ["--append-system-prompt-file", str(rules)]
    return cmd


def child_env(extra: Iterable[str] = ()) -> dict[str, str]:
    names = ENV_NAMES | {n.upper() for n in extra}
    return {k: v for k, v in os.environ.items() if k.upper() in names}


def launch(cmd: list[str], cwd: Path, env: dict, stdin: Path, stdout: Path, stderr: Path) -> subprocess.Popen:
    """Started in a group of its own, without a window, so neither Ctrl+C in
    ScarabHive's console nor its end takes the run along."""
    if os.name == "nt":
        options: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        options = {"start_new_session": True}
    with open(stdin, "rb") as task, open(stdout, "wb") as out, open(stderr, "wb") as err:
        return subprocess.Popen(cmd, cwd=cwd, env=env, stdin=task, stdout=out, stderr=err, **options)


def process_start(pid: int) -> float:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return 0.0


def kill_tree(pid: Optional[int], started: Optional[float], wait_s: float = 10) -> None:
    """The run and whatever it started (a shell, a test run), and back only
    once they are gone: on Windows the kill is asynchronous, and a caller
    finalizing right after it found the run still alive. The start time keeps
    a reused pid from being taken for it."""
    if not pid or not alive(pid, started):
        return
    try:
        root = psutil.Process(pid)
        procs = [*root.children(recursive=True), root]
    except psutil.Error:
        return
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=wait_s)


def events(path: Path) -> list[dict]:
    return events_from(path, 0)[0]


def events_from(path: Path, offset: int) -> tuple[list[dict], int]:
    """The stream's complete JSON lines from a byte offset on, and the offset
    after them; a torn or foreign line is skipped."""
    try:
        with open(path, "rb") as stream:
            stream.seek(offset)
            chunk = stream.read()
    except OSError:
        return [], offset
    end = chunk.rfind(b"\n") + 1
    found = []
    for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            found.append(event)
    return found, offset + end


def actions(event: dict, root: Path, tools: bool = True) -> list[str]:
    """What an assistant event did, one short line per tool call or text --
    the text only with tools=False."""
    if event.get("type") != "assistant":
        return []
    lines = []
    for block in (event.get("message") or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and tools:
            lines.append(tool_line(str(block.get("name") or "?"), block.get("input"), root))
        elif block.get("type") == "text":
            first = next((ln.strip() for ln in str(block.get("text") or "").splitlines() if ln.strip()), "")
            if first:
                lines.append(first)
    return [line[:CAP_ACTION] for line in lines]


def tool_line(name: str, args: Any, root: Path) -> str:
    args = args if isinstance(args, dict) else {}
    target = str(args.get("file_path") or args.get("path") or args.get("pattern") or args.get("command") or "")
    try:
        target = Path(target).relative_to(root).as_posix()
    except ValueError:
        pass
    return f"{name} {' '.join(target.split())}".strip()


def outcome(stream: Iterable[dict]) -> dict:
    """The parts of a finished stream the plugin keeps (M-CC-1, M-CC-2)."""
    found: dict[str, Any] = {"session": None, "result": None, "rate_limit": None}
    for event in stream:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            found["session"] = event.get("session_id")
        elif kind == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            found["rate_limit"] = event["rate_limit_info"]
        elif kind == "result":
            found["result"] = event
    return found


def windows(rate_limit: Any) -> dict[str, float]:
    """Utilization per subscription window, e.g. {"five_hour": 0.35}."""
    found = rate_limit.get("unifiedWindows") if isinstance(rate_limit, dict) else None
    return {name: float(w["utilization"]) for name, w in (found if isinstance(found, dict) else {}).items()
            if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float))}


def quota_block(rate_limit: Any, limit: float, now: float) -> str:
    """Why no run may start on the last known subscription state, or "". It is
    a lower bound: the user's own sessions have used it since."""
    if not isinstance(rate_limit, dict):
        return ""
    found = rate_limit.get("unifiedWindows")
    for name, w in (found if isinstance(found, dict) else {}).items():
        if not isinstance(w, dict):
            continue
        used, resets = w.get("utilization"), w.get("resetsAt")
        if isinstance(used, (int, float)) and used >= limit and isinstance(resets, (int, float)) and resets > now:
            return (f"the subscription's {name} window is at {used:.0%}, the limit for runs is {limit:.0%}; "
                    f"it resets {_when(resets)}")
    resets = rate_limit.get("resetsAt")
    if not str(rate_limit.get("status") or "allowed").startswith("allowed") \
            and isinstance(resets, (int, float)) and resets > now:
        return f"the subscription refuses requests ({rate_limit.get('status')}) until {_when(resets)}"
    return ""


def _when(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def git(cwd: Path, *args: str, timeout: float = 120, pinned: Sequence[str] = (), stdin: Optional[str] = None,
        ok: tuple[int, ...] = (0,)) -> str:
    try:
        done = subprocess.run(["git", *pinned, *args], cwd=cwd, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]}: {exc}") from exc
    if done.returncode not in ok:
        raise GitError(f"git {args[0]}: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout


class Worktree(NamedTuple):
    base: str
    git_dir: str
    hidden: list[str]


def make_worktree(repo: Path, path: Path, branch: str, exclude: Iterable[str]) -> Worktree:
    """A fresh worktree of the repo's HEAD on a new branch. Excluded files are
    taken out of it and hidden from git, so they are neither readable there nor
    committed as deleted (measured 22.09.) -- and so is every other file that
    holds one of the secret values they carry: a key copied into a doc or a
    test would be readable otherwise."""
    base = git(repo, "rev-parse", "HEAD").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)
    # Written by git a moment ago; the .git file in the worktree is the run's to rewrite.
    git_dir = git(path, "rev-parse", "--absolute-git-dir").strip()
    excluded = {t for rel in exclude for t in git(path, "ls-files", "-z", "--", rel).split("\0") if t}
    values = set()
    for rel in {*excluded, *exclude}:
        for source in (repo / rel, path / rel):
            values |= secret_values(source)
    hidden = sorted(excluded)
    if values:
        # On stdin, never as arguments: a command line is visible to every process.
        found = git(path, "grep", "-l", "-z", "-I", "-F", "-f", "-", stdin="\n".join(sorted(values)) + "\n",
                    ok=(0, 1))
        hidden = sorted(excluded | {f for f in found.split("\0") if f})
    for tracked in hidden:
        git(path, "update-index", "--skip-worktree", "--", tracked)
        (path / tracked).unlink(missing_ok=True)
    return Worktree(base, git_dir, hidden)


def secret_values(path: Path) -> set[str]:
    """What an excluded file keeps secret: every value of a .env file, the
    string values of secret-named keys in a YAML file. Short ones would hide
    half the repository and are no key."""
    found: set[str] = set()
    if path.name.endswith(".env"):
        # read as the loader reads it (a BOM, UTF-16, a line in another encoding), every line: a key rotated by
        # a second line of the same name is the live one wherever the last line wins
        try:
            # the loader strips `"` and then `'`; `KEY='"v"'` keeps the inner quotes there -- the key is v either way
            found = {form for _, value in _secrets_file_entries(path) for form in (value, value.strip("'\""))}
        except OSError:
            return set()
        return {v for v in found if len(v) >= MIN_SECRET_CHARS and "\n" not in v and "${" not in v}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return set()
    if path.suffix in (".yaml", ".yml"):
        try:
            _secret_leaves(yaml_io.safe_load(text), found)
        except yaml.YAMLError:
            return set()
    return {v for v in found if len(v) >= MIN_SECRET_CHARS and "\n" not in v and "${" not in v}


def _secret_leaves(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and _SECRET_KEY.search(str(key)):
                found.add(value)
            _secret_leaves(value, found)
    elif isinstance(node, list):
        for value in node:
            _secret_leaves(value, found)


def _pinned(path: Path, git_dir: str) -> list[str]:
    """ScarabHive's own git in a worktree a run wrote to: the git directory
    named at its creation, not the .git file the run could rewrite, and no hook
    or fsmonitor -- they would run programs in this process, with its keys."""
    return ["--git-dir", git_dir, "--work-tree", str(path),
            "-c", f"core.hooksPath={Path(git_dir) / 'coding_cli-no-hooks'}", "-c", "core.fsmonitor=false"]


def commit_all(path: Path, git_dir: str, message: str) -> Optional[str]:
    """Everything the run left, as one commit on its branch; None when it left nothing."""
    pinned = _pinned(path, git_dir)
    git(path, "add", "-A", pinned=pinned)
    if not git(path, "diff", "--cached", "--name-only", pinned=pinned).strip():
        return None
    git(path, "commit", "-q", "-m", message, pinned=pinned)
    return git(path, "rev-parse", "HEAD", pinned=pinned).strip()


def changes(path: Path, git_dir: str, base: str, limit: int = 100) -> list[str]:
    """The branch against its base, one "M<TAB>src/x.py" per file, plus what is not committed."""
    pinned = _pinned(path, git_dir)
    lines = [ln for ln in git(path, "diff", "--name-status", base, "HEAD", pinned=pinned).splitlines() if ln]
    lines += [f"uncommitted {ln}" for ln in git(path, "status", "--porcelain", pinned=pinned).splitlines() if ln]
    return lines[:limit] + ([f"... {len(lines) - limit} more"] if len(lines) > limit else [])
