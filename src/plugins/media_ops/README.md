# Media Ops Plugin

Moves images, sounds and videos between the disk and an agent's
conversation: an agent loads a file so its model can see or hear it, and
writes media that only exists in the conversation (a pasted image) to a file.
Every path, read or written, must lie inside the configured folders.

- **Tools:** `<instance>_load`, `<instance>_list_context`, `<instance>_save`
  (`save` is not offered on a `read_only` instance).
- **Hooks:** none.
- **Panel:** none.

Enable it with the `media_ops` entry in `config/plugins.yaml`
(`allowed_directories`, `max_file_size_mb`) and give an agent the tools with
`+media_ops/*` in its allowlist.

The full manual is `media_ops.guide`, in the Help panel.
