"""GitHub REST (and GraphQL for review threads), read into the plugin's common
shapes (docs/concept.md §2).

Differences to GitLab that shape this module:

* A pull request's comments come in three kinds: conversation comments,
  inline review comments in threads, and review summaries. Only threads can be
  resolved, and only over GraphQL; the other two are listed as threads that
  cannot be resolved.
* CI is the sum of check runs (GitHub Actions and any other app) and the
  older commit statuses on the head commit. A check run of GitHub Actions is
  also an Actions job with the same id -- only those have a log here.
* Draft is switched over GraphQL; the REST update has no field for it.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from .http import Api, ForgeError, ForgeNotFound

_THREADS = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id isResolved path line originalLine
          comments(first: 50) { nodes { databaseId body createdAt author { login } } } }
      }
    }
  }
}"""
_PR_NODE = "query($owner: String!, $name: String!, $number: Int!) " \
           "{ repository(owner: $owner, name: $name) { pullRequest(number: $number) { id } } }"
_REPLY = "mutation($id: ID!, $body: String!) " \
         "{ addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $id, body: $body}) { comment { databaseId } } }"
_RESOLVE = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"
_UNRESOLVE = "mutation($id: ID!) { unresolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"
_READY = "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
_TO_DRAFT = "mutation($id: ID!) { convertPullRequestToDraft(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"

MAX_REVIEWS = 1000          # a pull request's reviews read (each single line comment is one)
_FAILED = {"failure", "timed_out", "action_required", "startup_failure", "stale", "error"}
# mergeable_state values that block a merge; "unstable" (a check that is not
# required failed) and "has_hooks" do not. "behind" is left to GitHub: where a
# rule demands an up-to-date branch it refuses the merge itself, and its 405
# text reaches the model.
_BLOCKING = {"blocked": "blocked by a branch rule (required reviews or checks)", "dirty": "conflict",
             "draft": "draft"}


def check_state(status: Any, conclusion: Any) -> str:
    """A check run's status/conclusion, or a commit status's state, -> the common state."""
    status, conclusion = str(status or ""), str(conclusion or "")
    if status in ("queued", "requested", "waiting", "pending"):
        return "pending"
    if status == "in_progress":
        return "running"
    if conclusion in ("success", "neutral"):
        return "success"
    if conclusion == "skipped":
        return "skipped"
    if conclusion == "cancelled":
        return "canceled"
    if conclusion in _FAILED:
        return "failed"
    return conclusion or status or "unknown"


def overall(states: list[str]) -> str:
    if not states:
        return "none"
    if "failed" in states:
        return "failed"
    for waiting in ("running", "pending"):
        if waiting in states:
            return waiting
    if "canceled" in states:
        return "canceled"
    return "success" if all(s in ("success", "skipped") for s in states) else "unknown"


def graphql_url(api_url: str) -> str:
    """api.github.com/graphql; on GitHub Enterprise Server /api/v3 -> /api/graphql."""
    base = api_url.rstrip("/")
    return base[:-len("/v3")] + "/graphql" if base.endswith("/api/v3") else base + "/graphql"


class GitHub:
    provider = "github"
    git_user = "x-access-token"     # Basic auth over HTTPS: fine-grained, classic and app tokens all take it

    def __init__(self, api: Api, project: str) -> None:
        self.api = api
        self.project = project
        owner, _, name = project.partition("/")
        if not owner or not name or "/" in name:
            raise ForgeError(f"GitHub project must be owner/name, got {project!r}")
        self.owner, self.name = owner, name
        self._r = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        self._graphql = graphql_url(api.base_url)
        self._me: Optional[dict] = None

    async def _gql(self, query: str, variables: dict) -> dict:
        answer = await self.api.request("POST", self._graphql, json={"query": query, "variables": variables})
        if not isinstance(answer, dict):
            raise ForgeError("GitHub GraphQL answered no object")
        if answer.get("errors"):
            raise ForgeError("GitHub GraphQL: " + "; ".join(str(e.get("message", e)) for e in answer["errors"])[:300])
        return answer.get("data") or {}

    async def me(self) -> dict:
        if self._me is None:
            user = await self.api.request("GET", "/user")
            self._me = {"id": user["id"], "login": user["login"]}
        return self._me

    async def project_info(self) -> dict:
        repo = await self.api.request("GET", self._r)
        return {"default_branch": repo.get("default_branch") or "", "clone_url": repo.get("clone_url") or "",
                "web_url": repo.get("html_url") or ""}

    # ── issues ────────────────────────────────────────────────────────────

    async def issues(self, *, state: str, assigned_to_me: bool, labels: list[str], search: str, limit: int) -> list:
        if search:
            # Words with a colon are search qualifiers: "repo:other/x" would
            # widen the search beyond this repository (review S13).
            words = [w for w in search.replace('"', " ").split() if ":" not in w]
            query = [f"repo:{self.project}", "is:issue", *words]
            if state in ("open", "closed"):
                query.append(f"is:{state}")
            if assigned_to_me:
                query.append(f"assignee:{(await self.me())['login']}")
            query += ['label:"{}"'.format(label.replace('"', "")) for label in labels]
            found = await self.api.request("GET", "/search/issues", params={
                "q": " ".join(query), "sort": "updated", "order": "desc", "per_page": min(limit, 100)})
            return [_issue(i) for i in (found or {}).get("items", [])[:limit]]
        params: dict[str, Any] = {"state": state if state in ("open", "closed") else "all", "sort": "updated"}
        if assigned_to_me:
            params["assignee"] = (await self.me())["login"]
        if labels:
            params["labels"] = ",".join(labels)
        # The issue list holds pull requests too; asking for more keeps the
        # limit after they are dropped.
        items = await self.api.get_all(f"{self._r}/issues", params, limit=min(limit * 2, 300))
        return [_issue(i) for i in items if "pull_request" not in i][:limit]

    async def issue(self, number: int, *, comments: int) -> dict:
        raw = await self.api.request("GET", f"{self._r}/issues/{number}")
        if "pull_request" in raw:
            raise ForgeError(f"#{number} is a pull request -- read it with pr_get")
        issue = _issue(raw)
        issue["body"] = raw.get("body") or ""
        total = int(raw.get("comments") or 0)
        notes = await self._newest_comments(number, total, comments) if comments else []
        issue["comments"] = [_comment(c) for c in notes]
        issue["comments_left_out"] = max(0, total - len(notes))
        issue["related_prs"] = None     # GitHub has no plain endpoint for it; the timeline would cost a request per page
        return issue

    async def issue_comment(self, number: int, body: str) -> dict:
        comment = await self.api.request("POST", f"{self._r}/issues/{number}/comments", json={"body": body})
        return {"id": comment.get("id")}

    async def issue_update(self, number: int, *, add_labels: list[str], remove_labels: list[str],
                           state: str) -> dict:
        if add_labels:
            await self.api.request("POST", f"{self._r}/issues/{number}/labels", json={"labels": add_labels})
        for label in remove_labels:
            try:
                await self.api.request("DELETE", f"{self._r}/issues/{number}/labels/{quote(label, safe='')}")
            except ForgeNotFound:
                pass                        # not on the issue: the wanted state holds already
        if state:
            return _issue(await self.api.request("PATCH", f"{self._r}/issues/{number}", json={"state": state}))
        return _issue(await self.api.request("GET", f"{self._r}/issues/{number}"))

    # ── pull requests ─────────────────────────────────────────────────────

    async def prs(self, *, state: str, mine: bool, source_branch: str, limit: int) -> list:
        params: dict[str, Any] = {"state": "closed" if state == "merged" else state if state in ("open", "closed")
                                  else "all", "sort": "updated", "direction": "desc"}
        if source_branch:
            params["head"] = f"{self.owner}:{source_branch}"
        login = (await self.me())["login"] if mine else None
        # "closed" and "merged" share GitHub's closed list, and "mine" is filtered
        # here: all three are asked for in full before the limit is applied.
        items = await self.api.get_all(f"{self._r}/pulls", params,
                                       limit=300 if mine or state in ("merged", "closed") else limit)
        prs = [_pr(p) for p in items if (login is None or (p.get("user") or {}).get("login") == login)]
        if state == "merged":
            prs = [p for p in prs if p["state"] == "merged"]
        elif state == "closed":
            prs = [p for p in prs if p["state"] == "closed"]
        return prs[:limit]

    async def pr(self, number: int) -> dict:
        raw = await self.api.request("GET", f"{self._r}/pulls/{number}")
        pr = _pr(raw)
        pr["body"] = raw.get("body") or ""
        state = str(raw.get("mergeable_state") or "unknown")
        pr["merge_status"] = state
        mergeable = raw.get("mergeable")
        pr["mergeable"] = None if mergeable is None or state == "unknown" else bool(mergeable) and state not in _BLOCKING
        pr["merge_blockers"] = ([_BLOCKING[state]] if state in _BLOCKING else []) + (
            ["conflict"] if mergeable is False and state != "dirty" else [])
        pr["changes"] = raw.get("changed_files")
        pr["ci"] = None                 # read with ci(sha=...): GitHub has no pipeline object on the pull request
        return pr

    async def pr_files(self, number: int, *, limit: int) -> list:
        return [{"path": f.get("filename"), "old_path": f.get("previous_filename"),
                 "status": {"removed": "removed", "added": "added", "renamed": "renamed"}.get(f.get("status"), "modified"),
                 "diff": f.get("patch") or "",
                 "note": None if f.get("patch") is not None else "no diff shown (binary, too large or only renamed)"}
                for f in await self.api.get_all(f"{self._r}/pulls/{number}/files", limit=limit)]

    async def threads(self, number: int) -> list:
        threads: list[dict] = []
        after = None
        while True:
            data = await self._gql(_THREADS, {"owner": self.owner, "name": self.name, "number": number, "after": after})
            page = (((data.get("repository") or {}).get("pullRequest") or {}).get("reviewThreads") or {})
            for node in page.get("nodes") or []:
                threads.append({
                    "id": node.get("id"), "resolvable": True, "resolved": bool(node.get("isResolved")),
                    "file": node.get("path"), "line": node.get("line") or node.get("originalLine"),
                    "comments": [{"id": c.get("databaseId"), "author": (c.get("author") or {}).get("login"),
                                  "created": c.get("createdAt"), "body": c.get("body") or ""}
                                 for c in (node.get("comments") or {}).get("nodes") or []]})
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            if len(threads) >= 500:
                threads.append({"id": "not-read", "left_out": 1, "resolvable": False, "resolved": None,
                                "comments": []})
                break
            after = info.get("endCursor")
        # A reviewer's changes request stands until the same reviewer approves
        # or it is dismissed: only their latest deciding review counts. Reviews
        # come oldest first; a plain COMMENTED review decides nothing.
        # Oldest first, one more than kept: past the limit the newest would be
        # cut -- a changes request among them unseen (review of the fix round).
        reviews = await self.api.get_all(f"{self._r}/pulls/{number}/reviews", limit=MAX_REVIEWS + 1)
        cut = len(reviews) > MAX_REVIEWS
        reviews = reviews[:MAX_REVIEWS]
        deciding: dict[str, Any] = {}
        for review in reviews:
            if review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                deciding[(review.get("user") or {}).get("login") or ""] = review.get("id")
        for review in reviews:
            login = (review.get("user") or {}).get("login")
            blocking = review.get("state") == "CHANGES_REQUESTED" and deciding.get(login or "") == review.get("id")
            if review.get("body") or blocking:
                threads.append({"id": f"review-{review.get('id')}", "resolvable": False, "resolved": None,
                                "blocking": blocking, "file": None, "line": None, "review_state": review.get("state"),
                                "comments": [{"id": review.get("id"), "author": login,
                                              "created": review.get("submitted_at"), "body": review.get("body") or ""}]})
        total = int((await self.api.request("GET", f"{self._r}/issues/{number}")).get("comments") or 0)
        if cut:
            threads.append({"id": "newer-reviews", "left_out": 1, "resolvable": False, "resolved": None,
                            "comments": []})
        newest = await self._newest_comments(number, total, 100)
        for comment in newest:
            threads.append({"id": f"comment-{comment.get('id')}", "resolvable": False, "resolved": None,
                            "file": None, "line": None, "comments": [_comment(comment)]})
        if total > len(newest):
            threads.append({"id": "older-comments", "left_out": total - len(newest), "resolvable": False,
                            "resolved": None, "comments": []})
        return threads

    async def _newest_comments(self, number: int, total: int, want: int) -> list:
        """The newest ``want`` of an issue's ``total`` comments. The list has no
        order parameter and starts with the oldest, so its last pages are read."""
        got: list = []
        page = (total - 1) // 100 + 1
        while total > 0 and page >= 1 and len(got) < want:
            items = await self.api.request("GET", f"{self._r}/issues/{number}/comments",
                                           params={"per_page": 100, "page": page})
            got = list(items or []) + got
            page -= 1
        return got[-want:] if want else []

    async def pr_create(self, *, source: str, target: str, title: str, body: str, draft: bool) -> dict:
        return _pr(await self.api.request("POST", f"{self._r}/pulls", json={
            "head": source, "base": target, "title": title, "body": body, "draft": draft}))

    async def pr_update(self, number: int, *, title: Optional[str], body: Optional[str], draft: Optional[bool]) -> dict:
        update = {k: v for k, v in (("title", title), ("body", body)) if v is not None}
        raw = await self.api.request("PATCH", f"{self._r}/pulls/{number}", json=update) if update \
            else await self.api.request("GET", f"{self._r}/pulls/{number}")
        if draft is not None and bool(raw.get("draft")) != draft:
            await self._gql(_TO_DRAFT if draft else _READY, {"id": raw.get("node_id")})
            raw = await self.api.request("GET", f"{self._r}/pulls/{number}")
        return _pr(raw)

    async def pr_comment(self, number: int, body: str) -> dict:
        return await self.issue_comment(number, body)

    async def thread_reply(self, number: int, thread_id: str, body: str) -> dict:
        if not thread_id.startswith("PRRT_"):
            # Conversation comments and reviews have no thread to answer in.
            return await self.issue_comment(number, body)
        data = await self._gql(_REPLY, {"id": thread_id, "body": body})
        return {"id": ((data.get("addPullRequestReviewThreadReply") or {}).get("comment") or {}).get("databaseId")}

    async def thread_resolve(self, number: int, thread_id: str, resolved: bool) -> None:
        if not thread_id.startswith("PRRT_"):
            raise ForgeError(f"{thread_id} is a comment or review, not a review thread: it cannot be resolved")
        await self._gql(_RESOLVE if resolved else _UNRESOLVE, {"id": thread_id})

    async def line_comment(self, number: int, *, path: str, line: int, body: str, pr: dict) -> dict:
        comment = await self.api.request("POST", f"{self._r}/pulls/{number}/comments", json={
            "body": body, "commit_id": pr.get("head_sha"), "path": path, "line": line, "side": "RIGHT"})
        # The REST answer carries the comment, not its review thread (PRRT_...).
        return {"comment_id": comment.get("id"), "thread_id": None}

    async def merge(self, number: int, *, sha: str, method: str, delete_branch: bool) -> dict:
        pr = await self.api.request("GET", f"{self._r}/pulls/{number}")
        merged = await self.api.request("PUT", f"{self._r}/pulls/{number}/merge",
                                        json={"sha": sha, "merge_method": method})
        head = pr.get("head") or {}
        if delete_branch and (head.get("repo") or {}).get("full_name") == self.project and head.get("ref"):
            try:
                await self.api.request("DELETE", f"{self._r}/git/refs/heads/{quote(head['ref'], safe='/')}")
            except ForgeError:
                pass                        # the merge stands; a branch rule may keep the branch
        return {"state": "merged" if merged.get("merged") else "open", "merge_commit": merged.get("sha")}

    # ── CI ────────────────────────────────────────────────────────────────

    async def ci(self, *, sha: str = "", ref: str = "", pr: Optional[int] = None) -> Optional[dict]:
        """Check runs plus commit statuses on one commit: a pull request's head,
        a branch's tip or the commit itself."""
        if pr is not None:
            sha = (await self.api.request("GET", f"{self._r}/pulls/{pr}"))["head"]["sha"]
        elif not sha:
            branch = await self.api.request("GET", f"{self._r}/branches/{quote(ref, safe='')}")
            sha = branch["commit"]["sha"]
        runs = await self.api.get_all(f"{self._r}/commits/{sha}/check-runs", {"filter": "latest"}, limit=200,
                                      key="check_runs")
        combined = await self.api.request("GET", f"{self._r}/commits/{sha}/status")
        jobs = [{"id": r.get("id"), "name": r.get("name"),
                 "stage": ((r.get("check_suite") or {}).get("app") or r.get("app") or {}).get("slug"),
                 "state": check_state(r.get("status"), r.get("conclusion")), "allow_failure": False,
                 "url": r.get("html_url"), "has_log": ((r.get("app") or {}).get("slug") == "github-actions")}
                for r in runs]
        jobs += [{"id": None, "name": s.get("context"), "stage": "status", "state": check_state(None, s.get("state"))
                  if s.get("state") != "pending" else "pending", "allow_failure": False, "url": s.get("target_url"),
                  "has_log": False} for s in (combined or {}).get("statuses") or []]
        # A workflow run exists before its jobs do: without it the sum of the
        # checks that already exist could read green too early (review S4).
        workflows = await self.api.request("GET", f"{self._r}/actions/runs", params={"head_sha": sha, "per_page": 50})
        # Finished runs count with their conclusion too: a run held for approval
        # (a fork's pull request) may end as action_required without a job.
        jobs += [{"id": None, "name": w.get("name"), "stage": "workflow",
                  "state": check_state(w.get("status"), w.get("conclusion")), "allow_failure": False,
                  "url": w.get("html_url"), "has_log": False,
                  **({} if w.get("status") == "completed" else
                     {"note": "workflow run not finished -- its jobs appear as they start"})}
                 for w in (workflows or {}).get("workflow_runs") or []]
        # A run cancelled for a newer one on the same commit (concurrency with
        # cancel-in-progress, a push and a pull_request run of one workflow) is
        # no verdict: where one of the same name ran on, that one speaks. A
        # skipped one did not run -- the tests of that commit never did.
        ran_on = {(j["stage"], j["name"]) for j in jobs if j["state"] not in ("canceled", "skipped")}
        jobs = [j for j in jobs if j["state"] != "canceled" or (j["stage"], j["name"]) not in ran_on]
        if not jobs:
            return None
        return {"id": None, "state": overall([j["state"] for j in jobs]), "sha": sha, "ref": ref or None,
                "url": None, "jobs": jobs}

    async def job_log(self, job_id: int) -> str:
        return await self.api.request("GET", f"{self._r}/actions/jobs/{job_id}/logs", text=True)

    async def job(self, job_id: int) -> dict:
        raw = await self.api.request("GET", f"{self._r}/actions/jobs/{job_id}")
        return {"id": raw.get("id"), "name": raw.get("name"), "stage": raw.get("workflow_name"),
                "state": check_state(raw.get("status"), raw.get("conclusion")), "allow_failure": False,
                "url": raw.get("html_url"), "has_log": True}

    async def retry(self, job_id: int) -> dict:
        """A re-run is a new attempt with new job ids; the old id keeps showing
        the old attempt, so nothing is read back from it."""
        old = await self.job(job_id)
        await self.api.request("POST", f"{self._r}/actions/jobs/{job_id}/rerun")
        return {**old, "id": None, "state": "pending", "url": None,    # both name the old attempt
                "note": "GitHub starts a new attempt with new job ids -- ci_status shows it"}

    async def branch_exists(self, branch: str) -> bool:
        try:
            await self.api.request("GET", f"{self._r}/branches/{quote(branch, safe='')}")
        except ForgeNotFound:
            return False
        return True


def _issue(raw: dict) -> dict:
    return {"number": raw.get("number"), "title": raw.get("title"), "state": raw.get("state"),
            "labels": [label.get("name") if isinstance(label, dict) else label for label in raw.get("labels") or []],
            "assignees": [a.get("login") for a in raw.get("assignees") or []],
            "author": (raw.get("user") or {}).get("login"), "updated": raw.get("updated_at"),
            "comments_count": raw.get("comments"), "url": raw.get("html_url")}


def _pr(raw: dict) -> dict:
    head, base = raw.get("head") or {}, raw.get("base") or {}
    state = "merged" if raw.get("merged_at") or raw.get("merged") else raw.get("state") or "unknown"
    return {"number": raw.get("number"), "title": raw.get("title"), "state": state, "draft": bool(raw.get("draft")),
            "source_branch": head.get("ref"), "target_branch": base.get("ref"), "head_sha": head.get("sha"),
            "author": (raw.get("user") or {}).get("login"), "updated": raw.get("updated_at"),
            "url": raw.get("html_url"),
            "foreign_source": bool(head.get("repo") and base.get("repo")
                                   and (head["repo"] or {}).get("full_name") != (base["repo"] or {}).get("full_name"))}


def _comment(raw: dict) -> dict:
    return {"id": raw.get("id"), "author": (raw.get("user") or {}).get("login"),
            "created": raw.get("created_at"), "body": raw.get("body") or ""}
