# SSH Control Plugin

The SSH Control plugin provides comprehensive SSH-based control and management of multiple remote Linux machines. It offers command execution, file operations, connection management, and a terminal-style panel.

## Overview

This plugin enables secure remote machine control through SSH with support for multiple authentication methods, connection pooling, and a modern web UI. It combines MCP tools for programmatic access with an interactive terminal interface for manual operations.

## Features

### Core Operations
- **Command Execution**: Execute commands on remote machines
- **File Operations**: Upload/download files via SCP/SFTP
- **Connection Management**: Pooled connections with automatic reconnection
- **Multi-machine Support**: Manage multiple SSH servers simultaneously
- **Dynamic Provisioning**: Add/remove machines at runtime via MCP tools

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
    description: "SSH Control MCP Server"
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
├── server.py             # MCP server implementation
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
# Via MCP tool
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
