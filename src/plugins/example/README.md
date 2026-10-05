# example

A template for plugin authors: a small tool server to copy when starting a plugin of your own. Its tools are
deliberately trivial so that the pattern is what remains to read -- tools declared in `schema.yaml` and routed by
name, settings read from the server entry with defaults in code, and the handler contract (own argument checks,
errors as `{"status": "error"}`, a status end line naming the result, bounded answers).

- **Tools** `example_calculator`, `example_formatter`, `example_status` -- arithmetic on two numbers, a text
  transformation, the server's own status.
- **Hooks / panel** -- none.

Enabled in `config/plugins.yaml` (`example: {type: example, enabled: true}`); no shipped agent allows it -- add
`+example/*` to an agent's `tools.allowed`. `python -m plugins.example.cli` drives the tools without an agent.

The full manual -- what each file shows, every tool with its answers and errors, the settings -- is the plugin's
guide, `example.guide`, in the Help panel.
