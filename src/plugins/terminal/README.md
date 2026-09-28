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
- `wake` (boolean, optional): Only with `background=true` — wake this session when the process ends, so the turn can be ended instead of polling (default: false)

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

With `wake: true` the answer carries one more field, `"wake"`, and a
`"wake_note"` whenever it is `false`. See **Waking instead of polling**.

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

## Waking instead of polling

`execute(background=true, wake=true)` lets the caller end its turn over a long
command. When the process ends — finished or failed, there is no second ending —
the plugin tells the core that input is waiting for the calling session
(`core/session_presence.py`, `wake_session`): a session another process holds
reads that at its next step, a session nobody holds is continued in a run of its
own. The woken run is told that input waits; it reads the result with
`get_output` on the `process_id` from its own history.

**The answer says whether the wake is armed**, because a caller that asked for one
and silently did not get it would end its turn over work it never hears about
again. Both reasons are known before the process starts:

| `wake` | `wake_note` | What to do |
|---|---|---|
| `true` | — | End the turn. `get_output` when woken. |
| `false` | `session presence is off (config: session_presence.enabled)` | Poll `get_output`. |
| `false` | `this call belongs to no session, so there is nobody to wake` | Poll `get_output`. |

A `process_id` you choose yourself must not belong to a RUNNING process:
reusing one returns `ProcessIdInUse` rather than replacing the entry, which
would leave the process behind it running with no way to read or kill it.
Once that process is over the id is free again: the next run takes the name,
and anything recorded under it is dropped with it.

A call that did not ask is told nothing about a wake — the two fields are absent.

**An armed wake is best effort, not a promise.** What cannot be checked up front
is whether the process holding the work is still there when the work ends —
nothing marks which kind of process this is. A caller that is not woken should
poll `get_output`.

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
to five minutes) and stops early once the session has dealt with the process
itself — `get_output` on the finished result, or `kill_process`. Either way it
is not started again for something it already handled.

Three further cases end with no wake, and only the first is refused up front:

| Case | What happens |
|---|---|
| `session_presence.max_wake_depth` reached, or `0` | refused before the start, with the setting named |
| A sub-agent's session | armed, but never woken — the run that spawned it hands its result over. Reading this up front means parsing the whole session file on the event loop for every armed wake, so it is not checked |
| A one-shot `agent-cli run` | armed, but the run ends and takes the work with it |

`wake: true` without `background: true` is answered too: the result is already
in that answer, so there is nothing to wake for.

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
      
      # More than one command per call (&&, ||, ;, |, &, line breaks, substitutions)
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

Command substitution (`` `...` ``, `$(...)`) is not on this list; it is
refused with chains off and on every whitelisted instance (see Command Chains).

**Blacklist:** Add custom regex patterns to block specific commands.

**Whitelist:** If configured, only commands matching whitelist patterns are allowed --
and every command runs in the instance's configured working directory and
environment: the tool neither offers nor accepts `cwd` and `env_vars` then
(`error_type: "ConfiguredOnly"`), and no entry into the server gets past that --
the check sits where every process is spawned (`CommandExecutor.refusal`). A
whitelist checks the command string only; the directory decides which files the
allowed command touches, the environment how it runs (`PYTHONPATH`,
`PYTHONSTARTUP`, `PATH`, `LD_PRELOAD`), so taken from the model they would undo
it. Set `platform.initial_cwd` on such an instance when its commands name paths
relative to a directory: without it, commands start where the process was
started from. A relative `initial_cwd` such as `.` is the directory the server
runs from -- the checkout, as every relative path of the configuration assumes;
agent-cli and agent-run enter it at startup, the API has to be started there
(started elsewhere, a command that names a relative path fails). A whitelisted
instance also refuses every control character (below 0x20 except tab, and DEL)
before it asks a pattern, and keeps the shell line to one command whatever
`allow_command_chains` says (see Command Chains).

Writing the patterns:

- Anchor every pattern at both ends: `^...\Z`. `$` also matches before a final
  line break.
- Name the program exactly -- `^python ...`, not `^.*python ...` or
  `^[\w/.-]*python ...`: a free prefix lets any program whose name ends in those
  letters through. A path to the program is named in full and relative to
  `initial_cwd`, with nothing in front of it and with forward slashes: bash
  drops unquoted backslashes, so a backslash form only looks allowed.
- Separate words with literal spaces (` +`), not `\s`: `\s` also matches a line
  break, and `bash -c` runs every line as a command of its own.
- Name a program that does not run other commands from its arguments: the
  terminal keeps the shell line to one command, but what that command does
  with its arguments is up to the command.

**Command Chains:** With `allow_command_chains: false`, and on every instance
with a whitelist, the shell line holds one command. A command is refused when it
contains a line break, `;`, `&&`, `||`, `|`, `&`, a backtick, `$(`, `<(` or `>(`
anywhere -- also inside quotes: telling a quoted `;` from a live one takes a
shell parser, and a mistake there would let a second command through. The check
is lexical: a command that evaluates its own arguments (an evaluating builtin, a
nested shell) or an evaluating expansion can still run another, so the command
is exactly one only together with a whitelist that names the program. An
instance without a whitelist that needs such a character turns chains on.

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

On an instance with a whitelist, the `execute` tool has no `cwd` and no
`env_vars` parameter, and its description says the terminal runs only the
commands its configuration allows, in its configured directory and environment.
A call that sends either anyway -- to `execute` in the foreground or in the
background -- gets `error_type: "ConfiguredOnly"` with a message saying to send
the command without them; nothing is started, and a finished background
process whose `process_id` the call names keeps its recorded result. With chains
off or a whitelist, a second command in the same call is refused with a message
saying the terminal runs one command per call.

With `wake: true` the model gets back `"wake": true` or `"wake": false` with a
`wake_note` naming the reason. That one field decides whether it may end its turn
or has to poll `get_output`, so it is never omitted when it was asked for.

Under confinement, a denied write is **not** a plugin error: the command runs
and fails on its own, so the model sees the ordinary non-zero exit code and
the shell's `Permission denied` on stdout. That is deliberate — it is what the
same command does against an unwritable directory anywhere else.

### Token and cache effect

Append-only. The plugin contributes nothing to the system prompt or the tool
list beyond its three tool definitions -- static per instance: a whitelisted
instance renders `execute` without `cwd` and `env_vars` and with one sentence
more in its description, once, when its schema is built. Confinement adds no
tokens at all, because the wrapping happens below the model. Output is capped at
`max_output_size_kb` (60 KB default).

### Known gaps

- **Windows has no backend.** Confinement there is refusal, not enforcement.
- **File effects only.** A confined process still has the network.
- **The workspace is one directory.** No multi-root policy; a job needing two
  trees has to be given a common parent.
- **Confinement is decided at spawn.** A mode change takes effect on the next
  command, never on one already running.
- **A recorded result lives one hour and is read once.** It is dropped as soon
  as anybody reads the finished process — by the woken run that recalls it, by
  a `get_output` in the process that still holds the result, or by handing its
  `process_id` to a new run. A woken run that never calls `get_output` at all
  leaves it to expire; a second reader finds nothing.
- **A wake needs the process that started the work.** Background processes live
  in the tool server's memory, so the API and `agent-cli chat` (whose prompt waits
  on the same loop) can wake; a one-shot `agent-cli run` ends its turn and takes
  its background processes with it. `wake: true` is still reported as armed there
  — the session exists, and nothing in this process knows it is about to end.

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
├── server.py            # TerminalServer (tool interface)
├── executor.py          # CommandExecutor (subprocess management)
├── process_manager.py   # ProcessManager (background processes)
├── security.py          # CommandSecurityValidator
├── platform_detect.py   # PlatformDetector
└── README.md           # This file
```

**Key Components:**

- **TerminalServer**: tool server implementing tool handlers
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
  - Use `cwd` and `env_vars` parameters instead -- not on a whitelisted
    instance, which refuses both (`ConfiguredOnly`)
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
- **Solution:** Pass env variables explicitly, they don't persist between commands --
  not on a whitelisted instance, which refuses both `cwd` and `env_vars` (`ConfiguredOnly`)

## Version History

- **1.0.0** (2025-01-30): Initial release with core functionality
  - Cross-platform bash execution
  - Security validation
  - Background process management
  - Comprehensive test coverage (45 tests)
  - Unified `execute` method with `background` parameter
  - Legacy compatibility with `execute_command` and `execute_background`
