# File Operations

The tools an agent works on files with: read, list, find by name, search text, find code by meaning, and -- unless
the instance is read-only -- create, replace, edit, delete, move and rename. Every path must resolve inside the
instance's `allowed_directories`; network and device paths (`\\host\share`, `//host/share`, `\\?\`, `\\.\`,
`\??\`) are refused on their text, so the host is never contacted. The plugin runs as
several instances (`file_ops`, `coder_fs`, `coder_fs_ro`, ...), each with folders and tool names of its
own.

- **Tools** `<instance>_read_file`, `_list_directory`, `_search_files`, `_grep_search`, `_semantic_search` (off
  unless configured), and on a writable instance `_manage` (create, delete, move, rename) and
  `_replace_string_in_file`.
- No hooks, no panel.

Enable an instance in `config/plugins.yaml` (`file_ops: {type: file_ops, enabled: true, allowed_directories:
[data/workspace]}`) and allow `+file_ops/*` in an agent's tool list.

The full manual -- where an agent can read and write and how paths are checked, every parameter and answer, the
search filters, the semantic index and the server settings -- is the plugin's guide, `file_ops.guide`, in the Help
panel.
