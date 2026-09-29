# Message Debugger

Records what agents send to their LLM and what comes back: the messages of each step as the model gets them
(turns), and the raw provider requests with their responses, usage, duration and errors. Captures go into a SQLite
database through a background writer, so a slow database never stalls an agent; the database stays below a size
cap and keeps the cost columns longest. The **Message Debugger** panel shows each user the captures of their own
calls, an admin everyone's.

- **Hooks** `debugger_capture_pre_llm`, `debugger_capture_post_llm`, `debugger_capture_pre_request`,
  `debugger_capture_post_response` (on for every agent with hooks) -- only read; the model sees nothing of them.
- **Panel** Message Debugger -- turns and LLM requests with filters, each entry in full, and for admins prune and
  clear.

It is on in `config/plugins.yaml` (`message_debugger: {type: message_debugger, enabled: true}`); it has no tools.

The full manual -- the panel, who sees what, the hooks and their settings, retention, the HTTP API and the server
settings -- is the plugin's guide, `message_debugger.guide`, in the Help panel.
