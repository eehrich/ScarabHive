# Basic Operations

Two small tools for agents: a wait with a countdown in the chat's status line -- or, for a long pause, an answer at
once and a wake of the session when the time is up -- and a ping that answers with the server's time. No state, no
panel.

- **Tools** `basic_operations_wait` (`seconds`, `message`, `wake`) and `basic_operations_ping` (`include_details`).

Enable it in `config/plugins.yaml` (`basic_operations: {type: basic_operations, enabled: true}`) and allow
`+basic_operations/*` in an agent's tool list.

The full manual -- parameters and limits as the code applies them, answers and error texts, when a wake is armed and
when it is not, the settings and the status lines -- is the plugin's guide, `basic_operations.guide`, in the Help
panel.
