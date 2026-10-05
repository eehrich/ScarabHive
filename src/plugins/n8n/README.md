# n8n

Build n8n workflows from a request and prove that they run. The agent `n8n_agent` finds the nodes, writes
the workflow as n8n Workflow SDK code, creates it as an unpublished draft and test-runs it with every call
to the outside world mocked; on request it publishes, takes offline, archives or triggers it. The plugin
forwards a chosen set of n8n's own instance MCP tools and lays ScarabHive's policy on top: it touches only
workflows it tagged, refuses shell, file and legacy code nodes (also in the sub-workflows a published
workflow starts), and pins everything in a test that could act outside the run. Tested against n8n 2.39.9.

- **Tools** -- nineteen `n8n_*` tools: node knowledge (six, MCP key only), validate, create, update, read,
  test, executions, publish/unpublish/archive (only with `allow_publish: true`) and trigger (production
  webhook, with a wake when the run ends). No delete, no raw execute.
- **Agent** -- `n8n_agent` (UI and tool, 60 steps), three on-demand skills (`n8n-building`,
  `n8n-testing`, `n8n-recipes`) and its own OKF memory `data/okf/n8n` (instance `n8n_okf`).
- **Hooks / panel** -- none of its own.

Enable it by deploying n8n with [docs/deploy/](docs/deploy/README.md) and putting `N8N_BASE_URL`,
`N8N_MCP_KEY` and `N8N_API_KEY` (and `N8N_PUBLIC_URL` if callers reach n8n elsewhere) into
`config/secrets.env`; the instances in `agents/n8n.yaml` are picked up by convention. Without the values
the instance offers no tools.

The full manual -- test runs and the pin plan, publishing and triggering, the locks, every tool, what the
model sees and the settings -- is the plugin's guide, `n8n.guide`, in the Help panel. Design and measured
facts: [docs/design.md](docs/design.md), [docs/n8n_facts.md](docs/n8n_facts.md).
