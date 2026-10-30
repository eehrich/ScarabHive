"""coding_cli tool server: Claude Code as a coding tool for ScarabHive agents.

A run is one headless Claude Code process in a fresh git worktree of an
operator-listed repository:

* locked down: --restricted, no MCP servers but those the operator names per
  workdir, an explicit tool list, file tools confined to the worktree, a shell
  only for the operator's commands (run.py),
* on the operator's subscription: only listed users may start runs, and none
  starts while a subscription window is past the limit,
* its result is a branch: what the run changed is committed there, nothing is
  merged or pushed.

The process runs detached and streams into a file, so a run is on disk, not
in a process. Its owner -- the plugin instance that started it -- watches it
(progress, time limit, end), finalizes it and rings the session that asked to
be woken. Only when the owner is gone (a restart, a one-shot agent-cli run)
does another instance take the run over, found by its periodic sweep.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from agent_system.core.session_presence import alive, presence_for, wake_blocked, wake_depth, wake_session
from agent_system.paths import PROJECT_ROOT, data_path
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.servers.agent.components.tool_call_contract import FRAMEWORK_REQUEST_ID_KEYS
from plugins.mcp_client.auth import build_auth_headers
# The transport names the MCP client dials by ("streaming" is HTTP, "http+sse" SSE); stdio and a typo are not
# among them.
from plugins.mcp_client.connection import _SSE_ALIASES, _STREAMABLE_HTTP_ALIASES

from . import run as cli
from .live import LiveRun

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

# Runs, their streams and the worktrees: DATA_ROOT (tests set a tmp dir), else
# $CODING_CLI_DATA_ROOT, else data/coding_cli. Every started instance sweeps
# its root -- adopts the runs whose owner is gone, rings their wakes -- so a
# second process on the machine (an app a test starts) needs a root of its
# own, or it takes over the operator's runs and uses up their wakes.
DATA_ROOT: Optional[Path] = None
DATA_ROOT_ENV = "CODING_CLI_DATA_ROOT"
MAX_TASK_CHARS = 20_000
#: The most max_task_chars may be. A task goes in on stdin, so no command line bounds it; the real bound is
#: the model's context window (some 3.5 characters a token in prose, so 1M characters is about 290k tokens).
#: The cap only catches a runaway task; an instance sets its own limit below it.
MAX_TASK_CHARS_CAP = 1_000_000
# A json_schema as it stands on the command line, Windows quoting included (an
# escaped quote counts up to four times): the rest of the line stays far below
# Windows' 32,767 characters.
MAX_SCHEMA_CHARS = 20_000
CAP_RESULT = 12_000
MAX_RESULT_CHARS_CAP = 200_000
CAP_STDERR = 600
CAP_HIDDEN = 50
LAST_ACTIONS = 12
POLL_S = 1.0
# How long a CLI may stay silent after its result event (counted from its last event) before it is ended: the
# answer is on disk. agy 1.4.10-1.4.11 (2026-10-29/30): 75 of 257 new conversations did not exit after their
# result, no resumed one (0 of 227); 74 of the 75 had no title stored, against 5 of 182 that exited -- it seems
# to wait at its exit for a title that never came. The rest exited within 1.4 s (99 %), at most 12.5 s; Claude
# Code's 34 runs within 1.5 s.
RESULT_GRACE_S = 20.0
# Finalizing takes a few git calls; a claim this old belongs to a dead process.
STALE_CLAIM_S = 300
# How long a watcher that lost the claim waits for the winner's end.
CLAIM_WAIT_S = 30
# A ring lease is written right after it is created; one still unreadable this
# long after was left by a process that died in between.
STALE_LEASE_S = 10
# How often a started instance looks for runs whose owner is gone and for rings that were lost.
SWEEP_S = 30
SAVE_ATTEMPTS = 40
# Only a finished run writes quota.json, and a refused one never finishes: a window that reset early kept refusing
# until its old reset (measured: the file far above what the subscription reported, the reset moved). A state that
# would refuse and is older than this is asked anew first (cli.probe_quota), at most once in that time per
# instance and process.
QUOTA_FRESH_S = 300
PROBE_TIMEOUT_S = 60
# Claude Code's --effort levels; agy takes the same (run.py, build_agy_command).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLIS = ("claude", "antigravity")
# Run ids are made here; anything else the model passes never becomes a path.
_RUN_ID = re.compile(r"[0-9a-f]{12}")
_SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# ScarabHive session ids name read marks.
_SESSION_KEY = re.compile(r"[A-Za-z0-9_-]{1,80}")
FINAL_STATES = ("done", "failed", "cancelled")


class _NoStatus:
    """Stand-in when a handler is called without the framework's status scope."""

    async def progress(self, message, meta=None):
        pass

    async def end(self, message, meta=None):
        pass

    async def error(self, message, meta=None):
        pass


def _untrusted(content: Any) -> dict:
    """Claude Code's own words: it read files to write them, so they are data
    for the model, never instructions."""
    return {"untrusted": True, "content": content}


# run id -> the token of its gateway grant, for the revoke at its end. In memory, as the grants are (the gateway
# serves in this process only): a run another process finalizes has its grant end by its time limit.
_GRANTS: dict[str, str] = {}
# What a grant holds beyond the run's limit: the run's end (commit, finalize) still reads nothing through it.
GRANT_SLACK_S = 300.0


def _revoke(run_id: str) -> None:
    token = _GRANTS.pop(run_id, None)
    if token:
        from plugins.mcp_gateway import grants
        grants.revoke(token)


def _gateway(entry: Any) -> tuple[Optional[dict], str]:
    """A workdir's `gateway:` (the operator's): which tools of this installation its runs reach through an MCP
    gateway grant -- or why it cannot be one."""
    if entry is None:
        return None, ""
    if not isinstance(entry, dict):
        return None, "gateway: {tools: [...], ...}"
    unknown = set(entry) - {"tools", "pin", "allowed", "read_only", "confined", "instance"}
    if unknown:
        # A misspelt `allowed` or `pin` would leave the grant wider than meant.
        return None, (f"gateway: unknown key(s) {sorted(unknown)} -- tools, pin, allowed, read_only, confined, "
                      "instance")
    tools, pin, allowed = entry.get("tools"), entry.get("pin") or {}, entry.get("allowed") or {}
    instance = entry.get("instance") or "mcp_gateway"
    if not isinstance(tools, list) or not tools or not all(isinstance(t, str) and t for t in tools):
        return None, "gateway.tools: a list of tool name patterns"
    if not isinstance(pin, dict) or not all(
            isinstance(pattern, str) and pattern and isinstance(keys, list) and keys
            and all(isinstance(k, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", k)
                    and k not in FRAMEWORK_REQUEST_ID_KEYS for k in keys)
            for pattern, keys in pin.items()):
        return None, "gateway.pin: {tool pattern: [the parameters a run must fix]} (run_task pin)"
    confined = entry.get("confined") is True
    if confined and not pin:
        return None, "gateway.confined: needs a pin -- the bindings are what holds the writes to the run"
    if not isinstance(allowed, dict) or not all(
            isinstance(pattern, str) and pattern and isinstance(rules, dict) and rules
            and all(isinstance(path, str) and path and isinstance(values, list) and values
                    for path, values in rules.items())
            for pattern, rules in allowed.items()):
        return None, "gateway.allowed: {tool pattern: {path: [values]}}"
    from plugins.mcp_gateway.grants import _check_path
    try:
        for rules in allowed.values():
            for path in rules:
                _check_path(path)       # the gateway's own check, which would refuse every run
    except ValueError as exc:
        return None, f"gateway.{exc}"
    if not isinstance(instance, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", instance):
        return None, "gateway.instance: a plugin instance name"
    return {"instance": instance, "tools": tools, "pin": {p: sorted(set(k)) for p, k in pin.items()},
            "allowed": allowed, "read_only": entry.get("read_only") is True, "confined": confined}, ""


def _pin_problem(pin: Any, required: dict[str, list[str]]) -> str:
    """Why ``pin`` (a run's tool bindings, {tool pattern: {parameter: value}}) does not do for a gateway that
    requires ``required`` ({tool pattern: [parameters]}), or "". More than required only narrows the run further;
    the gateway checks the rest of the form when it issues the grant."""
    if not isinstance(pin, dict) or not all(
            isinstance(pattern, str) and pattern and isinstance(values, dict) and values
            and all(isinstance(k, str) and isinstance(v, (str, int, float)) for k, v in values.items())
            for pattern, values in pin.items()):
        return "pin: {tool pattern: {parameter: value}} -- values text, number or true/false"
    missing = [f"{pattern}: {key}" for pattern, keys in required.items() for key in keys
               if key not in (pin.get(pattern) or {})]
    return f"pin: this workdir's runs must fix {', '.join(missing)}" if missing else ""


def _transport(cfg: Any) -> str:
    return str(getattr(cfg, "transport", None) or "streaming").lower()


def _usable_mcp(name: str, cfg: Any) -> bool:
    """An operator's MCP server a run may load: one Claude Code dials, over a
    transport the MCP client knows. A stdio server would be started instead,
    its command and env written to disk; a misspelt transport the client
    refuses. The name goes into --allowedTools, a comma or space there would
    allow more. The url goes into the run's MCP file as it is: one carrying a
    user, a password or a query (a key) would put it on disk -- Claude Code's
    docs expand ${VAR} in a url too, unmeasured, so such a server is refused."""
    url = str(getattr(cfg, "url", "") or "")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)) and cfg is not None
            and bool(getattr(cfg, "enabled", False) and url) and not (parts.username or parts.password or parts.query)
            and _transport(cfg) in _STREAMABLE_HTTP_ALIASES | _SSE_ALIASES)


def _schema_ok(schema: Any) -> bool:
    try:
        return (isinstance(schema, dict) and cli.nesting(schema) <= cli.MAX_NESTING
                and len(subprocess.list2cmdline([json.dumps(schema, allow_nan=False)])) <= MAX_SCHEMA_CHARS)
    except (TypeError, ValueError):
        return False


def _bounded(value: Any, default: float, low: float, high: float) -> Optional[float]:
    try:
        return max(low, min(float(value if value not in (None, "") else default), high))
    except (TypeError, ValueError, OverflowError):
        return None


def _utilization_ok(value: Any) -> bool:
    """A threshold for the subscription's windows a call may ask for: a share above 0, at most 1 (100 %)."""
    return not isinstance(value, bool) and isinstance(value, (int, float)) and 0 < value <= 1


def _claim(path: Path, content: str = "") -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, content.encode("utf-8"))
    except BaseException:
        # An empty claim would hold everybody off for good.
        os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    os.close(fd)
    return True


def _instance_alive(owner: Any) -> bool:
    """Whether the process of a plugin instance, named by {pid, started,
    instance}, still runs. A stopped instance says so itself (stop_plugin)."""
    return isinstance(owner, dict) and alive(owner.get("pid"), owner.get("started"))


def _no_claude(command: str, label: str = "Claude Code") -> str:
    """Why run_task finds no CLI: a name looked up on PATH, or a path that holds none."""
    if os.path.dirname(command):
        return f"{label} is not installed here: no executable at {command!r} (coding_cli command)"
    return (f"{label} is not installed here: {command!r} is not on this process's PATH (coding_cli command: "
            "its full path)")


def _replace(path: Path, text: str) -> None:
    """`text` as the file at `path`, put in place in one step."""
    tmp = path.with_name(f"{path.stem}.{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(SAVE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows refuses the replace while another process reads the file.
            if attempt == SAVE_ATTEMPTS - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


class CodingCliServer(SchemaBasedToolServer):
    """Claude Code runs. Offered only when the executable and a workdir are there."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        super().__init__(name, system_config, server_config)
        # Which CLI runs: Claude Code, or Google's Antigravity CLI (agy, run.build_agy_command).
        self.cli = str(getattr(server_config, "cli", "") or "claude")
        self.agy = self.cli == "antigravity"
        self.label = "Antigravity" if self.agy else "Claude Code"
        self._command_name = str(getattr(server_config, "command", "") or ("agy" if self.agy else "claude"))
        # An unknown cli offers nothing: a typo must not spend the other subscription.
        self.command = self._find(self._command_name) if self.cli in CLIS else None
        if self.cli not in CLIS:
            logger.warning("coding_cli: %s: cli %r is none of %s -- no tools", name, self.cli, ", ".join(CLIS))
        remote = getattr(getattr(system_config, "external_servers", None), "remote_servers", None) or {}
        self.workdirs: dict[str, dict] = {}
        for wname, entry in (getattr(server_config, "workdirs", None) or {}).items():
            entry = entry if isinstance(entry, dict) else {"path": entry}
            path = Path(str(entry.get("path") or ""))
            path = (path if path.is_absolute() else PROJECT_ROOT / path).resolve()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(wname)) or not (path / ".git").exists():
                logger.warning("coding_cli: workdir %r skipped -- a plain name and a git repository are needed", wname)
                continue
            names = entry.get("mcp_servers") or []
            # `mcp_servers: remote_mcp` is one server, not ten letters.
            names = [str(n) for n in ([names] if isinstance(names, str) else names)]
            gateway, problem = _gateway(entry.get("gateway"))
            if problem or cli.GATEWAY_SERVER in names:
                logger.warning("coding_cli: workdir %r skipped -- %s", wname,
                               problem or f"mcp_servers names {cli.GATEWAY_SERVER!r}, the gateway's server")
                continue
            if gateway and entry.get("web") is True and not (gateway["read_only"] or gateway["confined"]):
                # As with mcp_servers below: a page could steer what the tools write. The operator vouches that
                # the tools and allowed values only read -- or that the run's bindings hold every write to what
                # the run writes anyway (its own file), where a page could steer no more than the answer.
                logger.warning("coding_cli: workdir %r skipped -- web and a gateway neither read_only nor "
                               "confined: a page the run reads could steer its tools", wname)
                continue
            if self.agy and (names or entry.get("web") is not True):
                # run.py, build_agy_command: it searches the web in every run, and loads no server of ours.
                logger.warning("coding_cli: workdir %r skipped -- an Antigravity run searches the web whatever it "
                               "is told and takes no mcp_servers: its workdirs say web: true and name none", wname)
                continue
            if names and entry.get("web") is True:
                # A page can tell the model what to do; a server's tools would do it with its token.
                logger.warning("coding_cli: workdir %r skipped -- web and mcp_servers together: a page the run "
                               "reads could steer the servers' tools; give research a workdir of its own", wname)
                continue
            unusable = [n for n in names if not _usable_mcp(n, remote.get(n))]
            if unusable:
                logger.warning("coding_cli: workdir %r skipped -- MCP server(s) %s unknown, disabled, stdio, without "
                               "a url or not a plain name (external_servers.remote_servers)", wname, unusable)
                continue
            self.workdirs[str(wname)] = {"path": path, "exclude": [str(e) for e in entry.get("exclude") or []],
                                         "mcp_servers": {n: remote[n] for n in names}, "web": entry.get("web") is True,
                                         "gateway": gateway}
        # The gateway serves under this process's API (its port as agent_system.app.run takes it).
        network = getattr(system_config, "network", None)
        self.gateway_port = int(os.getenv("PORT") or getattr(network, "port", 0) or 8000)
        host = str(os.getenv("HOST") or getattr(network, "host", "") or "")
        # A wildcard bind is reached on loopback; an IPv6 address goes in brackets.
        host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
        self.gateway_host = f"[{host}]" if ":" in host else host
        # The subscription belongs to one person (concept §7).
        users = getattr(server_config, "allowed_users", None) or ()
        # `allowed_users: admin` is one user, not five letters.
        self.allowed_users = frozenset([users] if isinstance(users, str) else map(str, users))
        self.allowed_commands = [str(c) for c in getattr(server_config, "allowed_commands", None) or ()]
        # A web workdir's runs keep their shell only when the operator says so: a page could steer it.
        self.web_with_shell = getattr(server_config, "web_with_shell", False) is True
        if self.agy and self.allowed_commands:
            logger.warning("coding_cli: %s: allowed_commands unused -- an Antigravity run has no shell, headless it "
                           "refuses every command", name)
        if self.allowed_commands and any(w["mcp_servers"] or w["gateway"] for w in self.workdirs.values()):
            logger.warning("coding_cli: %s allows shell commands and gives runs MCP servers -- every allowed "
                           "command inherits the environment that holds the servers' tokens", name)
        self.pass_env = [str(v) for v in getattr(server_config, "pass_env", None) or ()]
        # Values this instance's runs get (e.g. a CLI's output limits): set before the plugin's own, which win.
        env = getattr(server_config, "env", None) or {}
        env = env if isinstance(env, dict) else {}
        # A value with NUL would fail the launch after the record says running; an empty YAML value is no value.
        self.env = {str(k): str(v) for k, v in env.items()
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)) and v is not None and "\0" not in str(v)}
        if len(self.env) != len(env) or not isinstance(getattr(server_config, "env", None) or {}, dict):
            logger.warning("coding_cli: %s: env takes {NAME: value} with plain names and values -- the others are "
                           "left out", name)
        if not self.agy:
            unwritable = cli.claude_login_unwritable({**cli.child_env(self.pass_env), **self.env})
            if unwritable:
                logger.warning("coding_cli: %s: Claude Code cannot write its login files in this process (%s) -- "
                               "without its folder it cannot renew its token, and every run fails once it expires "
                               "(8 h): make them writable for the service", name, ", ".join(unwritable))
        self.model = str(getattr(server_config, "model", "") or "")
        # --restricted leaves the user's settings unread, their effortLevel too: the instance names its own.
        self.effort = str(getattr(server_config, "effort", "") or "")
        if self.effort and self.effort not in EFFORTS:
            logger.warning("coding_cli: %s: effort %r is none of %s -- %s's default instead",
                           name, self.effort, ", ".join(EFFORTS), self.label)
            self.effort = ""
        if self.agy and self.effort and self.model.endswith(tuple(f"-{level}" for level in EFFORTS)):
            # agy refuses every run whose model id names a level and an --effort beside it (run.py).
            logger.warning("coding_cli: %s: effort %r dropped -- model %r names its level already; an effort goes "
                           "with a model id without one", name, self.effort, self.model)
            self.effort = ""
        self.max_utilization = _bounded(getattr(server_config, "max_window_utilization", None), 0.8, 0.05, 1.0) or 0.8
        self.wait_s = _bounded(getattr(server_config, "wait_s", None), 300, 0, 3600)
        self.wait_s = 300.0 if self.wait_s is None else self.wait_s
        self.max_run_s = 60 * (_bounded(getattr(server_config, "max_run_minutes", None), 60, 1, 7 * 24 * 60) or 60)
        self.max_parallel = int(_bounded(getattr(server_config, "max_parallel", None), 1, 1, 8) or 1)
        self.max_task_chars = int(_bounded(getattr(server_config, "max_task_chars", None), MAX_TASK_CHARS, 1_000,
                                           MAX_TASK_CHARS_CAP) or MAX_TASK_CHARS)
        self.max_output_chars = int(_bounded(getattr(server_config, "max_output_chars", None), CAP_RESULT, 1_000,
                                             100_000) or CAP_RESULT)
        # The final report's bound: a caller that takes it as the answer itself (an LLM provider) needs more.
        self.max_result_chars = int(_bounded(getattr(server_config, "max_result_chars", None), CAP_RESULT, 1_000,
                                             MAX_RESULT_CHARS_CAP) or CAP_RESULT)
        self._monitors: dict[str, asyncio.Task] = {}
        self._listeners: dict[str, Any] = {}
        # run id -> its live view (live.py), for the life of the run, not only
        # while the call that started it waits.
        self._live: dict[str, LiveRun] = {}
        self._rings: dict[str, asyncio.Task] = {}
        self._starting: set[str] = set()
        self._sweeper: Optional[asyncio.Task] = None
        self._stopped = False
        self._start_lock = asyncio.Lock()
        self._probe_lock = asyncio.Lock()
        self._probed_at = 0.0
        self._instance = secrets.token_hex(6)
        self._me = {"pid": os.getpid(), "started": cli.process_start(os.getpid()), "instance": self._instance}

        if self.command is None:
            logger.info("coding_cli: no %s executable found -- %s offers no tools", self.label, name)
        elif not self.workdirs:
            # info, like no executable: an instance whose workdirs a user registers later starts this way
            logger.info("coding_cli: no usable workdir -- %s offers no tools", name)

    def _find(self, command: str) -> Optional[list[str]]:
        """The CLI's executable, looked up at each call: one installed after the start is found by the next run."""
        return (cli.find_agy if self.agy else cli.find_claude)(command)

    def get_template_vars(self) -> dict[str, Any]:
        template_vars = super().get_template_vars()
        template_vars["configured"] = bool(self.command and self.workdirs)
        # windows: Claude Code reports the subscription's, agy none -- no quota tool, no threshold to ask.
        template_vars["cli_name"], template_vars["windows"] = self.label, not self.agy
        template_vars["workdirs"] = sorted(self.workdirs)
        # pins: a workdir's gateway fixes values -- only then run_task offers pin.
        template_vars["pins"] = any(w["gateway"] and w["gateway"]["pin"] for w in self.workdirs.values())
        return template_vars

    async def start_plugin(self) -> None:
        """Takes over the runs whose owner is gone, now and every SWEEP_S:
        without it their time limit, their end and their ring would wait for
        somebody to look. A plugin stopped before starts again (the registry
        keeps it startable): its calls hold their runs again."""
        self._stopped = False
        await self._sweep_guarded()
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def stop_plugin(self) -> None:
        """Ends the watching, not the runs: they finish on their own, and
        another instance takes them over -- its process may live on, so the
        runs are released on disk. A run whose starting call still waits is
        stopped (its caller has no run id yet) and that call answers how it
        ended; a run whose call still answers is released by that call
        (run_task). A ring not delivered to the end gives its lease
        up, so it is rung again."""
        watched = list(self._monitors)
        tasks = [*self._monitors.values(), *self._rings.values(), *([self._sweeper] if self._sweeper else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._stopped = True
        for run_id in watched:
            # A run whose starting call still answers for it is released by that call (run_task).
            if run_id not in self._starting:
                self._release(run_id)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_S)
            await self._sweep_guarded()

    async def _sweep_guarded(self) -> None:
        try:
            await self._sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad record must not end the sweeping
            logger.exception("coding_cli: sweeping the runs failed")

    async def _sweep(self) -> None:
        # Every record ever made is read: off the loop.
        for record in await asyncio.to_thread(self._records):
            if record.get("state") == "running":
                if not self._owner_alive(record):
                    await self._adopt(record)
            elif self._file(record["run_id"], "wake").exists():
                self._ring(record["run_id"])

    async def _adopt(self, record: dict) -> None:
        """Becomes the owner of an orphaned run -- one instance per dead owner."""
        run_id = record["run_id"]
        gone = (record.get("owner") or {}).get("instance") or "none"
        # The claim settles who takes over a foreign owner's run. A run of its
        # own that nothing watches (its finalize failed) is this instance's
        # already: a claim here would be used up, and the next sweep skip it --
        # unless it released the run (a plugin stopped and started again): then
        # the others may take it too, and the claim settles it as for theirs.
        # A release names its own claim (its nonce): a run released, taken back
        # and released again is up for taking again.
        released = self._file(run_id, f"released-{gone}")
        try:
            nonce = released.read_text(encoding="utf-8").strip()
        except OSError:
            nonce = None
        claim = f"adopt-{gone}" + (f"-{nonce}" if nonce else "")
        if nonce is None and gone != self._instance and _instance_alive(record.get("owner")):
            return                                # its release was taken back since the sweep looked
        if (gone != self._instance or nonce is not None) and not _claim(self._file(run_id, claim)):
            return
        record = self._load(run_id) or record
        if record.get("state") != "running" or ((record.get("owner") or {}).get("instance") or "none") != gone:
            return                                # ended, or somebody took it meanwhile
        record["owner"] = self._me
        self._save(record)
        for stale in (self._root() / "runs").glob(f"{run_id}.released-*"):
            stale.unlink(missing_ok=True)         # it has an owner again: no release of an earlier one counts
        if alive(record.get("pid"), record.get("pid_started")) and not self._orphan_stop(record):
            self._watch(record, None)
        else:
            await self._settle(run_id)

    def _orphan_stop(self, record: dict) -> str:
        """Why a running run whose owner is gone is stopped now, or "" while it
        may go on. One whose starting call never answered goes like one whose
        turn was stopped: its caller got no run id and went on without it -- a
        stategraph hands the job to an agent on the same Scarab server. A
        record from before the answer was marked keeps the old rule."""
        if "instance_name" in record and not self._file(record["run_id"], "answered").exists():
            return "stopped: the call that started it never answered, its process ended"
        if time.time() - float(record.get("started_at") or 0) > self._limit_s(record):
            return f"stopped after the time limit of {self._limit_s(record) / 60:.0f} min"
        return ""

    def _limit_s(self, record: dict) -> float:
        """The run's time limit: its starting instance's, whoever takes it over."""
        try:
            return float(record.get("max_run_s") or self.max_run_s)
        except (TypeError, ValueError):
            return self.max_run_s

    def _release(self, run_id: str) -> None:
        """Lets the run go: whoever sweeps may take it over, through the claim this release names."""
        self._file(run_id, f"released-{self._instance}").write_text(secrets.token_hex(4), encoding="utf-8")

    def _owner_alive(self, record: dict) -> bool:
        """Whether somebody watches the run. This instance does while it
        prepares or monitors it."""
        owner = record.get("owner") or {}
        if owner.get("instance") == self._instance:
            return record.get("run_id") in self._monitors or record.get("run_id") in self._starting
        released = self._file(str(record.get("run_id")), f"released-{owner.get('instance')}").exists()
        return not released and _instance_alive(owner)

    # ── paths ──

    def _root(self) -> Path:
        if DATA_ROOT is not None:
            return DATA_ROOT
        configured = os.environ.get(DATA_ROOT_ENV, "").strip()
        if configured:
            # Relative like the workdirs: to the project, not to wherever the process runs.
            path = Path(os.path.expanduser(configured))
            return path if path.is_absolute() else PROJECT_ROOT / path
        return PROJECT_ROOT / data_path("coding_cli")

    def _file(self, run_id: str, suffix: str) -> Path:
        return self._root() / "runs" / f"{run_id}.{suffix}"

    def _load(self, run_id: str) -> Optional[dict]:
        path = self._file(run_id, "json")
        for attempt in range(SAVE_ATTEMPTS):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                return record if isinstance(record, dict) else None
            except PermissionError:
                # Windows refuses the read while another task replaces the file.
                if attempt == SAVE_ATTEMPTS - 1:
                    return None
                time.sleep(0.05)
            except (OSError, ValueError):
                return None
        return None

    def _save(self, record: dict) -> None:
        # ASCII: a lone surrogate from the stream (a broken escape in a result) is escaped, not a failed save.
        _replace(self._file(record["run_id"], "json"), json.dumps(record, indent=1))

    def _store_quota(self, state: dict) -> None:
        # Whole or not at all: a reader that caught it half written read no state, and no state refuses nothing.
        try:
            _replace(self._root() / "quota.json", json.dumps(state))
        except OSError as exc:
            # A hint for the next start, not the run's outcome: a run that finished stays finished, a probe's
            # answer still answers (Windows refuses the replace while a reader holds the file).
            logger.warning("coding_cli: %s: the subscription's state not stored: %s", self.name, exc)

    def _records(self) -> list[dict]:
        folder = self._root() / "runs"
        records = [self._load(p.stem) for p in folder.glob("*.json")] if folder.is_dir() else []
        return [r for r in records if r and _RUN_ID.fullmatch(str(r.get("run_id")))]

    def _mine(self, record: dict) -> bool:
        """Whether a run is this instance's: every instance of the plugin shares
        the data folder, but its runs, its max_parallel and its tools are its own.
        A record from before the name was kept counts for every instance. The
        subscription guard and the taking over of orphans stay shared."""
        return record.get("instance_name", self.name) == self.name

    def _own(self, run_id: Any, user_id: str) -> Optional[dict]:
        """The user's run of this instance, or None -- another user's run is no run to them."""
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            return None
        record = self._load(run_id)
        return record if record and record.get("user_id") == user_id and self._mine(record) else None

    async def _running(self) -> list[dict]:
        """The runs going now, of every instance, and those another process is
        starting. One nobody watches is settled first: it may have ended, or
        be past its time limit."""
        found = []
        for record in await asyncio.to_thread(self._records):
            if record.get("state") != "running":
                continue
            if not self._owner_alive(record):
                record = await self._settle(record["run_id"])
            if record.get("state") == "running" and (
                    alive(record.get("pid"), record.get("pid_started"))
                    or (not record.get("pid") and self._owner_alive(record))):
                found.append(record)
        return found

    def _quota(self) -> Any:
        try:
            return json.loads((self._root() / "quota.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _no_mcp(self) -> Path:
        mcp = self._root() / "no_mcp.json"
        if not mcp.exists():
            mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
        return mcp

    async def _quota_now(self, threshold: float, user_id: str) -> Any:
        """The last known state (_quota) -- asked anew when it would refuse a run at `threshold` and is older than
        QUOTA_FRESH_S, by a user who may run on the subscription (allowed_users). Without an answer the state on
        file stands. An Antigravity instance knows none: agy reports no window, and quota.json is Claude Code's."""
        if self.agy:
            return None
        state = self._quota()
        if (self.command is None or user_id not in self.allowed_users
                or not cli.quota_limit(state, threshold, time.time())):
            return state
        async with self._probe_lock:
            path = self._root() / "quota.json"
            try:
                written = path.stat().st_mtime
            except OSError:
                written = 0.0
            if time.time() - max(written, self._probed_at) < QUOTA_FRESH_S:
                return self._quota()
            self._probed_at = time.time()
            # Stored in the thread: a caller cancelled meanwhile does not take the answer along.
            found = await asyncio.to_thread(self._probe_and_store)
            if not isinstance(found, dict):
                logger.warning("coding_cli: %s: asking the subscription's state anew gave no answer -- the one on "
                               "file from %s stands", self.name, time.strftime("%Y-%m-%d %H:%M", time.localtime(written)))
                return self._quota()
            return found

    def _probe_and_store(self) -> Optional[dict]:
        # Under the environment the runs get: a login or proxy set in env is theirs.
        found = cli.probe_quota(self.command, self._no_mcp(), self._root(), {**cli.child_env(self.pass_env), **self.env},
                                PROBE_TIMEOUT_S)
        if isinstance(found, dict):
            self._store_quota(found)
        return found

    def _mark_read(self, run_id: str, session_id: str) -> None:
        if _SESSION_KEY.fullmatch(session_id or ""):
            self._file(run_id, f"read-{session_id}").touch()

    def _was_read(self, run_id: str, session_id: str) -> bool:
        return self._file(run_id, f"read-{session_id}").exists()

    # ── tools ──

    async def _fail(self, status, message: str, **extra: Any) -> dict:
        """An error answer; `extra` are fields beside it (limit: a budget limit, quota_limit's)."""
        await status.error(message[:140])
        return {"status": "error", "error": message, **extra}

    async def run_task(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        user_id, session_id = str(params.get("_user_id") or ""), str(params.get("_session_id") or "")
        if user_id not in self.allowed_users:
            return await self._fail(status, f"{self.label} runs on the operator's subscription: this user may not "
                                            "start runs (coding_cli allowed_users)")
        if self.command is None:
            # A machine calls the tool by name, offered or not; Claude Code installed after the start is found now.
            self.command = self._find(self._command_name) if self.cli in CLIS else None
            if self.command is None:
                return await self._fail(status, _no_claude(self._command_name, self.label) if self.cli in CLIS
                                        else f"cli {self.cli!r} is none of {', '.join(CLIS)} (coding_cli cli)")
        if not self.workdirs:
            # The same by-name call: every configured workdir was skipped at the start (the log says why).
            return await self._fail(status, "no usable workdir (coding_cli workdirs: a git repository each -- the "
                                            "log at the start says why)")
        task = params.get("task")
        if not isinstance(task, str) or not task.strip():
            return await self._fail(status, "task: the complete task as text")
        if len(task) > self.max_task_chars:
            return await self._fail(status, f"task: at most {self.max_task_chars} characters")
        try:
            # Before anything is made: a lone surrogate (a broken escape) fails the task file only after the worktree.
            task.encode("utf-8")
        except UnicodeEncodeError:
            return await self._fail(status, "task: not valid text (a broken character escape) -- send it again")
        mode = params.get("mode") or "edit"
        if mode not in ("edit", "plan"):
            return await self._fail(status, "mode: edit (change the code) or plan (read and answer with a plan)")
        wake = params.get("wake", True)
        if not isinstance(wake, bool):
            return await self._fail(status, "wake: true (woken at the end) or false (you wait with get_run)")
        limit = params.get("max_run_minutes")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float)) or not limit > 0):
            return await self._fail(status, f"max_run_minutes: a number of minutes above 0 -- a shorter limit for "
                                            f"this run; the instance's {self.max_run_s / 60:g} at most")
        utilization = params.get("max_window_utilization")
        if utilization is not None and not _utilization_ok(utilization):
            return await self._fail(status, f"max_window_utilization: a share above 0 and at most 1 -- a lower "
                                            f"threshold for this run; the instance's {self.max_utilization:g} at most")
        schema = params.get("json_schema")
        prior = None
        if params.get("resume"):
            prior = self._own(params["resume"], user_id)
            if prior is None:
                return await self._fail(status, f"resume: no run {str(params['resume'])[:20]} of yours")
            # Its process may be gone without anybody having looked.
            prior = await self._settle(prior["run_id"], session_id)
            if prior.get("state") not in FINAL_STATES:
                return await self._fail(status, f"resume: run {prior['run_id']} has not ended yet")
            if prior.get("cli", "claude") != self.cli:
                return await self._fail(status, f"resume: run {prior['run_id']} ran on {prior.get('cli', 'claude')}, "
                                                f"this instance runs {self.cli} -- start a new run")
            if not prior.get("claude_session") or not Path(prior["worktree"]).is_dir():
                return await self._fail(status, f"resume: run {prior['run_id']} left no conversation or "
                                                f"worktree to continue -- start a new run")
            workdir = prior["workdir"]
            if schema is None:
                schema = prior.get("json_schema")
            if limit is None and prior.get("asked_run_s"):
                # As the schema: the conversation goes on under the shorter limit its call asked for. One that
                # asked none takes the instance's as it is now.
                limit = float(prior["asked_run_s"]) / 60
            if utilization is None and prior.get("asked_utilization"):
                utilization = float(prior["asked_utilization"])     # and under the lower threshold, as asked
        else:
            workdir = params.get("workdir") or (next(iter(self.workdirs)) if len(self.workdirs) == 1 else None)
        if workdir not in self.workdirs:
            return await self._fail(status, f"workdir: one of {', '.join(sorted(self.workdirs))}")
        gateway, pin = self.workdirs[workdir]["gateway"], params.get("pin") or None
        if prior:
            # The conversation goes on with the values it was given: other values make another run.
            if pin is not None and pin != prior.get("pin"):
                return await self._fail(status, f"pin: a resume keeps run {prior['run_id']}'s -- "
                                                f"{json.dumps(prior.get('pin'))}")
            pin = prior.get("pin")
        if not gateway and pin:
            return await self._fail(status, f"pin: workdir {workdir} has no gateway -- leave it out")
        problem = _pin_problem(pin or {}, gateway["pin"]) if gateway else ""
        if problem:
            return await self._fail(status, f"resume: {problem}, which run {prior['run_id']} did not -- start a new "
                                            "run" if prior else problem)
        # As asked; _start holds it to the instance's, what its operator allows a run. None: the instance's.
        asked_run_s = 60 * limit if limit is not None else None
        # The given one or the one a resume inherits, before anything is made.
        if schema is not None and not _schema_ok(schema):
            return await self._fail(status, f"json_schema: a JSON schema as an object, at most {MAX_SCHEMA_CHARS} "
                                            f"characters as JSON on the command line, its quotes escaped, "
                                            f"nested at most {cli.MAX_NESTING} deep")
        # The call's threshold, lower only; else the instance's. A budget limit is said as a field too (limit), so a
        # caller tells it from every other refusal -- "N run(s) still going" is a wait, not the budget.
        threshold = min(utilization or 1.0, self.max_utilization)
        blocked = cli.quota_limit(await self._quota_now(threshold, user_id), threshold, time.time())
        if blocked:
            return await self._fail(status, f"not started: {cli.limit_text(blocked)}", limit=blocked)
        # Held until the process runs: a run being prepared is not on disk as running yet.
        async with self._start_lock:
            running = await self._running()
            # max_parallel counts this instance's runs; a worktree is busy whichever instance works in it -- two
            # instances may resume a record from before instances were told apart.
            mine = [r for r in running if self._mine(r)]
            if len(mine) >= self.max_parallel:
                return await self._fail(status, f"not started: {len(mine)} run(s) still going ("
                                                f"{', '.join(r['run_id'] for r in mine)}) -- wait for them or cancel one")
            if prior and any(r.get("worktree") == prior["worktree"] for r in running):
                return await self._fail(status, "resume: another run works in that worktree")
            run_id = secrets.token_hex(6)
            # Held until the call has answered: while it is in flight this instance answers for the run, its
            # watch dead or not -- a sweep would stop it as one whose call never answered (_orphan_stop).
            self._starting.add(run_id)
            started = False
            # A plain executor future, not a task: a sweep that cancels every
            # task at the loop's end does not reach it.
            prepare = asyncio.get_running_loop().run_in_executor(None, functools.partial(
                self._prepare, run_id, task, mode, workdir, prior, user_id, session_id, schema, asked_run_s,
                utilization, pin or None, str(params.get("_request_id") or run_id)))
            try:
                try:
                    record = await asyncio.shield(prepare)
                except asyncio.CancelledError:
                    # The thread goes on and may still start the process: wait
                    # for it through any further cancel, then stop what it started.
                    while not prepare.done():
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.shield(prepare)
                    if not prepare.cancelled() and prepare.exception() is None:
                        record = prepare.result()
                        self._watch(record, record.pop("_proc"))
                        await self._stop_run(record, "stopped with the turn that started it")
                    raise
                except (cli.GitError, OSError) as exc:
                    return await self._fail(status, f"not started: {exc}")
                monitor = self._watch(record, record.pop("_proc"))
                started = True
            finally:
                if not started:
                    self._starting.discard(run_id)
        try:
            return await self._answer(status, record, monitor, task, mode, params.get("_request_id"),
                                      session_id, user_id, wake)
        finally:
            self._starting.discard(run_id)
            if self._stopped:
                # The plugin stopped while this call answered for the run: released now, for whoever takes over.
                with contextlib.suppress(OSError):
                    self._release(run_id)

    async def _answer(self, status, record: dict, monitor: asyncio.Task, task: str, mode: str,
                      request_id: Any, session_id: str, user_id: str, wake: bool = True) -> dict[str, Any]:
        """run_task once the run is going: waits for its end up to wait_s, then answers in full or with the
        run id and a wake -- none when the caller waits with get_run itself (wake false: a state machine, whose
        session has nobody to wake)."""
        run_id = record["run_id"]
        live = await LiveRun.open(request_id, task, Path(record.get("worktree") or "."))
        if live is not None:
            live.label = self.label
        if live is not None:
            self._live[run_id] = live
        self._listeners[run_id] = status
        sub_agent = False
        try:
            try:
                await status.progress(f"run {run_id} started on {record['branch']} ({mode})")
                await asyncio.wait_for(asyncio.shield(monitor), timeout=self.wait_s)
            except asyncio.TimeoutError:
                pass
            finally:
                self._listeners.pop(run_id, None)
            if not self._ended(run_id):
                sub_agent = await self._is_sub_agent(session_id, user_id)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                # The turn was stopped before the call answered: the run goes with
                # it. Once the call has answered with a run id, the run is on its own.
                await self._stop_run(record, "stopped with the turn that started it")
                raise
            # No turn was stopped: the plugin was, and its watch with it. The run goes as well -- its caller has
            # no run id yet -- unless its process has ended: a late stop would make its own failure a cancel.
            # The call answers how it ended instead of passing the cancel on.
            if alive(record.get("pid"), record.get("pid_started")):
                await self._stop_run(record, "stopped with the plugin, while the call that started it waited")
            if await asyncio.to_thread(self._finalize, run_id) is None:
                # The cancelled watch's finalize still runs in its thread: wait for that end, as _monitor does.
                for _ in range(int(CLAIM_WAIT_S / 0.5)):
                    if self._ended(run_id):
                        break
                    await asyncio.sleep(0.5)
            if live is not None and self._ended(run_id):
                await live.close(self._load(run_id) or {})      # the cancelled watch did not
            if not self._ended(run_id):
                sub_agent = await self._is_sub_agent(session_id, user_id)
        # Only an end on disk is answered in full: a watch that ended early (a broken status channel, the plugin
        # stopped, a finalize still under way) leaves the run going -- it is answered as going, with its wake.
        if self._ended(run_id):
            return await self._finished(status, run_id, session_id)
        # No await from the look above to the armed wake: a monitor still
        # watching rings when the run ends, after the wake is there.
        answer = self._running_report(self._load(run_id) or record)
        answer.update(self._arm_wake(run_id, session_id, user_id, sub_agent) if wake else {
            "wake": False, "wake_note": f"no wake asked; {self.name}_get_run with wait_s waits for the end of run {run_id}"})
        try:
            await status.end(f"run {run_id} still going after {self.wait_s:.0f} s"
                             + (", wake armed" if answer["wake"] else ", no wake"))
        except BaseException as e:
            # Stopped before the answer went out, or the status channel broke: the caller never got
            # the run id. As while waiting, the run goes, and no wake rings for it.
            self._drop_wake(run_id)
            if alive(record.get("pid"), record.get("pid_started")):     # an ended run keeps its own end
                await self._stop_run(record, "stopped with the turn that started it" if isinstance(e, asyncio.CancelledError)
                                     else f"its answer could not be sent ({type(e).__name__})")
            raise
        # The caller gets the run id now: a process taking the run over lets it go on (_orphan_stop). Not
        # marked, the run is stopped should its owner die.
        with contextlib.suppress(OSError):
            self._file(run_id, "answered").touch()
        return answer

    def _ended(self, run_id: str) -> bool:
        return (self._load(run_id) or {}).get("state") in FINAL_STATES

    async def get_run(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        session_id = str(params.get("_session_id") or "")
        record = self._own(params.get("run_id"), str(params.get("_user_id") or ""))
        if record is None:
            return await self._fail(status, f"no run {str(params.get('run_id'))[:20]} of yours")
        run_id = record["run_id"]
        wait_s = _bounded(params.get("wait_s"), 0, 0, 600)
        if wait_s is None:
            return await self._fail(status, "wait_s: seconds, a number from 0 to 600")
        stop = params.get("stop_if_cancelled", False)
        if not isinstance(stop, bool):
            return await self._fail(status, "stop_if_cancelled: true or false")
        deadline = time.monotonic() + wait_s
        try:
            while True:
                record = await self._settle(run_id, session_id)
                if record.get("state") in FINAL_STATES or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(min(POLL_S * 2, max(0.0, deadline - time.monotonic())))
        except asyncio.CancelledError:
            # A caller that waits here for the run's whole length (a state machine, wake false) asks for run_task's
            # rule: its stop -- a terminate, a timeout, the hive shutting down its runs -- stops the run, which would
            # go on building where its slot is given to the next. The cancel stays what is raised.
            if stop and asyncio.current_task().cancelling():
                record = self._load(run_id) or record
                if record.get("state") == "running" and alive(record.get("pid"), record.get("pid_started")):
                    try:
                        await self._stop_run(record, "stopped with the caller that waited for it")
                    except OSError as exc:
                        logger.warning("coding_cli: run %s not stopped with its caller: %s", run_id, exc)
            raise
        if record.get("state") in FINAL_STATES:
            return await self._finished(status, run_id, session_id)
        answer = self._running_report(record)
        await status.end(f"run {run_id} running, {len(answer['last_actions']['content'])} recent action(s)")
        return answer

    async def quota(self, params: dict[str, Any]) -> dict[str, Any]:
        """The subscription's windows as the last run reported them -- asked anew when that would refuse and is
        older than QUOTA_FRESH_S, as run_task does -- and the budget limit a run would be refused for now (limit, as
        run_task's: None when none), at the instance's threshold, or the lower one asked. No run has reported yet:
        known false, no limit -- nothing to go by refuses nothing."""
        status = params.get("_status") or _NoStatus()
        if self.agy:
            # Not offered (schema.yaml); called by name it says why instead of "below 80 %".
            return await self._fail(status, "Antigravity reports no subscription windows: there is nothing to show")
        utilization = params.get("max_window_utilization")
        if utilization is not None and not _utilization_ok(utilization):
            return await self._fail(status, "max_window_utilization: a share above 0 and at most 1")
        threshold = min(utilization or 1.0, self.max_utilization)
        state = await self._quota_now(threshold, str(params.get("_user_id") or ""))
        limit = cli.quota_limit(state, threshold, time.time())
        await status.end(cli.limit_text(limit) if limit else f"below {threshold:.0%}")
        return {"status": "success", "known": isinstance(state, dict), "windows": cli.windows(state),
                "threshold": threshold, "limit": limit}

    async def cancel_run(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        session_id = str(params.get("_session_id") or "")
        record = self._own(params.get("run_id"), str(params.get("_user_id") or ""))
        if record is None:
            return await self._fail(status, f"no run {str(params.get('run_id'))[:20]} of yours")
        run_id = record["run_id"]
        # Only a process still there is cancelled: a run that ended on its own
        # keeps its outcome, even when nobody has looked at it yet.
        if record.get("state") == "running" and alive(record.get("pid"), record.get("pid_started")):
            await self._stop_run(record, "cancelled on request")
            monitor = self._monitors.get(run_id)
            if monitor is not None:
                await asyncio.wait([monitor], timeout=CLAIM_WAIT_S)
        elif record.get("state") == "running" and not record.get("pid"):
            # Still being started: the owner's monitor stops it once it runs.
            self._stop(run_id, "cancelled on request")
        # A run another live instance owns is finalized there: wait for it.
        for _ in range(int(CLAIM_WAIT_S / 0.5)):
            record = await self._settle(run_id, session_id)
            if record.get("state") in FINAL_STATES:
                break
            await asyncio.sleep(0.5)
        if record.get("state") not in FINAL_STATES:
            answer = self._running_report(record)
            answer["note"] = f"the stop is sent and the run is still ending -- look again with {self.name}_get_run"
            await status.error(f"run {run_id}: stop sent, still ending")
            return answer
        return await self._finished(status, run_id, session_id)

    # ── a run's life ──

    def _stop(self, run_id: str, reason: str) -> None:
        """Why the run ends early; _finalize reads it unless the run finished anyway."""
        self._file(run_id, "cancel").write_text(reason, encoding="utf-8")

    async def _stop_run(self, record: dict, reason: str) -> None:
        self._stop(record["run_id"], reason)
        await asyncio.to_thread(cli.kill_tree, record.get("pid"), record.get("pid_started"))

    def _prepare(self, run_id: str, task: str, mode: str, workdir: str, prior: Optional[dict],
                 user_id: str, session_id: str, schema: Optional[dict] = None,
                 asked_run_s: Optional[float] = None, asked_utilization: Optional[float] = None,
                 pin: Optional[dict] = None, request_id: str = "") -> dict:
        """Worktree, command line and record, then the process (sync, off the loop)."""
        (self._root() / "runs").mkdir(parents=True, exist_ok=True)
        spec = self.workdirs[workdir]
        if prior:
            worktree, branch = Path(prior["worktree"]), prior["branch"]
        else:
            worktree, branch = self._root() / "worktrees" / run_id, f"coding_cli/{run_id}"
        try:
            made = (cli.Worktree(prior["base"], prior.get("git_dir") or "", prior.get("hidden") or []) if prior
                    else cli.make_worktree(spec["path"], worktree, branch, spec["exclude"]))
            if not prior:
                cli.seed_worktree(spec["path"], worktree, spec.get("seed") or ())
            return self._start(run_id, task, mode, workdir, prior, user_id, session_id, worktree, branch, made, schema,
                               asked_run_s, asked_utilization, pin, request_id or run_id)
        except BaseException:
            issued = run_id in _GRANTS
            _revoke(run_id)
            if self.agy and issued:     # only the file written here: one the branch carries is the user's
                with contextlib.suppress(OSError):
                    cli.agy_gateway(worktree, "", None)
            # Answered "not started": no MCP file is left (a url may hold a key in its path), and for a fresh
            # run no worktree or branch that no answer names.
            with contextlib.suppress(OSError):
                self._file(run_id, "mcp.json").unlink(missing_ok=True)
            if not prior:
                cli.remove_worktree(spec["path"], worktree, branch)
            raise

    def _start(self, run_id: str, task: str, mode: str, workdir: str, prior: Optional[dict], user_id: str,
               session_id: str, worktree: Path, branch: str, made: cli.Worktree, schema: Optional[dict] = None,
               asked_run_s: Optional[float] = None, asked_utilization: Optional[float] = None,
               pin: Optional[dict] = None, request_id: str = "") -> dict:
        resume = prior["claude_session"] if prior else ""
        unsafe = cli.agy_unsafe(worktree) if self.agy else []
        if unsafe:
            # agy would run them, hooks without asking (run.py, AGY_SETTINGS).
            raise OSError(f"the worktree carries {', '.join(unsafe)} -- agy would load and run it; take it out of "
                          f"the repository (or the branch) first")
        # The call's own limit, shorter only; else the instance's. The asked one is kept as asked: a resume
        # inherits it, held to the instance's limit as it is then.
        max_run_s = min(asked_run_s, self.max_run_s) if asked_run_s else self.max_run_s
        if resume and not _SESSION.fullmatch(resume):
            raise OSError(f"run {prior['run_id']} holds no usable session id")
        # Plan mode only reads: no server whose tools could write.
        servers = self.workdirs[workdir]["mcp_servers"] if mode != "plan" else {}
        gateway = self.workdirs[workdir]["gateway"] if mode != "plan" else None
        token, url = None, ""
        if gateway:
            from plugins.mcp_gateway import grants
            try:
                # Under the caller's id (cost attribution by prefix), its own per run: two runs of one turn,
                # or a resume, count their calls apart.
                token = grants.issue(user_id, gateway["tools"], request_id=f"{request_id or run_id}_{run_id}",
                                     pinned=pin, allowed=gateway["allowed"], session_id=session_id or None,
                                     ttl_s=max_run_s + GRANT_SLACK_S, gateway=gateway["instance"])
            except (LookupError, ValueError) as exc:
                raise OSError(f"mcp gateway: {exc}") from None
            _GRANTS[run_id] = token
            url = f"http://{self.gateway_host}:{self.gateway_port}/plugins/{gateway['instance']}/mcp"
        env, values = {**cli.child_env(self.pass_env), **self.env}, {}
        if self.agy:
            # One per ScarabHive user: what agy keeps there (conversations for a resume, memories) is not handed
            # from one user's runs to another's.
            cli.agy_home(self._root() / "agy_home" / (re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:60] or "_"), env)
        else:
            # Claude Code gives up on an MCP tool that says nothing for 5 min (its default): a tool that works
            # longer without a word was given up on and called again. The run's own limit bounds a call.
            env["CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT"] = str(round(max_run_s * 1000))
        if servers or (token and not self.agy):
            mcp, config = self._file(run_id, "mcp.json"), {}
            for name, cfg in servers.items():
                headers = {}
                # Built now, so a rotated token applies. The file names a variable, the value is only in the
                # child's environment: Claude Code expands ${VAR} in a header (M-CC-10).
                for header, value in build_auth_headers(getattr(cfg, "auth", None)).items():
                    var = f"CODING_CLI_MCP_{len(values)}"
                    values[var], headers[header] = value, f"${{{var}}}"
                config[name] = {"type": "sse" if _transport(cfg) in _SSE_ALIASES else "http", "url": cfg.url,
                                "headers": headers}
            if token:
                var = f"CODING_CLI_MCP_{len(values)}"
                values[var] = f"Bearer {token}"
                config[cli.GATEWAY_SERVER] = {"type": "http", "url": url, "headers": {"Authorization": f"${{{var}}}"}}
            mcp.write_text(json.dumps({"mcpServers": config}, indent=1), encoding="utf-8")
            env.update(values)
        else:
            mcp = self._no_mcp()
        rules = worktree / "CLAUDE.md"
        if self.agy:
            if token:
                cli.agy_gateway(worktree, url, token)
            cmd = cli.build_agy_command(self.command, mode=mode, model=self.model, effort=self.effort,
                                        resume=resume, json_schema=schema)
        else:
            cmd = cli.build_command(self.command, mode=mode, mcp_config=mcp, allowed_commands=self.allowed_commands,
                                    mcp_servers={**{n: (getattr(c, "tools", None) and c.tools.blocked) or []
                                                    for n, c in servers.items()},
                                                 **({cli.GATEWAY_SERVER: []} if token else {})},
                                    model=self.model, effort=self.effort,
                                    resume=resume, rules=rules if rules.is_file() else None, json_schema=schema,
                                    web=self.workdirs[workdir]["web"], web_with_shell=self.web_with_shell)
        record = {"run_id": run_id, "instance_name": self.name, "cli": self.cli,
                  "max_run_s": max_run_s, "asked_run_s": asked_run_s,
                  "asked_utilization": asked_utilization,
                  "user_id": user_id,
                  "session_id": session_id, "workdir": workdir,
                  "mode": mode, "task": task[:300], "worktree": str(worktree), "branch": branch, "base": made.base,
                  "git_dir": made.git_dir, "hidden": made.hidden, "json_schema": schema,
                  "prior": prior["run_id"] if prior else None, "state": "running", "started_at": time.time(),
                  "pin": pin, "gateway": gateway["instance"] if gateway else None,
                  # Read at the end, by whichever instance finalizes: the starting one's bound.
                  "max_result_chars": self.max_result_chars,
                  "owner": self._me}
        if self.agy:
            # Before the task, the same for every run in the workdir: the cached prefix stays one.
            note = cli.agy_note(gateway["tools"] if token else None)
            self._file(run_id, "task").write_text(cli.agy_input(note + task), encoding="utf-8")
        else:
            self._file(run_id, "task").write_text(task, encoding="utf-8")
        self._save(record)
        try:
            proc = cli.launch(cmd, worktree, env, self._file(run_id, "task"),
                              self._file(run_id, "jsonl"), self._file(run_id, "err"))
        except OSError as exc:
            record.update(state="failed", ended_at=time.time(), note=f"{self.label} did not start: {exc}")
            if prior is None:
                # _prepare removes the worktree and branch: the record must not name them.
                for key in ("worktree", "branch", "base", "git_dir", "hidden"):
                    record.pop(key, None)
            self._save(record)
            raise
        record.update(pid=proc.pid, pid_started=cli.process_start(proc.pid))
        try:
            self._save(record)
        except OSError:
            # Answered "not started", it must not be running either.
            cli.kill_tree(proc.pid, record["pid_started"])
            raise
        return {**record, "_proc": proc}

    def _watch(self, record: dict, proc) -> asyncio.Task:
        run_id = record["run_id"]
        monitor = asyncio.create_task(self._monitor(record, proc))
        self._monitors[run_id] = monitor
        monitor.add_done_callback(lambda _t: self._monitors.pop(run_id, None))
        return monitor

    async def _monitor(self, record: dict, proc) -> None:
        """Progress while a tool call waits, the time limit, the end, the ring.
        proc is None for a run another process started: its pid is watched."""
        run_id, root = record["run_id"], Path(record.get("worktree") or ".")
        going = ((lambda: proc.poll() is None) if proc is not None
                 else (lambda: alive(record.get("pid"), record.get("pid_started"))))
        offset, deadline = 0, float(record.get("started_at") or time.time()) + self._limit_s(record)
        stopped, answered = False, None
        try:
            while going():
                await asyncio.sleep(POLL_S)
                if not stopped and self._file(run_id, "cancel").exists():
                    # A stop sent while the run had no process yet (cancel_run).
                    await asyncio.to_thread(cli.kill_tree, record.get("pid"), record.get("pid_started"))
                    stopped = True
                elif time.time() > deadline:
                    limit = self._limit_s(record) / 60
                    await self._stop_run(record, f"stopped after the time limit of {limit:.0f} min")
                    deadline, stopped = float("inf"), True
                found, offset = cli.events_from(self._file(run_id, "jsonl"), offset)
                if found and (answered is not None or any(event.get("type") == "result" for event in found)):
                    # Counted from its last word after a result: a CLI that goes on working after it is left to.
                    answered = time.time()
                elif answered is not None and not stopped and time.time() - answered > RESULT_GRACE_S:
                    # Its result is written and it says nothing more, nor exits: ended, the result stands
                    # (finalize: a success result wins over a stop that came after it).
                    logger.warning("coding_cli: run %s still going %.0f s after its result, ended", run_id,
                                   time.time() - answered)
                    await asyncio.to_thread(cli.kill_tree, record.get("pid"), record.get("pid_started"))
                    stopped = True
                listener, live = self._listeners.get(run_id), self._live.get(run_id)
                if listener is None and live is None:
                    continue
                if live is not None:
                    await live.feed(found)
                if listener is None:
                    continue
                # At most one line per poll: the rest is counted, not lost from the record.
                # A live view shows each tool call as a line of its own, with its result.
                lines = [line for event in found for line in cli.actions(event, root, tools=live is None)]
                if lines:
                    await listener.progress(lines[-1] if len(lines) == 1 else f"{lines[-1]} (+{len(lines) - 1})")
            live = self._live.get(run_id)
            if live is not None:
                # What the process wrote between the last poll and its exit.
                await live.feed(cli.events_from(self._file(run_id, "jsonl"), offset)[0])
            if await asyncio.to_thread(self._finalize, run_id) is None:
                # Somebody else ends it: wait for that end, then ring if they did not.
                for _ in range(int(CLAIM_WAIT_S / 0.5)):
                    if (self._load(run_id) or {}).get("state") in FINAL_STATES:
                        break
                    await asyncio.sleep(0.5)
            if live is not None:
                await live.close(self._load(run_id) or {})
            self._ring(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken watch costs progress and the ring, never the process
            logger.exception("coding_cli: watching run %s failed", run_id)
        finally:
            self._live.pop(run_id, None)

    async def _settle(self, run_id: str, reader: str = "") -> dict:
        """The record, finalized first when its process is gone or past its
        time. An ended run counts as read by the session asking, and rings the
        one that asked to be woken."""
        record = self._load(run_id) or {"run_id": run_id, "state": "failed", "note": "the record is gone"}
        if record.get("state") == "running" and not self._owner_alive(record):
            if alive(record.get("pid"), record.get("pid_started")):
                reason = self._orphan_stop(record)
                if not reason:
                    return record
                await self._stop_run(record, reason)
            record = await asyncio.to_thread(self._finalize, run_id) or self._load(run_id) or record
        if record.get("state") in FINAL_STATES:
            if reader:
                self._mark_read(run_id, reader)
            self._ring(run_id)
        return record

    def _ring(self, run_id: str) -> None:
        """Wakes the session that asked for it, unless it read the end already.
        The lease <id>.rung names the ringer; a ringer that died or was stopped
        before the end leaves it to be rung again."""
        try:
            wake = json.loads(self._file(run_id, "wake").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(wake, dict) or (self._load(run_id) or {}).get("state") not in FINAL_STATES:
            return
        session_id, user_id = str(wake.get("session_id") or ""), str(wake.get("user_id") or "")
        lease = self._file(run_id, "rung")
        if self._was_read(run_id, session_id):
            self._drop_wake(run_id)
            return
        # wake_session judges the RINGER's depth: a woken process would take
        # the lease and wake nobody. Left untaken, a process that can ring does.
        if wake_depth() or wake_blocked(self.system_config, session_id, user_id):
            return
        try:
            if lease.exists():
                try:
                    held = json.loads(lease.read_text(encoding="utf-8"))
                except ValueError:
                    held = None
                if not isinstance(held, dict):
                    # Being written this moment -- or left so by a process
                    # that died between creating and writing it.
                    if time.time() - lease.stat().st_mtime < STALE_LEASE_S:
                        return
                elif held.get("done"):
                    self._drop_wake(run_id)
                    return
                elif _instance_alive(held):
                    return
                lease.unlink(missing_ok=True)
            if not _claim(lease, json.dumps({**self._me, "done": False})):
                return
        except OSError as exc:
            logger.debug("coding_cli: ring of run %s not taken now: %s", run_id, exc)
            return
        self._rings[run_id] = asyncio.create_task(self._deliver(run_id, session_id, user_id))
        self._rings[run_id].add_done_callback(lambda _t: self._rings.pop(run_id, None))

    def _drop_wake(self, run_id: str) -> None:
        """A wake that did its job: every sweep would look at it again."""
        with contextlib.suppress(OSError):
            self._file(run_id, "wake").unlink(missing_ok=True)

    async def _deliver(self, run_id: str, session_id: str, user_id: str) -> None:
        lease = self._file(run_id, "rung")
        try:
            await wake_session(self.system_config, session_id, user_id, what=f"coding_cli run {run_id}",
                               still_needed=lambda: not self._was_read(run_id, session_id))
        except asyncio.CancelledError:
            # Not rung to the end: the next sweep, here or elsewhere, rings again.
            lease.unlink(missing_ok=True)
            raise
        # The wake first: gone, nobody rings again, whether or not the lease says done.
        self._drop_wake(run_id)
        try:
            lease.write_text(json.dumps({**self._me, "done": True}), encoding="utf-8")
        except OSError as exc:
            logger.warning("coding_cli: lease of run %s not marked done: %s", run_id, exc)

    def _finalize(self, run_id: str) -> Optional[dict]:
        """Once per run, by whoever sees its end first: commit, changes, outcome."""
        claim_path = self._file(run_id, "final")
        if not _claim(claim_path):
            # A claim that never became an end: its process died while
            # finalizing (an API restart). Taken over, or the run stays
            # "running" for good.
            try:
                stale = time.time() - claim_path.stat().st_mtime > STALE_CLAIM_S
            except OSError:
                stale = False
            if not (stale and (self._load(run_id) or {}).get("state") == "running"):
                return None
            claim_path.touch()
        # Read once, at the start: a url (its path may hold a key) need not lie on disk any longer.
        with contextlib.suppress(OSError):
            self._file(run_id, "mcp.json").unlink(missing_ok=True)
        _revoke(run_id)
        record = self._load(run_id) or {"run_id": run_id}
        if record.get("cli") == "antigravity" and record.get("gateway") and record.get("worktree"):
            # Its token is not committed, and a resume would not start on it (agy_unsafe).
            with contextlib.suppress(OSError):
                cli.agy_gateway(Path(record["worktree"]), "", None)
        leftover = (record.get("cli") == "antigravity" and record.get("gateway") and record.get("worktree")
                    and (Path(record["worktree"]) / cli.AGY_ROOTS[0] / "mcp_config.json").exists())
        try:
            found = cli.outcome(cli.events(self._file(run_id, "jsonl")))
            result = found["result"] if isinstance(found["result"], dict) else None
            # notes are this plugin's words; details quote git and Claude Code,
            # whose output can carry what the run wrote.
            notes, details = [], []
            try:
                cancelled = self._file(run_id, "cancel").read_text(encoding="utf-8")
            except OSError:
                cancelled = ""
            # Without its worktree and git directory there is nothing to commit
            # -- never in the process's own directory.
            git_dir, base = str(record.get("git_dir") or ""), str(record.get("base") or "")
            worktree = Path(record["worktree"]) if record.get("worktree") and git_dir else None
            if leftover:
                # Its token is dead, but the branch would carry the file -- and no resume would start on it.
                notes.append("not committed: the gateway's MCP file could not be removed from the worktree")
            elif record.get("mode") == "edit" and worktree and worktree.is_dir():
                try:
                    record["commit"] = cli.commit_all(worktree, git_dir, f"coding_cli {run_id}: {_first_line(record)}")
                except cli.GitError as exc:
                    notes.append("not committed, the changes are in the worktree")
                    details.append(str(exc))
            if worktree and worktree.is_dir() and base:
                try:
                    record["changes"] = cli.changes(worktree, git_dir, base)
                except cli.GitError as exc:
                    notes.append("changes not read")
                    details.append(str(exc))
            if found["rate_limit"]:
                self._store_quota(found["rate_limit"])
            if found["session"]:
                record["claude_session"] = found["session"]
            if isinstance(found["model"], str) and found["model"]:
                record["model"] = found["model"]
            # A success result wins over a stop that came too late: a killed
            # run never gets as far as one.
            if result and not result.get("is_error") and result.get("subtype") == "success":
                record["state"] = "done"
            elif cancelled:
                record["state"] = "cancelled"
                notes.append(cancelled)
            else:
                record["state"] = "failed"
                # The subscription stopped it: said as a field (limit), as a refusal before the start is
                limit = cli.usage_limit(result, found["rate_limit"])
                if limit:
                    record["limit"] = limit
                    notes.append(f"stopped by the subscription: {cli.limit_text(limit)}")
                if not result:
                    try:
                        err = self._file(run_id, "err").read_text(encoding="utf-8", errors="replace").strip()
                    except OSError:
                        err = ""
                    label = "Antigravity" if record.get("cli") == "antigravity" else "Claude Code"
                    notes.append(f"{label} ended without a result" + (", its stderr is in details" if err else ""))
                    if err:
                        details.append(err[-CAP_STDERR:])
            if result:
                # cost_usd: on the subscription a notional amount, the measure the stream reports (M-CC-7).
                text, cap = str(result.get("result") or ""), int(record.get("max_result_chars") or CAP_RESULT)
                record.update(result=text[:cap], result_cut=len(text) > cap,
                              subtype=result.get("subtype"), turns=result.get("num_turns"),
                              duration_s=round(float(result.get("duration_ms") or 0) / 1000),
                              denials=[_denial(d) for d in result.get("permission_denials") or []][:20],
                              cost_usd=result.get("total_cost_usd"))
                output = result.get("structured_output")
                if output is not None:
                    size = len(json.dumps(output, ensure_ascii=False))
                    if size > self.max_output_chars:
                        # Cut, it would be no object of the schema any more.
                        notes.append(f"the structured output ({size} characters as JSON) is over "
                                     f"{self.max_output_chars} and "
                                     f"was dropped")
                    elif cli.nesting(output) > cli.MAX_NESTING:
                        notes.append(f"the structured output nests deeper than {cli.MAX_NESTING} and was dropped")
                    else:
                        record["output"] = output
            record.update(ended_at=time.time(), abo=cli.windows(found["rate_limit"]), note="; ".join(notes),
                          details=details)
        except Exception as exc:  # noqa: BLE001 - a run must end in a state, whatever broke
            logger.exception("coding_cli: finalizing run %s failed", run_id)
            record.update(state="failed", ended_at=time.time(), note=f"finalizing failed: {exc}")
        self._save(record)
        return record

    # ── answers ──

    async def _finished(self, status, run_id: str, session_id: str) -> dict:
        record = self._load(run_id) or {"run_id": run_id, "state": "failed", "note": "the record is gone"}
        self._mark_read(run_id, session_id)
        changes = record.get("changes") or []
        # File names, denied paths and git's or Claude Code's messages are the run's words too.
        answer = {"status": "success", **{k: record.get(k) for k in (
            "run_id", "state", "mode", "workdir", "branch", "base", "commit", "worktree", "turns",
            "duration_s", "abo", "cost_usd", "model")}, "changes": _untrusted(changes),
            "denials": _untrusted(record.get("denials") or [])}
        hidden = record.get("hidden") or []
        if hidden:
            answer["hidden"] = hidden[:CAP_HIDDEN] + ([f"... {len(hidden) - CAP_HIDDEN} more"]
                                                      if len(hidden) > CAP_HIDDEN else [])
        if record.get("result") is not None:
            answer["result"] = _untrusted(record["result"])
            if record.get("result_cut"):
                answer["result_cut"] = True
        if record.get("output") is not None:
            # An object matching json_schema, filled from what the run read: data as much as the result.
            answer["output"] = _untrusted(record["output"])
        if record.get("note"):
            answer["note"] = record["note"]
        if record.get("limit"):
            answer["limit"] = record["limit"]
        if record.get("details"):
            answer["details"] = _untrusted(record["details"])
        answer["next"] = (f"resume={run_id} continues this conversation in the same worktree; "
                          f"nothing is merged -- git diff {str(record.get('base'))[:10]}..{record.get('branch')} shows it")
        files = len([c for c in changes if not c.startswith("...")])
        window = (record.get("abo") or {}).get("five_hour")
        line = (f"run {run_id} {record.get('state')}: {record.get('turns') or 0} turn(s), {files} file(s), "
                f"{record.get('duration_s') or 0} s" + (f", 5h window {window:.0%}" if window is not None else ""))
        if record.get("state") == "done":
            await status.end(line)
        else:
            await status.error(f"{line} -- {record.get('note') or record.get('subtype') or ''}"[:140])
        return answer

    def _running_report(self, record: dict) -> dict:
        root = Path(record.get("worktree") or ".")
        lines = [line for event in cli.events(self._file(record["run_id"], "jsonl")) for line in cli.actions(event, root)]
        return {"status": "success", "run_id": record["run_id"], "state": "running", "branch": record.get("branch"),
                "running_s": round(time.time() - float(record.get("started_at") or time.time())),
                "last_actions": _untrusted(lines[-LAST_ACTIONS:])}

    def _arm_wake(self, run_id: str, session_id: str, user_id: str, sub_agent: bool) -> dict:
        """Whether the session is rung at the end. Every refusal says what to do instead."""
        if wake_depth():
            blocked = "this run was itself woken and ends with its turn"
        else:
            blocked = wake_blocked(self.system_config, session_id, user_id)
        if not blocked and sub_agent:
            blocked = "a sub-agent's session is never woken"
        if not blocked and not _SESSION_KEY.fullmatch(session_id):
            blocked = "this session cannot be watched"
        if blocked:
            return {"wake": False, "wake_note": f"{blocked}; you are not woken -- give run id {run_id} to whoever "
                                                f"asked; {self.name}_get_run with wait_s waits for its end"}
        self._file(run_id, "wake").write_text(json.dumps({"session_id": session_id, "user_id": user_id}),
                                              encoding="utf-8")
        return {"wake": True, "wake_note": f"you are woken when the run ends: give the user run id {run_id} and "
                                           f"end your turn, then read it with {self.name}_get_run. A one-shot "
                                           f"agent-cli run is never woken -- there, wait with {self.name}_get_run wait_s"}

    async def _is_sub_agent(self, session_id: str, user_id: str) -> bool:
        """wake_blocked leaves this out (core/session_presence/wake.py): a sub-agent's
        session is never woken. Asked off the loop, as it parses the session file."""
        presence = presence_for(self.system_config)
        if presence is None or not session_id:
            return False
        try:
            state = await asyncio.to_thread(presence.get, session_id, user_id)
        except Exception as exc:  # noqa: BLE001 - the run has started, nothing may raise now
            logger.warning("coding_cli: could not tell whether session %s is a sub-agent's: %s", session_id, exc)
            return False
        return bool(state and state.get("sub_agent"))


def _first_line(record: dict) -> str:
    return next((ln.strip() for ln in str(record.get("task") or "").splitlines() if ln.strip()), "")[:60]


def _denial(denial: Any) -> str:
    if not isinstance(denial, dict):
        return str(denial)[:120]
    args = denial.get("tool_input") if isinstance(denial.get("tool_input"), dict) else {}
    target = args.get("file_path") or args.get("command") or args.get("path") or ""
    return f"{denial.get('tool_name')} {' '.join(str(target).split())}".strip()[:120]
