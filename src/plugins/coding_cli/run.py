"""Claude Code -- or the Antigravity CLI (agy) -- as a child process: its
command line, its environment, its stream, and the git worktree it works in.
agy's stream is read in Claude Code's shape (_from_agy).

Every flag here was measured on Claude Code 2.1.257, the MCP servers and
--json-schema on 2.1.285 (concept §2, M-CC-*). The
process runs detached and writes its stream into a file, so a run outlives
the ScarabHive process that started it -- an API restart, a one-shot
agent-cli run -- and whoever looks next finds its end on disk.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Sequence

import yaml

from agent_system.utils import proctree as _proctree
from agent_system import own_console
from agent_system.config.settings import _secrets_file_entries
from agent_system.utils import yaml_io
from agent_system.utils import path_sandbox

# Planning reads; editing also writes. --tools is always explicit: under
# --restricted the list still held Artifact, SendMessage, PushNotification and
# more (M-CC-6), and naming exactly these left exactly these (M-CC-9).
PLAN_TOOLS = ("Read", "Glob", "Grep")
EDIT_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
# A workdir with web: Claude Code's own search and page reader. Headless, each needs approval like
# an MCP server's tools; measured on 2.1.285 under --restricted: searched and read, nothing refused.
WEB_TOOLS = ("WebSearch", "WebFetch")
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


def find_agy(command: str) -> Optional[list[str]]:
    """The Antigravity CLI to start, or None. Its installer places a native executable; a .cmd or .bat would run
    it through cmd.exe, which parses the arguments -- json_schema among them -- by its own rules."""
    found = shutil.which(command or "agy")
    return [found] if found and not (os.name == "nt" and found.lower().endswith((".cmd", ".bat"))) else None


def shell_tool() -> str:
    """Claude Code's shell tool here: on Windows it offers PowerShell, and a
    Bash named in --tools does not appear (M-CC-9)."""
    return "PowerShell" if os.name == "nt" else "Bash"


def secret_rules() -> list[str]:
    """Deny rules for the installation's private files (path_sandbox.private_places: config/*.env, the user store,
    the sessions and their archive, the message debugger's store):
    a worktree holds no copy (they are git-ignored), but the read tools reach outside it. Claude
    Code writes an absolute path as //<path>, a Windows drive as /c/ (its permission docs); Read
    covers the read tools, Edit the writing ones."""
    def spelled(path: Path) -> str:
        text = path.resolve().as_posix()
        return f"/{text[0].lower()}{text[2:]}" if re.match(r"^[A-Za-z]:/", text) else text

    secrets, users_db, folders = path_sandbox.private_places()
    patterns = [f"{spelled(secrets)}/*.env", f"{spelled(users_db)}*", *(f"{spelled(folder)}/**" for folder in folders)]
    return [f"{tool}(/{pattern})" for pattern in patterns for tool in ("Read", "Edit")]


def build_command(exe: list[str], *, mode: str, mcp_config: Path, allowed_commands: Iterable[str] = (),
                  mcp_servers: Optional[Mapping[str, Iterable[str]]] = None, model: str = "", resume: str = "",
                  rules: Optional[Path] = None, json_schema: Optional[dict] = None, web: bool = False,
                  effort: str = "", web_with_shell: bool = False, files: bool = True) -> list[str]:
    """The whole command line. The task goes in on stdin, so nothing the model
    wrote becomes an argument but json_schema; resume is a session id this
    plugin read, mcp_servers the operator's names, each with the tools its
    entry blocks. web: the workdir's runs search and read the web (WEB_TOOLS), in either mode, and
    get no shell: a page can tell the model what to do, and a file it wrote and an allowed command
    would run it -- unless the operator accepts that (web_with_shell, the instance's setting). What the
    worktree shows can still leave in a URL WebFetch calls. files=False: a run in a room (claude_input) gets no
    file tool and no shell -- others work in the same folder -- and takes its task as stream-json."""
    file_tools = (PLAN_TOOLS if mode == "plan" else EDIT_TOOLS) if files else ()
    tools = [*file_tools, *(WEB_TOOLS if web else ())]
    cmd = [*exe, "-p", "--output-format", "stream-json", "--verbose", "--restricted",
           "--strict-mcp-config", "--mcp-config", str(mcp_config),
           "--permission-mode", "plan" if mode == "plan" else "acceptEdits"]
    if not files:
        cmd += ["--input-format", "stream-json"]
    commands = ([c for c in allowed_commands if c] if files and mode != "plan" and (web_with_shell or not web)
                else [])
    servers = (mcp_servers or {}) if mode != "plan" else {}
    shell = shell_tool()
    # Headless, a command nobody approved is refused (M-CC-9): the shell runs
    # exactly these, never the denied ones. An MCP server's tools need the same
    # approval (M-CC-10). --tools does not limit them; plan mode loads no server.
    allowed = [*(f"{shell}({c})" for c in commands), *(f"mcp__{s}" for s in servers), *(WEB_TOOLS if web else ())]
    # A tool the server's entry blocks stays refused, as the MCP client refuses it: a deny rule wins over
    # every allow rule. Claude Code names it mcp__<server>__<tool>, any other character than A-Z a-z 0-9 _ -
    # replaced by _ (its MCP docs) -- so the name is one value, too.
    denied = [*(f"{shell}({c}:*)" for c in DENIED_COMMANDS if commands),
              *(f"mcp__{s}__{re.sub(r'[^A-Za-z0-9_-]', '_', str(t))}"
                for s, blocked in servers.items() for t in blocked),
              *secret_rules()]
    # Variadic: each value list runs to the next flag, and one always follows.
    if allowed:
        cmd += ["--allowedTools", *allowed]
    if denied:
        cmd += ["--disallowedTools", *denied]
    if commands:
        tools.append(shell)
    cmd += ["--tools", ",".join(tools)]
    if isinstance(json_schema, dict):
        # The model's schema, as one value: the JSON of a dict starts with "{",
        # so it is never read as a flag. NaN is no JSON.
        cmd += ["--json-schema", json.dumps(json_schema, allow_nan=False)]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if resume:
        cmd += ["--resume", resume]
    if rules is not None:
        # --restricted leaves the project's CLAUDE.md unread; appended, it is read (M-CC-9).
        cmd += ["--append-system-prompt-file", str(rules)]
    return cmd


# The Antigravity CLI (agy), measured on 1.4.8, 2026-10-27. Headless it refuses whatever needs an approval and
# reports it as denied_actions: every shell command, every file outside the workspace (its working directory);
# accept-edits lets it write files inside, plan only read them. Its web search needs no approval and no setting
# switched it off (disabledTools, excludeTools, toolPermission strict): an instance takes only web workdirs.
# It has no allow list for single commands and loads no MCP server of the operator's here. All of this holds for
# the settings agy_home pins; the user's own (an auto-approval, an MCP server, memories) stay out of a run.
# --effort works with a model id without its level (gemini-3.8-flash); with one that names it
# (gemini-3.8-flash-medium) agy refuses the run -- the server drops such an effort.
def build_agy_command(exe: list[str], *, mode: str, model: str = "", resume: str = "",
                      json_schema: Optional[dict] = None, effort: str = "") -> list[str]:
    """The whole agy command line; the task goes in on stdin (agy_input), as Claude Code's does."""
    cmd = [*exe, "--output-format", "stream-json", "--input-format", "stream-json", "--disable-slash-commands",
           "--mode", "plan" if mode == "plan" else "accept-edits"]
    if isinstance(json_schema, dict):
        cmd += ["--json-schema", json.dumps(json_schema, allow_nan=False)]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if resume:
        cmd += ["--conversation", resume]
    # Last and empty: --print takes the next word as its prompt, a flag too.
    return [*cmd, "--print="]


# The settings an agy run reads: its home is this folder, not the user's (agy_home). The permission preset stays
# agy's default (request-review; agy writes a default it finds back as nothing). agy runs the hooks, plugins and MCP
# servers a workspace's customization roots name, hooks without asking (measured 1.4.8: a command hook in
# hooks.json ran at the session start, from each of the four roots), and an edit run may write its worktree -- so it
# may write no root (a deny rule holds in every mode, measured), and a run does not start on a worktree that
# carries such a file (agy_unsafe).
AGY_ROOTS = (".agents", "_agents", ".agent", "_agent")
# The MCP server a run's gateway grant is reached by (both CLIs). agy calls a server's tools headless only with an
# allow rule (measured 1.4.8); only the file agy_gateway writes names this one -- a repository's is refused.
GATEWAY_SERVER = "gateway"
AGY_SETTINGS = {"permissions": {"allow": [f"mcp({GATEWAY_SERVER}/*)"],
                                "deny": [f"write_file({root}/)" for root in AGY_ROOTS]}}
# What agy loads from a root and would run: a repository or an earlier run may not hand it a run.
AGY_UNSAFE = ("hooks.json", "plugins", "mcp_config.json")
# Claude Code's own variables: its login token goes to no other program.
CLAUDE_ONLY_ENV = frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"})


def claude_login_unwritable(env: Mapping[str, str]) -> list[str]:
    """Where Claude Code keeps its login and could not write it under *env* -- its config folder (CLAUDE_CONFIG_DIR,
    else ~/.claude) and, without that variable, ~/.claude.json, which it keeps writing as it runs. The renewed access
    token goes into the folder (.credentials.json); unwritable, every login holds only until the token expires
    (8 h), then each run fails with 401 and its caller falls back -- measured on a server whose service unit made
    the home read-only. A long-lived token (CLAUDE_CODE_OAUTH_TOKEN) is renewed by nobody: nothing to write. No
    home to be found: nothing said -- the check is a warning, never what keeps an instance from starting."""
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return []
    try:
        home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    except (RuntimeError, OSError):  # no HOME and no passwd entry for the uid
        return []
    configured = env.get("CLAUDE_CONFIG_DIR")
    paths = [Path(configured)] if configured else [home / ".claude", home / ".claude.json"]
    # One that is not there yet is written into its folder.
    return [str(path) for path in paths if not os.access(path if path.exists() else path.parent, os.W_OK)]


def agy_home(root: Path, env: dict) -> None:
    """Points env's home at `root`, holding the settings an agy run is measured under (AGY_SETTINGS) and nothing
    of the user's -- their permission presets, MCP servers and memories live in their own home. The login is not
    there: on Windows agy finds it in the credential store (measured, 1.4.8).

    The home is given resolved: agy reads its own files (the schemas of a gateway's tools) through a path it
    resolves, and a home reached over a symlink made them files outside the run -- refused, and the run never
    reached the gateway (measured 1.4.10, 2026-10-29)."""
    root = root.resolve()
    settings = root / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    # Rewritten only when it holds anything else -- in one step, while parallel runs start: a truncated file is no
    # settings at all.
    if current != AGY_SETTINGS:
        tmp = settings.with_name(f"settings.{os.getpid()}.{time.monotonic_ns()}.tmp")
        tmp.write_text(json.dumps(AGY_SETTINGS), encoding="utf-8")
        os.replace(tmp, settings)
    for key in [k for k in env if k.upper() in CLAUDE_ONLY_ENV | {"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"}]:
        del env[key]
    env["HOME"] = str(root)
    if os.name == "nt":
        env["USERPROFILE"] = str(root)


def agy_unsafe(worktree: Path) -> list[str]:
    """What in `worktree` agy would load and run (AGY_UNSAFE in an AGY_ROOTS folder): a run must not start on it.
    Names are compared without case -- a Windows file system finds .AGENTS/ as .agents/."""
    return sorted(f"{root.name}/{item.name}" for root in worktree.iterdir()
                  if root.name.lower() in AGY_ROOTS and root.is_dir()
                  for item in root.iterdir() if item.name.lower() in AGY_UNSAFE)


def agy_gateway(worktree: Path, url: str, token: Optional[str]) -> None:
    """Writes (token) or removes (None) the MCP file through which an agy run reaches its gateway grant. The token
    stands in it as it is: agy expands no ${VAR} in a header (measured 1.4.8). Written after agy_unsafe looked,
    removed before the run's commit."""
    path = worktree / AGY_ROOTS[0] / "mcp_config.json"
    if token is None:
        path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            path.parent.rmdir()         # only when nothing else is in it
        return
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {GATEWAY_SERVER: {
        "serverUrl": url, "headers": {"Authorization": f"Bearer {token}"}}}}), encoding="utf-8")


def agy_note(tools: Optional[Iterable[str]] = None) -> str:
    """What every agy run reads before its task. A headless run has no shell: a command it runs (to list a folder,
    `git status`) is refused and the run ends without an answer -- measured with and without a gateway (1 of 30
    runs with one; 3 of 8 of a tool-less answering agent without, 2026-10-29).

    *tools*: those of the run's gateway grant. agy offers MCP tools only through its call_mcp_tool, under no name
    of their own; a task that names them is read as naming agy's own file tools, and those a headless run is refused
    for a file outside its workspace. A task on a file outside the workspace that named no tool: with the note every
    run took the gateway, without it every run its own tools (measured 1.4.9, 2026-10-29). Inside the working
    directory its own tools stay the ones to use."""
    if not tools:
        return "You have no shell: a command is refused. Work from the task and your file tools.\n\n"
    return (f"Besides your own tools you have the MCP tools of the server `{GATEWAY_SERVER}`: {', '.join(tools)}. "
            f"You reach them only through call_mcp_tool (server `{GATEWAY_SERVER}`). For files outside your working "
            "directory use them: your own file tools (read_file, write_file, edit_file, ...) are refused there. "
            "You have no shell: a command is refused, so look at folders and files with these tools too.\n\n")


def claude_input(blocks: Sequence[str]) -> str:
    """The task as the one stdin message of Claude Code's stream-json input, a text block for each part -- for a run
    without files, in its workdir's room. Measured 2026-11-06 on 2.1.x, each against the subscription:
    - Claude Code writes the working directory and the git status (branch, commits, files) into its system prompt,
      ahead of the task: the same task in another folder, on another branch or after a commit read only Claude
      Code's own part from the cache. A run in a worktree of its own shares no prefix with any other; a room is the
      same folder and repository for every run. includeGitInstructions would leave the git part out, but
      --restricted leaves --settings unread.
    - The API caches at a block's end: a task that begins with a whole earlier task reads it back only when that
      part is a block of its own. The same text as one block was written anew, plain stdin text matched no block."""
    content = [{"type": "text", "text": block} for block in blocks if block]
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False) + "\n"


def agy_input(task: str) -> str:
    """The task as the one stdin message agy's stream-json input takes."""
    return json.dumps({"event": "user", "message": {"role": "user", "content": task}}, ensure_ascii=False) + "\n"


# agy's tool parameters under the names tool_line looks for.
_AGY_ARGS = {"Path": "path", "AbsolutePath": "path", "TargetFile": "path", "CommandLine": "command",
             "query": "pattern", "Query": "pattern"}


def _agy_error(error: Any) -> str:
    """A failed step's reason: measured as {"type", "message"}; anything else as it stands."""
    return str(error.get("message") or "") if isinstance(error, dict) else str(error or "")


def _from_agy(event: dict) -> Optional[dict]:
    """An agy stream event in Claude Code's shape, so outcome, actions and the live view read both; None for what
    they need not see -- the answer's text deltas: the result carries it whole."""
    kind = event.get("event")
    if kind == "init":
        init = event.get("init") if isinstance(event.get("init"), dict) else {}
        return {"type": "system", "subtype": "init", "session_id": event.get("conversation_id"),
                "model": init.get("model")}
    if kind == "result":
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        ok = result.get("status") == "SUCCESS"
        denied = result.get("denied_actions") if isinstance(result.get("denied_actions"), list) else []
        return {"type": "result", "subtype": "success" if ok else "error", "is_error": not ok,
                "result": result.get("response") if ok else (result.get("error") or result.get("response")),
                "num_turns": result.get("num_turns"),
                "duration_ms": 1000 * float(result.get("duration_seconds") or 0),
                "permission_denials": [{"tool_name": d.get("display_name") or d.get("action")}
                                       for d in denied if isinstance(d, dict)],
                "structured_output": result.get("structured_output"), "usage": result.get("usage")}
    step = event.get("step_update")
    if kind != "step_update" or not isinstance(step, dict) or step.get("step_type") != "tool":
        return None
    info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
    call = f"{step.get('conversation_id')}-{step.get('step_index')}"
    if step.get("state") == "ACTIVE":
        params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
        args = {_AGY_ARGS.get(k, k): v for k, v in params.items()}
        return {"type": "assistant", "message": {"id": call, "content": [
            {"type": "tool_use", "id": call, "name": step.get("tool_name"), "input": args}]}}
    if step.get("state") in ("DONE", "ERROR"):
        return {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": call, "is_error": step.get("state") == "ERROR",
             "content": str(info.get("output") or _agy_error(info.get("error")))}]}}
    return None


def child_env(extra: Iterable[str] = ()) -> dict[str, str]:
    names = ENV_NAMES | {n.upper() for n in extra}
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    # An allowed command such as `python -m pytest` runs this process's interpreter, with its packages --
    # not whichever Python the PATH it was started with finds first.
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    env[key] = os.pathsep.join(p for p in (str(Path(sys.executable).parent), env.get(key, "")) if p)
    return env


def launch(cmd: list[str], cwd: Path, env: dict, stdin: Path, stdout: Path, stderr: Path) -> subprocess.Popen:
    """Started in a group of its own, without a window, so neither Ctrl+C in
    ScarabHive's console nor its end takes the run along."""
    if os.name == "nt":
        options: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        options = {"start_new_session": True}
    with open(stdin, "rb") as task, open(stdout, "wb") as out, open(stderr, "wb") as err:
        # out of the job an API on a console of its own dies with (own_console)
        return own_console.popen_outliving(cmd, cwd=cwd, env=env, stdin=task, stdout=out, stderr=err, **options)


# One implementation in the core (agent_system.utils.proctree); the names stay
# here for the server and the tests.
process_start = _proctree.process_start
kill_tree = _proctree.kill_tree


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
            if isinstance(event, dict) and _SURROGATE_ESCAPE.search(line):
                event = _text(event)
        except (ValueError, RecursionError):
            # No JSON, or nested past what the parser takes (the model's deep tool arguments): no event.
            continue
        if isinstance(event, dict) and "event" in event and "type" not in event:
            event = _from_agy(event)
        if isinstance(event, dict):
            found.append(event)
    return found, offset + end


# How deep the model's objects (json_schema, the structured output, a tool call's arguments) may nest where
# Python's own code walks them -- the record's encoder (indent=...), the live view -- which recursed out at
# about 1,000 levels; the parser takes about 3,000.
MAX_NESTING = 100


def nesting(value: Any) -> int:
    """How deep lists and objects nest in value, counted without recursion: a
    model can nest them deeper than Python's own encoder (indent=...) recurses."""
    depth, level = 0, [value]
    while True:
        level = [x for x in level if isinstance(x, (dict, list))]
        if not level:
            return depth
        depth += 1
        level = [v for x in level for v in (x.values() if isinstance(x, dict) else x)]


# A \ud800-\udfff escape: alone (a broken emoji the model wrote) json.loads makes it a lone surrogate, which no
# UTF-8 writer takes -- the record, the live view, the framework's stream of the answer.
_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F]")


def _text(value: Any) -> Any:
    """value with every lone surrogate replaced by U+FFFD, as a decoder replaces a broken byte."""
    return json.loads(json.dumps(value, ensure_ascii=False).encode("utf-16", "surrogatepass")
                      .decode("utf-16", "replace"))


def message(event: dict) -> dict:
    """A stream event's message -- {} where it has none that is an object: Claude Code's system
    and error events carry text there."""
    found = event.get("message")
    return found if isinstance(found, dict) else {}


def content(event: dict) -> list:
    """The content blocks of a stream event's message; [] where they are no list."""
    found = message(event).get("content")
    return found if isinstance(found, list) else []


def actions(event: dict, root: Path, tools: bool = True) -> list[str]:
    """What an assistant event did, one short line per tool call or text --
    the text only with tools=False."""
    if event.get("type") != "assistant":
        return []
    lines = []
    for block in content(event):
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
    found: dict[str, Any] = {"session": None, "result": None, "rate_limit": None, "model": None}
    for event in stream:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            found["session"] = event.get("session_id")
            found["model"] = event.get("model")     # the run's model (a helper model may answer parts of it)
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


def quota_limit(rate_limit: Any, limit: float, now: float) -> Optional[dict]:
    """The budget limit that keeps a run from starting on the last known subscription state, or None: a window
    (five_hour, seven_day -- every one the stream reports) at or past `limit`, or a subscription that refuses
    requests, until it resets. It is a lower bound: the user's own sessions have used it since. No state known
    (no run has reported one yet): None -- nothing to go by is no reason to refuse.

    The answer's fields: window (its name, or the rateLimitType of a refusal), utilization (a window's, 0-1),
    threshold (`limit`), status (a refusal's), resets_at (epoch seconds) and resets (local time, as a person
    reads it)."""
    if not isinstance(rate_limit, dict):
        return None
    found = rate_limit.get("unifiedWindows")
    for name, w in (found if isinstance(found, dict) else {}).items():
        if not isinstance(w, dict):
            continue
        used, resets = w.get("utilization"), w.get("resetsAt")
        if isinstance(used, (int, float)) and used >= limit and isinstance(resets, (int, float)) and resets > now:
            return {"window": str(name), "utilization": float(used), "threshold": limit, "resets_at": resets,
                    "resets": _when(resets)}
    resets = rate_limit.get("resetsAt")
    if not str(rate_limit.get("status") or "allowed").startswith("allowed") \
            and isinstance(resets, (int, float)) and resets > now:
        return {"window": str(rate_limit.get("rateLimitType") or "subscription"), "status": rate_limit.get("status"),
                "threshold": limit, "resets_at": resets, "resets": _when(resets)}
    return None


def limit_text(limit: dict) -> str:
    """A budget limit (quota_limit, usage_limit) in words."""
    if limit.get("status"):
        return f"the subscription refuses requests ({limit['status']}) until {limit.get('resets') or 'unknown'}"
    if "utilization" in limit:
        return (f"the subscription's {limit['window']} window is at {limit['utilization']:.0%}, the limit for runs "
                f"is {limit['threshold']:.0%}; it resets {limit.get('resets') or 'at an unknown time'}")
    return (f"the subscription's {limit.get('window')} limit is reached -- it resets "
            f"{limit.get('resets') or 'at an unknown time'}")


def quota_block(rate_limit: Any, limit: float, now: float) -> str:
    """Why no run may start on the last known subscription state (quota_limit), or ""."""
    found = quota_limit(rate_limit, limit, now)
    return limit_text(found) if found else ""


# The smallest request there is, for the subscription's state now: Claude Code reports it before the answer
# (measured on 2.1.285, 2026-10-14: a rate_limit_event with both windows, 2.3 s on haiku, no tool, one turn).
PROBE_TASK = "Reply with: ok"


def probe_command(exe: list[str], mcp_config: Path) -> list[str]:
    return [*exe, "-p", "--output-format", "stream-json", "--verbose", "--restricted", "--strict-mcp-config",
            "--mcp-config", str(mcp_config), "--tools", "", "--model", "haiku", "--max-turns", "1",
            "--no-session-persistence"]


def probe_quota(exe: list[str], mcp_config: Path, cwd: Path, env: dict, timeout: float) -> Optional[dict]:
    """The subscription's state as Claude Code reports it now (a rate_limit_event's rate_limit_info), asked under
    the login the runs use; None when the answer says none."""
    options: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        done = subprocess.run(probe_command(exe, mcp_config), input=PROBE_TASK.encode(), capture_output=True,
                              cwd=cwd, env=env, timeout=timeout, **options)
    except (OSError, subprocess.SubprocessError):
        return None
    events = []
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return outcome(events)["rate_limit"]


# What a run the subscription stopped midway ends with (measured on Claude Code 2.1.295, 2026-10-09, 7 runs): its
# result is_error with api_error "usage_limit_reached" (HTTP 429), its text "You've hit your session limit · resets
# 1:10am (Europe/Berlin)", and the stream's last rate_limit_event "rejected" with the window and resetsAt.
USAGE_LIMIT_ERRORS = ("usage_limit_reached",)


def usage_limit(result: Any, rate_limit: Any) -> Optional[dict]:
    """The budget limit a run ended on, or None: its result says so (api_error, USAGE_LIMIT_ERRORS) or the stream's
    last rate_limit_event refuses (status "rejected"). Fields as quota_limit's, the reset from the event."""
    said = isinstance(result, dict) and result.get("is_error") and result.get("api_error") in USAGE_LIMIT_ERRORS
    info = rate_limit if isinstance(rate_limit, dict) else {}
    refused = str(info.get("status") or "allowed") == "rejected"
    if not (said or refused):
        return None
    resets = info.get("resetsAt")
    resets = resets if isinstance(resets, (int, float)) else None
    return {"window": str(info.get("rateLimitType") or "subscription"),
            "api_error": result.get("api_error") if isinstance(result, dict) else None,
            "resets_at": resets, "resets": _when(resets) if resets else ""}


def _when(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def git(cwd: Path, *args: str, timeout: float = 120, pinned: Sequence[str] = (), stdin: Optional[str] = None,
        ok: tuple[int, ...] = (0,)) -> str:
    try:
        done = _proctree.run_bounded(["git", *pinned, *args], cwd=cwd, timeout=timeout, input=stdin or "")
    except OSError as exc:
        raise GitError(f"git {args[0]}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills git alone and then waits for its pipes: a filter
        # or hook git started holds them open, and the timeout bounded nothing.
        raise GitError(f"git {args[0]}: timed out after {timeout:g} s") from exc
    proc, stdout, stderr = done, done.stdout, done.stderr
    if proc.returncode not in ok:
        raise GitError(f"git {args[0]}: {(stderr or stdout).strip()[:300]}")
    return stdout


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


def seed_worktree(repo: Path, path: Path, folders: Iterable[str]) -> None:
    """Untracked folders of the repo copied into a fresh worktree (it holds only what is committed): a
    caller's files for the run to read, set by code only (files a caller hands the run), never by the
    config. The caller keeps them out of git (info/exclude): commit_all's add would take them along."""
    for rel in folders:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ValueError(f"seed {rel!r}: a folder inside the repository")
        source = repo / rel
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, path / rel, dirs_exist_ok=True)


def remove_worktree(repo: Path, path: Path, branch: str) -> None:
    """Takes back what make_worktree made for a run that never started, as far as it got. Only this
    worktree's entry -- `remove --force` drops it even when the folder is gone (measured, git 2.36): a
    repo-wide prune would drop the entries of the owner's own worktrees whose folder is missing just now
    (an unmounted drive)."""
    for args in (("worktree", "remove", "--force", str(path)), ("branch", "-D", branch)):
        try:
            git(repo, *args)
        except GitError:
            pass


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
