# Todo

A task list per chat session for agents: they break bigger work into tasks, track status, progress and
dependencies, and mark what is done. The **Todos** panel shows the list of a session and lets you start, complete
or delete tasks yourself.

- **Tool** `todo` -- one tool with the operations `create`, `update`, `get`, `list`, `delete` and `summary`.
  Near-duplicate titles return the existing task; dependencies block a task until they are done.
- **Hook** `inject_todo_tasks` (off by default) -- appends the open tasks to the history before an LLM call, only
  when the list has changed, so the cached prompt stays intact.
- **Panel** Todos -- the tasks of the session in the chat, with figures, filters and actions.

Enable it in `config/plugins.yaml` (`todo: {type: todo, enabled: true}`) and allow `+todo/*` in an agent's tool
list.

The full manual -- the panel, every parameter and answer, the duplicate and dependency rules, the hook's settings
and the server settings -- is the plugin's guide, `todo.guide`, in the Help panel.
