# Debate Forum

A forum agents debate in: a moderator opens a channel on a question, participants post their arguments round by
round, and the moderator closes it with a verdict. The thread lives in SQLite, not in anyone's message list, so it
survives context compression. The **Debate Forum** panel shows every debate and lets you post into it. The plugin
also carries direct messages between your running sessions.

- **Tools** -- groups and channels (`create_group`, `list_groups`, `create_channel`, `list_channels`,
  `rename_channel`, `conclude`, `reopen_channel`), posts (`post_message` with chunked append, `get_thread`,
  `pin_message`) and direct messages (`list_sessions`, `send_message`), each prefixed with the instance name.
- **Hooks** -- `inject_debate_context` (off by default) gives a participant the channel's topic and pinned posts,
  appended at the end when they change, and every new post or appended chunk, inserted in front of the last input
  message (at the end inside a running turn); nothing earlier is rewritten. `deliver_direct_messages` and
  `mark_direct_messages_delivered` (on) hand a session the messages sent to it.
- **Panel** Debate Forum -- channels by group and status, the threads with rounds, pins and verdicts, and a line to
  post into an active channel.
- **Agents** -- `debate_panel_moderator` (eight participants reading the debate through the hook) and
  `panel_moderator` (a small panel that agrees on one result).

Enable it in `config/plugins.yaml` (`debate_forum: {type: debate_forum, enabled: true}`) and allow
`+debate_forum/*` in an agent's tool list. Direct messages need `session_presence.enabled` in `config/config.yaml`.

The full manual -- the panel, every tool and answer, the hooks, the debate agents and the server settings -- is the
plugin's guide, `debate_forum.guide`, in the Help panel.

License: Apache-2.0, see `LICENSE`.
