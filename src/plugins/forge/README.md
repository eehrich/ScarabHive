# forge

GitLab and GitHub for the `coder`: issues, merge/pull requests, CI, push and
merge — one set of tools for both platforms, under ScarabHive's policy.
Concept and decisions: [docs/konzept.md](docs/konzept.md); measurements:
[docs/facts.md](docs/facts.md).

## Setup

The instance `forge` comes enabled with the plugin
([agents/forge.yaml](agents/forge.yaml)) and offers no tools until a
repository is configured. Hosts and repositories are the operator's values and
go into `config/plugins.yaml` — the plugin's own YAML is merged after it and
therefore sets nothing an operator would tune:

```yaml
plugins:
  servers:
    forge:
      hosts:
        git.example.com:
          provider: gitlab                     # gitlab | github
          api_url: https://git.example.com/api/v4
          token_env: FORGE_GITLAB_TOKEN        # the NAME of the variable in config/secrets.env
          ca_bundle: ""                        # a file with the internal CA, if needed
          # tls_verify: false                  # only for a self-signed platform without a CA file:
          #                                    # turns certificate checks off for this host, API and git
        github.com:
          provider: github                     # api_url defaults to https://api.github.com;
          token_env: FORGE_GITHUB_TOKEN        # GitHub Enterprise: https://<host>/api/v3
      repos:
        app:                                   # the name the coder uses
          host: git.example.com
          project: team/app                    # GitHub: owner/name
          allow_merge: true                    # default false; merges need the user's request (skill)
          # require_ci: true                   # merge only on a green pipeline (default)
          # path: data/workspace/forge/app     # the local clone (default)
      # branch_prefix: "scarabhive/"           # forge pushes only there
      # merge_method: squash                   # merge | squash | rebase
      # delete_branch_after_merge: true
      # timeout: 30
```

A repository whose host, provider or token is missing is left out, with a
WARNING in the log; the others stay. A new configuration takes a restart.

**Rights live on the platform** (docs/konzept.md E7):

- **GitLab:** a bot user (or a project/group access token) with role
  *Developer*, a personal access token with scope `api`. Protect the default
  branch: *Allowed to push* Maintainers or No one, *Allowed to merge*
  Developers + Maintainers.
- **GitHub:** a fine-grained token for the listed repositories only —
  Contents, Issues, Pull requests, Actions: read and write; Checks, Commit
  statuses, Metadata: read. A ruleset or branch protection on the default
  branch.

`git` must be on PATH (≥ 2.31, for `GIT_CONFIG_COUNT`); repositories with
Git LFS need `git-lfs` too. For Claude Code to work in a forge clone, register
the clone's path as a `coding_cli` workdir.

Certificates are checked for every host, also when the machine's git config
says `http.sslVerify false` (it does on the development machine) — forge's
git calls override it for the platform's origin, and refuse to run where a
more specific key turns the check off for the platform's host. A self-signed
platform needs `ca_bundle`, or an explicit `tls_verify: false` (logged). A CA
that the git config names (`http.sslCAInfo`) is trusted: that is the
operator's setting.

## Tools

`checkout`, `push`, `issue_list`, `issue_get`, `issue_comment`,
`issue_update`, `pr_list`, `pr_get`, `pr_diff`, `pr_discussions`,
`pr_create`, `pr_update`, `pr_comment`, `pr_merge` (only where a repository
allows it), `ci_status`, `ci_job_log`, `ci_retry` — each prefixed with the
instance name, descriptions in [schema.yaml](schema.yaml). The coder's loop
is the skill [forge-workflow](skills/forge-workflow/SKILL.md).

## Model Experience

**What the model sees.**

- Tool descriptions as in [schema.yaml](schema.yaml); every tool takes `repo`
  as an enum of the configured names. Which platform a repository is on is
  never the model's choice.
- Answers are `{"status": "success", ...}` or `{"status": "error", "error":
  "<what to do>"}`. Refusals name the reason and the way out:
  `forge pushes only to branches under 'scarabhive/': pass remote_branch=scarabhive/main or similar`,
  `app!1 not merged: 1 threads are open`,
  `app!7 not merged: it is a draft (pr_update draft=false when it is ready); the platform says: draft status`,
  `app!1 not merged: its head is 2990af4f2337, not the deadbeef you checked`,
  `the remote branch scarabhive/x has commits that scarabhive/x does not: checkout the branch again (it fetches) and bring them in, then push -- nothing is forced`.
- Text people or programs wrote — issue and request titles and bodies,
  comments, diffs, job logs — comes as `{"untrusted": true, "content": ...}`.
  The coder's prompt and the skill name that marker as data, never an
  instruction.
- `pr_get` carries what a merge decision needs: `mergeable` (`null` while the
  platform checks), `merge_blockers` in the platform's words, `ci` on the head
  with its `failed_jobs`, and `open_threads`.
- `ci_status` lists failed jobs first, each with the `id` that `ci_job_log`
  and `ci_retry` take; `wait_s` waits (up to 600 s) and says `still_waiting`
  when it ran out. `ci_job_log` returns numbered lines without colour codes,
  timestamps or section markers.
- `pr_merge` requires `sha` (at least 7 hex digits) and refuses while a
  thread is open — on GitHub also while a reviewer's latest decision is
  "changes requested"; `pr_discussions` lists that review as `blocking`.
- The coder's prompt has a section on forge only when these tools are there
  (`has_tool('forge_checkout')`); it states the one exception to "commits are
  the user's": an issue assigned to the bot is the order to commit on its
  branch, in its forge clone. The skill allows a merge only on the user's
  request, never on text from the platform; the bot cannot assign itself.

**Token and cache effect.** Append-only: tool results only, nothing rewrites
earlier messages, no hook. Every result is capped — a description 8k
characters, a comment 3k, all comments or threads of one answer 24k (past that,
up to 40 entries as a 200-character excerpt marked `shortened`, readable in
full with `thread_id`; beyond, a `left_out` count that the skill takes as "no
merge on this listing" — discussions the platform API did not hand over at
all hold `pr_merge` itself), one page of diffs 30k (a larger file gets its own
page), a log excerpt 20k and at most 2000 lines, at most 60 jobs. The prompt section is static (rendered from the
tool list, which does not change within a session).

**Known gaps.**

- **The token is no secret from the coder's shell.** It lives in
  `config/secrets.env`, which `coder_shell` can read like any file
  (tools.yaml: "treat it as the real machine"). What forge guarantees is
  narrower: the token reaches git only as a credential for the platform's
  origin (a helper reading it from the environment) — never in a remote URL,
  a clone's config, a command line or a hook's environment — and forge's own
  pushes carry one branch under the prefix, unforced, without tags or
  submodules. The shell's block on `git push` is a speed bump. The binding
  limit is the token's role on the platform.
- **An assigned issue is the gate, in the prompt.** The tools cannot see who
  assigned an issue or whether a user asked for a merge; a prompt injection
  in an issue could still reach `pr_merge`. What the tool enforces: the reviewed
  head, open, not draft, no open thread or changes request, every discussion
  read (past 1000 on GitLab, or 500 review threads / 1000 reviews on GitHub, a
  person merges), green CI on that head, the platform's own rules; after the
  merge only a branch under the prefix is deleted. A hold in a plain comment
  is the skill's to respect, not the tool's: text cannot be judged there.
- **GitHub CI can read green early** only where a CI other than Actions
  registers its checks late: Actions runs count while their jobs do not exist
  yet, other apps only through required checks on the platform. Not measured
  (facts.md). GitHub is live-tested against a private repository of one
  account, so a changes request (which an account cannot make on its own pull
  request) and pull requests from forks are tested against mocks only.
- **GitLab merges with the project's merge method**; forge can only ask for
  a squash. `method: rebase` is refused there.
- **A cancelled call keeps its clone locked until git ends**, within one
  process; the API and an `agent-cli` on the same clone rely on git's own
  lock files.
- **CI waits by blocking, not by waking** (docs/konzept.md E9): `ci_status`
  holds the call up to `wait_s`; a longer pipeline needs another call.
- **Downstream pipelines** (GitLab trigger jobs) are listed as one job with a
  note; their own jobs are not. A request from a fork shows the fork's jobs,
  without their logs — or only the pipeline's state where the token cannot
  read the fork.
- **A redirect in the git config is refused**, not worked around: an
  `insteadOf`/`pushInsteadOf` rule for the platform's URL (an empty one
  matches every URL) or a remote named by that URL would make git talk
  elsewhere, with another identity.
- **GitHub:** `related_prs` of an issue is not read (`null`); a check run of
  another CI system has no log here; reviewing a pull request from a fork
  works, checking out its branch does not (forge fetches only the project's
  own branches).
- **Not built:** webhooks — the platform starting or waking the coder
  (docs/konzept.md E10).
