# forge

GitLab and GitHub for the `coder`: issues, merge/pull requests, CI, push and merge -- one set of tools for both
platforms, under ScarabHive's policy. It pushes only to branches under a prefix (`scarabhive/`), never to the
default branch and never forced; it merges only the reviewed head of an open, non-draft request without open
threads and with green CI, and only where a repository allows it; text from the platform comes back marked
untrusted. On GitLab a project webhook starts the coder for an issue assigned to the bot and wakes the session
that works on a commented request or a failed pipeline.

- **Tools** -- seventeen `forge_*` tools: `checkout` and `push` on a local clone, issues (list, get, comment,
  update), requests (list, get, diff, discussions, create, update, comment, merge) and CI (status with waiting,
  job log, retry). The coder's loop is the skill `forge-workflow`.
- **Hooks** -- `deliver_webhook_events` hands a session the webhook's news before an LLM call;
  `mark_webhook_events_delivered` marks it delivered once the run is saved.
- **Web** -- the webhook route `POST /plugins/forge/webhook`; no panel.

The instance comes enabled with the plugin and offers no tools until a repository is configured: hosts and
repositories go under `plugins.servers.forge` in `config/plugins.yaml`, the tokens into `config/secrets.env`.
The coder allows `+forge/*`.

The full manual -- the ticket loop, the policy, the webhook, every tool, what the model sees and the settings --
is the plugin's guide, `forge.guide`, in the Help panel. Design and measured facts:
[docs/konzept.md](docs/konzept.md), [docs/facts.md](docs/facts.md).
