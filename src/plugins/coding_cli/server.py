"""coding_cli tool server: Claude Code as a coding tool for ScarabHive agents.

A run is one headless Claude Code process in a fresh git worktree of an
operator-listed repository (docs/coding_cli_plugin_konzept.md §3-§6):

* locked down: --restricted, no MCP servers, an explicit tool list, file tools
  confined to the worktree, a shell only for the operator's commands (run.py),
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
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from agent_system.core.session_presence import alive, presence_for, wake_blocked, wake_depth, wake_session
from agent_system.paths import PROJECT_ROOT
from agent_system.tools.schema_based import SchemaBasedToolServer

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
CAP_RESULT = 12_000
CAP_STDERR = 600
CAP_HIDDEN = 50
LAST_ACTIONS = 12
POLL_S = 1.0
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


def _bounded(value: Any, default: float, low: float, high: float) -> Optional[float]:
    try:
        return max(low, min(float(value if value not in (None, "") else default), high))
    except (TypeError, ValueError, OverflowError):
        return None


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


class CodingCliServer(SchemaBasedToolServer):
    """Claude Code runs. Offered only when the executable and a workdir are there."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        super().__init__(name, system_config, server_config)
        self.command = cli.find_claude(str(getattr(server_config, "command", "") or "claude"))
        self.workdirs: dict[str, dict] = {}
        for wname, entry in (getattr(server_config, "workdirs", None) or {}).items():
            entry = entry if isinstance(entry, dict) else {"path": entry}
            path = Path(str(entry.get("path") or ""))
            path = (path if path.is_absolute() else PROJECT_ROOT / path).resolve()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(wname)) or not (path / ".git").exists():
                logger.warning("coding_cli: workdir %r skipped -- a plain name and a git repository are needed", wname)
                continue
            self.workdirs[str(wname)] = {"path": path, "exclude": [str(e) for e in entry.get("exclude") or []]}
        # The subscription belongs to one person (concept §7).
        self.allowed_users = frozenset(str(u) for u in getattr(server_config, "allowed_users", None) or ())
        self.allowed_commands = [str(c) for c in getattr(server_config, "allowed_commands", None) or ()]
        self.pass_env = [str(v) for v in getattr(server_config, "pass_env", None) or ()]
        self.model = str(getattr(server_config, "model", "") or "")
        self.max_utilization = _bounded(getattr(server_config, "max_window_utilization", None), 0.8, 0.05, 1.0) or 0.8
        self.wait_s = _bounded(getattr(server_config, "wait_s", None), 300, 0, 3600)
        self.wait_s = 300.0 if self.wait_s is None else self.wait_s
        self.max_run_s = 60 * (_bounded(getattr(server_config, "max_run_minutes", None), 60, 1, 24 * 60) or 60)
        self.max_parallel = int(_bounded(getattr(server_config, "max_parallel", None), 1, 1, 8) or 1)
        self._monitors: dict[str, asyncio.Task] = {}
        self._listeners: dict[str, Any] = {}
        # run id -> its live view (live.py), for the life of the run, not only
        # while the call that started it waits.
        self._live: dict[str, LiveRun] = {}
        self._rings: dict[str, asyncio.Task] = {}
        self._starting: set[str] = set()
        self._sweeper: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()
        self._instance = secrets.token_hex(6)
        self._me = {"pid": os.getpid(), "started": cli.process_start(os.getpid()), "instance": self._instance}

        if self.command is None:
            logger.info("coding_cli: no Claude Code executable found -- %s offers no tools", name)
        elif not self.workdirs:
            logger.warning("coding_cli: no usable workdir -- %s offers no tools", name)

    def get_template_vars(self) -> dict[str, Any]:
        template_vars = super().get_template_vars()
        template_vars["configured"] = bool(self.command and self.workdirs)
        template_vars["workdirs"] = sorted(self.workdirs)
        return template_vars

    async def start_plugin(self) -> None:
        """Takes over the runs whose owner is gone, now and every SWEEP_S:
        without it their time limit, their end and their ring would wait for
        somebody to look."""
        await self._sweep_guarded()
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def stop_plugin(self) -> None:
        """Ends the watching, not the runs: they finish on their own, and
        another instance takes them over -- its process may live on, so the
        runs are released on disk. A ring not delivered to the end gives its
        lease up, so it is rung again."""
        watched = list(self._monitors)
        tasks = [*self._monitors.values(), *self._rings.values(), *([self._sweeper] if self._sweeper else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for run_id in watched:
            self._file(run_id, f"released-{self._instance}").touch()

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
        # already: a claim here would be used up, and the next sweep skip it.
        if gone != self._instance and not _claim(self._file(run_id, f"adopt-{gone}")):
            return
        record = self._load(run_id) or record
        if record.get("state") != "running":
            return
        record["owner"] = self._me
        self._save(record)
        if alive(record.get("pid"), record.get("pid_started")):
            self._watch(record, None)
        else:
            await self._settle(run_id)

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
        return PROJECT_ROOT / "data" / "coding_cli"

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
        path = self._file(record["run_id"], "json")
        tmp = path.with_name(f"{path.stem}.{os.getpid()}-{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
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

    def _records(self) -> list[dict]:
        folder = self._root() / "runs"
        records = [self._load(p.stem) for p in folder.glob("*.json")] if folder.is_dir() else []
        return [r for r in records if r and _RUN_ID.fullmatch(str(r.get("run_id")))]

    def _own(self, run_id: Any, user_id: str) -> Optional[dict]:
        """The user's run, or None -- another user's run is no run to them."""
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            return None
        record = self._load(run_id)
        return record if record and record.get("user_id") == user_id else None

    async def _running(self) -> list[dict]:
        """The runs going now, and those another instance is starting. One
        nobody watches is settled first: it may have ended, or be past its
        time limit."""
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

    def _mark_read(self, run_id: str, session_id: str) -> None:
        if _SESSION_KEY.fullmatch(session_id or ""):
            self._file(run_id, f"read-{session_id}").touch()

    def _was_read(self, run_id: str, session_id: str) -> bool:
        return self._file(run_id, f"read-{session_id}").exists()

    # ── tools ──

    async def _fail(self, status, message: str) -> dict:
        await status.error(message[:140])
        return {"status": "error", "error": message}

    async def run_task(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("_status") or _NoStatus()
        user_id, session_id = str(params.get("_user_id") or ""), str(params.get("_session_id") or "")
        if user_id not in self.allowed_users:
            return await self._fail(status, "Claude Code runs on the operator's subscription: this user may not "
                                            "start runs (coding_cli allowed_users)")
        task = params.get("task")
        if not isinstance(task, str) or not task.strip():
            return await self._fail(status, "task: the complete task as text")
        if len(task) > MAX_TASK_CHARS:
            return await self._fail(status, f"task: at most {MAX_TASK_CHARS} characters")
        try:
            # Before anything is made: a lone surrogate (a broken escape) fails the task file only after the worktree.
            task.encode("utf-8")
        except UnicodeEncodeError:
            return await self._fail(status, "task: not valid text (a broken character escape) -- send it again")
        mode = params.get("mode") or "edit"
        if mode not in ("edit", "plan"):
            return await self._fail(status, "mode: edit (change the code) or plan (read and answer with a plan)")
        prior = None
        if params.get("resume"):
            prior = self._own(params["resume"], user_id)
            if prior is None:
                return await self._fail(status, f"resume: no run {str(params['resume'])[:20]} of yours")
            # Its process may be gone without anybody having looked.
            prior = await self._settle(prior["run_id"], session_id)
            if prior.get("state") not in FINAL_STATES:
                return await self._fail(status, f"resume: run {prior['run_id']} has not ended yet")
            if not prior.get("claude_session") or not Path(prior["worktree"]).is_dir():
                return await self._fail(status, f"resume: run {prior['run_id']} left no conversation or "
                                                f"worktree to continue -- start a new run")
            workdir = prior["workdir"]
        else:
            workdir = params.get("workdir") or (next(iter(self.workdirs)) if len(self.workdirs) == 1 else None)
        if workdir not in self.workdirs:
            return await self._fail(status, f"workdir: one of {', '.join(sorted(self.workdirs))}")
        blocked = cli.quota_block(self._quota(), self.max_utilization, time.time())
        if blocked:
            return await self._fail(status, f"not started: {blocked}")
        # Held until the process runs: a run being prepared is not on disk as running yet.
        async with self._start_lock:
            running = await self._running()
            if len(running) >= self.max_parallel:
                return await self._fail(status, f"not started: {len(running)} run(s) still going ("
                                                f"{', '.join(r['run_id'] for r in running)}) -- wait for them or cancel one")
            if prior and any(r.get("worktree") == prior["worktree"] for r in running):
                return await self._fail(status, "resume: another run works in that worktree")
            run_id = secrets.token_hex(6)
            self._starting.add(run_id)
            # A plain executor future, not a task: a sweep that cancels every
            # task at the loop's end does not reach it.
            prepare = asyncio.get_running_loop().run_in_executor(None, functools.partial(
                self._prepare, run_id, task, mode, workdir, prior, user_id, session_id))
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
            finally:
                self._starting.discard(run_id)
        live = await LiveRun.open(params.get("_request_id"), task, Path(record.get("worktree") or "."))
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
            if not monitor.done():
                sub_agent = await self._is_sub_agent(session_id, user_id)
        except asyncio.CancelledError:
            # The turn was stopped before the call answered: the run goes with
            # it. Once the call has answered with a run id, the run is on its own.
            await self._stop_run(record, "stopped with the turn that started it")
            raise
        if monitor.done():
            return await self._finished(status, run_id, session_id)
        # No await from the look above to the armed wake: the monitor, which
        # rings when the run ends, cannot end in between.
        answer = self._running_report(self._load(run_id) or record)
        answer.update(self._arm_wake(run_id, session_id, user_id, sub_agent))
        await status.end(f"run {run_id} still going after {self.wait_s:.0f} s"
                         + (", wake armed" if answer["wake"] else ", no wake"))
        return answer

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
        deadline = time.monotonic() + wait_s
        while True:
            record = await self._settle(run_id, session_id)
            if record.get("state") in FINAL_STATES or time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(POLL_S * 2, max(0.0, deadline - time.monotonic())))
        if record.get("state") in FINAL_STATES:
            return await self._finished(status, run_id, session_id)
        answer = self._running_report(record)
        await status.end(f"run {run_id} running, {len(answer['last_actions']['content'])} recent action(s)")
        return answer

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
            answer["note"] = "the stop is sent and the run is still ending -- look again with coding_cli_get_run"
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
                 user_id: str, session_id: str) -> dict:
        """Worktree, command line and record, then the process (sync, off the loop)."""
        (self._root() / "runs").mkdir(parents=True, exist_ok=True)
        mcp = self._root() / "no_mcp.json"
        if not mcp.exists():
            mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
        if prior:
            worktree, branch = Path(prior["worktree"]), prior["branch"]
            made = cli.Worktree(prior["base"], prior.get("git_dir") or "", prior.get("hidden") or [])
        else:
            worktree, branch = self._root() / "worktrees" / run_id, f"coding_cli/{run_id}"
            spec = self.workdirs[workdir]
            made = cli.make_worktree(spec["path"], worktree, branch, spec["exclude"])
        rules = worktree / "CLAUDE.md"
        resume = prior["claude_session"] if prior else ""
        if resume and not _SESSION.fullmatch(resume):
            raise OSError(f"run {prior['run_id']} holds no usable session id")
        cmd = cli.build_command(self.command, mode=mode, mcp_config=mcp, allowed_commands=self.allowed_commands,
                                model=self.model, resume=resume, rules=rules if rules.is_file() else None)
        record = {"run_id": run_id, "user_id": user_id, "session_id": session_id, "workdir": workdir,
                  "mode": mode, "task": task[:300], "worktree": str(worktree), "branch": branch, "base": made.base,
                  "git_dir": made.git_dir, "hidden": made.hidden,
                  "prior": prior["run_id"] if prior else None, "state": "running", "started_at": time.time(),
                  "owner": self._me}
        self._file(run_id, "task").write_text(task, encoding="utf-8")
        self._save(record)
        try:
            proc = cli.launch(cmd, worktree, cli.child_env(self.pass_env), self._file(run_id, "task"),
                              self._file(run_id, "jsonl"), self._file(run_id, "err"))
        except OSError as exc:
            record.update(state="failed", ended_at=time.time(), note=f"Claude Code did not start: {exc}")
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
        offset, deadline = 0, float(record.get("started_at") or time.time()) + self.max_run_s
        stopped = False
        try:
            while going():
                await asyncio.sleep(POLL_S)
                if not stopped and self._file(run_id, "cancel").exists():
                    # A stop sent while the run had no process yet (cancel_run).
                    await asyncio.to_thread(cli.kill_tree, record.get("pid"), record.get("pid_started"))
                    stopped = True
                elif time.time() > deadline:
                    await self._stop_run(record, f"stopped after the time limit of {self.max_run_s / 60:.0f} min")
                    deadline, stopped = float("inf"), True
                listener, live = self._listeners.get(run_id), self._live.get(run_id)
                if listener is None and live is None:
                    continue
                found, offset = cli.events_from(self._file(run_id, "jsonl"), offset)
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
                if time.time() - float(record.get("started_at") or 0) <= self.max_run_s:
                    return record
                await self._stop_run(record, f"stopped after the time limit of {self.max_run_s / 60:.0f} min")
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
        record = self._load(run_id) or {"run_id": run_id}
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
            if record.get("mode") == "edit" and worktree and worktree.is_dir():
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
                (self._root() / "quota.json").write_text(json.dumps(found["rate_limit"]), encoding="utf-8")
            if found["session"]:
                record["claude_session"] = found["session"]
            # A success result wins over a stop that came too late: a killed
            # run never gets as far as one.
            if result and not result.get("is_error") and result.get("subtype") == "success":
                record["state"] = "done"
            elif cancelled:
                record["state"] = "cancelled"
                notes.append(cancelled)
            else:
                record["state"] = "failed"
                if not result:
                    try:
                        err = self._file(run_id, "err").read_text(encoding="utf-8", errors="replace").strip()
                    except OSError:
                        err = ""
                    notes.append("Claude Code ended without a result" + (", its stderr is in details" if err else ""))
                    if err:
                        details.append(err[-CAP_STDERR:])
            if result:
                text = str(result.get("result") or "")
                record.update(result=text[:CAP_RESULT], result_cut=len(text) > CAP_RESULT,
                              subtype=result.get("subtype"), turns=result.get("num_turns"),
                              duration_s=round(float(result.get("duration_ms") or 0) / 1000),
                              denials=[_denial(d) for d in result.get("permission_denials") or []][:20])
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
            "duration_s", "abo")}, "changes": _untrusted(changes), "denials": _untrusted(record.get("denials") or [])}
        hidden = record.get("hidden") or []
        if hidden:
            answer["hidden"] = hidden[:CAP_HIDDEN] + ([f"... {len(hidden) - CAP_HIDDEN} more"]
                                                      if len(hidden) > CAP_HIDDEN else [])
        if record.get("result") is not None:
            answer["result"] = _untrusted(record["result"])
            if record.get("result_cut"):
                answer["result_cut"] = True
        if record.get("note"):
            answer["note"] = record["note"]
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
                                                f"asked; coding_cli_get_run with wait_s waits for its end"}
        self._file(run_id, "wake").write_text(json.dumps({"session_id": session_id, "user_id": user_id}),
                                              encoding="utf-8")
        return {"wake": True, "wake_note": f"you are woken when the run ends: give the user run id {run_id} and "
                                           f"end your turn, then read it with coding_cli_get_run. A one-shot "
                                           f"agent-cli run is never woken -- there, wait with coding_cli_get_run wait_s"}

    async def _is_sub_agent(self, session_id: str, user_id: str) -> bool:
        """wake_blocked leaves this out (core/session_presence.py): a sub-agent's
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
    return f"{denial.get('tool_name')} {' '.join(str(target).split())}"[:120]
