# Agent Editor

A panel for admins to create and edit agents -- the `plugins.servers.<name>` entries of the config tree -- in a form:
model chain, run settings, tools, sub-agent managers, prompt, skills and hooks, with what each agent inherits shown
beside what it sets itself. A save shows the change as a diff, writes only the agent's own entry back into the file it
came from (comments and layout kept), and rolls back when the result does not load. The panel also says which agents
run with older settings than the files hold, and what a config reload or a restart would apply.

- **Panel** Agent Editor (launcher: *Agents & tools*) -- the agent list with problems and restart marks, the editor
  tabs, new agents, delete, and the sub-agent managers.
- **No tools, no hooks** -- the plugin serves the panel and its admin-only JSON API.

Enable it in `config/plugins.yaml` (`agent_editor: {type: agent_editor, enabled: true}`). It needs
`auth.enabled: true` and an admin account; new agents are written to `config/agents/<name>.yaml`.

The full manual -- the list, every tab, how saving and deleting work, what needs a restart, the API and the known
gaps -- is the plugin's guide, `agent_editor.guide`, in the Help panel.
