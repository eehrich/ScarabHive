# Session Archive

Old conversations move out of the live session store into zip files: once a conversation and every sub-agent
session it started are older than the retention (30 days as shipped) and none of them is in use, the app's archive
sweep packs the whole tree into one zip and takes it out of the store. This plugin is the archive's face -- the
**Session Archive** panel and the HTTP routes behind it; the archive itself is `agent_system.services.session_archive`.

- **Panel** Session Archive -- the requesting user's archived conversations with figures; restore one, delete one
  for good, or run the sweep for your own conversations at once (*Archive now*).
- **Routes** under `/plugins/session_archive/` -- list, restore, delete and sweep, always for the requesting user.
- No tools, no hooks.

The plugin is on in `config/plugins.yaml` (`session_archive: {type: session_archive, enabled: true}`); the sweep
itself is set under `session_archive:` in `config/config.yaml`. The same archive is reachable with `agent-cli run
--list-archived`, `--archive-sessions` and `--restore-session`.

The full manual -- the panel, what moves and when, the routes and their answers, the command line and the settings --
is the plugin's guide, `session_archive.guide`, in the Help panel.
