# image_gen

Generates and edits images through configured `llm_system.image_profiles` (for example Google Gemini image models
or OpenRouter image models), writes the result into a path sandbox, and optionally attaches a preview so a
vision-capable agent can inspect and iterate on it.

- **Tools** `<instance>_generate` (text-to-image and multi-reference image editing) and `<instance>_list_profiles`.
- **Sandboxes** Separate `PathSandbox` instances for writing (`output_directories`, default `data/image_gen`) and
  reading reference images (`input_directories`, default same as `output_directories`); secret files and host/UNC
  paths are refused before any API call.
- **Hooks / panel** -- none. `file_checkpoints` counts `<instance>_generate` as an untracked write.

Enable an instance in `config/plugins.yaml` (`type: image_gen`, `output_directories`, optional `input_directories`,
`profiles`, `default_profile`) and allow `+<instance>/*` in an agent's tool list.

The full manual -- tools, parameters, model experience, and settings -- is `image_gen.guide` in the Help panel.
