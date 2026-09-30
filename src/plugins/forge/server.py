"""forge tool server: GitLab and GitHub for the coder, one set of tools for both.

The backend a repository uses is its configuration's business, never the
model's (docs/konzept.md E2). This module knows no platform: it checks the
arguments, resolves the repository, lays the policy on top and bounds what
goes back --

* pushes only to branches under ``branch_prefix``, never to the default
  branch, never forced (E6),
* merges only where ``allow_merge`` is set, only an open, non-draft request
  whose CI is green on the head it merges and that has no open thread; the
  checked head goes to the platform as ``sha`` (E4),
* text people or programs wrote -- issues, comments, diffs, logs -- comes back
  marked untrusted (E8), every result capped, one status line per call.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

from agent_system.paths import PROJECT_ROOT, data_path, resolve_data_path
from agent_system.tools.schema_based import SchemaBasedToolServer

from . import gitops
from .events import Store
from .github import GitHub
from .gitlab import GitLab
from .hooks import ForgeHooks
from .http import Api, ForgeError, ForgeNotFound
from .webhook import Intake, WebhookConfig

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

PROVIDERS = {"gitlab": GitLab, "github": GitHub}
DEFAULT_API = {"github": "https://api.github.com"}
CAP_BODY = 8_000            # one issue or request description
CAP_COMMENT = 3_000         # one comment
CAP_TEXT = 24_000           # all comments / threads of one answer
CAP_DIFF = 30_000           # one page of diffs
CAP_LOG = 20_000            # one log excerpt
MAX_JOBS = 60
MAX_EXCERPTS = 40           # pr_discussions entries shown shortened once the text budget is spent
MAX_FILES = 300             # entries in pr_diff's file list
MAX_WAIT_S = 600
NONE_GRACE_S = 45           # how long ci_status waits for a pipeline to appear at all
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")
_GITLAB_SECTION = re.compile(r"section_(?:start|end):\d+:[A-Za-z0-9_.-]+(?:\[[^\]]*\])?\r?")
_RUNNER_PREFIX = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z [0-9a-f]{2}[OE](?:(\+)| )?(.*)", re.S)
_ACTIONS_PREFIX = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")
_WAITING = ("pending", "running")
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


class _NoStatus:
    """Stand-in when a handler is called without the framework's status scope."""

    async def progress(self, message, meta=None):
        pass

    async def end(self, message, meta=None):
        pass

    async def error(self, message, meta=None):
        pass


@dataclass
class Repo:
    name: str
    host: str
    provider: str
    project: str
    api_url: str
    token_env: str
    ca_bundle: str
    tls_verify: bool
    path: Path
    allow_merge: bool
    require_ci: bool


def _untrusted(content: Any) -> dict:
    """Written by people or programs on the platform: data, never instructions (E8)."""
    return {"untrusted": True, "content": content}


def _cap(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n… [{len(text) - limit} more characters cut]"


def _short(value: Any, limit: int = 50) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _number(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # GitHub's job ids passed 10**10 (facts.md F-GH5); 2**53 is where a JSON
    # number stops being exact.
    return number if 0 < number < 2**53 and str(value).strip().lstrip("+").isdigit() else None


def _bounded(value: Any, default: int, low: int, high: int) -> Optional[int]:
    try:
        return max(low, min(int(value if value not in (None, "") else default), high))
    except (TypeError, ValueError, OverflowError):
        return None


def _strings(value: Any) -> Optional[list[str]]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        return None
    return [v.strip() for v in value]


def clean_log(text: str) -> list[str]:
    """A job log as a reader sees it: no colour codes, no GitLab section
    markers, and of a line redrawn with carriage returns only its last state.

    GitLab 19 runners prefix every line with a timestamp and a stream tag
    (``2026-09-27T23:41:19.623723Z 01O ``); a ``+`` after the tag continues
    the line before. GitHub Actions prefixes a timestamp alone, starts with a
    BOM and folds with ``##[group]``/``##[endgroup]`` (facts.md F-LOG1, F-LOG2).
    The prefixes go, a continuation joins its line, a fold keeps its title."""
    lines: list[str] = []
    markers: set[int] = set()       # lines that held only a section marker
    for raw in (text or "").split("\n"):
        prefixed = _RUNNER_PREFIX.match(raw)
        if prefixed:
            plain = _ANSI.sub("", prefixed.group(2))
        else:
            plain = _ANSI.sub("", _ACTIONS_PREFIX.sub("", raw, count=1))
            if plain.startswith("##[endgroup]"):
                continue
            if plain.startswith("##[group]"):
                plain = plain[len("##[group]"):]
        unmarked = _GITLAB_SECTION.sub("", plain)
        # Trailing blanks go only at the end: a continuation may follow them.
        content = unmarked.rstrip("\r").split("\r")[-1]
        if prefixed and prefixed.group(1) and lines:
            lines[-1] += content
            continue
        if not content.strip() and unmarked != plain:
            markers.add(len(lines))
        lines.append(content)
    lines = [line.rstrip() for i, line in enumerate(lines) if line.strip() or i not in markers]
    while lines and not lines[-1]:
        lines.pop()
    return lines


class ForgeServer(SchemaBasedToolServer):
    """The forge tools. Offered only when at least one repository is usable."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        super().__init__(name, system_config, server_config)
        self.branch_prefix = str(getattr(server_config, "branch_prefix", "") or "scarabhive/")
        self.merge_method = str(getattr(server_config, "merge_method", "") or "squash")
        if self.merge_method not in ("merge", "squash", "rebase"):
            logger.warning("forge %s: merge_method %r unknown, using squash", name, self.merge_method)
            self.merge_method = "squash"
        # Only a real false turns it off, only a real true keeps it: a string is a typo, taken as off.
        self.delete_branch = getattr(server_config, "delete_branch_after_merge", True) is True
        self.timeout = float(getattr(server_config, "timeout", 30) or 30)
        self.repos: dict[str, Repo] = {}
        self._apis: dict[str, Api] = {}
        self._backends: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._hosts = getattr(server_config, "hosts", None) or {}
        self._load(self._hosts, getattr(server_config, "repos", None) or {})
        # The webhook (docs/konzept.md §7): its inbox is read in every process a
        # session runs in -- the hook -- and its route only in the API's.
        self.webhook = WebhookConfig.read(getattr(server_config, "webhook", None))
        self.events = Store(data_path("forge", "events.db"))
        self.hooks_plugin = ForgeHooks(name, self.events)
        self._intake = Intake(self)

    def _load(self, hosts: Any, repos: Any) -> None:
        """Repositories whose host, provider and token are all there. A broken
        entry costs that entry, logged -- never the other repositories."""
        if not isinstance(hosts, dict) or not isinstance(repos, dict):
            logger.warning("forge %s: hosts and repos must be mappings", self.name)
            return
        for repo_name, spec in repos.items():
            problem = self._repo_problem(str(repo_name), spec, hosts)
            if problem:
                logger.warning("forge %s: repo %s is left out: %s", self.name, repo_name, problem)
        if repos and not self.repos:
            logger.warning("forge %s: no usable repository -- no tools are offered", self.name)
        for host, api_url in sorted({(r.host, r.api_url) for r in self.repos.values()}):
            if urlsplit(api_url).scheme == "http" and urlsplit(api_url).hostname not in _LOOPBACK:
                logger.warning("forge %s: host %s is plain HTTP (%s): its token travels unencrypted, to the "
                               "API and to git", self.name, host, api_url)

    def _repo_problem(self, repo_name: str, spec: Any, hosts: dict) -> Optional[str]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", repo_name):
            return "the name must be letters, digits, _ . - (it becomes a folder name)"
        if not isinstance(spec, dict) or not spec.get("host") or not spec.get("project"):
            return "needs host and project"
        host = hosts.get(spec["host"])
        if not isinstance(host, dict):
            return f"host {spec['host']!r} is not under hosts"
        provider = str(host.get("provider") or "")
        if provider not in PROVIDERS:
            return f"provider must be one of {', '.join(PROVIDERS)}, got {provider!r}"
        api_url = str(host.get("api_url") or DEFAULT_API.get(provider, "")).rstrip("/")
        if not api_url.startswith(("https://", "http://")):
            return "the host needs api_url (https://<host>/api/v4 for GitLab)"
        token_env = str(host.get("token_env") or "")
        if not token_env or not os.environ.get(token_env):
            return f"token_env {token_env or '(missing)'} is not set in the environment (config/secrets.env)"
        path = Path(resolve_data_path(str(spec["path"]))) if spec.get("path") else data_path("workspace", "forge",
                                                                                             repo_name)
        self.repos[repo_name] = Repo(
            name=repo_name, host=str(spec["host"]), provider=provider, project=str(spec["project"]).strip("/"),
            api_url=api_url, token_env=token_env, ca_bundle=str(host.get("ca_bundle") or ""),
            # Only a real false turns certificate checks off -- for this host, for both
            # the API and git, whatever the global git config says (review S1).
            tls_verify=host.get("tls_verify") is not False,
            path=path if path.is_absolute() else PROJECT_ROOT / path,
            # Only a real true counts: the framework does not check plugin config.
            allow_merge=spec.get("allow_merge") is True, require_ci=spec.get("require_ci", True) is not False)
        if not self.repos[repo_name].tls_verify:
            logger.warning("forge %s: repo %s -- certificate checks are OFF for host %s (tls_verify: false): "
                           "anyone in the network path can read the token", self.name, repo_name, spec["host"])
        return None

    def get_template_vars(self) -> dict[str, Any]:
        template_vars = super().get_template_vars()
        template_vars["configured"] = bool(self.repos)
        template_vars["repos"] = sorted(self.repos)
        template_vars["merge"] = any(r.allow_merge for r in self.repos.values())
        template_vars["branch_prefix"] = self.branch_prefix
        return template_vars

    def webhook_secrets(self) -> dict[str, str]:
        """host -> the secret its GitLab webhooks carry; empty while the webhook is not configured."""
        if self.webhook is None or not isinstance(self._hosts, dict):
            return {}
        secrets = {}
        for host, spec in self._hosts.items():
            env = str((spec or {}).get("webhook_secret_env") or "") if isinstance(spec, dict) else ""
            if env and os.environ.get(env) and any(r.host == host and r.provider == "gitlab"
                                                   for r in self.repos.values()):
                secrets[str(host)] = os.environ[env]
        return secrets

    def get_web_router(self) -> Any:
        """The webhook route, only where a webhook user and a host secret are configured."""
        if not self.webhook_secrets():
            return None
        return self._intake.router()

    def _bind(self, params: dict, repo: Repo, target: str, key: str) -> None:
        """The calling session works on this target: the webhook's news for it goes there (W5)."""
        session, user = params.get("_session_id"), params.get("_user_id")
        if self.webhook is None or not isinstance(session, str) or not isinstance(user, str) or not session:
            return
        try:
            self.events.bind(repo.name, target, key, user, session)
        except Exception:  # noqa: BLE001 - the tool's work is done; only its wake-ups would miss
            logger.exception("forge %s: binding %s %s %s to session %s failed", self.name, repo.name, target, key,
                             session)

    async def stop_plugin(self) -> None:
        for api in self._apis.values():
            await api.aclose()
        self._apis.clear()
        self._backends.clear()

    # ── shared building blocks ────────────────────────────────────────────

    def _repo(self, params: dict) -> Repo:
        name = params.get("repo")
        if not isinstance(name, str) or name not in self.repos:
            raise ForgeError(f"repo must be one of: {', '.join(sorted(self.repos))}")
        return self.repos[name]

    def _backend(self, repo: Repo) -> Any:
        backend = self._backends.get(repo.name)
        if backend is None:
            verify: Any = (repo.ca_bundle or True) if repo.tls_verify else False
            api = self._apis.get(repo.host)
            if api is None:
                api = self._apis[repo.host] = Api(repo.api_url, os.environ.get(repo.token_env, ""),
                                                  timeout=self.timeout, verify=verify, token_env=repo.token_env,
                                                  headers={"Accept": "application/vnd.github+json",
                                                           "X-GitHub-Api-Version": "2022-11-28"}
                                                  if repo.provider == "github" else None)
            backend = self._backends[repo.name] = PROVIDERS[repo.provider](api, repo.project)
        return backend

    def _ref(self, repo: Repo, kind: str, number: int) -> str:
        return f"{repo.name}{'!' if kind == 'pr' and repo.provider == 'gitlab' else '#'}{number}"

    async def _run(self, params: dict, work) -> dict:
        """One tool call: the repository resolved, ForgeError turned into the
        error shape with its status line, anything else logged and reported."""
        status = params.get("_status") or _NoStatus()
        try:
            repo = self._repo(params)
            return await work(repo, self._backend(repo), status)
        except ForgeError as exc:
            await status.error(_short(str(exc), 140))
            return {"status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a tool answers, it does not crash the turn
            logger.exception("forge %s failed", self.name)
            await status.error(_short(f"{type(exc).__name__}: {exc}", 140))
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    def _need_number(self, params: dict, key: str = "number") -> int:
        number = _number(params.get(key))
        if number is None:
            raise ForgeError(f"{key} must be a positive whole number")
        return number

    def _need_text(self, params: dict, key: str, limit: int = 60_000) -> str:
        text = params.get(key)
        if not isinstance(text, str) or not text.strip():
            raise ForgeError(f"{key} is required")
        if len(text) > limit:
            raise ForgeError(f"{key} is {len(text)} characters; at most {limit}")
        return text

    def _lock(self, repo: Repo) -> asyncio.Lock:
        return self._locks.setdefault(repo.name, asyncio.Lock())

    async def _locked(self, repo: Repo, func, *args):
        """``func`` in a thread under the repository's lock. The lock is
        released when the THREAD ends, not the call: git cannot be stopped, and
        a retry must not run a second git on the same clone meanwhile. A
        cancelled call returns at once, however often it is cancelled."""
        lock = self._lock(repo)
        await lock.acquire()
        try:
            job = asyncio.ensure_future(asyncio.to_thread(func, *args))
        except BaseException:
            lock.release()
            raise
        job.add_done_callback(lambda _: lock.release())
        return await asyncio.shield(job)

    def _git_env(self, repo: Repo, backend, info: dict) -> dict:
        """The environment for git with the token, refused where it would
        travel in clear text: a platform behind a TLS proxy may still name an
        http:// clone URL while its API is https (review S8)."""
        clone_url = str(info.get("clone_url") or "")
        if clone_url.startswith("http://") and repo.api_url.startswith("https://"):
            raise ForgeError(f"the platform names an http:// clone URL ({clone_url}) while its API is https -- "
                             f"the token would travel unencrypted; set the platform's external URL to https")
        return gitops.auth_env(clone_url, backend.git_user, os.environ.get(repo.token_env, ""), repo.ca_bundle,
                               verify=repo.tls_verify)

    # ── local clone ───────────────────────────────────────────────────────

    async def checkout(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            branch = params.get("branch") or ""
            base = params.get("base") or ""
            for key, value in (("branch", branch), ("base", base)):
                if value and (not isinstance(value, str) or not gitops.valid_branch(value)):
                    raise ForgeError(f"{key} {_short(value)!r} is no valid branch name")
            info = await backend.project_info()
            env = self._git_env(repo, backend, info)
            answer = await self._locked(repo, self._checkout, repo, info, env, branch, base or info["default_branch"])
            if answer["branch"].startswith(self.branch_prefix):         # its own branches, not one it reviews
                self._bind(params, repo, "branch", answer["branch"])
            await status.end(f"{repo.name}: {'cloned, ' if answer['cloned'] else ''}on {_short(answer['branch'], 50)} "
                             + (f"at {answer['head']}" if answer["head"] else "without commits")
                             + (f", {answer['sync']}" if answer.get("sync") else ""))
            return answer

        return await self._run(params, work)

    def _checkout(self, repo: Repo, info: dict, env: dict, branch: str, base: str) -> dict:
        path = repo.path
        cloned = False
        if not path.exists() or (path.is_dir() and not any(path.iterdir())):
            gitops.clone(info["clone_url"], path, env, info["default_branch"], verify=repo.tls_verify)
            cloned = True
        elif not (path / ".git").exists():
            raise ForgeError(f"{path} exists and is no git clone -- forge never writes into it; set another path "
                             f"for repo {repo.name}")
        else:
            origin = gitops.origin_url(path)
            if not gitops.same_repo(origin, info["clone_url"]):
                raise ForgeError(f"{path} is a clone of {origin or 'no origin'}, not of {info['clone_url']} -- "
                                 f"forge leaves it alone")
            gitops.refuse_rewrites(info["clone_url"], env, path, verify=repo.tls_verify)
            gitops.fetch(path, info["clone_url"], env)
            unborn = gitops.current_branch(path)
            if unborn and not gitops.has_ref(path, "HEAD") and gitops.has_ref(path, f"refs/remotes/origin/{unborn}"):
                # A clone that stopped before its first checkout: finish it (review of the fix round).
                gitops.adopt(path, unborn, env)
        answer: dict[str, Any] = {"status": "success", "repo": repo.name, "path": str(path), "cloned": cloned,
                                  "default_branch": info["default_branch"]}
        current = gitops.current_branch(path)
        if branch and branch != current:
            changed = gitops.dirty(path)
            if changed:
                raise ForgeError(f"{path} has uncommitted changes on {current or 'a detached HEAD'} "
                                 f"({len(changed)} files) -- commit or stash them before switching to {branch}")
            if gitops.has_ref(path, f"refs/heads/{branch}"):
                gitops.switch(path, branch, env=env)
            elif gitops.has_ref(path, f"refs/remotes/origin/{branch}"):
                gitops.switch(path, branch, f"refs/remotes/origin/{branch}", env)
            else:
                if not branch.startswith(self.branch_prefix):
                    raise ForgeError(f"a new branch must start with {self.branch_prefix!r} (forge pushes only "
                                     f"there), e.g. {self.branch_prefix}{branch}")
                if not gitops.has_ref(path, f"refs/remotes/origin/{base}"):
                    raise ForgeError(f"base branch {base!r} does not exist on the platform")
                gitops.switch(path, branch, f"refs/remotes/origin/{base}", env)
                answer["created"] = True
            current = branch
        answer["branch"] = current
        if current and gitops.has_ref(path, f"refs/remotes/origin/{current}") and not gitops.dirty(path):
            answer["sync"] = gitops.sync(path, f"refs/remotes/origin/{current}", env)
            if answer["sync"] == "diverged":
                answer["note"] = (f"{current} and the platform's {current} both have commits the other lacks; "
                                  f"nothing was changed -- merge or rebase origin/{current} yourself")
        if gitops.has_ref(path, "HEAD"):
            answer["head"] = gitops.rev(path, "HEAD")[:12]
        else:
            answer["head"] = None
            answer["note"] = "no commits yet on this branch (an empty project?)"
        uncommitted = gitops.dirty(path)
        if uncommitted:
            answer["uncommitted"] = len(uncommitted)
        return answer

    async def push(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            branch = params.get("branch")
            if not isinstance(branch, str) or not gitops.valid_branch(branch):
                raise ForgeError("branch: the local branch to push")
            remote = params.get("remote_branch") or branch
            if not isinstance(remote, str) or not gitops.valid_branch(remote):
                raise ForgeError(f"remote_branch {_short(remote)!r} is no valid branch name")
            if not remote.startswith(self.branch_prefix):
                raise ForgeError(f"forge pushes only to branches under {self.branch_prefix!r}: pass "
                                 f"remote_branch={self.branch_prefix}{remote.split('/')[-1]} or similar")
            info = await backend.project_info()
            if remote == info["default_branch"]:
                raise ForgeError("never to the default branch: open a merge/pull request instead")
            if not (repo.path / ".git").exists():
                raise ForgeError(f"no clone at {repo.path} -- run checkout first")
            env = self._git_env(repo, backend, info)

            def run() -> tuple[str, list[str]]:
                if not gitops.same_repo(gitops.origin_url(repo.path), info["clone_url"]):
                    raise ForgeError(f"{repo.path} is no clone of {info['clone_url']}")
                if not gitops.has_ref(repo.path, f"refs/heads/{branch}"):
                    raise ForgeError(f"no local branch {branch} in {repo.path}")
                gitops.refuse_rewrites(info["clone_url"], env, repo.path, verify=repo.tls_verify)
                gitops.push(repo.path, info["clone_url"], branch, remote, env)
                uncommitted = gitops.dirty(repo.path) if gitops.current_branch(repo.path) == branch else []
                return gitops.rev(repo.path, f"refs/heads/{branch}"), uncommitted

            sha, uncommitted = await self._locked(repo, run)
            answer: dict[str, Any] = {"status": "success", "remote_branch": remote, "head": sha[:12]}
            if uncommitted:
                answer["note"] = f"{len(uncommitted)} files with uncommitted changes were not pushed"
            try:
                open_prs = await backend.prs(state="open", mine=False, source_branch=remote, limit=1)
            except ForgeError as exc:
                # The push stands; only the look for its request failed.
                answer["pr_lookup"] = f"pushed, but the open request of the branch could not be read: {exc}"
            else:
                if open_prs:
                    answer["pr"] = {k: open_prs[0][k] for k in ("number", "url", "draft")}
                else:
                    answer["next"] = f"pr_create with source_branch={remote}"
            await status.end(f"{repo.name}: pushed {_short(branch, 40)} -> {_short(remote, 50)} at {sha[:8]}")
            return answer

        return await self._run(params, work)

    # ── issues ────────────────────────────────────────────────────────────

    async def issue_list(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            state = params.get("state") or "open"
            scope = params.get("assigned") or "me"
            labels = _strings(params.get("labels"))
            limit = _bounded(params.get("limit"), 20, 1, 100)
            search = params.get("search") or ""
            if state not in ("open", "closed", "all") or scope not in ("me", "any") or labels is None \
                    or limit is None or not isinstance(search, str):
                raise ForgeError("state: open|closed|all, assigned: me|any, labels: list of strings, limit: 1-100")
            issues = await backend.issues(state=state, assigned_to_me=scope == "me", labels=labels,
                                          search=search.strip(), limit=limit)
            await status.end(f"{repo.name}: {len(issues)} {state} issues" + (" assigned to me" if scope == "me" else "")
                             + (f": #{', #'.join(str(i['number']) for i in issues[:6])}" if issues else ""))
            return {"status": "success", "count": len(issues), "issues": _untrusted(issues)}

        return await self._run(params, work)

    async def issue_get(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            number = self._need_number(params)
            comments = _bounded(params.get("comments"), 20, 0, 100)
            if comments is None:
                raise ForgeError("comments: 0-100")
            issue = await backend.issue(number, comments=comments)
            text = {"title": issue.pop("title"), "body": _cap(issue.pop("body"), CAP_BODY),
                    "comments": _cap_comments(issue.pop("comments"))}
            await status.end(f"{self._ref(repo, 'issue', number)} {issue['state']}: {_short(text['title'], 60)} "
                             f"({len(text['comments'])} comments)")
            return {"status": "success", "issue": issue, "text": _untrusted(text)}

        return await self._run(params, work)

    async def issue_comment(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            number = self._need_number(params)
            done = await backend.issue_comment(number, self._need_text(params, "body"))
            self._bind(params, repo, "issue", str(number))
            await status.end(f"{self._ref(repo, 'issue', number)}: comment {done.get('id')} posted")
            return {"status": "success", "comment_id": done.get("id")}

        return await self._run(params, work)

    async def issue_update(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            number = self._need_number(params)
            add, remove = _strings(params.get("add_labels")), _strings(params.get("remove_labels"))
            state = params.get("state") or ""
            if add is None or remove is None or state not in ("", "open", "closed"):
                raise ForgeError("add_labels/remove_labels: lists of strings, state: open|closed")
            if not (add or remove or state):
                raise ForgeError("nothing to change: give add_labels, remove_labels or state")
            issue = await backend.issue_update(number, add_labels=add, remove_labels=remove, state=state)
            await status.end(f"{self._ref(repo, 'issue', number)} {issue['state']}, labels "
                             f"{_short(', '.join(issue['labels']) or 'none', 60)}")
            return {"status": "success", "issue": {k: issue[k] for k in ("number", "state", "labels", "assignees", "url")}}

        return await self._run(params, work)

    # ── merge / pull requests ─────────────────────────────────────────────

    async def pr_list(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            state = params.get("state") or "open"
            limit = _bounded(params.get("limit"), 20, 1, 100)
            source = params.get("source_branch") or ""
            if state not in ("open", "closed", "merged", "all") or limit is None or not isinstance(source, str):
                raise ForgeError("state: open|closed|merged|all, limit: 1-100")
            prs = await backend.prs(state=state, mine=params.get("mine") is True, source_branch=source, limit=limit)
            await status.end(f"{repo.name}: {len(prs)} {state} requests"
                             + (f": {', '.join(str(p['number']) for p in prs[:8])}" if prs else ""))
            return {"status": "success", "count": len(prs), "prs": _untrusted(prs)}

        return await self._run(params, work)

    async def _pr_state(self, backend, number: int) -> dict:
        """A request with what a merge decision needs: CI of the request (on
        GitLab its head_pipeline, the one GitLab itself checks), open threads --
        a changes request counts as one; discussions the backend could not read
        (``left_out``) count as unknown, not as none."""
        pr = await backend.pr(number)
        threads = await backend.threads(number)
        pr["open_threads"] = sum(1 for t in threads if _is_open(t))
        unread = sum(int(t.get("left_out") or 0) for t in threads)
        if unread:
            pr["unread_threads"] = unread
        ci = await backend.ci(pr=number) if pr.get("head_sha") else None
        if ci is not None and ci.get("sha") != pr["head_sha"]:
            # The newest pipeline is an older head's: the head's has not appeared (yet).
            ci = None
        pr["ci"] = {"state": ci["state"], "sha": ci["sha"], "url": ci.get("url"),
                    "failed_jobs": [{"id": j["id"], "name": j["name"]} for j in ci["jobs"]
                                    if j["state"] == "failed" and not j["allow_failure"]][:10],
                    **{k: ci[k] for k in ("pipeline_sha", "note") if ci.get(k)}} \
            if ci else {"state": "none"}
        return pr

    async def pr_get(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            number = self._need_number(params)
            pr = await self._pr_state(backend, number)
            pr.pop("_diff_refs", None)
            text = {"title": pr.pop("title"), "body": _cap(pr.pop("body"), CAP_BODY)}
            await status.end(f"{self._ref(repo, 'pr', number)} {pr['state']}{' draft' if pr['draft'] else ''}, "
                             f"CI {pr['ci']['state']}, {pr['open_threads']} open threads")
            return {"status": "success", "pr": pr, "text": _untrusted(text)}

        return await self._run(params, work)

    async def pr_diff(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            number = self._need_number(params)
            offset = _bounded(params.get("offset"), 0, 0, 10_000)
            wanted = params.get("file") or ""
            if offset is None or not isinstance(wanted, str):
                raise ForgeError("offset: a file index from next_offset, file: a path from the list")
            files = await backend.pr_files(number, limit=3000)
            if wanted:
                match = [f for f in files if f["path"] == wanted or f.get("old_path") == wanted]
                if not match:
                    raise ForgeError(f"{wanted} is not among the {len(files)} changed files")
                diffs = [_diff_entry(match[0])]
                next_offset = None
            else:
                diffs, size, next_offset = [], 0, None
                for i, f in enumerate(files[offset:], start=offset):
                    if diffs and size + len(f["diff"]) > CAP_DIFF:
                        next_offset = i
                        break
                    diffs.append(_diff_entry(f))
                    size += len(f["diff"])
            answer: dict[str, Any] = {"status": "success", "files_total": len(files), "diffs": _untrusted(diffs),
                                      "next_offset": next_offset}
            if not wanted and offset == 0:
                # The file list only on the first page, and bounded: a request
                # touching thousands of files would fill the context with names.
                answer["files"] = [_file_entry(i, f) for i, f in enumerate(files[:MAX_FILES])]
                if len(files) > MAX_FILES:
                    answer["note"] = f"{len(files) - MAX_FILES} more files are not listed; offset pages through all diffs"
            await status.end(f"{self._ref(repo, 'pr', number)}: {len(files)} files, {len(diffs)} diffs shown"
                             + (f", more from offset {next_offset}" if next_offset is not None else ""))
            return answer

        return await self._run(params, work)

    async def pr_discussions(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            number = self._need_number(params)
            wanted = params.get("thread_id") or ""
            if not isinstance(wanted, str):
                raise ForgeError("thread_id: an id from this list")
            threads = await backend.threads(number)
            # What the backend could not read at all comes as a marker with a count.
            unread = sum(int(t.get("left_out") or 0) for t in threads)
            threads = [t for t in threads if not t.get("left_out")]
            open_only = params.get("unresolved_only", True) is not False
            if wanted:
                shown = [t for t in threads if str(t["id"]) == wanted]
                if not shown:
                    raise ForgeError(f"no thread or comment {_short(wanted, 60)!r} on this request")
            else:
                # The default leaves out resolved threads only. A plain comment
                # can never be resolved and may hold what blocks a merge ("wait
                # for the sign-off"): it follows the open threads, newest first
                # (review of the fix round).
                shown = [t for t in threads if _is_open(t)] + sorted(
                    (t for t in threads if not _is_open(t) and not (open_only and t.get("resolved"))),
                    key=lambda t: str((t["comments"][-1] if t["comments"] else {}).get("created") or ""),
                    reverse=True)
            budget, shortened, left_out = CAP_TEXT, 0, unread
            out: list[dict] = []
            for thread in shown:
                capped = {**thread, "comments": _cap_comments(thread["comments"])}
                size = sum(len(c.get("body") or "") for c in capped["comments"])
                if out and size > budget:
                    # Past the budget an entry still says who opened it with
                    # what: a hold must not vanish for size (review of the fix round).
                    if shortened < MAX_EXCERPTS:
                        out.append(_excerpt(thread))
                        shortened += 1
                    else:
                        left_out += 1
                    continue
                budget -= size
                out.append(capped)
            open_count = sum(1 for t in threads if _is_open(t))
            await status.end(f"{self._ref(repo, 'pr', number)}: {len(threads)} threads, {open_count} open, "
                             f"{len(out)} shown" + (f", {left_out} not listed" if left_out else ""))
            answer = {"status": "success", "open": open_count, "threads": _untrusted(out)}
            notes = []
            if shortened:
                notes.append(f"{shortened} entries shortened for size -- read one in full with thread_id")
            if left_out:
                answer["left_out"] = left_out
                notes.append(f"at least {left_out} entries not listed -- a hold among them would go unseen: "
                             f"do not merge on this listing, tell the user")
            if notes:
                answer["note"] = "; ".join(notes)
            return answer

        return await self._run(params, work)

    async def pr_create(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            source = params.get("source_branch")
            if not isinstance(source, str) or not gitops.valid_branch(source):
                raise ForgeError("source_branch: the pushed branch")
            title = self._need_text(params, "title", 250)
            body = params.get("description") or ""
            target = params.get("target_branch") or ""
            closes = params.get("closes_issue")
            if not isinstance(body, str) or not isinstance(target, str) or len(body) > 60_000:
                raise ForgeError("description and target_branch must be text")
            if closes not in (None, ""):
                number = _number(closes)
                if number is None:
                    raise ForgeError("closes_issue: an issue number")
                # Once only: the model often writes it itself (measured in the E2E run).
                if not re.search(rf"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#{number}\b", body):
                    body = f"{body.rstrip()}\n\nCloses #{number}".lstrip()
            existing = await backend.prs(state="open", mine=False, source_branch=source, limit=1)
            if existing:
                pr = existing[0]
                self._bind_request(params, repo, pr["number"], source, closes)
                await status.end(f"{self._ref(repo, 'pr', pr['number'])} exists already for {_short(source, 60)}")
                return {"status": "success", "created": False, "pr": pr,
                        "note": "an open request for this branch exists; pr_update changes it"}
            if not await backend.branch_exists(source):
                raise ForgeError(f"{source} is not on the platform -- push it first")
            target = target or (await backend.project_info())["default_branch"]
            pr = await backend.pr_create(source=source, target=target, title=title, body=body,
                                         draft=params.get("draft") is True)
            self._bind_request(params, repo, pr["number"], source, closes)
            await status.end(f"{self._ref(repo, 'pr', pr['number'])} created: {_short(source, 40)} -> {target}")
            return {"status": "success", "created": True, "pr": pr}

        return await self._run(params, work)

    def _bind_request(self, params: dict, repo: Repo, number: Any, source: str, closes: Any) -> None:
        self._bind(params, repo, "mr", str(number))
        if source.startswith(self.branch_prefix):
            self._bind(params, repo, "branch", source)
        if _number(closes) is not None:
            self._bind(params, repo, "issue", str(_number(closes)))

    async def pr_update(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            number = self._need_number(params)
            title, body, draft = params.get("title"), params.get("description"), params.get("draft")
            if (title is not None and (not isinstance(title, str) or not title.strip())) or \
                    (body is not None and not isinstance(body, str)) or (draft is not None and not isinstance(draft, bool)):
                raise ForgeError("title: text, description: text, draft: true|false")
            if title is None and body is None and draft is None:
                raise ForgeError("nothing to change: give title, description or draft")
            pr = await backend.pr_update(number, title=title, body=body, draft=draft)
            await status.end(f"{self._ref(repo, 'pr', number)} updated: {'draft' if pr['draft'] else 'ready'}, "
                             f"{_short(pr['title'], 60)}")
            return {"status": "success", "pr": pr}

        return await self._run(params, work)

    async def pr_comment(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            number = self._need_number(params)
            thread = params.get("thread_id") or ""
            resolve = params.get("resolve")
            file, line = params.get("file") or "", params.get("line")
            body = params.get("body")
            if not isinstance(thread, str) or not isinstance(file, str) or (resolve is not None and not isinstance(resolve, bool)):
                raise ForgeError("thread_id and file are text, resolve is true|false")
            if body is not None and (not isinstance(body, str) or len(body) > 60_000):
                raise ForgeError("body: text up to 60000 characters")
            ref = self._ref(repo, "pr", number)
            self._bind(params, repo, "mr", str(number))       # a conversation it takes part in
            if thread:
                if file or line is not None:
                    raise ForgeError("a reply goes into its thread: give thread_id or file+line, not both")
                if not body and resolve is None:
                    raise ForgeError("give body (reply), resolve, or both")
                # Resolve first: it is what can be refused (not a thread), and a
                # refusal must not leave a posted reply that a retry posts again.
                if resolve is not None:
                    await backend.thread_resolve(number, thread, resolve)
                try:
                    done = await backend.thread_reply(number, thread, body) if body else {}
                except ForgeError as exc:
                    if resolve is None:
                        raise
                    raise ForgeError(f"the thread was {'resolved' if resolve else 'reopened'}, but the reply failed: "
                                     f"{exc} -- send the reply alone") from None
                what = " and ".join(w for w in ("replied" if body else "", "resolved" if resolve else
                                                "reopened" if resolve is False else "") if w)
                await status.end(f"{ref}: thread {_short(thread, 30)} {what}")
                return {"status": "success", "thread_id": thread, "comment_id": done.get("id"), "resolved": resolve}
            if resolve is not None:
                raise ForgeError("resolve needs thread_id")
            if not body or not body.strip():
                raise ForgeError("body is required")
            if file or line is not None:
                line_no = _number(line)
                if not file or line_no is None:
                    raise ForgeError("a line comment needs file and line (a line of the new version)")
                pr = await backend.pr(number)
                done = await backend.line_comment(number, path=file, line=line_no, body=body, pr=pr)
                await status.end(f"{ref}: comment on {_short(file, 60)}:{line_no}")
                answer = {"status": "success", "thread_id": done.get("thread_id"), "comment_id": done.get("comment_id")}
                if not done.get("thread_id"):
                    answer["note"] = "its thread id shows in pr_discussions"
                return answer
            done = await backend.pr_comment(number, body)
            await status.end(f"{ref}: comment {done.get('id')} posted")
            return {"status": "success", "comment_id": done.get("id")}

        return await self._run(params, work)

    async def pr_merge(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            number = self._need_number(params)
            ref = self._ref(repo, "pr", number)
            if not repo.allow_merge:
                raise ForgeError(f"merging is not enabled for repo {repo.name} (allow_merge) -- leave it to a person")
            expected = params.get("sha")
            method = params.get("method") or self.merge_method
            # The framework does not enforce "required": without the head the
            # caller reviewed, whatever was pushed since would be merged.
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", expected):
                raise ForgeError("sha is required: the head commit you reviewed (head_sha from pr_get), "
                                 "at least 7 hex digits")
            if method not in ("merge", "squash", "rebase"):
                raise ForgeError("method: merge|squash|rebase")
            pr = await self._pr_state(backend, number)
            refusals = []
            if pr["state"] != "open":
                refusals.append(f"it is {pr['state']}")
            if pr["draft"]:
                refusals.append("it is a draft (pr_update draft=false when it is ready)")
            if not str(pr["head_sha"] or "").lower().startswith(expected.lower()):
                refusals.append(f"its head is {str(pr['head_sha'])[:12]}, not the {expected[:12]} you checked")
            if pr["open_threads"]:
                refusals.append(f"{pr['open_threads']} threads are open")
            if pr.get("unread_threads"):
                # Past what the platform lists, a hold or a changes request
                # would go unseen (review of the fix round).
                refusals.append(f"at least {pr['unread_threads']} discussions could not be read -- a person merges")
            ci = pr["ci"]
            if repo.require_ci and ci["state"] != "success":
                refusals.append(f"CI on the head is {ci['state']}" + (" (ci_status wait_s waits for it)"
                                                                      if ci["state"] in _WAITING else ""))
            if pr["mergeable"] is None:
                refusals.append("the platform is still checking mergeability -- ask again in a few seconds")
            elif pr["merge_blockers"]:
                refusals.append("the platform says: " + ", ".join(pr["merge_blockers"]))
            if refusals:
                raise ForgeError(f"{ref} not merged: " + "; ".join(refusals))
            # Only forge's own branches go: a long-lived source (develop -> main)
            # is someone's branch, and a squash would leave its commits nowhere.
            delete = self.delete_branch and str(pr.get("source_branch") or "").startswith(self.branch_prefix)
            done = await backend.merge(number, sha=pr["head_sha"], method=method, delete_branch=delete)
            if done["state"] != "merged":
                raise ForgeError(f"{ref}: the platform accepted the merge but reports {done['state']} -- check it")
            await status.end(f"{ref} merged ({method}) at {str(done.get('merge_commit') or '')[:8]}")
            return {"status": "success", "merged": True, "merge_commit": done.get("merge_commit"),
                    "head_sha": pr["head_sha"], "url": pr.get("url")}

        return await self._run(params, work)

    # ── CI ────────────────────────────────────────────────────────────────

    async def ci_status(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            given = {k: params.get(k) for k in ("pr", "branch", "sha") if params.get(k) not in (None, "")}
            wait_s = _bounded(params.get("wait_s"), 0, 0, MAX_WAIT_S)
            if len(given) != 1 or wait_s is None:
                raise ForgeError("give exactly one of pr, branch, sha; wait_s: 0-600")
            kind, value = next(iter(given.items()))
            head = None
            if kind == "pr":
                number = _number(value)
                if number is None:
                    raise ForgeError("pr: a request number")
                # The request's own CI -- on GitLab its head_pipeline, which is
                # what GitLab checks -- held against its head: a pipeline of an
                # older head means the new one has not appeared yet.
                head = (await backend.pr(number))["head_sha"]
                target: dict[str, Any] = {"pr": number}
                what = self._ref(repo, "pr", number)
            elif not isinstance(value, str) or (kind == "branch" and not gitops.valid_branch(value)) or \
                    (kind == "sha" and not re.fullmatch(r"[0-9a-fA-F]{7,64}", value)):
                raise ForgeError(f"{kind} {_short(value)!r} is not valid")
            else:
                target = {"ref": value} if kind == "branch" else {"sha": value}
                what = f"{repo.name} {_short(value, 40)}"
            token = params.get("_cancellation_token")
            started = time.monotonic()
            deadline = started + wait_s
            delay = 5.0
            while True:
                ci = await backend.ci(**target)
                if ci is not None and head and ci.get("sha") != head:
                    ci = None
                state = ci["state"] if ci else "none"
                # Right after a push the pipeline does not exist yet (F-CI1):
                # "none" is waited out for a while, then taken as "no CI".
                appearing = state == "none" and time.monotonic() - started < NONE_GRACE_S
                if (state not in _WAITING and not appearing) or time.monotonic() + delay > deadline:
                    break
                if token and token.is_cancelled:
                    return {"error": "Tool 'ci_status' was cancelled.", "cancelled": True,
                            "forced": bool(getattr(token, "is_forced", False))}
                await status.progress(f"{what}: CI {state}, waiting")
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 30.0)
            if ci is None:
                await status.end(f"{what}: no pipeline or checks" + (f" for head {head[:8]}" if head else ""))
                return {"status": "success", "state": "none", **({"sha": head} if head else {}),
                        "note": "no pipeline or check ran for it (yet) -- just pushed? ask again with wait_s"}
            jobs = sorted(ci["jobs"], key=lambda j: (j["state"] != "failed", j["state"] not in _WAITING))
            failed = [j["name"] for j in ci["jobs"] if j["state"] == "failed" and not j["allow_failure"]]
            # pipeline_sha: the commit a GitLab pipeline really ran on (a merged
            # result); note: what the platform does not show (a fork's jobs).
            answer = {"status": "success", **{k: ci[k] for k in ("id", "state", "sha", "ref", "url")},
                      **{k: ci[k] for k in ("pipeline_sha", "note") if ci.get(k)}, "jobs": jobs[:MAX_JOBS]}
            if len(jobs) > MAX_JOBS:
                answer["note"] = "; ".join(filter(None, [answer.get("note"), f"{len(jobs) - MAX_JOBS} more jobs "
                                                         f"left out (failed and running come first)"]))
            if state in _WAITING and wait_s:
                answer["still_waiting"] = f"not finished after {wait_s} s -- ask again with wait_s"
            await status.end(f"{what}: CI {state}, {len(ci['jobs'])} jobs"
                             + (f", failed: {_short(', '.join(failed), 60)}" if failed else ""))
            return answer

        return await self._run(params, work)

    async def ci_job_log(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            job_id = self._need_number(params, "job_id")
            tail = _bounded(params.get("tail_lines"), 150, 10, 2000)
            grep = params.get("grep") or ""
            if tail is None or not isinstance(grep, str):
                raise ForgeError("tail_lines: 10-2000, grep: text")
            try:
                lines = clean_log(await backend.job_log(job_id))
            except ForgeNotFound:
                raise ForgeError(f"job {job_id} has no log here -- a check of another CI system, or a wrong id "
                                 f"(ids come from ci_status)") from None
            numbered = [(i + 1, line) for i, line in enumerate(lines)]
            if grep:
                needle = grep.lower()
                numbered = [(n, line) for n, line in numbered if needle in line.lower()]
            shown = numbered[-tail:]
            text = "\n".join(f"{n}: {line}" for n, line in shown)
            if len(text) > CAP_LOG:
                text = "…\n" + text[-CAP_LOG:]
            await status.end(f"{repo.name} job {job_id}: {len(lines)} lines, {len(shown)} shown"
                             + (f" matching {_short(grep, 30)!r}" if grep else ""))
            return {"status": "success", "job_id": job_id, "lines_total": len(lines),
                    "lines_matching": len(numbered) if grep else None, "log": _untrusted(text)}

        return await self._run(params, work)

    async def ci_retry(self, params: dict[str, Any]) -> dict[str, Any]:
        async def work(repo: Repo, backend, status):
            job_id = self._need_number(params, "job_id")
            job = await backend.retry(job_id)
            note = job.pop("note", None) or ("the retry is a new job with a new id" if job["id"] != job_id else None)
            await status.end(f"{repo.name} job {job_id} retried: {_short(job.get('name'), 40)} "
                             + (f"as job {job['id']}" if job["id"] else "as a new attempt"))
            return {"status": "success", "job": job, "note": note}

        return await self._run(params, work)


def _is_open(thread: dict) -> bool:
    """An unresolved review thread, or a changes request its reviewer has not
    taken back -- both hold a merge."""
    return bool(thread.get("blocking")) or bool(thread["resolvable"] and not thread["resolved"])


def _diff_entry(f: dict) -> dict:
    entry = {"path": f["path"], "diff": _cap(f["diff"], CAP_DIFF)}
    if len(f["diff"]) > CAP_DIFF:
        entry["note"] = "cut -- the rest of this file's diff is not shown here; read the file in a checkout"
    return entry


def _file_entry(index: int, f: dict) -> dict:
    lines = f["diff"].splitlines()
    return {"index": index, "path": f["path"], "status": f["status"],
            **({"old_path": f["old_path"]} if f.get("old_path") else {}),
            "added": sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++")),
            "removed": sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---")),
            **({"note": f["note"]} if f.get("note") else {})}


def _excerpt(thread: dict) -> dict:
    """An entry past the text budget: where it is, who opened it with what, how much follows."""
    comments = thread.get("comments") or []
    first = comments[0] if comments else {}
    return {**{k: thread[k] for k in ("id", "resolvable", "resolved", "blocking", "file", "line") if k in thread},
            "shortened": True, "replies": max(0, len(comments) - 1),
            "comments": [{"id": first.get("id"), "author": first.get("author"), "created": first.get("created"),
                          "body": _cap(first.get("body") or "", 200)}]}


def _cap_comments(comments: list[dict]) -> list[dict]:
    """Newest comments kept whole-ish; older ones dropped once the budget is spent."""
    out: list[dict] = []
    budget = CAP_TEXT
    for comment in reversed(comments):
        body = _cap(comment.get("body") or "", CAP_COMMENT)
        if out and len(body) > budget:
            out.append({"note": "older comments left out for size"})
            break
        budget -= len(body)
        out.append({**comment, "body": body})
    return list(reversed(out))
