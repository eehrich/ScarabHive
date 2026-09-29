"""GitLab REST v4, read into the plugin's common shapes (docs/konzept.md §2).

A merge request is a "pr" here; ``number`` is its project-scoped ``iid``, the
number people write as !12 -- never the global ``id``.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional
from urllib.parse import quote

from .http import Api, ForgeError, ForgeNotFound, ForgeUnavailable

BRANCH_SETTLE_TRIES = 10
BRANCH_SETTLE_PAUSE_S = 1.0
MAX_DISCUSSIONS = 1000     # a request's discussions read; system notes (one per push) count too
_HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# GitLab's job and pipeline states -> the common ones.
_STATE = {
    "created": "pending", "waiting_for_resource": "pending", "preparing": "pending", "pending": "pending",
    "scheduled": "pending", "running": "running", "canceling": "running", "success": "success",
    "failed": "failed", "canceled": "canceled", "skipped": "skipped", "manual": "manual",
}
# detailed_merge_status values that are no blocker, and those that only mean "ask again".
_MERGEABLE = {"mergeable"}
_CHECKING = {"checking", "unchecked", "approvals_syncing", "preparing"}


def ci_state(value: Any) -> str:
    return _STATE.get(str(value or ""), str(value or "unknown"))


class GitLab:
    provider = "gitlab"
    git_user = "oauth2"             # Basic auth over HTTPS takes any user name with a token; this one is GitLab's own

    def __init__(self, api: Api, project: str) -> None:
        self.api = api
        self.project = project
        self._p = f"/projects/{quote(project, safe='')}"
        self._me: Optional[dict] = None
        self._id: Optional[int] = None

    async def me(self) -> dict:
        if self._me is None:
            user = await self.api.request("GET", "/user")
            self._me = {"id": user["id"], "login": user["username"]}
        return self._me

    async def project_info(self) -> dict:
        project = await self.api.request("GET", self._p)
        return {"default_branch": project.get("default_branch") or "", "clone_url": project.get("http_url_to_repo") or "",
                "web_url": project.get("web_url") or ""}

    # ── issues ────────────────────────────────────────────────────────────

    async def issues(self, *, state: str, assigned_to_me: bool, labels: list[str], search: str, limit: int) -> list:
        params: dict[str, Any] = {"state": {"open": "opened", "closed": "closed"}.get(state, "all"),
                                  "order_by": "updated_at", "sort": "desc"}
        if assigned_to_me:
            params["assignee_username"] = (await self.me())["login"]
        if labels:
            params["labels"] = ",".join(labels)
        if search:
            params["search"] = search
        return [_issue(i) for i in await self.api.get_all(f"{self._p}/issues", params, limit=limit)]

    async def issue(self, number: int, *, comments: int) -> dict:
        raw = await self.api.request("GET", f"{self._p}/issues/{number}")
        issue = _issue(raw)
        issue["body"] = raw.get("description") or ""
        notes = await self.api.get_all(f"{self._p}/issues/{number}/notes",
                                       {"sort": "desc", "order_by": "created_at"}, limit=100)
        human = [n for n in notes if not n.get("system")]
        issue["comments"] = [_note(n) for n in reversed(human[:comments])]
        issue["comments_left_out"] = max(0, len(human) - comments)
        related = await self.api.get_all(f"{self._p}/issues/{number}/related_merge_requests", limit=20)
        issue["related_prs"] = [{"number": m.get("iid"), "title": m.get("title"), "state": _pr_state(m),
                                 "url": m.get("web_url")} for m in related]
        return issue

    async def issue_comment(self, number: int, body: str) -> dict:
        note = await self.api.request("POST", f"{self._p}/issues/{number}/notes", json={"body": body})
        return {"id": note.get("id")}

    async def issue_update(self, number: int, *, add_labels: list[str], remove_labels: list[str],
                           state: str) -> dict:
        body: dict[str, Any] = {}
        if add_labels:
            body["add_labels"] = ",".join(add_labels)
        if remove_labels:
            body["remove_labels"] = ",".join(remove_labels)
        if state:
            body["state_event"] = {"closed": "close", "open": "reopen"}[state]
        return _issue(await self.api.request("PUT", f"{self._p}/issues/{number}", json=body))

    # ── merge requests ────────────────────────────────────────────────────

    async def prs(self, *, state: str, mine: bool, source_branch: str, limit: int) -> list:
        params: dict[str, Any] = {"state": {"open": "opened", "closed": "closed", "merged": "merged"}.get(state, "all"),
                                  "order_by": "updated_at", "sort": "desc"}
        if mine:
            params["author_username"] = (await self.me())["login"]
        if source_branch:
            params["source_branch"] = source_branch
        return [_pr(m) for m in await self.api.get_all(f"{self._p}/merge_requests", params, limit=limit)]

    async def pr(self, number: int) -> dict:
        raw = await self.api.request("GET", f"{self._p}/merge_requests/{number}")
        pr = _pr(raw)
        pr["body"] = raw.get("description") or ""
        status = str(raw.get("detailed_merge_status") or "")
        pr["merge_status"] = status
        pr["mergeable"] = None if status in _CHECKING else status in _MERGEABLE
        pr["merge_blockers"] = [] if status in _MERGEABLE | _CHECKING else [status.replace("_", " ") or "unknown"]
        pipeline = raw.get("head_pipeline") or {}
        pr["ci"] = ({"state": ci_state(pipeline.get("status")), "sha": pipeline.get("sha"), "url": pipeline.get("web_url")}
                    if pipeline else None)
        pr["changes"] = raw.get("changes_count")
        pr["_diff_refs"] = raw.get("diff_refs") or {}
        return pr

    async def pr_files(self, number: int, *, limit: int) -> list:
        return [{"path": d.get("new_path"), "old_path": d.get("old_path") if d.get("renamed_file") else None,
                 "status": "added" if d.get("new_file") else "removed" if d.get("deleted_file")
                 else "renamed" if d.get("renamed_file") else "modified",
                 "diff": d.get("diff") or "",
                 "note": "too large for GitLab to show" if d.get("too_large") or d.get("collapsed") else None}
                for d in await self.api.get_all(f"{self._p}/merge_requests/{number}/diffs", limit=limit)]

    async def threads(self, number: int) -> list:
        threads: list[dict] = []
        # One more than kept: only then is it known that some were cut.
        discussions = await self.api.get_all(f"{self._p}/merge_requests/{number}/discussions",
                                             limit=MAX_DISCUSSIONS + 1)
        for d in discussions[:MAX_DISCUSSIONS]:
            notes = [n for n in d.get("notes") or [] if not n.get("system")]
            if not notes:
                continue
            first = notes[0]
            position = first.get("position") or {}
            threads.append({
                "id": d.get("id"),
                "resolvable": bool(first.get("resolvable")),
                "resolved": bool(first.get("resolved")) if first.get("resolvable") else None,
                "file": position.get("new_path") or position.get("old_path"),
                "line": position.get("new_line") or position.get("old_line"),
                "comments": [_note(n) for n in notes],
            })
        if len(discussions) > MAX_DISCUSSIONS:
            # Oldest first: what is cut off are the newest (review of the fix round).
            threads.append({"id": "not-read", "left_out": 1, "resolvable": False, "resolved": None, "comments": []})
        return threads

    async def pr_create(self, *, source: str, target: str, title: str, body: str, draft: bool) -> dict:
        """Right after a push GitLab may still refuse the branch as missing
        although the branch API already shows it (facts.md F-GL12): that one
        refusal is waited out for a few seconds."""
        payload = {"source_branch": source, "target_branch": target, "description": body,
                   "title": _draft_title(title, draft)}
        for attempt in range(BRANCH_SETTLE_TRIES):
            try:
                return _pr(await self.api.request("POST", f"{self._p}/merge_requests", json=payload))
            except ForgeError as exc:
                if "source_branch: does not exist" not in str(exc) or attempt == BRANCH_SETTLE_TRIES - 1:
                    raise
            await asyncio.sleep(BRANCH_SETTLE_PAUSE_S)
        raise ForgeError("unreachable")

    async def pr_update(self, number: int, *, title: Optional[str], body: Optional[str], draft: Optional[bool]) -> dict:
        update: dict[str, Any] = {}
        if body is not None:
            update["description"] = body
        if title is not None or draft is not None:
            current = title
            if current is None or draft is None:
                raw = await self.api.request("GET", f"{self._p}/merge_requests/{number}")
                current = title if title is not None else _plain_title(str(raw.get("title") or ""))
                draft = bool(raw.get("draft")) if draft is None else draft
            update["title"] = _draft_title(current, draft)
        return _pr(await self.api.request("PUT", f"{self._p}/merge_requests/{number}", json=update))

    async def pr_comment(self, number: int, body: str) -> dict:
        note = await self.api.request("POST", f"{self._p}/merge_requests/{number}/notes", json={"body": body})
        return {"id": note.get("id")}

    async def thread_reply(self, number: int, thread_id: str, body: str) -> dict:
        note = await self.api.request("POST", f"{self._p}/merge_requests/{number}/discussions/{quote(thread_id, safe='')}"
                                              f"/notes", json={"body": body})
        return {"id": note.get("id")}

    async def thread_resolve(self, number: int, thread_id: str, resolved: bool) -> None:
        await self.api.request("PUT", f"{self._p}/merge_requests/{number}/discussions/{quote(thread_id, safe='')}",
                               json={"resolved": resolved})

    async def line_comment(self, number: int, *, path: str, line: int, body: str, pr: dict) -> dict:
        """A thread on ``line`` of the new version. GitLab places an unchanged
        line by both its old and new number, an added one by its new number
        only, and a renamed file under both paths -- read from the diff."""
        refs = pr.get("_diff_refs") or {}
        if not refs.get("head_sha"):
            raise ForgeError(f"!{number} has no diff to comment on")
        files = [f for f in await self.pr_files(number, limit=3000) if f["path"] == path]
        if not files:
            raise ForgeError(f"{path} is not among the files !{number} changes -- a comment needs a changed file "
                             f"(just pushed? GitLab updates the request's diff a moment later, facts.md F-GL11)")
        old_line = old_line_of(files[0]["diff"], line)
        position = {"position_type": "text", "base_sha": refs.get("base_sha"), "start_sha": refs.get("start_sha"),
                    "head_sha": refs.get("head_sha"), "new_path": path, "old_path": files[0]["old_path"] or path,
                    "new_line": line}
        if old_line is not None:
            position["old_line"] = old_line
        discussion = await self.api.request("POST", f"{self._p}/merge_requests/{number}/discussions",
                                            json={"body": body, "position": position})
        notes = discussion.get("notes") or [{}]
        return {"thread_id": discussion.get("id"), "comment_id": notes[0].get("id")}

    async def merge(self, number: int, *, sha: str, method: str, delete_branch: bool) -> dict:
        if method == "rebase":
            # The merge API has no method field: merge commit, rebase or
            # fast-forward is the project's setting; only squashing is per request.
            raise ForgeError("GitLab takes rebase or fast-forward from the project's merge method setting -- "
                             "pass method merge or squash")
        merged = await self.api.request("PUT", f"{self._p}/merge_requests/{number}/merge", json={
            "sha": sha, "squash": method == "squash", "should_remove_source_branch": delete_branch})
        return {"state": _pr_state(merged), "merge_commit": merged.get("merge_commit_sha") or merged.get("squash_commit_sha")}

    # ── CI ────────────────────────────────────────────────────────────────

    async def ci(self, *, sha: str = "", ref: str = "", pr: Optional[int] = None) -> Optional[dict]:
        """The pipeline of a merge request's head, or the newest of a branch or
        a commit, with its jobs.

        For a merge request it is GitLab's own ``head_pipeline`` -- the one
        GitLab checks before a merge. A list of the request's pipelines would
        be wrong twice: a merged-results pipeline carries the merge commit as
        its ``sha``, not the head, and a request from a fork runs its pipeline
        in the fork, whose jobs the target project does not know (fix-round
        review). Its ``sha`` here is therefore the head it stands for;
        ``pipeline_sha`` names the commit it really ran on when that differs."""
        head = None
        if pr is not None:
            mr = await self.api.request("GET", f"{self._p}/merge_requests/{pr}")
            pipeline, head = mr.get("head_pipeline"), mr.get("sha")
        else:
            params = {"order_by": "id", "sort": "desc", **({"sha": sha} if sha else {"ref": ref})}
            pipelines = await self.api.get_all(f"{self._p}/pipelines", params, limit=1)
            pipeline = pipelines[0] if pipelines else None
        if not pipeline:
            return None
        project_id = pipeline.get("project_id")
        own = project_id is None or project_id == await self._project_id()
        where = self._p if own else f"/projects/{project_id}"
        hidden = None
        try:
            jobs = await self.api.get_all(f"{where}/pipelines/{pipeline['id']}/jobs", limit=200)
            bridges = await self.api.get_all(f"{where}/pipelines/{pipeline['id']}/bridges", limit=50)
        except ForgeError as exc:
            if own or isinstance(exc, ForgeUnavailable):
                raise
            # A private fork the bot is no member of (404), or one that shows
            # CI to members only (403): its pipeline's state is on the
            # request, its jobs are not readable (reviews of the fix rounds).
            jobs, bridges = [], []
            hidden = "the pipeline runs in the fork's project, which this token cannot read -- its jobs are not listed"
        foreign = {} if own else {"has_log": False, "note": "a job in the fork's project -- its log is there"}
        answer = {
            "id": pipeline.get("id"), "state": ci_state(pipeline.get("status")), "sha": head or pipeline.get("sha"),
            "ref": pipeline.get("ref"), "url": pipeline.get("web_url"),
            "jobs": [{**_job(j), **foreign} for j in sorted(jobs, key=lambda j: j.get("id") or 0)]
                    + [{**_job(b), "has_log": False, "note": "downstream pipeline -- its jobs are not listed here"}
                       for b in bridges],
        }
        if head and pipeline.get("sha") and pipeline["sha"] != head:
            answer["pipeline_sha"] = pipeline["sha"]
        if hidden:
            answer["note"] = hidden
        return answer

    async def _project_id(self) -> Optional[int]:
        if self._id is None:
            self._id = (await self.api.request("GET", self._p)).get("id")
        return self._id

    async def job_log(self, job_id: int) -> str:
        return await self.api.request("GET", f"{self._p}/jobs/{job_id}/trace", text=True)

    async def job(self, job_id: int) -> dict:
        return _job(await self.api.request("GET", f"{self._p}/jobs/{job_id}"))

    async def retry(self, job_id: int) -> dict:
        return _job(await self.api.request("POST", f"{self._p}/jobs/{job_id}/retry"))

    async def branch_exists(self, branch: str) -> bool:
        try:
            await self.api.request("GET", f"{self._p}/repository/branches/{quote(branch, safe='')}")
        except ForgeNotFound:
            return False
        return True


def old_line_of(diff: str, new_line: int) -> Optional[int]:
    """The old number of ``new_line`` in a unified diff, or None when the
    line was added. Lines outside every hunk are unchanged and shifted by
    what the hunks before them added or removed."""
    delta = 0                                   # old number minus new number
    old = new = 0
    for text in diff.splitlines():
        hunk = _HUNK.match(text)
        if hunk:
            old, new = int(hunk.group(1)), int(hunk.group(2))
            if new_line < new:
                return new_line + delta
            delta = old - new
            continue
        if not new:
            continue
        if text.startswith("+"):
            if new == new_line:
                return None
            new += 1
        elif text.startswith("-"):
            old += 1
        elif not text.startswith("\\"):
            if new == new_line:
                return old
            old += 1
            new += 1
        delta = old - new
    return new_line + delta


def _issue(raw: dict) -> dict:
    return {"number": raw.get("iid"), "title": raw.get("title"),
            "state": "open" if raw.get("state") == "opened" else raw.get("state"),
            "labels": raw.get("labels") or [], "assignees": [a.get("username") for a in raw.get("assignees") or []],
            "author": (raw.get("author") or {}).get("username"), "updated": raw.get("updated_at"),
            "comments_count": raw.get("user_notes_count"), "url": raw.get("web_url")}


def _pr_state(raw: dict) -> str:
    state = str(raw.get("state") or "unknown")
    return "open" if state in ("opened", "locked") else state


def _pr(raw: dict) -> dict:
    return {"number": raw.get("iid"), "title": raw.get("title"), "state": _pr_state(raw),
            "draft": bool(raw.get("draft") or raw.get("work_in_progress")),
            "source_branch": raw.get("source_branch"), "target_branch": raw.get("target_branch"),
            "head_sha": raw.get("sha"), "author": (raw.get("author") or {}).get("username"),
            "updated": raw.get("updated_at"), "url": raw.get("web_url"),
            "foreign_source": raw.get("source_project_id") != raw.get("target_project_id")
            if raw.get("source_project_id") and raw.get("target_project_id") else False}


def _note(raw: dict) -> dict:
    return {"id": raw.get("id"), "author": (raw.get("author") or {}).get("username"),
            "created": raw.get("created_at"), "body": raw.get("body") or ""}


def _job(raw: dict) -> dict:
    return {"id": raw.get("id"), "name": raw.get("name"), "stage": raw.get("stage"),
            "state": ci_state(raw.get("status")), "allow_failure": bool(raw.get("allow_failure")),
            "url": raw.get("web_url"), "has_log": True}


def _plain_title(title: str) -> str:
    for prefix in ("Draft:", "[Draft]", "(Draft)", "WIP:", "[WIP]"):
        if title[:len(prefix)].lower() == prefix.lower():
            return title[len(prefix):].lstrip()
    return title


def _draft_title(title: str, draft: bool) -> str:
    plain = _plain_title(title)
    return f"Draft: {plain}" if draft else plain
