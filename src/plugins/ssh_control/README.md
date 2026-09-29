# SSH Control

Commands, background jobs and file copies on remote machines over SSH, for agents that look after servers. The
machines are named in the configuration; an agent addresses them by name, runs any command line as the configured
user, starts long commands in the background and reads or stops them later. The **SSH Machines** panel shows each
machine with its state and history, runs commands and adds or removes machines -- for administrators only.

- **Tools** -- `execute` (foreground or `background: true`, with an optional wake), `get_output`, `kill_process`,
  `upload_file`, `download_file`, `list_machines`, `check_connection`, `add_machine`, `remove_machine`.
- **Panel** SSH Machines -- a terminal per machine: the agents' and the panel's commands, a line to run more, add
  and remove.
- **Security** -- no command allowlist: the remote account is the limit. Strict host key checking against
  `known_hosts` is on by default; every command, background start and file copy is written to the server log as an
  `AUDIT:` line. `add_machine` and the file tools reach beyond the configured machines and folders -- allow the
  tools one by one where that is not wanted.

Configure it with its machines in the agent's YAML (`plugins.servers.ssh_control: {type: ssh_control, machines:
[...]}`) and allow `+ssh_control/*` (or single tools) in the agent's tool list. It needs `asyncssh`.

The full manual -- the panel, what an agent can and cannot do, every tool with its parameters and answers,
background commands and wakes, the machine and security settings -- is the plugin's guide, `ssh_control.guide`, in
the Help panel.
