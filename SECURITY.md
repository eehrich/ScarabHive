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
  user's sessions or runs.
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

### Known limitation: every user may run every agent

There is no per-agent role check yet: any signed-in user can start any agent
by name through `/run` or `/events` -- `metadata.visibility` only decides which
agents the UI lists. An agent whose allowlist names a shell tool (`terminal`,
`coder_shell`, `coding_cli`), `ssh_control`, or a file_ops instance with `.` in
its allowed directories gives every user what that tool can do, including
reading the logs and `config/secrets.env`. Several shipped agents have such
tools (among them `sysadmin_agent`, the `coder` and `gamedev` harnesses,
`godot_agent`, `amiga_coder`, `skills_agent`); on an instance whose users you
would not give a shell, remove every such agent from the configuration.

## Hardening a deployment

- Change `auth.secret_key` and `auth.default_admin_password` in
  `config/config.yaml` before the first start; the shipped values are for
  development. The config loader expands `${VAR}` placeholders from the
  environment and `config/secrets.env`, e.g. `secret_key: "${AUTH_SECRET_KEY}"`.
  The server does not start with an empty or short (under 32 characters) key,
  and logs an error for a published one, such as the shipped development key;
  `auth.reject_default_secret_key: true` makes that a startup error as well.
- Replace the wildcard in `auth.cors_origins` with the origins you serve.
- Keep the server on the loopback interface and put a reverse proxy with TLS
  in front of it. `docker-compose.yml` publishes the port on `127.0.0.1` only.
- Grant `terminal`, `file_ops`, `ssh_control` and similar tools only to agents
  whose users you would also give a shell. In the provided container, what
  they can reach on disk is the container and what is mounted into it; their
  network access is not restricted.
