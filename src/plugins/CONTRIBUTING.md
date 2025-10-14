Plugin contribution guidelines

- Put each plugin in its own folder under `plugins/<plugin_name>/`.
- Use `plugin.py` as the entrypoint file. Export exactly one symbol: `PLUGIN_FACTORY` (callable taking name, config, ssl_verify).
- Include `plugin.yaml` with metadata: `name`, `description`, `version`.
- Keep plugin dependencies minimal and document them in `plugin.yaml` if needed.
- Prefer async `call()` implementations to match MCPServer interface.
- Activate and configure it in `config/plugins.yaml`
