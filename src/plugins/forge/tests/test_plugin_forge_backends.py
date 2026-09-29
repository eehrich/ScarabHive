"""Both backends against httpx.MockTransport: how each platform's answer is
read into the common shapes, and how HTTP failures become ForgeErrors. The
GitLab answer shapes were measured on GitLab CE 19.4.1 (facts.md); the GitHub
ones follow GitHub's REST/GraphQL documentation."""
import json

import httpx
import pytest

from plugins.forge.github import GitHub, check_state, graphql_url, overall
from plugins.forge.gitlab import GitLab
from plugins.forge.http import Api, ForgeError, ForgeNotFound, ForgeUnavailable


class Router:
    """(method, path) -> answer; records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.raw_path.decode().split("?")[0])
        answer = self.routes.get(key)
        if answer is None:
            return httpx.Response(404, json={"message": f"no route {key}"})
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def bodies(self, method, path):
        return [json.loads(r.content) for r in self.requests
                if r.method == method and r.url.raw_path.decode().split("?")[0] == path and r.content]


def gitlab(routes):
    router = Router(routes)
    api = Api("https://gl.test/api/v4", "secret-token", transport=httpx.MockTransport(router), token_env="GL_TOKEN")
    return GitLab(api, "team/app"), router


def github(routes):
    router = Router(routes)
    api = Api("https://api.github.com", "secret-token", transport=httpx.MockTransport(router), token_env="GH_TOKEN")
    return GitHub(api, "owner/repo"), router


P = "/api/v4/projects/team%2Fapp"
R = "/repos/owner/repo"


# ── the HTTP layer ────────────────────────────────────────────────────────

@pytest.mark.parametrize("response,kind,text", [
    (httpx.Response(401, json={"message": "401 Unauthorized"}), ForgeError, "check GL_TOKEN"),
    (httpx.Response(404, json={"message": "404 Not found"}), ForgeNotFound, "not found"),
    (httpx.Response(502, text="bad gateway"), ForgeUnavailable, "server error 502"),
    (httpx.Response(429, headers={"retry-after": "30"}, json={}), ForgeUnavailable, "retry after 30"),
    (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "API rate limit"}),
     ForgeUnavailable, "rate limited"),
    (httpx.Response(403, json={"message": "403 Forbidden"}), ForgeError, "role or scope"),
    (httpx.Response(422, json={"message": {"source_branch": ["is invalid"]}}), ForgeError, "source_branch: is invalid"),
])
async def test_http_failures_become_forge_errors(response, kind, text):
    backend, _ = gitlab({("GET", "/api/v4/user"): response})
    with pytest.raises(kind) as caught:
        await backend.me()
    assert text in str(caught.value)
    assert "secret-token" not in str(caught.value)


async def test_get_all_follows_numbered_pages_until_a_short_one():
    pages = {1: [{"id": i} for i in range(100)], 2: [{"id": 100}]}
    backend, router = gitlab({("GET", f"{P}/issues"): lambda r: pages[int(r.url.params["page"])]})
    assert len(await backend.api.get_all(f"{backend._p}/issues", limit=500)) == 101
    assert [r.url.params["page"] for r in router.requests] == ["1", "2"]


async def test_the_token_goes_as_bearer_and_the_project_path_stays_encoded():
    backend, router = gitlab({("GET", P): {"default_branch": "main", "http_url_to_repo": "https://gl.test/team/app.git"}})
    await backend.project_info()
    assert router.requests[0].headers["authorization"] == "Bearer secret-token"
    assert router.requests[0].url.raw_path.decode() == P


# ── GitLab ────────────────────────────────────────────────────────────────

def mr(**extra):
    return {"iid": 7, "title": "T", "state": "opened", "draft": False, "source_branch": "s", "target_branch": "main",
            "sha": "abc", "author": {"username": "bot"}, "web_url": "u", "description": "D",
            "detailed_merge_status": "mergeable", "diff_refs": {"head_sha": "abc"}, **extra}


@pytest.mark.parametrize("status,mergeable,blockers", [
    ("mergeable", True, []),
    ("checking", None, []),
    ("unchecked", None, []),
    ("discussions_not_resolved", False, ["discussions not resolved"]),
    ("need_rebase", False, ["need rebase"]),
])
async def test_gitlab_merge_status(status, mergeable, blockers):
    backend, _ = gitlab({("GET", f"{P}/merge_requests/7"): mr(detailed_merge_status=status)})
    pr = await backend.pr(7)
    assert (pr["mergeable"], pr["merge_blockers"]) == (mergeable, blockers)
    assert pr["state"] == "open" and pr["number"] == 7


async def test_gitlab_draft_is_the_title_prefix():
    backend, router = gitlab({("GET", f"{P}/merge_requests/7"): mr(title="Draft: T", draft=True),
                              ("PUT", f"{P}/merge_requests/7"): lambda r: mr(title=json.loads(r.content)["title"])})
    await backend.pr_update(7, title=None, body=None, draft=False)
    await backend.pr_update(7, title="New", body=None, draft=True)
    assert [b["title"] for b in router.bodies("PUT", f"{P}/merge_requests/7")] == ["T", "Draft: New"]


async def test_gitlab_issues_assigned_to_me_ask_by_username():
    backend, router = gitlab({("GET", "/api/v4/user"): {"id": 5, "username": "bot"},
                              ("GET", f"{P}/issues"): [{"iid": 1, "title": "x", "state": "opened"}]})
    issues = await backend.issues(state="open", assigned_to_me=True, labels=["a", "b"], search="", limit=10)
    params = router.requests[-1].url.params
    assert (params["assignee_username"], params["labels"], params["state"]) == ("bot", "a,b", "opened")
    assert issues[0]["state"] == "open"


async def test_gitlab_threads_skip_system_notes_and_carry_the_position():
    backend, _ = gitlab({("GET", f"{P}/merge_requests/7/discussions"): [
        {"id": "d1", "notes": [{"id": 1, "system": True, "body": "added 1 commit"}]},
        {"id": "d2", "notes": [{"id": 2, "body": "why?", "resolvable": True, "resolved": False,
                                "author": {"username": "rev"}, "position": {"new_path": "a.py", "new_line": 3}},
                               {"id": 3, "body": "because", "author": {"username": "bot"}}]},
        {"id": "d3", "notes": [{"id": 4, "body": "LGTM", "resolvable": False, "author": {"username": "rev"}}]}]})
    threads = await backend.threads(7)
    assert [t["id"] for t in threads] == ["d2", "d3"]
    assert threads[0]["file"] == "a.py" and threads[0]["line"] == 3 and threads[0]["resolved"] is False
    assert [c["body"] for c in threads[0]["comments"]] == ["why?", "because"]
    assert threads[1]["resolvable"] is False and threads[1]["resolved"] is None


async def test_gitlab_ci_maps_states_and_names_downstream_pipelines():
    backend, _ = gitlab({
        ("GET", f"{P}/pipelines"): [{"id": 9, "status": "waiting_for_resource", "sha": "abc", "ref": "s"}],
        ("GET", f"{P}/pipelines/9/jobs"): [{"id": 2, "name": "b", "status": "failed", "allow_failure": True},
                                           {"id": 1, "name": "a", "status": "canceling"}],
        ("GET", f"{P}/pipelines/9/bridges"): [{"id": 3, "name": "deploy", "status": "created"}]})
    ci = await backend.ci(ref="s")
    assert ci["state"] == "pending"
    assert [(j["id"], j["state"]) for j in ci["jobs"]] == [(1, "running"), (2, "failed"), (3, "pending")]
    assert "downstream" in ci["jobs"][2]["note"] and ci["jobs"][2]["has_log"] is False


DIFF = "@@ -1,4 +1,5 @@\n a\n-b\n+B\n+C\n c\n d\n@@ -10,2 +11,3 @@\n j\n+K\n k\n"


@pytest.mark.parametrize("new_line,old_line", [
    (1, 1), (2, None), (3, None), (4, 3), (5, 4),      # first hunk: context, added, added, context
    (8, 7),                                            # between hunks: shifted by +1
    (11, 10), (12, None), (13, 11),                    # second hunk
    (20, 18),                                          # after the last hunk
])
def test_gitlab_old_line_of(new_line, old_line):
    from plugins.forge.gitlab import old_line_of
    assert old_line_of(DIFF, new_line) == old_line


async def test_gitlab_line_comment_places_an_unchanged_line_by_both_numbers():
    """GitLab answers 400 "line_code can't be blank" for a context line given
    by its new number alone (review F4)."""
    backend, router = gitlab({
        ("GET", f"{P}/merge_requests/7/diffs"): [{"new_path": "b.py", "old_path": "a.py", "renamed_file": True,
                                                  "diff": DIFF}],
        ("POST", f"{P}/merge_requests/7/discussions"): {"id": "d1", "notes": [{"id": 5}]}})
    pr = {"_diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}}
    assert await backend.line_comment(7, path="b.py", line=4, body="x", pr=pr) == {"thread_id": "d1", "comment_id": 5}
    await backend.line_comment(7, path="b.py", line=2, body="y", pr=pr)
    context, added = [b["position"] for b in router.bodies("POST", f"{P}/merge_requests/7/discussions")]
    assert (context["old_path"], context["new_path"], context["old_line"], context["new_line"]) == ("a.py", "b.py", 3, 4)
    assert "old_line" not in added and added["new_line"] == 2
    with pytest.raises(ForgeError, match="not among the files"):
        await backend.line_comment(7, path="zzz.py", line=1, body="z", pr=pr)


async def test_gitlab_waits_for_a_just_pushed_branch_to_settle(monkeypatch):
    """Measured live (F-GL12): the branch API shows the branch, the request
    creation still calls it missing for a moment."""
    from plugins.forge import gitlab as gitlab_module
    monkeypatch.setattr(gitlab_module, "BRANCH_SETTLE_PAUSE_S", 0)
    answers = [httpx.Response(400, json={"message": {"source_branch": ["does not exist"]}}),
               httpx.Response(201, json=mr(iid=8))]
    backend, router = gitlab({("POST", f"{P}/merge_requests"): lambda r: answers.pop(0)})
    created = await backend.pr_create(source="s", target="main", title="T", body="", draft=False)
    assert created["number"] == 8 and len(router.requests) == 2


async def test_gitlab_does_not_retry_other_refusals(monkeypatch):
    from plugins.forge import gitlab as gitlab_module
    monkeypatch.setattr(gitlab_module, "BRANCH_SETTLE_PAUSE_S", 0)
    backend, router = gitlab({("POST", f"{P}/merge_requests"): httpx.Response(
        409, json={"message": ["Another open merge request already exists for this source branch"]})})
    with pytest.raises(ForgeError, match="already exists"):
        await backend.pr_create(source="s", target="main", title="T", body="", draft=False)
    assert len(router.requests) == 1


async def test_gitlab_refuses_rebase_as_a_merge_method():
    """The merge API has no method field; rebase is the project's setting (review F8)."""
    backend, router = gitlab({})
    with pytest.raises(ForgeError, match="project's merge method setting"):
        await backend.merge(7, sha="abc", method="rebase", delete_branch=True)
    assert router.requests == []


async def test_gitlab_ci_of_a_request_is_gitlabs_head_pipeline():
    """A merged-results pipeline runs on the merge commit, a fork's in the fork
    (fix-round review): the request's head_pipeline is what GitLab checks."""
    backend, router = gitlab({
        ("GET", P): {"id": 1},
        ("GET", f"{P}/merge_requests/7"): mr(sha="head1", head_pipeline={
            "id": 9, "project_id": 2, "sha": "mergecommit", "ref": "refs/merge-requests/7/merge", "status": "success"}),
        ("GET", "/api/v4/projects/2/pipelines/9/jobs"): [{"id": 3, "name": "unit", "status": "success"}],
        ("GET", "/api/v4/projects/2/pipelines/9/bridges"): []})
    ci = await backend.ci(pr=7)
    assert (ci["sha"], ci["pipeline_sha"], ci["state"]) == ("head1", "mergecommit", "success")
    assert ci["jobs"][0]["has_log"] is False and "fork" in ci["jobs"][0]["note"]


@pytest.mark.parametrize("refusal", [None, httpx.Response(403, json={"message": "403 Forbidden"})],
                         ids=["private-fork-404", "members-only-ci-403"])
async def test_gitlab_a_fork_the_token_cannot_read_still_shows_its_pipeline_state(refusal):
    """A private fork answers 404 for its jobs; pr_get, ci_status and pr_merge
    failed whole on it (reviews of the fix rounds)."""
    routes = {("GET", P): {"id": 1}, ("GET", f"{P}/merge_requests/7"): mr(sha="head1", head_pipeline={
        "id": 9, "project_id": 2, "sha": "head1", "ref": "feature", "status": "failed"})}
    if refusal is not None:
        routes[("GET", "/api/v4/projects/2/pipelines/9/jobs")] = refusal
    backend, _ = gitlab(routes)
    ci = await backend.ci(pr=7)
    assert (ci["state"], ci["jobs"]) == ("failed", []) and "cannot read" in ci["note"]


async def test_gitlab_a_fork_that_is_down_is_not_taken_for_unreadable():
    backend, _ = gitlab({("GET", P): {"id": 1}, ("GET", f"{P}/merge_requests/7"): mr(sha="h", head_pipeline={
        "id": 9, "project_id": 2, "sha": "h", "status": "success"}),
        ("GET", "/api/v4/projects/2/pipelines/9/jobs"): httpx.Response(503)})
    with pytest.raises(ForgeUnavailable):
        await backend.ci(pr=7)


async def test_gitlab_marks_discussions_it_did_not_read(monkeypatch):
    """Oldest first: past the limit the newest were cut without a word (review of the fix round)."""
    from plugins.forge import gitlab as gitlab_module
    monkeypatch.setattr(gitlab_module, "MAX_DISCUSSIONS", 2)
    note = {"id": 1, "body": "x", "author": {"username": "r"}, "resolvable": False}
    listed = [{"id": "a", "notes": [note]}, {"id": "b", "notes": [note]}]
    exactly, _ = gitlab({("GET", f"{P}/merge_requests/7/discussions"): listed})
    assert not any(t.get("left_out") for t in await exactly.threads(7))            # at the limit, nothing is cut
    more, _ = gitlab({("GET", f"{P}/merge_requests/7/discussions"): listed + [{"id": "c", "notes": [note]}]})
    threads = await more.threads(7)
    assert [t["id"] for t in threads] == ["a", "b", "not-read"] and threads[-1]["left_out"] == 1


async def test_gitlab_a_missing_pipeline_of_the_own_project_is_an_error():
    backend, _ = gitlab({("GET", P): {"id": 1}, ("GET", f"{P}/merge_requests/7"): mr(sha="h", head_pipeline={
        "id": 9, "project_id": 1, "sha": "h", "status": "success"})})
    with pytest.raises(ForgeNotFound):
        await backend.ci(pr=7)


async def test_gitlab_a_request_without_head_pipeline_has_no_ci_yet():
    backend, _ = gitlab({("GET", f"{P}/merge_requests/7"): mr(head_pipeline=None)})
    assert await backend.ci(pr=7) is None


async def test_gitlab_merge_sends_the_sha():
    backend, router = gitlab({("PUT", f"{P}/merge_requests/7/merge"): mr(state="merged", merge_commit_sha="m1")})
    done = await backend.merge(7, sha="abc", method="squash", delete_branch=True)
    assert done == {"state": "merged", "merge_commit": "m1"}
    assert router.bodies("PUT", f"{P}/merge_requests/7/merge") == [
        {"sha": "abc", "squash": True, "should_remove_source_branch": True}]


# ── GitHub ────────────────────────────────────────────────────────────────

def test_graphql_url():
    assert graphql_url("https://api.github.com") == "https://api.github.com/graphql"
    assert graphql_url("https://ghe.corp/api/v3/") == "https://ghe.corp/api/graphql"


@pytest.mark.parametrize("status,conclusion,state", [
    ("queued", None, "pending"), ("in_progress", None, "running"), ("completed", "success", "success"),
    ("completed", "neutral", "success"), ("completed", "skipped", "skipped"), ("completed", "cancelled", "canceled"),
    ("completed", "timed_out", "failed"), ("completed", "action_required", "failed"), (None, "error", "failed"),
    (None, "failure", "failed"),
])
def test_github_check_states(status, conclusion, state):
    assert check_state(status, conclusion) == state


def test_github_overall():
    assert overall([]) == "none"
    assert overall(["success", "running", "failed"]) == "failed"
    assert overall(["success", "pending"]) == "pending"
    assert overall(["success", "skipped"]) == "success"


async def test_github_issue_list_drops_pull_requests():
    backend, _ = github({("GET", f"{R}/issues"): [{"number": 1, "title": "i"},
                                                  {"number": 2, "title": "p", "pull_request": {}}]})
    issues = await backend.issues(state="open", assigned_to_me=False, labels=[], search="", limit=10)
    assert [i["number"] for i in issues] == [1]


@pytest.mark.parametrize("mergeable,state,expected,blockers", [
    (True, "clean", True, []),
    (True, "unstable", True, []),
    (None, "unknown", None, []),
    (True, "blocked", False, ["blocked by a branch rule (required reviews or checks)"]),
    (False, "dirty", False, ["conflict"]),
])
async def test_github_mergeability(mergeable, state, expected, blockers):
    backend, _ = github({("GET", f"{R}/pulls/3"): {"number": 3, "state": "open", "mergeable": mergeable,
                                                    "mergeable_state": state, "head": {"sha": "h", "ref": "b"},
                                                    "base": {"ref": "main"}}})
    pr = await backend.pr(3)
    assert (pr["mergeable"], pr["merge_blockers"]) == (expected, blockers)


async def test_github_ci_sums_check_runs_and_statuses():
    backend, _ = github({
        ("GET", f"{R}/commits/h/check-runs"): {"total_count": 2, "check_runs": [
            {"id": 11, "name": "test", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}},
            {"id": 12, "name": "external", "status": "completed", "conclusion": "failure", "app": {"slug": "circleci"}}]},
        ("GET", f"{R}/commits/h/status"): {"statuses": [{"context": "legacy", "state": "pending"}]},
        ("GET", f"{R}/actions/runs"): {"workflow_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]}})
    ci = await backend.ci(sha="h")
    assert ci["state"] == "failed"
    assert [(j["name"], j["state"], j["has_log"]) for j in ci["jobs"]] == [
        ("test", "success", True), ("external", "failed", False), ("legacy", "pending", False), ("ci", "success", False)]


async def test_github_ci_is_not_green_while_a_workflow_run_has_no_checks_yet():
    """A workflow run exists before its jobs; the sum of the checks that exist
    would read green too early (review S4)."""
    backend, router = github({
        ("GET", f"{R}/commits/h/check-runs"): {"check_runs": [
            {"id": 11, "name": "lint", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}]},
        ("GET", f"{R}/commits/h/status"): {"statuses": []},
        ("GET", f"{R}/actions/runs"): {"workflow_runs": [{"name": "test", "status": "queued", "html_url": "w"}]}})
    ci = await backend.ci(sha="h")
    assert ci["state"] == "pending" and ci["jobs"][-1]["stage"] == "workflow"
    assert router.requests[-1].url.params["head_sha"] == "h"


async def test_github_a_finished_run_counts_with_its_conclusion():
    """A fork's run held for approval may end as action_required without a job."""
    backend, _ = github({
        ("GET", f"{R}/commits/h/check-runs"): {"check_runs": []},
        ("GET", f"{R}/commits/h/status"): {"statuses": [{"context": "cla", "state": "success"}]},
        ("GET", f"{R}/actions/runs"): {"workflow_runs": [
            {"name": "test", "status": "completed", "conclusion": "action_required"}]}})
    assert (await backend.ci(sha="h"))["state"] == "failed"


@pytest.mark.parametrize("second,expected", [("success", "success"), ("failure", "failed"), ("cancelled", "canceled"),
                                             ("skipped", "canceled")])     # the run that stayed never tested
async def test_github_a_run_cancelled_for_a_newer_one_is_no_verdict(second, expected):
    """concurrency: cancel-in-progress stops the push run once the pull_request
    run starts; read as "canceled", no merge could ever pass (review of the fix round)."""
    def check(i, conclusion):
        return {"id": i, "name": "unit", "status": "completed", "conclusion": conclusion,
                "app": {"slug": "github-actions"}}
    backend, _ = github({
        ("GET", f"{R}/commits/h/check-runs"): {"check_runs": [check(1, "cancelled"), check(2, second)]},
        ("GET", f"{R}/commits/h/status"): {"statuses": []},
        ("GET", f"{R}/actions/runs"): {"workflow_runs": [
            {"name": "ci", "status": "completed", "conclusion": "cancelled"},
            {"name": "ci", "status": "completed", "conclusion": second}]}})
    ci = await backend.ci(sha="h")
    assert ci["state"] == expected and len(ci["jobs"]) == (4 if second in ("cancelled", "skipped") else 2)


async def test_github_threads_merge_three_kinds_of_comment():
    graphql = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False},
        "nodes": [{"id": "PRRT_1", "isResolved": False, "path": "a.py", "line": 4,
                   "comments": {"nodes": [{"databaseId": 5, "body": "fix", "author": {"login": "rev"}}]}}]}}}}}
    backend, _ = github({
        ("POST", "/graphql"): graphql,
        ("GET", f"{R}/pulls/3/reviews"): [{"id": 8, "state": "CHANGES_REQUESTED", "body": "", "user": {"login": "rev"}},
                                           {"id": 9, "state": "APPROVED", "body": "", "user": {"login": "other"}}],
        ("GET", f"{R}/issues/3"): {"number": 3, "comments": 1},
        ("GET", f"{R}/issues/3/comments"): [{"id": 21, "body": "hello", "user": {"login": "p"}}]})
    threads = await backend.threads(3)
    assert [(t["id"], t["resolvable"]) for t in threads] == [
        ("PRRT_1", True), ("review-8", False), ("comment-21", False)]
    assert threads[1]["review_state"] == "CHANGES_REQUESTED" and threads[1]["blocking"] is True


async def test_github_marks_conversation_comments_it_did_not_read():
    """Only the newest 100 are read; the rest went unmentioned (review of the fix round)."""
    backend, _ = github({
        ("POST", "/graphql"): {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}},
        ("GET", f"{R}/pulls/3/reviews"): [],
        ("GET", f"{R}/issues/3"): {"number": 3, "comments": 130},
        ("GET", f"{R}/issues/3/comments"): [{"id": i, "body": "c", "user": {"login": "p"}} for i in range(100)]})
    threads = await backend.threads(3)
    assert threads[-1]["left_out"] == 30 and len(threads) == 101


async def test_github_marks_reviews_it_did_not_read(monkeypatch):
    """Reviews come oldest first: past the limit a newer changes request was cut unseen."""
    from plugins.forge import github as github_module
    monkeypatch.setattr(github_module, "MAX_REVIEWS", 2)
    review = {"state": "COMMENTED", "body": "", "user": {"login": "r"}}
    backend, _ = github({
        ("POST", "/graphql"): {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}},
        ("GET", f"{R}/pulls/3/reviews"): [{**review, "id": i} for i in range(3)],
        ("GET", f"{R}/issues/3"): {"number": 3, "comments": 0}})
    assert [t["id"] for t in await backend.threads(3)] == ["newer-reviews"]


@pytest.mark.parametrize("later,blocking", [
    ({"id": 10, "state": "APPROVED", "body": "", "user": {"login": "rev"}}, False),
    ({"id": 10, "state": "DISMISSED", "body": "", "user": {"login": "rev"}}, False),
    ({"id": 10, "state": "COMMENTED", "body": "still?", "user": {"login": "rev"}}, True),
    ({"id": 10, "state": "APPROVED", "body": "", "user": {"login": "someone-else"}}, True),
])
async def test_github_a_changes_request_stands_until_its_reviewer_takes_it_back(later, blocking):
    """Only the reviewer's latest deciding review counts (review F2/S2)."""
    backend, _ = github({
        ("POST", "/graphql"): {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}},
        ("GET", f"{R}/pulls/3/reviews"): [{"id": 8, "state": "CHANGES_REQUESTED", "body": "no", "user": {"login": "rev"}},
                                           later],
        ("GET", f"{R}/issues/3"): {"number": 3, "comments": 0}})
    threads = await backend.threads(3)
    assert [t["id"] for t in threads if t.get("blocking")] == (["review-8"] if blocking else [])


async def test_github_reads_the_newest_comments_of_a_long_issue():
    """The comment list starts with the oldest and has no order (review F16)."""
    pages = {3: [{"id": 201 + i, "body": str(i)} for i in range(50)], 2: [{"id": 101 + i} for i in range(100)]}
    backend, router = github({
        ("GET", f"{R}/issues/5"): {"number": 5, "comments": 250, "body": ""},
        ("GET", f"{R}/issues/5/comments"): lambda r: pages[int(r.url.params["page"])]})
    issue = await backend.issue(5, comments=60)
    assert [c["id"] for c in issue["comments"]][-1] == 250 and len(issue["comments"]) == 60
    assert issue["comments"][0]["id"] == 191 and issue["comments_left_out"] == 190
    assert [r.url.params.get("page") for r in router.requests[1:]] == ["3", "2"]


async def test_github_search_stays_in_the_repository():
    """A repo: qualifier in the search text would widen it (review S13)."""
    backend, router = github({("GET", "/search/issues"): {"items": []}})
    await backend.issues(state="open", assigned_to_me=False, labels=['a"b'], search='crash repo:other/x "q', limit=5)
    params = router.requests[-1].url.params
    assert params["q"] == 'repo:owner/repo is:issue crash q is:open label:"ab"'
    assert (params["sort"], params["order"]) == ("updated", "desc")


async def test_github_closed_pulls_are_read_in_full_before_merged_ones_drop():
    backend, router = github({("GET", f"{R}/pulls"): [
        {"number": i, "state": "closed", "merged_at": "x" if i % 2 else None} for i in range(10)]})
    prs = await backend.prs(state="closed", mine=False, source_branch="", limit=3)
    assert [p["number"] for p in prs] == [0, 2, 4]
    assert router.requests[0].url.params["per_page"] == "100"


async def test_github_retry_does_not_read_the_old_attempt_back():
    """A re-run gets new job ids; the old id still shows the old failure (review F3)."""
    backend, router = github({
        ("GET", f"{R}/actions/jobs/7"): {"id": 7, "name": "unit", "status": "completed", "conclusion": "failure",
                                         "html_url": "https://github.com/o/r/actions/runs/1/job/7"},
        ("POST", f"{R}/actions/jobs/7/rerun"): httpx.Response(201)})
    job = await backend.retry(7)
    assert job["id"] is None and job["url"] is None and job["state"] == "pending" and "new job ids" in job["note"]
    assert [r.method for r in router.requests] == ["GET", "POST"]


async def test_github_a_file_without_patch_says_why():
    backend, _ = github({("GET", f"{R}/pulls/3/files"): [{"filename": "a.png", "status": "added", "changes": 0}]})
    files = await backend.pr_files(3, limit=10)
    assert files[0]["diff"] == "" and "binary" in files[0]["note"]


async def test_github_secondary_rate_limit_is_a_wait_not_a_permission_problem():
    backend, _ = github({("GET", "/user"): httpx.Response(403, headers={"retry-after": "60"},
                                                           json={"message": "secondary rate limit"})})
    with pytest.raises(ForgeUnavailable, match="retry after 60"):
        await backend.me()


async def test_github_line_comment_has_no_thread_id():
    backend, router = github({("POST", f"{R}/pulls/3/comments"): {"id": 44}})
    done = await backend.line_comment(3, path="a.py", line=2, body="x", pr={"head_sha": "h"})
    assert done == {"comment_id": 44, "thread_id": None}
    assert router.bodies("POST", f"{R}/pulls/3/comments") == [
        {"body": "x", "commit_id": "h", "path": "a.py", "line": 2, "side": "RIGHT"}]


async def test_github_merge_deletes_only_a_branch_of_this_repository():
    pull = {"number": 3, "head": {"ref": "scarabhive/x", "repo": {"full_name": "fork/repo"}}}
    backend, router = github({("GET", f"{R}/pulls/3"): pull,
                              ("PUT", f"{R}/pulls/3/merge"): {"merged": True, "sha": "m"}})
    done = await backend.merge(3, sha="h", method="squash", delete_branch=True)
    assert done == {"state": "merged", "merge_commit": "m"}
    assert router.bodies("PUT", f"{R}/pulls/3/merge") == [{"sha": "h", "merge_method": "squash"}]
    assert not any(r.method == "DELETE" for r in router.requests)


async def test_github_reply_to_a_comment_is_a_new_comment_and_it_cannot_be_resolved():
    backend, router = github({("POST", f"{R}/issues/3/comments"): {"id": 30}})
    assert await backend.thread_reply(3, "comment-21", "ok") == {"id": 30}
    with pytest.raises(ForgeError, match="cannot be resolved"):
        await backend.thread_resolve(3, "comment-21", True)


async def test_github_draft_switches_over_graphql():
    pulls = {"number": 3, "state": "open", "draft": True, "node_id": "PR_n", "head": {}, "base": {}}
    backend, router = github({("GET", f"{R}/pulls/3"): pulls,
                              ("POST", "/graphql"): {"data": {"markPullRequestReadyForReview": {}}}})
    await backend.pr_update(3, title=None, body=None, draft=False)
    sent = router.bodies("POST", "/graphql")
    assert len(sent) == 1 and "markPullRequestReadyForReview" in sent[0]["query"]
    assert sent[0]["variables"] == {"id": "PR_n"}


async def test_github_project_must_be_owner_slash_name():
    with pytest.raises(ForgeError, match="owner/name"):
        GitHub(Api("https://api.github.com", "t"), "group/sub/repo")
