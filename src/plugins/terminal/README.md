# Terminal Plugin

The Terminal plugin enables safe, controlled shell command execution within the AgentSystem. It provides both synchronous and background command execution with comprehensive security features.

## Features

- **Cross-platform Support**: Works on Windows (Git Bash preferred, WSL fallback) and Linux
- **Security First**: Pattern-based dangerous command blocking, whitelist/blacklist support
- **Background Processes**: Start, monitor, and manage long-running commands
- **Output Management**: Automatic truncation, stream filtering (stdout/stderr)
- **Flexible Execution**: Configurable timeouts, working directories, environment variables
- **Cancellation Support**: Interrupt long-running commands via cancellation tokens

## Tools

### execute

Execute a shell command either synchronously (wait for completion) or as a background process.

**Parameters:**
- `command` (string, required): The shell command to execute
- `background` (boolean, optional): Run as background process (default: false)
- `timeout` (number, optional): Timeout in seconds for foreground commands (default: from config, max: 3600s, ignored for background)
- `cwd` (string, optional): Working directory for command execution
- `env_vars` (object, optional): Additional environment variables (key-value pairs)
- `process_id` (string, optional): Custom process ID for background processes (auto-generated if not provided)

**Returns (foreground execution, background=false):**
```json
{
  "status": "success",
  "exit_code": 0,
  "stdout": "command output",
  "stderr": "",
  "execution_time": 0.123,
  "truncated": false,
  "cwd": "/path/to/working/directory",
  "command": "echo 'Hello World'"
}
```

**Returns (background execution, background=true):**
```json
{
  "status": "success",
  "process_id": "bg_12345",
  "pid": 67890,
  "command": "python server.py"
}
```

**Examples:**

Foreground execution (wait for completion):
```json
{
  "command": "ls -la /tmp",
  "timeout": 10,
  "cwd": "/home/user"
}
```

Background execution:
```json
{
  "command": "python -u server.py",
  "background": true,
  "process_id": "my_server"
}
```

### execute_command (legacy)

Execute a shell command and wait for completion. This method is maintained for backward compatibility and internally calls `execute` with `background=false`.

**Use `execute` with `background=false` instead for new code.**

### execute_background (legacy)

Start a command as a background process. This method is maintained for backward compatibility and internally calls `execute` with `background=true`.

**Use `execute` with `background=true` instead for new code.**

### get_output

Retrieve output from a background process.

**Parameters:**
- `process_id` (string, required): Process ID returned by execute_background
- `stream` (string, optional): Stream to retrieve - "both" (default), "stdout", "stderr"
- `clear_buffer` (boolean, optional): Clear output buffer after retrieval (default: false)

**Returns:**
```json
{
  "status": "success",
  "stdout": "process output",
  "stderr": "",
  "is_running": true,
  "exit_code": null,
  "process_id": "bg_12345"
}
```

**Example:**
```json
{
  "process_id": "my_server",
  "stream": "stdout",
  "clear_buffer": true
}
```

### kill_process

Terminate a background process.

**Parameters:**
- `process_id` (string, required): Process ID to terminate
- `force` (boolean, optional): Use SIGKILL instead of SIGTERM (default: false)

**Returns:**
```json
{
  "status": "success",
  "message": "Process 'my_server' terminated",
  "process_id": "my_server",
  "signal": "SIGTERM"
}
```

**Example:**
```json
{
  "process_id": "my_server",
  "force": false
}
```

## Configuration

The Terminal plugin is configured in `config/plugins.yaml`:

```yaml
terminal:
  plugin: terminal
  enabled: true
  config:
    security:
      # Blacklisted commands (regex patterns)
      blacklist:
        - "^rm\\s+-rf\\s+/"
        - "^dd\\s+"
      
      # Allow command chains (&&, ||, ;)
      allow_command_chains: true
    
    limits:
      # Maximum output size in KB
      max_output_size_kb: 60
      
      # Timeout limits
      timeouts:
        default: 30
        maximum: 3600
    
    platform:
      # Path to bash executable (auto-detected if "auto")
      bash_path: "auto"
```

### Security Settings

**Dangerous Command Patterns (built-in):**
- `rm -rf /` - Recursive deletion from root
- `dd` - Low-level disk operations
- `:(){ :|:& };:` - Fork bombs
- `mkfs.*` - Filesystem creation
- `chmod -R 777` - Unsafe permission changes
- `wget|sh`, `curl.*|.*bash` - Piped execution from web
- Command substitution: `` `...` ``, `$(...)` (when not properly escaped)

**Blacklist:** Add custom regex patterns to block specific commands.

**Whitelist:** If configured, only commands matching whitelist patterns are allowed.

**Command Chains:** Control whether `&&`, `||`, and `;` are permitted.

### Platform Detection

The plugin automatically detects the bash executable:

**Windows:**
1. Git Bash (preferred): `C:\Program Files\Git\usr\bin\bash.exe`
2. Bash in PATH: Any `bash.exe` found via `where bash`
3. WSL: `C:\Windows\System32\bash.exe` (fallback)

**Linux:**
1. Bash in PATH: Located via `which bash`
2. Default: `/bin/bash`

## Usage Examples

### Simple Command Execution

```python
result = await terminal.execute({
    "command": "echo 'Hello from terminal'",
    "timeout": 5
})
print(result["stdout"])  # "Hello from terminal\n"
```

### Working Directory and Environment

```python
result = await terminal.execute({
    "command": "python script.py",
    "cwd": "/path/to/project",
    "env_vars": {
        "API_KEY": "secret123",
        "DEBUG": "1"
    }
})
```

### Background Process with Monitoring

```python
# Start background process
start = await terminal.execute({
    "command": "python -u long_running_script.py",
    "background": true,
    "process_id": "script1"
})

# Check output periodically
await asyncio.sleep(5)
output = await terminal.get_output({
    "process_id": "script1",
    "stream": "stdout"
})
print(output["stdout"])

# Terminate when done
await terminal.kill_process({
    "process_id": "script1"
})
```

### Command with Cancellation

```python
from agent_system.core.cancellation import CancellationToken

token = CancellationToken()

# Start long command
task = asyncio.create_task(
    terminal.execute({
        "command": "sleep 100",
        "_cancellation_token": token
    })
)

# Cancel after 5 seconds
await asyncio.sleep(5)
token.cancel()

try:
    await task
except asyncio.CancelledError:
    print("Command cancelled")
```

## Confinement (`sandbox:`)

The pattern list in `security.py` is a hand-brake against slips, **not** a
security boundary — it says so itself, and it is right: everything it blocks
is reachable through `sh -c`, a pipe, `xargs` or `python -c`. Filtering
command *text* buys nothing.

Confinement works the other way round: the process is handed to the kernel
already unable to reach outside its workspace, whatever it then runs.

```yaml
terminal:
  type: terminal
  sandbox:
    mode: workspace-write      # read-only | workspace-write | danger-full-access
    workspace_root: .          # optional; defaults to initial_cwd
```

| Mode | File effects |
|---|---|
| `read-only` | the process may not modify files |
| `workspace-write` | it may modify files under `workspace_root` |
| `danger-full-access` | no restriction — **the default** |

The default is `danger-full-access`, which behaves exactly as this plugin did
before confinement existed. Confinement is opt-in because no backend covers
every platform we run on yet.

**Backends.** Linux: bubblewrap (`apt install bubblewrap`), verified live
against 0.9.0. Windows: none — a confining mode there refuses every command
rather than running it unconfined. Modes describe **file effects only**; no
network, syscall or device restriction is claimed, because none is enforced.

**Fail-closed.** If the requested mode cannot be enforced, the command does
not run:

```text
sandbox mode "workspace-write" is requested but no sandbox backend is usable
on this host; refusing to run the command unconfined. Install bubblewrap
(Linux) — otherwise switch the consumer to danger-full-access.
```

## Model Experience

### What the model sees

Normal results are unchanged: `{"status": "success", "exit_code": ..., "stdout": ...}`.

A command refused by the pattern list returns `error_type: "SecurityError"`
naming the pattern. A command that cannot be confined returns
`error_type: "SandboxUnavailable"` with the text above — the model can tell
"your command was rejected" from "this host cannot confine me" and does not
retry the latter with a reworded command.

Under confinement, a denied write is **not** a plugin error: the command runs
and fails on its own, so the model sees the ordinary non-zero exit code and
the shell's `Permission denied` on stdout. That is deliberate — it is what the
same command does against an unwritable directory anywhere else.

### Token and cache effect

Append-only. The plugin contributes nothing to the system prompt or the tool
list beyond its three static tool definitions; confinement adds no tokens at
all, because the wrapping happens below the model. Output is capped at
`max_output_size_kb` (60 KB default).

### Known gaps

- **Windows has no backend.** Confinement there is refusal, not enforcement.
- **File effects only.** A confined process still has the network.
- **The workspace is one directory.** No multi-root policy; a job needing two
  trees has to be given a common parent.
- **The persistent session path is wired but unreachable.** Nothing currently
  instantiates `PersistentTerminal`; every command runs as its own `bash -c`.
  Its spawn is confined anyway, so reviving the class cannot revive an
  unconfined spawn — but that path is not live-verified.
- **Confinement is decided at spawn.** A mode change takes effect on the next
  command, never on one already running.

## Security Best Practices

1. **Prefer confinement over pattern lists** - `sandbox.mode` is a boundary, the blacklist is a hand-brake
2. **Never execute untrusted user input directly** - Always validate and sanitize commands
3. **Use whitelist when possible** - Restrict to known-safe commands
4. **Limit timeouts** - Prevent resource exhaustion with reasonable timeout values
5. **Monitor background processes** - Track and clean up background processes
6. **Review blacklist** - Customize blocked patterns for your environment
7. **Disable command chains if not needed** - Reduces attack surface

## Architecture

```
terminal/
├── __init__.py          # Package initialization
├── plugin.yaml          # Plugin metadata
├── schema.yaml          # Tool definitions
├── server.py            # TerminalServer (MCP interface)
├── executor.py          # CommandExecutor (subprocess management)
├── process_manager.py   # ProcessManager (background processes)
├── security.py          # CommandSecurityValidator
├── platform_detect.py   # PlatformDetector
└── README.md           # This file
```

**Key Components:**

- **TerminalServer**: MCP server implementing tool handlers
- **CommandExecutor**: Executes commands via subprocess, handles timeouts/truncation
- **ProcessManager**: Manages background processes, captures output
- **CommandSecurityValidator**: Validates commands against security rules
- **PlatformDetector**: Finds appropriate bash executable for the platform

## Testing

Run terminal plugin tests:

```bash
# All terminal tests
pytest tests/test_plugin_terminal*.py -v

# Security tests only
pytest tests/test_plugin_terminal_security.py -v

# Platform detection tests
pytest tests/test_plugin_terminal_platform.py -v

# Basic functionality tests
pytest tests/test_plugin_terminal_basic.py -v

# Integration tests (background processes)
pytest tests/test_plugin_terminal_integration.py -v
```

## Limitations

- **No persistent session state**: Each command runs in a separate subprocess
  - Environment variables set with `export` do not persist
  - Directory changes with `cd` do not persist
  - Use `cwd` and `env` parameters instead
- **Output buffering**: Python scripts should use `-u` flag for unbuffered output
- **Windows limitations**: Git Bash is preferred; WSL may have encoding issues
- **Resource limits**: Maximum output size configurable to prevent memory exhaustion

## Troubleshooting

**Issue:** Commands timeout unexpectedly
- **Solution:** Increase timeout in command or config, check for interactive prompts

**Issue:** No output from background process
- **Solution:** Use Python's `-u` flag for unbuffered output, wait longer before reading

**Issue:** Dangerous command not blocked
- **Solution:** Add custom regex pattern to security.blacklist in config

**Issue:** Bash not found on Windows
- **Solution:** Install Git Bash or specify bash_path in config

**Issue:** Environment variable not available
- **Solution:** Pass env variables explicitly, they don't persist between commands

## Version History

- **1.0.0** (2025-01-30): Initial release with core functionality
  - Cross-platform bash execution
  - Security validation
  - Background process management
  - Comprehensive test coverage (45 tests)
  - Unified `execute` method with `background` parameter
  - Legacy compatibility with `execute_command` and `execute_background`
