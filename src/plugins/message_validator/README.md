# Message Validator

Repairs the structure of the conversation history before every LLM call, so a provider does not refuse the whole
request: tool calls nobody answered, tool answers without their call, messages wedged between a tool call and its
answers, tool names with characters providers reject, empty or doubled assistant turns, a history that does not start
with a user turn. It never rewrites what a message says, except that two assistant turns in a row are joined and a
turn whose every tool call was dropped, with no text of its own, reads "Tool execution was interrupted".

- **Hooks** `validate_messages` and `validate_structure` (`pre_llm_call`, on by default, every agent) -- the first
  checks and repairs; the repaired history replaces the session's, so each defect is repaired once, and the history
  from the first repaired message on (with its reasoning artifacts) is what changes. A clean history is left
  untouched and keeps the prompt cache. Large or JSON-looking tool answers are only logged. The second only logs
  messages without a role. Neither adds a message.
- No tools, no panel.

Enabled in `config/plugins.yaml` (`message_validator: {type: message_validator, enabled: true}`); the only settings
are the two size thresholds for the warnings (`max_tool_response_size_kb`, `warn_tool_response_size_kb`). Switch it
off for one agent with `hooks.overrides."message_validator.validate_messages": {enabled: false}`.

The full manual -- every check and its repair, what it only reports, the cache effect and the settings -- is the
plugin's guide, `message_validator.guide`, in the Help panel.
