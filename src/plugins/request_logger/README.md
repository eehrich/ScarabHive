# Request Logger

Writes a trace of every agent's LLM calls into the server's normal log (`logs/api.log`, `logs/cli.log`): a line
before and after each call with session, agent, duration and a short preview of the last message and the answer,
a line when a session starts and a summary when a run ends. Meant for debugging, and the simplest example of a
hook plugin. The previews are unmasked conversation text in a log shared by all users.

- **Hooks** `log_pre_llm`, `log_post_llm`, `log_session_start`, `log_session_end` -- they only log, and run for
  every agent once the plugin is on.
- No tools, no panel.

Off by default. Enable it in `config/plugins.yaml` (`request_logger: {type: request_logger, enabled: true}`);
settings go in the entry's `config:` block.

The full manual -- what each line means, secrets and who can read the log, rotation, switching hooks off per
agent, and the settings -- is the plugin's guide, `request_logger.guide`, in the Help panel.
