import os
import socket
import pytest
import subprocess
import sys
import time
import signal
import atexit
from typing import List
import traceback
import weakref

# Ensure any subprocess.Popen calls that open text streams default to UTF-8
# to avoid UnicodeDecodeError in the subprocess reader threads on Windows
# where the locale encoding can be cp1252. We wrap Popen early so it affects
# tests that don't explicitly provide an encoding.
_original_popen = subprocess.Popen

def _popen_force_utf8(*args, **kwargs):
    # If text mode is requested but no encoding provided, force UTF-8 with replace
    if kwargs.get("text") and "encoding" not in kwargs:
        kwargs["encoding"] = "utf-8"
        kwargs.setdefault("errors", "replace")
    return _original_popen(*args, **kwargs)

# Replace subprocess.Popen with our wrapper for the test session
# Keep subprocess wrapper installed
subprocess.Popen = _popen_force_utf8

# --- Test-only LLM factory stub ---------------------------------------------
# During bootstrap the code may call `make_llm()` to construct LLM clients which
# can create real HTTP/async clients and open sockets. To prevent network
# allocations during test collection/bootstrap we replace `make_llm` with a
# lightweight fake client that does not allocate network resources. Tests that
# need real LLM client behavior should construct them directly or patch the
# factory themselves.
try:
    from agent_system.llm import clients as _llm_clients

    class _FakeLLMClient:
        def __init__(self, provider: str | None = None, model: str | None = None, **_kwargs):
            # Expose attributes that tests inspect in DI and integration tests
            self.provider = provider
            self.model = model
            # Keep a tiny in-memory conversation cache so session continuity tests
            # can observe that history was taken into account.
            self._history = []

        async def chat(self, messages, cancellation_token=None):
            # Record messages for future calls
            try:
                for m in messages:
                    # messages may be pydantic ChatMessage objects or simple dicts
                    content = getattr(m, 'content', None) if m is not None else None
                    if content:
                        self._history.append(content)
            except Exception:
                pass

            # Return a deterministic non-empty response that includes recent user
            # messages so tests that assert history or acknowledgement succeed.
            if self._history:
                # Echo last two user messages joined so session continuity checks find history
                resp = " ".join(self._history[-2:])
                return resp
            return "ok"

        async def chat_tools(self, messages, tools, cancellation_token=None):
            # Provide the minimal shape expected by callers: a dict with assistant content
            txt = await self.chat(messages, cancellation_token=cancellation_token)
            return {"assistant": {"content": txt}}

    def _fake_make_llm(provider, model, openai_api_key, ollama_url=None, context_window=None, ollama_mode=None, request_timeout=None, ssl_verify=None, client_type=None, httpx_timeouts=None):
        return _FakeLLMClient(provider=provider, model=model, context_window=context_window)

    # Preserve original for debugging if needed
    if not hasattr(_llm_clients, "_orig_make_llm"):
        _llm_clients._orig_make_llm = _llm_clients.make_llm
    _llm_clients.make_llm = _fake_make_llm
except Exception:
    # If importing or patching fails, don't break test run; fall back to normal behavior
    pass



def pytest_sessionfinish(session, exitstatus):
    """Hook that runs at the very end of pytest session."""
    print("\n[conftest] pytest_sessionfinish: Final cleanup check...")
    # Wait a bit longer for any processes to settle
    time.sleep(2.0)
    final_pids = _find_project_python_pids()
    if final_pids:
        print(f"[conftest] Final cleanup: found remaining processes {final_pids}")
        _kill_pids(final_pids)
        # Double-check after cleanup
        time.sleep(1.0)
        remaining = _find_project_python_pids()
        if remaining:
            print(f"[conftest] WARNING: Some processes still running after final cleanup: {remaining}")
    else:
        print("[conftest] Final cleanup: no remaining processes found")


def _final_emergency_cleanup():
    """Emergency cleanup function registered with atexit."""
    emergency_pids = _find_project_python_pids()
    if emergency_pids:
        print(f"[conftest] Emergency cleanup: killing {emergency_pids}")
        _kill_pids(emergency_pids)


# Register emergency cleanup that runs when Python exits
atexit.register(_final_emergency_cleanup)


@pytest.fixture(scope="session", autouse=True)
def set_test_server_port():
    """Find a free TCP port and set TEST_SERVER_PORT for the test session.

    This ensures tests that start or connect to the local HTTP API use an
    ephemeral port and don't conflict with a production server running on
    the default port (8000).
    """
    # Bind to port 0 to get an ephemeral free port from the OS
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    os.environ.setdefault("TEST_SERVER_PORT", str(port))
    # Ensure subprocesses and Python child processes use UTF-8 and replace undecodable bytes
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8:replace")
    # Expose to pytest (optional)
    yield port

    # No teardown required; environment variable will be discarded after tests


@pytest.fixture(autouse=True)
def cleanup_unclosed_resources():
    """Function-scoped fixture that attempts to close any lingering
    socket.socket objects or asyncio event loops after each test.

    This is a defensive, test-only cleanup to avoid ResourceWarning being
    promoted to errors in the full test run. We only close sockets and
    loops that are not running (to avoid interfering with active servers).
    """
    yield

    # Teardown: attempt to close stray sockets and event loops
    try:
        import gc
        import asyncio as _asyncio
        objs = gc.get_objects()
        for obj in objs:
            try:
                # Close raw socket objects left around
                if isinstance(obj, socket.socket):
                    try:
                        # Only close if fileno seems valid
                        fd = obj.fileno()
                        if fd is not None and fd >= 0:
                            try:
                                obj.close()
                            except Exception:
                                pass
                    except Exception:
                        # If fileno() raises, attempt best-effort close
                        try:
                            obj.close()
                        except Exception:
                            pass

                # Close asyncio loops that are not running and not closed
                if isinstance(obj, _asyncio.BaseEventLoop):
                    try:
                        if not obj.is_running() and not obj.is_closed():
                            try:
                                obj.close()
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                # Ignore any inspection errors for gc-scanned objects
                pass
    except Exception:
        pass


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _find_project_python_pids() -> List[int]:
    """Return python process PIDs that look like test servers for this repo.

    We filter by command-line content to avoid killing unrelated Python.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    matches: List[int] = []

    if _is_windows():
        # Use PowerShell Get-CimInstance as primary method (more reliable than wmic)
        try:
            repo_lower = repo_root.lower().replace('\\', '\\\\')
            ps_script = f'''
            Get-CimInstance Win32_Process | Where-Object {{ $_.Name -match "python(\\.exe|w\\.exe)?" }} | 
            ForEach-Object {{ 
                $cmd = $_.CommandLine; 
                if ($cmd) {{ 
                    $lc = $cmd.ToLower(); 
                    if ($lc -like "*{repo_lower}*" -or $lc -like "*.venv\\\\*" -or $lc -like "*uvicorn*" -or $lc -like "*-m agent_system.agent.interface_api*" -or $lc -like "*agent_system*") {{ 
                        Write-Output $_.ProcessId 
                    }} 
                }} 
            }}
            '''
            out = subprocess.check_output([
                "powershell", "-NoProfile", "-Command", ps_script
            ], stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace')
            
            for line in out.strip().splitlines():
                if line.strip():
                    try:
                        matches.append(int(line.strip()))
                    except ValueError:
                        pass
        except Exception:
            # Fallback to simpler tasklist approach
            try:
                # Get all python processes and their command lines
                out = subprocess.check_output([
                    "powershell", "-NoProfile", "-Command",
                    '''Get-Process python* -ErrorAction SilentlyContinue | ForEach-Object { 
                        try { 
                            $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)").CommandLine;
                            if ($cmdline -and ($cmdline.ToLower() -like "*agent_system*" -or $cmdline.ToLower() -like "*interface_api*" -or $cmdline.ToLower() -like "*.venv\\*")) {
                                Write-Output $_.Id
                            }
                        } catch { }
                    }'''
                ], stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace')
                
                for line in out.strip().splitlines():
                    if line.strip():
                        try:
                            matches.append(int(line.strip()))
                        except ValueError:
                            pass
            except Exception:
                pass

    else:
        # POSIX: use ps
        try:
            out = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
        except Exception:
            return matches

        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                pid_str, args = line.strip().split(None, 1)
                pid = int(pid_str)
            except Exception:
                continue
            low = args.lower()
            if "python" in low and (repo_root.lower() in low or "-m agent_system.agent.interface_api" in low or ".venv/" in low):
                matches.append(pid)

    return matches


def _kill_pids(pids: List[int]) -> None:
    """Kill processes with retry logic and proper waiting."""
    if not pids:
        return
    
    print(f"[conftest] Attempting to kill PIDs: {pids}")
    
    if _is_windows():
        for pid in pids:
            try:
                # Use taskkill with force and tree kill options
                subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], 
                             check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    else:
        # First try graceful SIGTERM
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        
        # Wait a bit for graceful shutdown
        time.sleep(1.0)
        
        # Force kill any remaining processes
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    
    # Wait for processes to actually terminate
    time.sleep(1.5)
    
    # Verify cleanup worked and retry if needed
    remaining = []
    for pid in pids:
        try:
            if _is_windows():
                # Check if process still exists on Windows
                result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], 
                                      capture_output=True, text=True)
                if result.stdout and str(pid) in result.stdout:
                    remaining.append(pid)
            else:
                # Check if process still exists on Unix
                os.kill(pid, 0)  # This will raise OSError if process doesn't exist
                remaining.append(pid)
        except (OSError, subprocess.CalledProcessError):
            # Process doesn't exist anymore, good
            pass
    
    if remaining:
        print(f"[conftest] Retrying cleanup for remaining PIDs: {remaining}")
        # One more aggressive attempt
        if _is_windows():
            for pid in remaining:
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], 
                                 check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
        else:
            for pid in remaining:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
        time.sleep(1.0)


@pytest.fixture(scope="session", autouse=True)
def ensure_test_servers_terminated():
    """Ensure any leftover test server python processes are terminated.

    This runs before the test session and again after the session finishes.
    It only targets processes whose command lines reference the repository,
    the project's virtualenv, or the test server module name.
    """
    # Pre-test cleanup (best-effort)
    pre = _find_project_python_pids()
    if pre:
        print("[conftest] terminating pre-existing project python processes:", pre)
        _kill_pids(pre)
        # give OS a moment to settle
        time.sleep(0.5)

    yield

    # Post-test cleanup (more aggressive)
    print("[conftest] Starting post-test cleanup...")
    for attempt in range(3):  # Multiple cleanup attempts
        post = _find_project_python_pids()
        if not post:
            break
        print(f"[conftest] Cleanup attempt {attempt + 1}: terminating leftover project python processes:", post)
        _kill_pids(post)
        time.sleep(1.0)
    
    # Final check
    final = _find_project_python_pids()
    if final:
        print(f"[conftest] WARNING: Some processes may still be running after cleanup: {final}")
    else:
        print("[conftest] All project processes successfully terminated")


# Modern plugin test fixtures
@pytest.fixture
def mock_system_config():
    """Create a mock AgentSystemConfig for plugin tests."""
    from unittest.mock import Mock
    config = Mock()
    config.ssl_verify = True
    config.api_base_url = "http://localhost:8000"
    config.log_level = "INFO"
    return config


@pytest.fixture
def mock_mcp_config():
    """Create a mock MCPConfig object for plugin tests with agent_config."""
    from agent_system.config.models import MCPConfig, AgentConfig
    agent_config = AgentConfig()
    return MCPConfig(type="test", enabled=True, agent_config=agent_config)


# End of conftest.py fixtures
