# OKF

Curated knowledge for agents, kept as plain markdown in the Open Knowledge Format (OKF v0.1): a bundle is a folder,
every `.md` file in it a concept with a YAML header (at least a non-empty `type`), and markdown links between
concepts make the bundle a graph. Agents search, read, walk and write bundles; people can read and edit the same
files and keep them in git.

- **Tools** `okf_list`, `okf_search`, `okf_read_concept`, `okf_neighbors`, `okf_subgraph`, `okf_write_concept`,
  `okf_append_log`, `okf_reindex`, `okf_validate` -- sandboxed to the configured directories; writes keep unknown
  header keys and are locked against concurrent writers.
- **Hook** `okf_context_injection` (off by default) -- appends the concepts that fit the person's latest message,
  plus the concepts they link to, to the history before an LLM call; only when the selection changed, so the cached
  prompt stays intact.
- No panel.

Enable it in `config/plugins.yaml` (`okf: {type: okf, enabled: true, allowed_directories: [data/okf]}`) and allow
`+okf/*` in an agent's tool list; the hook is switched on per agent under `hooks.overrides` with a `hook_bundle`.

The full manual -- the format, every parameter and answer, the hook's settings and the server settings -- is the
plugin's guide, `okf.guide`, in the Help panel.
