# SSH Control Plugin

The SSH Control plugin provides comprehensive SSH-based control and management of multiple remote Linux machines. It offers command execution, file operations, connection management, and a terminal-style panel.

## Overview

This plugin enables secure remote machine control through SSH with support for multiple authentication methods, connection pooling, and a modern web UI. It combines tools for programmatic access with an interactive terminal interface for manual operations.

## Features

### Core Operations
- **Command Execution**: Execute commands on remote machines
- **File Operations**: Upload/download files via SCP/SFTP
- **Connection Management**: Pooled connections with automatic reconnection
- **Multi-machine Support**: Manage multiple SSH servers simultaneously
- **Dynamic Provisioning**: Add/remove machines at runtime via tools

### Security
- **Multiple Auth Methods**: SSH keys, passwords, SSH agent
- **Secure Storage**: No plaintext passwords in config files
- **Connection Validation**: Test connections before adding machines
- **Host Key Verification**: Configurable host key checking

## Configuration

Configure the SSH Control plugin in its agent YAML under `config/agents/`
(the block is `ssh_control:` inside `plugins.servers`):

```yaml
mcp:
  enabled_servers:
  - ssh_control

servers:
  ssh_control:
    enabled: true
    type: plugin
    plugin: ssh_control
    description: "SSH Control Tool Server"
    machines:
      - name: production-web
        host: ssh.example.com
        port: 22
        username: ubuntu
        auth_method: key
        key_path: /home/user/.ssh/id_rsa
        tags: [production, web]
        max_connections: 4
        timeout: 30

      - name: staging
        host: staging.example.com
        port: 22
        username: deploy
        auth_method: agent
        tags: [staging]
        max_connections: 2
```

### Configuration Options

#### Machine Configuration
- **name**: Unique identifier for the machine
- **host**: Hostname or IP address
- **port**: SSH port (default: 22)
- **username**: SSH username
- **auth_method**: Authentication method (`key`, `password`, or `agent`)
- **key_path**: Path to SSH private key (only for `auth_method: key`)
- **tags**: List of tags for organization
- **max_connections**: Maximum concurrent connections to this machine (default: 5)
- **timeout**: Connection timeout in seconds (default: 30)

### Environment Variables
- `SSH_CONTROL_DEFAULT_TIMEOUT`: Default connection timeout (default: 30)
- `SSH_CONTROL_MAX_CONNECTIONS`: Default max connections per machine (default: 5)

## Usage Examples

### MCP Tool Usage

#### Execute Command
```json
{
  "tool": "ssh_control_execute",
  "machine": "production-web",
  "command": "df -h"
}
```

#### Background Command
```json
{
  "tool": "ssh_control_execute",
  "machine": "production-web",
  "command": "make -C /srv/app build",
  "background": true,
  "wake": true
}
```

#### Upload File
```json
{
  "tool": "ssh_control_upload",
  "machine": "production-web",
  "local_path": "/local/file.txt",
  "remote_path": "/remote/destination/file.txt"
}
```

#### Download File
```json
{
  "tool": "ssh_control_download",
  "machine": "staging",
  "remote_path": "/var/log/application.log",
  "local_path": "/local/logs/app.log"
}
```

#### Add Machine Dynamically
```json
{
  "tool": "ssh_control_add_machine",
  "name": "temp-server",
  "host": "temp.example.com",
  "username": "admin",
  "auth_method": "key",
  "key_path": "/home/user/.ssh/temp_key",
  "persistent": false
}
```

#### Remove Machine
```json
{
  "tool": "ssh_control_remove_machine",
  "machine": "temp-server"
}
```

## The panel

**SSH Machines** in the panel launcher (category *system*). One tab per machine, each with a dot for its
connection: green with the latency once commands went through in the last five minutes, grey when not connected
yet or idle, red when unreachable.

- **Terminal.** The tab shows the last 50 commands run on that machine -- by agents and from the panel, failed runs
  included -- with their output, exit code, duration and time. The line at the bottom runs a command on the machine
  shown; while it runs, that machine's line is locked, the other machines stay usable. A machine that cannot be
  reached or a command that does not finish within the machine's `command_timeout` is reported and recorded.
- **Refreshing.** Every 5 seconds the panel looks at the machines and brings new commands and machines an agent
  added. It does not connect for that. The refresh button also pings each machine that was connected to before, so
  a machine gone away shows as unreachable. A machine whose connections are all busy is not pinged -- its last use
  stands.
- **Add machine.** Name, host, port, username and the way to authenticate. The connection is tested before the
  machine is added (the same path as the `add_machine` tool); a refusal is shown in the dialog. *Keep across
  restarts* stores it in `data/ssh_control/machines.<instance>.yaml` -- a machine with a password is added but not
  stored, since no password is written to disk.
- **Remove.** Asks first, then closes the machine's connections and removes it from the store too. A machine from
  the configuration comes back at the next start; the panel says so.

The panel never receives a password or a key path. Anyone who may open it may run commands on every machine:
restrict `/plugins/<instance>/*` in `auth.plugin_security` if that is not everyone.

## Background commands and waking

`execute(background=true)` starts a long command and returns at once with a
`process_id`. `get_output` reads what it has written so far and whether it still
runs; `kill_process` stops it. One machine per call — the answer carries one
process id, and how many may run at once is what the pool allows (see below).

A `process_id` you choose yourself must not belong to a RUNNING command:
reusing one returns `ProcessIdInUse` rather than replacing the entry, which
would leave the command behind it running with no way to read its output or
stop it. Once that command is over the id is free again: the next run takes
the name, and anything recorded under it is dropped with it.

**What a background command costs:** it holds one connection out of that
machine's pool for its whole life. `max_connections` is 3 by default, and one
connection always stays free for ordinary commands — so two background commands
per machine, and the third is refused with `BackgroundLimitReached` naming the
setting. With `max_connections: 1` there is nothing to spare and background
commands are refused outright; raise it for that machine if you want them.

`wake: true` lets the caller end its turn over the command. When it ends —
finished, failed or stopped, there is no second ending — the plugin tells the
core that input is waiting for the calling session (`core/session_presence.py`,
`wake_session`): a session another process holds reads that at its next step,
a session nobody holds is continued in a run of its own. The woken run reads the
result with `get_output` on the `process_id` from its own history.

**The answer says whether the wake is armed**, because a caller that asked for
one and silently did not get it would end its turn over work it never hears
about again. Both reasons are known before the command starts:

| `wake` | `wake_note` | What to do |
|---|---|---|
| `true` | — | End the turn. `get_output` when woken. |
| `false` | `session presence is off (config: session_presence.enabled)` | Poll `get_output`. |
| `false` | `this call belongs to no session, so there is nobody to wake` | Poll `get_output`. |

A call that did not ask is told nothing about a wake — both fields are absent.

**An armed wake is best effort, not a promise.** What cannot be checked up front
is whether the process holding the command is still there when it ends. A caller
that is not woken should poll `get_output`.

**A woken run is a different process, so the outcome is recorded.** Waking a
session nobody holds starts a fresh `agent-cli run`, which builds its own tool
servers — its process registry is empty, and `get_output` on an id from the old
process would find nothing. When a wake is armed, the outcome (exit code and
the tail of both streams) is therefore written to the plugin's cache
(`data/cache/<instance>/`, one hour, at most 30 000 characters per stream).
`get_output` answers from it when the process is not in this process's memory,
marks the answer `"source": "recorded"`, and drops the record — it is handed
over, not kept. A call without `wake` writes nothing: its caller polls from the
process that holds the result anyway.

**A wake is rung more than once.** The core only leaves a marker, and a session
that is in the middle of a turn takes that marker at its next step expecting a
hook to hand the waiting input over — nothing hands over "your command
finished". So the ringing repeats while the session stays busy (10 s apart, up
to five minutes) and stops early once the session has dealt with the command
itself — `get_output` on the finished result, or `kill_process`. Either way it
is not started again for something it already handled.

Three further cases end with no wake, and only the first is refused up front:

| Case | What happens |
|---|---|
| `session_presence.max_wake_depth` reached, or `0` | refused before the start, with the setting named |
| A sub-agent's session | armed, but never woken — the run that spawned it hands its result over. Reading this up front means parsing the whole session file on the event loop for every armed wake, so it is not checked |
| A one-shot `agent-cli run` | armed, but the run ends and takes the work with it |

`wake: true` without `background: true` is answered too: the results are already
in that answer, so there is nothing to wake for.

### Model Experience

**What the model sees.** The same vocabulary as the `terminal` plugin —
`background`, `wake`, `process_id`, `get_output`, `kill_process` — so one tool
does not have to be learned twice. A refusal names what to change:
`BackgroundLimitReached` carries the machine's `max_connections`,
`InvalidParameter` says that a list of machines is not a background command,
`ProcessIdInUse` that an id you chose is taken (the command behind it keeps
running, untouched), `StoppedBeforeStart` that the command was stopped while
its channel was still opening,
`ProcessNotFound` covers both a wrong id and another session's process.

**Token and cache effect.** Append-only: two more tool definitions, and three
more parameters on `execute`. Output is capped at 1000 lines per stream and
older lines are dropped, so a long build cannot grow the answer without bound.

**Known gaps.**

- **A recorded result lives one hour and is read once.** It is dropped as soon
  as anybody reads the finished command — by the woken run that recalls it, by
  a `get_output` in the process that still holds the result, or by handing its
  `process_id` to a new run. A woken run that never calls `get_output` at all
  leaves it to expire; a second reader finds nothing.
- **Only the last 50 finished commands stay readable.** Each entry holds two
  line buffers, so they cannot be kept for the life of the process. What is
  still running is never dropped, and neither is the one that just ended.
- **Nothing survives a restart.** The registry is in memory. A restarted
  process loses every `process_id`, and the remote command keeps running with
  nobody reading it.
- **A wake needs the process that started the command.** The API and
  `agent-cli chat` (whose prompt waits on the same loop) can wake; a one-shot
  `agent-cli run` ends its turn and takes its background commands with it.
- **Output is read, not streamed.** `get_output` returns what has arrived; a
  command that writes nothing for an hour looks the same as one that hangs.
- **The connection is the lifeline.** If it drops, the capture ends and the
  command is recorded as finished with whatever status arrived — the remote
  process itself may well run on.

## Security Best Practices

### Authentication
- **Prefer SSH Keys**: Use key-based authentication over passwords
- **Use SSH Agent**: For environments with multiple keys, use agent authentication
- **Avoid Password Persistence**: The plugin intentionally does not save passwords to config files

### Key Management
- **Proper Permissions**: Ensure SSH keys have correct permissions (600)
- **Key Path**: Use absolute paths for `key_path` configuration
- **Key Rotation**: Regularly rotate SSH keys and update configurations

### Network Security
- **Reverse Proxy**: Deploy API behind authenticated reverse proxy
- **Access Control**: Restrict SSH Control panel access to authorized users
- **Firewall Rules**: Ensure SSH ports are properly firewalled

### Host Verification
- **Known Hosts**: Maintain proper `known_hosts` file
- **Host Key Checking**: Enable host key verification in production

## Web API Endpoints

What the panel calls, under `/plugins/<instance>/`:

- `GET api/machines[?active=true]` -- the machines and their connection state
- `POST api/machines` -- add a machine (body like the `add_machine` tool); a refusal is `400`
- `DELETE api/machines/{name}` -- remove a machine, from the store too; `404` if unknown
- `GET api/machines/{name}/history` -- the last 50 runs, oldest first
- `POST api/execute` -- `{"machine": "...", "command": "..."}`; `404` unknown machine, `502` not reachable,
  `504` timed out

## Troubleshooting

### Connection Issues

#### "SSH key not found"
- **Cause**: The `key_path` points to a non-existent file
- **Solution**: Verify the key path exists and is accessible to the API process
- **Error Response**: `502 SSH key not found: /path/to/key`

#### Unreachable (red dot)
- **Cause**: Machine is unreachable or authentication failed
- **Check**: Verify host, port, and credentials are correct
- **Test**: Use `ssh username@host -p port` from API host to test manually

### Authentication Problems

#### Agent Authentication Not Working
- **Check**: Ensure SSH agent is running: `ssh-add -l`
- **Forward Agent**: If API runs remotely, enable agent forwarding
- **Permissions**: Verify socket permissions for SSH agent

#### Key Authentication Fails
- **Permissions**: Key file must have 600 permissions
- **Format**: Ensure key is in OpenSSH format (not PuTTY)
- **Passphrase**: If key has passphrase, use SSH agent

### Performance Issues

#### Slow Command Execution
- **Check Latency**: High latency shown in status indicator
- **Network**: Test network connectivity and bandwidth
- **Connection Pool**: Increase `max_connections` if needed

#### Connection Pool Exhausted
- **Symptom**: Commands wait or timeout
- **Solution**: Increase `max_connections` in machine config
- **Cleanup**: Remove stale connections via machine remove/add cycle

## Development

### Plugin Structure
```
src/plugins/ssh_control/
├── __init__.py
├── plugin.yaml           # Plugin metadata
├── server.py             # tool server implementation
├── web_endpoints.py      # FastAPI endpoints
├── connection_manager.py # Connection pooling
├── auth.py               # Authentication helpers
├── templates/
│   └── panel.html        # Panel template (kit/panel_base.html)
├── static/
│   ├── panel.js          # Panel script
│   └── panel.css         # Panel styles
└── README.md            # This file
```

### Testing
```bash
# Run plugin tests
pytest tests/test_ssh_control*.py -v

# Test with mock SSH server
pytest tests/test_ssh_control_integration.py
```

### Extending the Plugin

#### Add New MCP Tool
1. Add tool definition to `schema.yaml`
2. Implement handler in `server.py`
3. Add API endpoint in `web_endpoints.py` if needed
4. Update this README with usage example

#### Change the Panel
1. `templates/panel.html`, `static/panel.js`, `static/panel.css` -- built on the UI kit (`/ui/kit`)
2. Endpoints in `web_endpoints.py`, listed under `web_ui.endpoints` in `schema.yaml`
3. `tests/test_plugin_ssh_control_panel.py` drives the panel in a real browser

## Examples

### Example 1: Basic Command Execution
```bash
# Via tool
agent-cli "Execute 'uptime' on production-web using ssh_control"

# Expected output:
# Connected to production-web
# Command: uptime
# Output: 14:23:45 up 42 days, 3:15, 2 users, load average: 0.15, 0.12, 0.10
# Exit code: 0
```

### Example 2: File Transfer Workflow
```bash
# Upload configuration
agent-cli "Upload local config.yaml to /etc/app/config.yaml on staging using ssh_control"

# Verify upload
agent-cli "Execute 'cat /etc/app/config.yaml' on staging"

# Download logs
agent-cli "Download /var/log/app.log from staging to ./logs/ using ssh_control"
```

### Example 3: Multi-machine Command
```bash
# Execute on multiple machines
agent-cli "Execute 'systemctl status nginx' on all production machines using ssh_control"
```

## Version History

- **0.1.0** (2025-10-08): Initial release
  - Basic SSH command execution
  - Multi-machine support
  - Web UI with terminal emulation
  - Connection pooling
  - File transfer operations

- **0.1.1** (2025-10-09): UI/UX improvements
  - Fixed misleading "Connected" message
  - Improved error message display
  - Added proper status indicators (connected/disconnected)
  - Backend error categorization (401/503/504)
  - Verified persistence to config/mcp.yaml (a file nothing read back;
    replaced 2026-09-04 by data/ssh_control/machines.<instance>.yaml)

## License

This plugin is part of the AgentSystem project and follows the same license.

## Support

For issues, questions, or contributions:
- File issues in the main AgentSystem repository
- Check logs in `logs/api.log` for detailed error messages
- Review the `ssh_control:` block in `config/agents/` for configuration issues,
  and `data/ssh_control/machines.<instance>.yaml` for machines added at runtime
