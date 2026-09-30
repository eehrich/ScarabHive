# Markdown Formatter

Makes agents' answers readable where they are shown: the Markdown a model writes reaches the web chat as cleaned
HTML (headings, lists, tables, code blocks for the browser's colouring) and the terminal as Markdown drawn with
colours. The session keeps the Markdown; only the copy that is shown is converted.

- **Hooks** `format_markdown_output` (converts the shown answer; changes nothing the model sees) and
  `inject_markdown_system_prompt` (optional: appends a short "write Markdown" instruction to the first system
  message before each LLM call).
- No tools, no panel.

The plugin is enabled in `config/plugins.yaml`, but both hooks are off for all agents by default; an agent turns one on
in its YAML under `agent_config.hooks.overrides` (`markdown_formatter.format_markdown_output: {enabled: true}`).
Settings go in the server entry's `config:` block.

The full manual -- what is shown where, the HTML cleaning and its settings, the cost of very long answers, and what
the model sees -- is the plugin's guide, `markdown_formatter.guide`, in the Help panel.
