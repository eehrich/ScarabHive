# Terminal

A shell for agents: every command runs in a fresh `bash -c` (Git Bash on Windows), in the foreground with a timeout
or in the background, where the agent reads its output later, ends it, or asks to be woken when it finishes. A plain
terminal runs whatever the agent asks, with the server's rights and environment -- its pattern lists are a hand-brake,
not a boundary. A whitelist narrows it to named commands in a fixed directory and environment; confinement
(`sandbox.mode`, bubblewrap on Linux, Seatbelt on macOS) keeps its writes inside one directory.

- **Tools** `terminal_execute` (foreground or background), `terminal_get_output`, `terminal_kill_process`. A timeout,
  a stopped run or a kill ends the command at once, with what it started -- except, on Linux and macOS, a process that
  detaches into a group of its own. Output is capped at 60 KB per answer; background processes are limited per session.
- Credentials the server read from its secrets files are left out of the commands' environment (not a boundary: the
  file stays readable).
- No hooks, no panel.

Enable it in `config/plugins.yaml` (`terminal: {type: terminal, enabled: true}`) and allow `+terminal/*` in an
agent's tool list -- only for agents whose users may run anything the server can.

The full manual -- every parameter and answer, background processes and waking, what an agent can run, the pattern
lists, the whitelist and confinement, and the server settings -- is the plugin's guide, `terminal.guide`, in the Help
panel.
