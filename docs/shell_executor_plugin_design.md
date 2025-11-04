# Shell Executor Plugin - Design Document

## Overview

The `shell_executor` plugin provides a secure MCP server for executing shell commands in bash (Windows and Linux). This plugin enables agents to run terminal commands, manage background processes, and capture output, similar to GitHub Copilot's `run_in_terminal` capability.

## Design Goals

1. **Cross-Platform**: Support both Windows (Git Bash, WSL) and Linux bash
2. **Persistent Sessions**: Maintain environment variables, working directory, and context across commands
3. **Security First**: Prevent dangerous commands with whitelist/blacklist (improvement over GitHub Copilot)
4. **Background Process Management**: Execute long-running commands with process control
5. **Output Management**: Capture stdout/stderr interleaved, with automatic truncation to prevent context overflow
6. **Resource Control**: Enforce timeouts and output limits

## Plugin Metadata

```yaml
# plugin.yaml
name: shell_executor
version: 1.0.0
description: "Secure bash command execution with process management"
author: AgentSystem Team
entrypoint: server:ShellExecutorServer
type: mcp_only
category: utilities
```

## Architecture

### Components

```
src/plugins/shell_executor/
├── plugin.yaml          # Plugin metadata
├── schema.yaml          # Tool definitions
├── server.py           # Main MCP server (ShellExecutorServer)
├── executor.py         # Command execution with persistent sessions
├── security.py         # Security validation and filtering
├── process_manager.py  # Background process tracking
├── platform_detect.py  # Platform-specific configuration
├── cli.py              # CLI support
├── README.md           # Documentation
└── __init__.py
```

### Class Structure

```python
# server.py
class ShellExecutorServer(SchemaBasedMCPServer):
    """MCP server for executing shell commands with persistent sessions."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        # Initialize security validator, executor, process manager
        # Create default persistent terminal session
        pass
    
    async def run_command(self, params: dict) -> dict: ...
    async def run_background(self, params: dict) -> dict: ...
    async def get_terminal_output(self, params: dict) -> dict: ...
    async def kill_process(self, params: dict) -> dict: ...

# executor.py
class PersistentTerminal:
    """Persistent bash terminal session (like GitHub Copilot's run_in_terminal)."""
    
    def __init__(self, bash_path: str, initial_cwd: str = None):
        self.process: asyncio.subprocess.Process = None
        self.bash_path = bash_path
        self.cwd = initial_cwd or os.getcwd()
        self.env = os.environ.copy()
        self._output_buffer = []
    
    async def execute(
        self,
        command: str,
        timeout: float = None,
        is_background: bool = False
    ) -> dict:
        """Execute command in persistent session."""
        pass
    
    async def get_output(self) -> str:
        """Get accumulated output from terminal."""
        pass

class CommandExecutor:
    """Manages multiple terminal sessions and background processes."""
    
    def __init__(self, security_validator: CommandSecurityValidator):
        self.default_terminal = PersistentTerminal(...)
        self.background_processes: dict[str, asyncio.subprocess.Process] = {}
        self.security = security_validator
    
    async def execute_in_terminal(
        self,
        command: str,
        is_background: bool = False,
        timeout: float = None
    ) -> dict:
        """Execute in persistent terminal (stateful)."""
        pass

# security.py
class CommandSecurityValidator:
    """Validates commands against security rules."""
    
    def __init__(self, whitelist: list[str] = None, blacklist: list[str] = None):
        self.whitelist_patterns = whitelist or []
        self.blacklist_patterns = blacklist or []
    
    def validate_command(self, command: str) -> tuple[bool, str]:
        """Validate command is safe to execute."""
        # Check blacklist patterns
        # Check whitelist if configured
        # Detect shell injection attempts
        # Validate argument safety
        pass

# executor.py
class CommandExecutor:
    """Executes shell commands with output capture."""
    
    async def execute(
        self,
        command: str,
        cwd: str = None,
        timeout: float = None,
        env: dict = None
    ) -> dict:
        """Execute command and capture output."""
        pass
    
    async def execute_background(
        self,
        command: str,
        cwd: str = None,
        env: dict = None
    ) -> str:
        """Start background process, return process ID."""
        pass

# process_manager.py
class ProcessManager:
    """Manages background processes."""
    
    def __init__(self):
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.output_buffers: dict[str, list[str]] = {}
    
    def register_process(self, process_id: str, process: asyncio.subprocess.Process): ...
    async def get_output(self, process_id: str) -> dict: ...
    async def kill_process(self, process_id: str) -> bool: ...
    def list_processes(self) -> list[dict]: ...

# platform_detect.py
class PlatformDetector:
    """Detects and configures platform-specific shell."""
    
    def detect_bash(self) -> tuple[str, str]:
        """Return (bash_path, shell_name) for current platform."""
        # Windows: Check for Git Bash, WSL
        # Linux: Use /bin/bash
        pass
```

## Tool Definitions

### 1. execute_command

**Purpose**: Execute a shell command and wait for completion.

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_execute_command"
    description: "Execute a shell command and wait for completion. Returns stdout, stderr, and exit code."
    parameters:
      type: object
      properties:
        command:
          type: string
          description: "Shell command to execute"
        cwd:
          type: string
          description: "Working directory for command execution"
        timeout:
          type: number
          minimum: 1
          maximum: 3600
          default: 300
          description: "Timeout in seconds (1-3600)"
        env_vars:
          type: object
          description: "Environment variables to set"
          additionalProperties:
            type: string
        capture_output:
          type: boolean
          default: true
          description: "Capture stdout/stderr"
      required: ["command"]
      additionalProperties: false
```

**Response**:
```json
{
  "status": "success",
  "exit_code": 0,
  "stdout": "command output here",
  "stderr": "",
  "execution_time": 1.23,
  "command": "ls -la",
  "cwd": "/home/user",
  "truncated": false
}
```

### 2. execute_background

**Purpose**: Start a long-running command in the background.

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_execute_background"
    description: "Execute a long-running command in background. Returns process ID for later control."
    parameters:
      type: object
      properties:
        command:
          type: string
          description: "Shell command to execute"
        cwd:
          type: string
          description: "Working directory"
        env_vars:
          type: object
          description: "Environment variables"
          additionalProperties:
            type: string
        process_id:
          type: string
          description: "Optional custom process ID (generated if omitted)"
      required: ["command"]
      additionalProperties: false
```

**Response**:
```json
{
  "status": "success",
  "process_id": "bg_proc_001",
  "pid": 12345,
  "command": "python server.py",
  "cwd": "/app",
  "started_at": "2025-11-04T19:30:00Z"
}
```

### 3. get_output

**Purpose**: Retrieve output from a background process.

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_get_output"
    description: "Get stdout/stderr from a background process."
    parameters:
      type: object
      properties:
        process_id:
          type: string
          description: "Process ID from execute_background"
        stream:
          type: string
          enum: ["stdout", "stderr", "both"]
          default: "both"
          description: "Which output stream to retrieve"
        clear_buffer:
          type: boolean
          default: false
          description: "Clear buffer after reading"
      required: ["process_id"]
      additionalProperties: false
```

**Response**:
```json
{
  "status": "success",
  "process_id": "bg_proc_001",
  "stdout": "Server started on port 8000\n",
  "stderr": "",
  "is_running": true,
  "exit_code": null
}
```

### 4. kill_process

**Purpose**: Terminate a background process.

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_kill_process"
    description: "Terminate a background process."
    parameters:
      type: object
      properties:
        terminal_id:
          type: string
          description: "Terminal/process ID to kill"
        force:
          type: boolean
          default: false
          description: "Use SIGKILL instead of SIGTERM"
      required: ["terminal_id"]
      additionalProperties: false
```

**Response**:
```json
{
  "status": "success",
  "terminal_id": "bg_001",
  "killed": true,
  "signal": "SIGTERM"
}
```

**Note**: Removed `list_processes` tool - background processes are tracked internally, agents can track their own process IDs in conversation history.

## Security Model

### Command Validation

```python
class CommandSecurityValidator:
    # Dangerous command patterns to block
    DANGEROUS_PATTERNS = [
        r'rm\s+-rf\s+/',           # Recursive delete from root
        r'dd\s+if=.*of=/dev/',     # Disk operations
        r':\(\)\{.*\};:',          # Fork bomb
        r'mkfs\.',                 # Format filesystem
        r'chmod\s+-R\s+777',       # Dangerous permissions
        r'chown\s+-R\s+root',      # Owner changes
        r'wget.*\|.*sh',           # Download and execute
        r'curl.*\|.*bash',         # Download and execute
        r'\$\(.*\)',               # Command substitution abuse
        r'`.*`',                   # Backtick injection
    ]
    
    def validate_command(self, command: str) -> tuple[bool, str]:
        """Validate command safety."""
        # Check for dangerous patterns
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"Command blocked: matches dangerous pattern '{pattern}'"
        
        # Check whitelist if configured
        if self.whitelist_patterns:
            if not any(re.match(pat, command) for pat in self.whitelist_patterns):
                return False, "Command not in whitelist"
        
        # Check blacklist
        for pattern in self.blacklist_patterns:
            if re.search(pattern, command):
                return False, f"Command blocked by blacklist pattern '{pattern}'"
        
        # Check for shell injection attempts
        if self._has_injection_risk(command):
            return False, "Potential shell injection detected"
        
        return True, "OK"
    
    def _has_injection_risk(self, command: str) -> bool:
        """Check for shell injection patterns."""
        # Multiple commands chained
        if '&&' in command or '||' in command or ';' in command:
            # Allow specific safe patterns
            if not self._is_safe_chain(command):
                return True
        
        # Unescaped quotes
        if command.count('"') % 2 != 0 or command.count("'") % 2 != 0:
            return True
        
        return False
```

### Configuration

```yaml
# config/mcp_servers.yaml
servers:
  shell_executor:
    type: shell_executor
    enabled: true
    
    # Security settings (improvement over GitHub Copilot)
    security:
      # Whitelist (if set, only these patterns allowed)
      whitelist:
        - "^git .*"
        - "^python .*"
        - "^npm .*"
        - "^pytest .*"
        - "^ls .*"
        - "^cat .*"
        - "^grep .*"
      
      # Blacklist (always blocked)
      blacklist:
        - "rm -rf /"
        - "dd if="
        - "mkfs"
        - "chmod 777"
      
      # Allow chained commands (&&, ||, ;)
      allow_command_chains: true  # Enable for convenience
    
    # Resource limits (like GitHub Copilot)
    limits:
      max_output_size_kb: 60  # Same as Copilot's run_in_terminal
      default_timeout_seconds: 300
      max_timeout_seconds: 3600
      max_concurrent_background: 10
    
    # Platform settings
    platform:
      bash_path: "auto"  # Auto-detect or specify
      initial_cwd: null  # Default to workspace root
```

## Implementation Details

### Persistent Terminal Session

The core improvement over basic command execution:

```python
class PersistentTerminal:
    """
    Persistent bash session (like GitHub Copilot's run_in_terminal).
    
    Key Features:
    - Environment variables persist across commands
    - Working directory changes persist (cd commands)
    - Activated virtual environments stay active
    - Output from multiple commands accumulates
    """
    
    def __init__(self, bash_path: str, initial_cwd: str = None):
        self.bash_path = bash_path
        self.cwd = initial_cwd or os.getcwd()
        self.env = os.environ.copy()
        self.process = None
        self._output_buffer = []
        self._lock = asyncio.Lock()
    
    async def start(self):
        """Start persistent bash session."""
        self.process = await asyncio.create_subprocess_exec(
            self.bash_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # Merge stderr into stdout for interleaved output
            cwd=self.cwd,
            env=self.env
        )
        
        # Start output reader task
        asyncio.create_task(self._read_output())
    
    async def execute(
        self,
        command: str,
        timeout: float = None,
        is_background: bool = False
    ) -> dict:
        """Execute command in persistent session."""
        async with self._lock:
            start_time = time.time()
            
            # Clear previous output buffer
            self._output_buffer.clear()
            
            # Send command to bash
            command_with_marker = f"{command}\necho '__CMD_DONE__'\n"
            self.process.stdin.write(command_with_marker.encode())
            await self.process.stdin.drain()
            
            if is_background:
                # Don't wait, return immediately
                return {
                    "status": "success",
                    "background": True,
                    "command": command
                }
            
            # Wait for completion marker or timeout
            try:
                output = await asyncio.wait_for(
                    self._wait_for_completion(),
                    timeout=timeout
                )
                
                execution_time = time.time() - start_time
                
                # Check for output size limit
                truncated = len(output) > (self.max_output_kb * 1024)
                if truncated:
                    output = output[:self.max_output_kb * 1024]
                    output += "\n... [OUTPUT TRUNCATED - exceeded 60KB limit]"
                
                return {
                    "status": "success",
                    "exit_code": 0,  # Determined by checking $? in future impl
                    "output": output,
                    "execution_time": execution_time,
                    "truncated": truncated,
                    "cwd": self.cwd
                }
                
            except asyncio.TimeoutError:
                return {
                    "status": "error",
                    "error": f"Command timed out after {timeout}s",
                    "error_type": "TimeoutError",
                    "partial_output": "".join(self._output_buffer)
                }
    
    async def _read_output(self):
        """Continuously read output from bash process."""
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            decoded = line.decode('utf-8', errors='replace')
            self._output_buffer.append(decoded)
    
    async def _wait_for_completion(self) -> str:
        """Wait for command completion marker."""
        output_lines = []
        while True:
            if self._output_buffer:
                line = self._output_buffer.pop(0)
                if '__CMD_DONE__' in line:
                    break
                output_lines.append(line)
            await asyncio.sleep(0.01)
        
        return ''.join(output_lines)
```

### Output Truncation

Automatic truncation prevents context overflow (same as GitHub Copilot):

```python
# Automatic truncation at 60KB (like GitHub Copilot)
MAX_OUTPUT_KB = 60

if len(output) > MAX_OUTPUT_KB * 1024:
    output = output[:MAX_OUTPUT_KB * 1024]
    output += "\n... [OUTPUT TRUNCATED - Use filters like 'head', 'tail', 'grep' to limit output]"
    truncated = True
```

**Best Practices for Agents** (to avoid truncation):
- Use `head -n 20` instead of `cat large_file.txt`
- Use `grep pattern` to filter
- Use `wc -l` to count lines before printing
- Use `git --no-pager` or `| cat` to disable paging

### Background Process Management

```python
class ProcessManager:
    async def execute_background(
        self,
        command: str,
        process_id: str,
        cwd: str = None,
        env: dict = None
    ) -> dict:
        """Start background process."""
        # Prepare command
        shell_cmd = [self.bash_path, "-c", command]
        
        # Prepare environment
        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)
        
        # Create subprocess
        process = await asyncio.create_subprocess_exec(
            *shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=exec_env
        )
        
        # Register process
        self.processes[process_id] = {
            "process": process,
            "command": command,
            "cwd": cwd,
            "started_at": datetime.now().isoformat(),
            "stdout_buffer": [],
            "stderr_buffer": []
        }
        
        # Start output capture task
        asyncio.create_task(self._capture_output(process_id))
        
        return {
            "status": "success",
            "process_id": process_id,
            "pid": process.pid,
            "command": command
        }
    
    async def _capture_output(self, process_id: str):
        """Capture output from background process."""
        proc_info = self.processes[process_id]
        process = proc_info["process"]
        
        # Capture stdout and stderr concurrently
        async def read_stream(stream, buffer):
            while True:
                line = await stream.readline()
                if not line:
                    break
                buffer.append(line.decode('utf-8', errors='replace'))
                
                # Enforce buffer size limit
                if len(buffer) > self.max_buffer_lines:
                    buffer.pop(0)
        
        await asyncio.gather(
            read_stream(process.stdout, proc_info["stdout_buffer"]),
            read_stream(process.stderr, proc_info["stderr_buffer"])
        )
        
        # Mark as finished
        proc_info["finished_at"] = datetime.now().isoformat()
        proc_info["exit_code"] = process.returncode
```

### Platform Detection

```python
class PlatformDetector:
    def detect_bash(self) -> tuple[str, str]:
        """Detect bash executable path."""
        import platform
        import shutil
        
        system = platform.system()
        
        if system == "Windows":
            # Try Git Bash
            git_bash = "C:\\Program Files\\Git\\bin\\bash.exe"
            if os.path.exists(git_bash):
                return git_bash, "Git Bash"
            
            # Try WSL bash
            wsl_bash = shutil.which("wsl")
            if wsl_bash:
                return wsl_bash, "WSL"
            
            # Try bash in PATH
            bash = shutil.which("bash")
            if bash:
                return bash, "bash"
            
            raise RuntimeError("No bash executable found on Windows")
        
        else:  # Linux, macOS
            bash = shutil.which("bash") or "/bin/bash"
            if os.path.exists(bash):
                return bash, "bash"
            
            raise RuntimeError("No bash executable found")
```

## Status Reporting

```python
async def execute_command(self, params: dict) -> dict:
    status = params["_status"]
    command = params["command"]
    
    await status.progress(f"Executing: {command}")
    
    # Validate security
    is_safe, msg = self.security.validate_command(command)
    if not is_safe:
        await status.error(f"Command blocked: {msg}")
        return {"status": "error", "error": msg}
    
    # Execute
    result = await self.executor.execute(command, ...)
    
    if result["status"] == "success":
        await status.complete(
            f"Command completed (exit code {result['exit_code']})",
            meta={"execution_time": result["execution_time"]}
        )
    else:
        await status.error(f"Command failed: {result['error']}")
    
    return result
```

## Cancellation Support

```python
async def execute_command(self, params: dict) -> dict:
    token = params.get("_cancellation_token")
    
    # Start process
    process = await asyncio.create_subprocess_exec(...)
    
    # Register cleanup callback
    async def cleanup():
        if process.returncode is None:
            process.kill()
            await process.wait()
    
    if token:
        token.add_cleanup_callback(cleanup)
    
    try:
        # Wait for completion with cancellation check
        while process.returncode is None:
            if token and token.is_cancelled:
                await cleanup()
                return {"status": "cancelled", "process_killed": True}
            
            await asyncio.sleep(0.1)
        
        # Get output
        stdout, stderr = await process.communicate()
        
        return {"status": "success", ...}
    
    finally:
        if token:
            token.remove_cleanup_callback(cleanup)
```

## Error Handling

```python
async def execute_command(self, params: dict) -> dict:
    try:
        command = params["command"]
        
        # Security validation
        is_safe, msg = self.security.validate_command(command)
        if not is_safe:
            return {
                "status": "error",
                "error": msg,
                "error_type": "SecurityError"
            }
        
        # Execute
        result = await self.executor.execute(command, ...)
        
        return result
        
    except FileNotFoundError as e:
        return {
            "status": "error",
            "error": f"Working directory not found: {params.get('cwd')}",
            "error_type": "FileNotFoundError"
        }
    except PermissionError as e:
        return {
            "status": "error",
            "error": f"Permission denied: {e}",
            "error_type": "PermissionError"
        }
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__
        }
```

## Testing Strategy

### Unit Tests

```python
# tests/test_plugin_shell_executor_basic.py

async def test_execute_simple_command():
    """Test simple command execution."""
    server = ShellExecutorServer("test", system_config, mcp_config)
    
    result = await server.execute_command({
        "command": "echo 'Hello World'",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert "Hello World" in result["stdout"]
    assert result["exit_code"] == 0

async def test_dangerous_command_blocked():
    """Test dangerous commands are blocked."""
    server = ShellExecutorServer("test", system_config, mcp_config)
    
    result = await server.execute_command({
        "command": "rm -rf /",
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "SecurityError" in result["error_type"]

async def test_command_timeout():
    """Test command timeout enforcement."""
    server = ShellExecutorServer("test", system_config, mcp_config)
    
    result = await server.execute_command({
        "command": "sleep 10",
        "timeout": 1,
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "timeout" in result["error"].lower()
```

### Integration Tests

```python
# tests/test_plugin_shell_executor_integration.py

async def test_background_process_lifecycle():
    """Test complete background process lifecycle."""
    server = ShellExecutorServer("test", system_config, mcp_config)
    
    # Start background process
    start_result = await server.execute_background({
        "command": "python -c 'import time; [print(i) for i in range(5)]; time.sleep(1)'",
        "_status": mock_status
    })
    
    assert start_result["status"] == "success"
    process_id = start_result["process_id"]
    
    # Wait a bit for output
    await asyncio.sleep(0.5)
    
    # Get output
    output_result = await server.get_output({
        "process_id": process_id,
        "_status": mock_status
    })
    
    assert output_result["status"] == "success"
    assert output_result["is_running"]
    
    # Kill process
    kill_result = await server.kill_process({
        "process_id": process_id,
        "confirm": True,
        "_status": mock_status
    })
    
    assert kill_result["status"] == "success"
```

## CLI Support

```bash
# Execute command
shell-exec run "ls -la" --cwd /tmp --timeout 30

# Execute in background
shell-exec bg "python server.py" --id "my_server"

# Get output
shell-exec output --id "my_server"

# List processes
shell-exec list

# Kill process
shell-exec kill --id "my_server" --confirm
```

## Future Enhancements

1. **Interactive shell sessions**: Persistent shell with REPL-like interaction
2. **Output streaming**: Real-time output via SSE
3. **Docker execution**: Run commands in Docker containers
4. **SSH execution**: Execute commands on remote systems
5. **Script execution**: Execute multi-line scripts from files
6. **Process monitoring**: Resource usage tracking (CPU, memory)
7. **Command history**: Track and replay previous commands
8. **Smart completion**: Suggest commands based on context

## References

- GitHub Copilot `run_in_terminal` tool
- Python asyncio subprocess documentation
- Bash security best practices
- Shell injection prevention techniques
- MCP protocol specification
- AgentSystem plugin architecture
