# file_checkpoints

Checkpoints for files, not only for the conversation. The plugin records how each file looked before the agent
first changed it in a turn, so the chat can put it back: `/undo files` drops the last exchange together with its
file changes, `/rewind` lists the checkpoints of a session and `/rewind <n>` puts the files back as they were
before checkpoint n. A rewind never touches a path it has no record of, never writes outside the directories of
the tool server that recorded it, and refuses when a file was changed outside the agent since (`overwrite` puts it
back anyway). Shell commands and similar tools are not recorded, only counted.

- **Hooks** `record_before_change` (pre_tool_call) and `record_after_change` (post_tool_call) -- record the state
  before and after every file_ops / media_ops write. Off for every agent; an agent switches both on in
  `hooks.overrides`. The coder agent and its sub-agents do.
- No tool (the model does not see the plugin), no panel. The chat commands work in the terminal chat and the
  browser.

Enable the `file_checkpoints` instance in `config/plugins.yaml` (settings under `config:`), then switch both hooks
on for the agents whose changes should be recorded.

The full manual -- the chat commands, what a rewind does and refuses, what is recorded, the hooks, the settings and
where the data lives -- is the plugin's guide, `file_checkpoints.guide`, in the Help panel.
