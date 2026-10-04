# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.7.x   | yes       |
| < 0.7   | no        |

Fixes land on the latest release line. There are no backports to older
versions.

## Reporting a vulnerability

Please **do not** open a public issue, pull request or discussion for a
security problem.

Report it privately through GitHub's private vulnerability reporting: open the
repository's **Security** tab and click **Report a vulnerability**
(direct link: <https://github.com/eehrich/ScarabHive/security/advisories/new>).

Include as much of this as you can:

- the version or commit you tested,
- the affected component (core API, a plugin, the web UI, ...) and the
  configuration needed to reproduce it -- **without** real API keys, tokens or
  passwords,
- steps to reproduce or a proof of concept, and what an attacker gains.

We will acknowledge the report in the advisory, keep you updated there, and
credit you in the advisory once a fix is published, unless you prefer not to
be named.

## Scope

ScarabHive runs LLM agents that act through tools, and some tools are powerful
**by design**: `terminal` runs shell commands, `file_ops` reads and writes
files, `ssh_control` runs commands on remote machines. Whoever can talk to an
agent -- and any content the agent reads, such as a web page or a file -- can
make it use the tools it has been granted. Which tools an agent gets is configuration
(`agent_config.tools.allowed` / `blocked` in its YAML).

### Reported as vulnerabilities

- **Authentication bypass**: reaching an endpoint that requires
  authentication without valid credentials; forging or reusing JWTs or API
  keys beyond their intended validity.
- **Privilege escalation**: a `user` or `guest` reaching admin-only endpoints
  or panels; one user reading, changing, stopping or taking over another
  user's sessions or runs; a user running an agent, or using a tool it
  serves, whose `metadata.min_role` their role does not reach.
- **Escaping a boundary the code enforces**: an agent calling a tool its
  `tools.allowed` / `blocked` lists do not grant; a path outside the allowed
  directories of `file_ops` (or another plugin that checks paths); code in
  `script_interpreter` or `tool_script` reaching beyond their restricted
  interpreter, or a `tool_script` script calling a tool its agent is not
  granted; a process leaving the `read-only` or `workspace-write` process
  sandbox where a sandbox backend enforces it.
- **Secret leakage**: API keys from `config/secrets.env` or the environment,
  the JWT signing key, password hashes or API keys exposed through the API,
  the web UI or the logs to someone who is not an administrator.
- **Web vulnerabilities** in the bundled UI or API (XSS, CSRF, open redirects,
  ...) that let one user act as another.
- Vulnerabilities in how ScarabHive uses a dependency. A vulnerability in the
  dependency itself belongs to its upstream project.

### Expected behaviour, not vulnerabilities

- An agent doing what an enabled tool does: running a shell command through
  `terminal`, writing a file inside the allowed directories of `file_ops`,
  and so on -- including when prompt injection made it do so.
- Getting around the command pattern list of `terminal`. It guards against
  accidents and is documented as not being a security boundary
  (`src/plugins/terminal/security.py`); the boundary is whether an agent is
  granted the tool at all.
- Commands running unconfined under the process sandbox mode
  `danger-full-access`, which is the default.
- Anything that requires admin rights: administrators can change the
  configuration and enable any tool.
- Deployments that keep the development values of the shipped
  `config/config.yaml` (see the checklist below).

### Known limitation: data separation between users

Multi-user mode (`auth.enabled`) authenticates users and separates
**sessions** per user. Data that plugins keep for themselves (plugin databases, panels, log
views) is not yet separated per user in 0.7.x. Until it is, treat the users of
one instance as trusted with each other's plugin data, or run one instance per
group of users who trust each other. Reports about session separation are in
scope; reports that only restate this limitation for a plugin's own data are
not.

### Per-agent role gate (`metadata.min_role`)

With `auth.enabled`, an agent whose metadata sets `min_role` (`guest`, `user` or
`admin`) runs only for accounts of at least that role -- on every path a run
starts: `/run`, `/events`, `/chat/command`, sessions created for it, the
OpenAI-compatible API (`openai_api`: not listed as a model, 404
`model_not_found`), sub-agents (sub-agent manager), agents called as tools,
stategraph activities, woken sessions, and each tool the agent itself serves. A
run that cannot be tied to an
account is judged as `anonymous`: refused unless anonymous access is enabled with
a sufficient role (the sub-agent manager and an agent's own tools refuse it
outright). Over HTTP, a refusal answers like an agent that does not exist, except
for the default agent on `/run` and `/events` with no agent named and for
`POST /api/sessions`, which answer 403.
`metadata.visibility` only decides where an agent is listed; it is not an access
control.

The shipped configuration gates every agent with a shell (`terminal`,
`coder_shell`), `coding_cli`, `ssh_control`, a tool that runs arbitrary code
(`blender_execute`, `godot_script`) or file access to the whole checkout at
`admin`: `amiga_coder`, `blender_agent`, `claude_code_agent`, `coder`,
`coder_explorer`, `coder_reviewer`, `coder_tester`, `file_ops_test_agent`,
`gamedev`, `gamedev_tester`, `godot_agent`, `skills_agent`,
`skills_agent_multimodal`, `sysadmin_agent`. `state_graph_agent` and
`state_graph_agent_ui` carry a terminal that a whitelist restricts to one
analysis script (`state_graph_terminal`: one command per call, started in the
directory the server runs from -- the checkout, as every relative path of the
configuration assumes; a whitelisted terminal takes no `cwd` and no `env_vars`
from the model and refuses control characters), and are gated at `user`.
Gate every agent you add with such tools, and every agent with file access to
the checkout or above, to `config/`, to `data/` itself (it holds the user store
and every user's sessions; a folder of the agent's own below it, such as
`data/workspace`, is fine) or with write access to `src/` (the code that runs).
The gate is inherited through `type:`, and `min_role: null` does not lift an
inherited one.

Remaining limits:

- Without `auth.enabled` there are no roles and no gate; the server logs the
  gated agents it cannot enforce at start.
- `agent-cli` and `agent-run` are local and trusted: their default user
  `cli_user` passes every gate there (not in the API, and not while an account
  of that name exists).
- `n8n_agent` is not gated, and what it can do depends on the n8n instance
  (Code and Execute Command nodes).
- `POST /api/sessions` answers a gated agent with a 403 (it accepts names of
  agents that do not exist).
- A config reload moves the gate of running agents; the wake check and the
  start-up warning read the start configuration until a restart.

## Hardening a deployment

- Give the installation its own `auth.secret_key` and the admin its own
  password before the first start: the install scripts do both
  (`python -m agent_system.config.local_layer signing-key`,
  `python -m agent_system.auth.first_admin`). The shipped signing key is for
  development; the shipped config sets no admin password, so the API generates
  one on its first start and shows it on the console once. The config loader expands `${VAR}` placeholders from the
  environment and `config/secrets.env`, e.g. `secret_key: "${AUTH_SECRET_KEY}"`.
  The server does not start with an empty or short (under 32 characters) key,
  and logs an error for a published one, such as the shipped development key;
  `auth.reject_default_secret_key: true` makes that a startup error as well.
- Replace the wildcard in `auth.cors_origins` with the origins you serve.
- `POST /auth/register` is reachable by anyone who reaches the server and
  creates a `user` account. Turn it off (`auth.registration.enabled: false`) or
  hold new accounts until an admin activates them
  (`auth.registration.require_approval: true`, as the shipped configuration
  does).
- Keep the server on the loopback interface and put a reverse proxy with TLS
  in front of it. `docker-compose.yml` publishes the port on `127.0.0.1` only.
- Grant `terminal`, `file_ops`, `ssh_control` and similar tools only to agents
  whose users you would also give a shell. In the provided container, what
  they can reach on disk is the container and what is mounted into it; their
  network access is not restricted.
