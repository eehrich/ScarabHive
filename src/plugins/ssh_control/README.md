# SSH Control Plugin

The SSH Control plugin provides comprehensive SSH-based control and management of multiple remote Linux machines. It offers command execution, file operations, connection management, and a terminal-style web interface with real-time command streaming.

## Overview

This plugin enables secure remote machine control through SSH with support for multiple authentication methods, connection pooling, and a modern web UI. It combines MCP tools for programmatic access with an interactive terminal interface for manual operations.

## Features

### Core Operations
- **Command Execution**: Execute commands on remote machines with streaming output
- **File Operations**: Upload/download files via SCP/SFTP
- **Connection Management**: Pooled connections with automatic reconnection
- **Multi-machine Support**: Manage multiple SSH servers simultaneously
- **Dynamic Provisioning**: Add/remove machines at runtime via MCP tools

### Web Interface
- **Terminal Emulation**: Tab-based terminal UI for each machine
- **Real-time Streaming**: SSE-based command output streaming
- **Status Monitoring**: Connection status and latency indicators
- **Command History**: Persistent command history per machine
- **Machine Management**: Add/remove machines via web UI modal

### Security
- **Multiple Auth Methods**: SSH keys, passwords, SSH agent
- **Secure Storage**: No plaintext passwords in config files
- **Connection Validation**: Test connections before adding machines
- **Host Key Verification**: Configurable host key checking

## Configuration

Configure the SSH Control plugin in `config/mcp.yaml`:

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

### Web UI Usage

#### Access Terminal Interface
1. Navigate to the main web UI at `http://localhost:8000`
2. Select the SSH Control panel from the plugin list
3. Click on machine tabs to switch between servers
4. Use the command input at the bottom to execute commands

#### Add Machine via Web UI
1. Click the "+" button in the tab bar
2. Fill in the machine details:
   - Name (unique identifier)
   - Host (hostname or IP)
   - Port (default: 22)
   - Username
   - Authentication method (key/password/agent)
   - Credentials (key path or password)
   - Tags (optional, comma-separated)
3. Check "Save to config file" to persist the machine
4. Click "Add Machine" to test connection and add

#### View Connection Status
- Connected machines show latency in milliseconds (e.g., "45ms")
- Disconnected machines show an em-dash (—)
- Hover over status indicator for tooltip

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

### Command Execution
- `POST /plugins/ssh_control/api/execute`
  - Body: `{"machine": "name", "command": "ls -la"}`
  - Returns: `{"stdout": "...", "stderr": "...", "exit_code": 0, "duration": 1.23}`

### Machine Status
- `GET /plugins/ssh_control/api/machines/{name}/status`
  - Returns: `{"connected": true, "latency_ms": 45}`

### Machine Management
- `POST /plugins/ssh_control/api/machines/add`
  - Body: Machine configuration with optional `persistent` flag
  - Returns: `{"success": true, "machine": "name", "persisted": true}`
- `DELETE /plugins/ssh_control/api/machines/{name}`
  - Returns: `{"success": true, "message": "Machine removed"}`

### Command History
- `GET /plugins/ssh_control/api/machines/{name}/history?limit=50`
  - Returns: Array of command history entries with timestamps

### SSE Streaming
- `GET /plugins/ssh_control/api/stream/{machine}`
  - Server-Sent Events endpoint for real-time command output
  - Subscribe with EventSource in browser

## Troubleshooting

### Connection Issues

#### "SSH key not found"
- **Cause**: The `key_path` points to a non-existent file
- **Solution**: Verify the key path exists and is accessible to the API process
- **Error Response**: `401 Authentication failed: SSH key not found: /path/to/key`

#### Disconnected Status (em-dash)
- **Cause**: Machine is unreachable or authentication failed
- **Check**: Verify host, port, and credentials are correct
- **Test**: Use `ssh username@host -p port` from API host to test manually

#### "Exit code: undefined" (legacy)
- **Cause**: Old browser cache with outdated JavaScript
- **Solution**: Clear browser cache and reload the SSH Control panel
- **Note**: Modern UI shows explicit error messages

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
│   └── panel.html        # Web UI template
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

#### Customize Web UI
1. Edit `templates/panel.html` for UI changes
2. Update JavaScript event handlers for new functionality
3. Add corresponding API endpoints in `web_endpoints.py`

## Error Handling

The plugin provides detailed error responses with appropriate HTTP status codes:

- `400 Bad Request`: Invalid parameters or malformed request
- `401 Unauthorized`: Authentication failure (missing key, wrong password)
- `403 Forbidden`: Permission denied on remote machine
- `404 Not Found`: Machine not found in configuration
- `500 Internal Server Error`: Unexpected server error
- `503 Service Unavailable`: Connection refused by remote machine
- `504 Gateway Timeout`: Connection or command timeout

Web UI displays these errors as clear messages in the terminal output.

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

### Example 4: SSE Stream Subscription (JavaScript)
```javascript
// Subscribe to command stream
const eventSource = new EventSource(
  '/plugins/ssh_control/api/stream/production-web'
);

eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Command:', data.command);
  console.log('Output:', data.stdout);
  console.log('Exit code:', data.exit_code);
};

eventSource.onerror = (error) => {
  console.error('SSE connection error:', error);
  eventSource.close();
};
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
  - Verified persistence to config/mcp.yaml

## License

This plugin is part of the AgentSystem project and follows the same license.

## Support

For issues, questions, or contributions:
- File issues in the main AgentSystem repository
- Check logs in `logs/api.log` for detailed error messages
- Review `config/mcp.yaml` for configuration issues
