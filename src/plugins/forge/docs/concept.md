# forge — GitLab and GitHub for the coder

As of 2026-09-28. Assignment from the user: the `coder` should access GitLab —
work off tickets, review and edit merge requests, check builds.
GitHub must work as well, as an optional second connection.

## 1. Decisions

| # | Decision | Rationale |
|---|---|---|
| E1 | **A plugin of our own, no MCP.** | The GitLab MCPs (GitLab's built-in `/api/v4/mcp`, `@zereight/mcp-gitlab`) only pass the REST API through — unlike with n8n there is no logic in them that we would want to rebuild. A plugin of our own caps results (job logs and diffs quickly reach megabytes), marks foreign text, checks before the merge and writes our status lines. The official MCP also needs Premium/Ultimate and OAuth in the browser. |
| E2 | **One tool set, two backends.** GitLab first, GitHub optional. | The coder learns one workflow (skill `forge-workflow`), not two foreign tool sets. Which backend applies is decided by the repo's configuration — never by the model. |
| E3 | **Self-hosted, configurable** (user, 2026-09-28). | `api_url` per host, own CA via `ca_bundle`. GitHub Enterprise Server works through the same setting. |
| E4 | **The coder may merge** (user, 2026-09-28). | `pr_merge` checks by itself: open, not a draft, CI green on the current head commit, no open threads. The checked commit goes to the merge API as `sha` — if a push comes in between, the platform refuses. Enabled per repo (`allow_merge`), off by default in the code. |
| E5 | **What is meant is our `coder`, which uses `coding_cli`** (user, 2026-09-28). | The `coder` gets the forge tools and drives the workflow. Claude Code gets none of it; it keeps programming only in the worktree. |
| E6 | **Pushing happens only through forge.** | `coder_shell` blocks `git push` ([tools.yaml](../../coder/agents/tools.yaml)), `coding_cli` never pushes. `forge_push` pushes only to branches with the prefix (`branch_prefix`, default `scarabhive/`), never with force. The token reaches git only as a credential for the platform's origin (a helper reads it from the environment of the one git process; an `extraHeader` failed with git-lfs, facts.md F-GL10) — not in the remote URL, not in the clone's configuration, not on a command line, and no hook runs with it. Certificates are always verified, even against a global `http.sslVerify false` (F-ENV1). It is not secret from the coder's shell: that can read `config/secrets.env` like any file. The binding boundary is E7. |
| E7 | **The platform regulates the permissions.** | Bot account with the role *Developer*. Default branch protected: push Maintainer only (or nobody), merge Developer + Maintainer. This bolt holds even if forge has a bug. |
| E8 | **Foreign text is data.** | Issue, comment and log text is written by people and programs. forge returns it as `{"untrusted": true, "content": …}` — like n8n and `coding_cli`. The coder works on a ticket only if it is assigned to the bot account or the user names it; it cannot assign itself (no tool for that). It merges only at the user's request, never because text on the platform demands it (review 2026-09-28, S3). |
| E9 | **Waiting for CI blocks, no wake-up call.** | `ci_status` with `wait_s` (≤ 600 s) — the same way as `coding_cli_get_run`. Right after a push there is no pipeline yet (F-CI1); "none" is therefore waited out for up to 45 s and only then taken as "no CI". The wake-up mechanism from n8n (watch, markers across processes) would be the next step if pipelines regularly run longer. |
| E10 | **Webhooks: GitLab wakes the coder** (user, 2026-09-28: "we need webhooks"). | An assigned issue starts a coder run, a comment or a red pipeline wakes the session that works on it. Design in §7. Not GitHub: github.com cannot reach a ScarabHive on the LAN; the same distribution would take it up. |

## 2. Structure

```
src/plugins/forge/
  plugin.toml, plugin.py
  server.py      Tools: check arguments, resolve repo, policy, caps, status
  http.py        shared httpx client, error classes, paging
  gitlab.py      REST v4 → uniform shape
  github.py      REST + GraphQL (review threads) → uniform shape
  gitops.py      clone/fetch/switch/push with the token only in the environment
  schema.yaml    Tools (Jinja: only offered if a repo is configured)
  agents/forge.yaml            server entry, enabled, without repos
  skills/forge-workflow/SKILL.md   workflow ticket → merge
  tests/         unit (MockTransport, local bare repos), config, live
  tests/live/gitlab/compose.yaml   test instance GitLab CE + runner
```

**Uniform shape.** Backends return normalized dicts; `server.py`
knows no platform. CI states are mapped to `pending | running | success |
failed | canceled | skipped | manual`, merge/pull requests are called
`pr` in the tool names.

**Where the platforms differ** (the actual work):

- *Comments:* GitLab has discussions, resolvable via REST. GitHub has
  conversation comments, inline review comments and reviews; threads are resolved
  only via GraphQL. `pr_discussions` merges them into one list.
- *CI:* GitLab — pipeline per ref. GitHub — check runs (Actions and foreign CI)
  plus old commit statuses on the head commit; the state is the sum.
- *Logs:* GitLab `/jobs/:id/trace` (text). GitHub `/actions/jobs/:id/logs`
  (redirect to text); a check run of a foreign CI has no log here.

## 3. Configuration

The server entry comes with the plugin (`agents/forge.yaml`, without repos).
Hosts and repos are the operator's values and belong in `config/plugins.yaml`
(the entries are merged):

```yaml
plugins:
  servers:
    forge:
      hosts:
        git.example.com:
          provider: gitlab
          api_url: https://git.example.com/api/v4
          token_env: FORGE_GITLAB_TOKEN      # name of the variable in config/secrets.env
          ca_bundle: ""                      # own CA, if needed
      repos:
        app:                                 # the name the coder uses
          host: git.example.com
          project: team/app
          allow_merge: true
          # path: data/workspace/forge/app   # local clone (default)
      branch_prefix: "scarabhive/"
      merge_method: squash                   # merge | squash | rebase
```

Without a repo the instance offers no tools. The token is never in the YAML,
only the name of the variable.

## 4. Tools

| Tool | Does | Policy |
|---|---|---|
| `checkout` | clones or fetches the state, creates a working branch or switches to it | only into the configured path; never across uncommitted changes |
| `push` | pushes a local branch | target branch with prefix, never default, never force |
| `issue_list`, `issue_get` | read tickets | text untrusted, capped |
| `issue_comment`, `issue_update` | comment, labels, close/reopen | assigning stays with humans |
| `pr_list`, `pr_get`, `pr_diff`, `pr_discussions` | read merge requests | diff page by page per file |
| `pr_create`, `pr_update` | create (optionally "Closes #n"), title/text/draft | source must be pushed |
| `pr_comment` | comment, reply to a thread, resolve a thread, line comment | |
| `pr_merge` | merge | `allow_merge`, `sha` mandatory, green on the head commit, no open threads or change requests; afterwards delete only branches under the prefix |
| `ci_status` | pipeline/checks of a PR, branch or commit; optionally waits | |
| `ci_job_log` | end of the job log, optionally filtered | ANSI removed, capped, untrusted |
| `ci_retry` | restart a job | |

## 5. Setting up the platform

**GitLab:** bot user (or project/group access token) with the role
*Developer*, personal access token with scope `api`. Default branch protected:
*Allowed to push* = Maintainers or No one, *Allowed to merge* = Developers +
Maintainers. Whoever enables "Pipelines must succeed" has the bolt twice.

**GitHub:** fine-grained token only for the approved repos — Contents,
Issues, Pull requests, Actions: read/write; Checks, Commit statuses, Metadata:
read. Ruleset or branch protection on the default branch.

## 6. Measuring

Live test instance: GitLab CE 19.4.1 with a runner on the Docker test host
(`tests/live/gitlab/`). Live tests run only with `FORGE_LIVE=1`. Findings
are in `facts.md`.

## 7. Webhooks

```
GitLab ──POST /plugins/forge/webhook──▶ API ──▶ events.db ──notify──▶ session
          X-Gitlab-Token                 │        (inbox)       (wake-up: free → new
                                         │                       agent-cli process,
                                         └─ new work: create     busy → next step)
                                            session for webhook.user
Hook deliver_webhook_events (pre_llm_call) ──▶ hands over the inbox as one message
```

| # | Decision | Why |
|---|---|---|
| W1 | **Route on the main API, not a port of its own.** | Only the API process mounts web routers; a listener in the plugin would run in every `agent-cli` process. Like stategraph's callback: the path is released anonymously in both auth layers, the route checks by itself. For this the API must listen on the LAN (`network.host`, in `config/local.yaml`: the server behind nginx must not inherit it) — and then may show the LAN only this path: `network.remote_paths` (core, `auth/remote_paths.py`) answers every other request that does not come from this machine with 404, before any other layer. Without this guard any machine on the LAN could have registered (`POST /auth/register` is open) and started the coder with a shell (webhook review). |
| W2 | **Authenticity via `X-Gitlab-Token`**, constant-time against the host's `webhook_secret_env`. | GitLab does not sign, it sends the secret along. Without a configured secret the route accepts nothing. Body at most 1 MB, only configured projects. |
| W3 | **Answer immediately, work afterwards.** | GitLab waits 10 s and disables a hook after failures. Duplicate deliveries are recognized by the `Idempotency-Key` — a resend keeps it, the `X-Gitlab-Event-UUID` renews it (F-GL15). If the distribution fails, the key is forgotten again so that a resend gets through. |
| W4 | **One distribution for everything: inbox + wake-up.** | Every event becomes an entry in a session's inbox, then `notify`. Free session → a new process resumes it; busy → marker, the hook hands it over at the next step. New work gets a new session (agent `coder`, user `webhook.user`) and starts the same way — no second start path. |
| W5 | **forge remembers which session works on what.** | `checkout` (branch under the prefix), `pr_create` (MR, branch, closed issue), `pr_comment` (MR) and `issue_comment` bind their target to the calling session (`_session_id`, `_user_id`); the distribution binds the issue to a new session. The most recent binding wins. A binding to a deleted session drops out at the next event; new work then gets a new session. Per target forge distributes one at a time: assignment and mention at once start one session, not two. |
| W6 | **Events:** issue assigned to the bot → new work (or the bound session); comment on a bound MR/issue → wake; comment that mentions the bot without a binding → new session that answers (no assignment to commit); pipeline red on a bound branch → wake. Issue changes and comments by the bot itself are discarded (otherwise it wakes itself with every comment) — a pipeline is not: its `user` is whoever pushed, on its own branches therefore the bot. A mention on a bound target is, for its session, a comment like any other. | These are the places where a human would otherwise have to nudge the coder. |
| W7 | **The message names only identifiers**, no foreign text: "New comment from @alice on !14 (note 345) — read it with forge_pr_discussions". | The inbox is handed over as a trusted message; titles and comments still come through the tools, as `untrusted`. |
| W8 | **A webhook run merges only with `webhook.merge: true`.** Default: open the MR, CI green, then report — a human merges. | Nobody in this session asked for the merge (E8). |
| W9 | **Caps:** at most `webhook.max_new_per_hour` new sessions (default 6) and `webhook.max_wakes_per_hour` wake-ups (default 20). Over the wake cap the message stays in the inbox and arrives with the next run; over the cap for new sessions nothing is started, and forge forgets the `Idempotency-Key` — a resend from GitLab gets through later. | Tokens cost money; a script that assigns issues, or any commenter on a public project, must not generate a bill. The wake-up itself prevents only simultaneous runs, not consecutive ones (webhook review). Without `session_presence` forge starts no session — nothing would let it run. |
