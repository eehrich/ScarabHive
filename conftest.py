import os
import socket
import pytest
import subprocess
import sys
import time
import signal
import atexit
from typing import List
from pathlib import Path
import tempfile

# Disable WAL mode for writer plugins in tests (avoids Windows file locking issues)
os.environ["WRITER_DISABLE_WAL"] = "true"

# Set test session storage path BEFORE any imports of agent_system
# This ensures all tests use a temporary directory for sessions
_TEST_SESSION_DIR = Path(tempfile.mkdtemp(prefix="agent_test_sessions_"))
os.environ["AGENT_SESSION_STORAGE_PATH"] = str(_TEST_SESSION_DIR)

# Patch SessionManager to force test storage path
# This must happen before agent_system is imported
def _patch_session_manager():
    """Monkey-patch SessionManager.__init__ to use test storage path.

    If a test explicitly provides storage_path, we use it (for unit tests).
    Otherwise, create a unique tmp directory (for integration tests).
    """
    try:
        from agent_system.services.session_manager import SessionManager
        _original_init = SessionManager.__init__

        def _test_init(self, storage_path=None, *args, **kwargs):
            # If test explicitly provides a path, use it
            if storage_path is not None:
                return _original_init(self, storage_path=storage_path, *args, **kwargs)

            # Otherwise, create unique tmp directory for this instance
            import tempfile
            test_path = Path(tempfile.mkdtemp(prefix="agent_test_session_"))
            return _original_init(self, storage_path=str(test_path), *args, **kwargs)

        SessionManager.__init__ = _test_init
        print("[conftest] Patched SessionManager to use unique tmp directories per instance")
    except Exception as e:
        print(f"[conftest] Failed to patch SessionManager: {e}")

# Apply the patch
_patch_session_manager()


# Configure logging for tests: use NullHandler to prevent file I/O
# This ensures tests don't write to production logs (logs/api.log)
def _configure_test_logging():
    """Configure logging to suppress file output during tests.
    
    This prevents test runs from writing to production log files like logs/api.log.
    pytest's caplog fixture will still capture logs for assertions.
    """
    import logging
    
    # Set environment variable to indicate test mode
    os.environ["AGENT_SYSTEM_TEST_MODE"] = "1"
    
    # Get root logger and remove any existing handlers
    root = logging.getLogger()
    for handler in list(root.handlers):
        try:
            # Only remove file handlers to preserve pytest's capturing
            if isinstance(handler, (logging.FileHandler, logging.handlers.RotatingFileHandler)):
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        except Exception:
            pass
    
    # Set default level to allow caplog to capture all levels
    root.setLevel(logging.DEBUG)
    
    # Add NullHandler to prevent "No handler found" warnings
    if not any(isinstance(h, logging.NullHandler) for h in root.handlers):
        root.addHandler(logging.NullHandler())
    
    # Suppress specific noisy loggers during tests
    for logger_name in ['httpcore', 'httpx', 'asyncio', 'urllib3', 'filelock']:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

# Import logging.handlers for RotatingFileHandler check (used below and in
# _reset_all_global_state; NOT auto-available from `import logging` alone).
import logging.handlers  # noqa: F401,E402

# Apply test logging configuration early
_configure_test_logging()


# Ensure any subprocess.Popen calls that open text streams default to UTF-8
# to avoid UnicodeDecodeError in the subprocess reader threads on Windows
# where the locale encoding can be cp1252. We wrap Popen early so it affects
# tests that don't explicitly provide an encoding.
_original_popen = subprocess.Popen

class _PopenForceUtf8:
    """Wrapper around subprocess.Popen that forces UTF-8 encoding in text mode.
    
    This is a class wrapper instead of a function to preserve __class_getitem__
    support for type annotations like subprocess.Popen[bytes] used by mcp library.
    """
    def __new__(cls, *args, **kwargs):
        # If text mode is requested but no encoding provided, force UTF-8 with replace
        if kwargs.get("text") and "encoding" not in kwargs:
            kwargs["encoding"] = "utf-8"
            kwargs.setdefault("errors", "replace")
        return _original_popen(*args, **kwargs)
    
    def __class_getitem__(cls, item):
        # Support type annotations like Popen[bytes]
        return _original_popen.__class_getitem__(item)

# Replace subprocess.Popen with our wrapper for the test session
# Keep subprocess wrapper installed
subprocess.Popen = _PopenForceUtf8

# --- Test-only LLM factory stub ---------------------------------------------
# During bootstrap the code may call `registry.build_client()` to construct LLM
# clients which can create real HTTP/async clients and open sockets. To prevent
# network allocations during test collection/bootstrap we replace it with a
# lightweight fake client that does not allocate network resources. Tests that
# need real LLM client behavior should construct them directly, use
# `registry._orig_build_client`, or patch the factory themselves.
try:
    from agent_system.llm import registry as _llm_registry

    class _FakeLLMClient:
        def __init__(self, provider: str | None = None, model: str | None = None, **_kwargs):
            # Expose attributes that tests inspect in DI and integration tests
            self.provider = provider
            self.model = model
            # Keep a tiny in-memory conversation cache so session continuity tests
            # can observe that history was taken into account.
            self._history = []

        def supports_streaming(self) -> bool:
            """Return False to follow non-streaming code paths in tests."""
            return False

        def set_app_title(self, title: str) -> None:
            # No-op: real clients set the OpenRouter HTTP-Referer header;
            # the fake doesn't open HTTP connections. Without this method,
            # BatchLLMClient.set_app_title (forwards to underlying_client)
            # crashes during bootstrap of *_batch plugin agents.
            pass

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

        async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
            # Provide the minimal shape expected by callers: a dict with assistant content
            txt = await self.chat(messages, cancellation_token=cancellation_token)
            return {"assistant": {"content": txt}}

        async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
            # Streaming version: yield chunks that mimic real LLM streaming responses
            txt = await self.chat(messages, cancellation_token=cancellation_token)
            # Yield a single chunk with the full response
            yield {"delta": {"content": txt}, "done": False}
            yield {"delta": {}, "done": True}

    def _fake_build_client(cfg, ssl_verify=None, **_kwargs):
        return _FakeLLMClient(provider=getattr(cfg, "provider", None),
                              model=getattr(cfg, "model", None),
                              context_window=getattr(cfg, "context_window", None))

    # Preserve original for tests that exercise the real construction path
    if not hasattr(_llm_registry, "_orig_build_client"):
        _llm_registry._orig_build_client = _llm_registry.build_client
    _llm_registry.build_client = _fake_build_client
except Exception as _seam_error:
    # Stop, don't warn: without the seam, tests build REAL clients — with the
    # real keys from the environment, opening real sockets during bootstrap.
    # A warning would not do here anyway (pytest.ini sets
    # filterwarnings=error, so it ends the session with a confusing
    # "ImportError while loading conftest"); this says what actually
    # happened. tests/llm pins the seam too, but a targeted partial run — the
    # normal way to work here — doesn't include that test.
    raise RuntimeError(
        f"LLM build_client seam could not be installed ({_seam_error!r}). "
        f"Tests would construct real clients and open real sockets, so the "
        f"run is stopped instead."
    ) from _seam_error



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


class WokeForReal(BaseException):
    """Not an Exception on purpose: session_presence.notify() catches Exception
    and turns a failed wake into a queued message. A guard that is an Exception
    is swallowed there -- the test goes green and measures nothing."""


@pytest.fixture(autouse=True)
def never_wake_a_session_for_real():
    """Session presence (agent_system/core/session_presence.py) wakes an idle
    session by STARTING agent-cli -- a real run, with real LLM calls and real
    money. A test that reaches that path by accident has to fail, not spend.

    Tests that mean to wake replace spawn_wake themselves; their patch wins for
    as long as they run, and this puts the refusal back afterwards.
    """
    try:
        from agent_system.core import session_presence
    except Exception:  # the module is not part of every checkout state
        yield
        return

    original = session_presence.spawn_wake

    def refuse(session_id, user_id, depth):
        raise WokeForReal(
            f"a test tried to wake session {session_id} for real -- spawn_wake "
            "starts agent-cli. Replace spawn_wake in the test.")

    session_presence.spawn_wake = refuse
    try:
        yield
    finally:
        session_presence.spawn_wake = original


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
    # This conftest lives at the repository root, so its directory IS repo_root.
    repo_root = os.path.abspath(os.path.dirname(__file__))
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
                    if ($lc -like "*{repo_lower}*" -or $lc -like "*.venv\\\\*" -or $lc -like "*uvicorn*" -or $lc -like "*-m agent_system.app*" -or $lc -like "*agent_system*") {{
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
                if "python" in low and (repo_root.lower() in low or "-m agent_system.app" in low or ".venv/" in low):
                    matches.append(pid)

        # Filter matches to avoid killing the current pytest process or its parent
        filtered: List[int] = []
        current_pid = os.getpid()
        parent_pid = os.getppid()
        for pid in dict.fromkeys(matches):  # preserve order, remove duplicates
            if pid in (current_pid, parent_pid):
                # Never consider the running test process or its immediate parent
                continue
            try:
                # Try to inspect the command line for the pid to avoid false positives
                cmd = None
                try:
                    with open(f"/proc/{pid}/cmdline", "r", encoding="utf-8", errors="ignore") as f:
                        raw = f.read().replace('\x00', ' ').strip()
                        cmd = raw.lower()
                except Exception:
                    # Fallback to ps if /proc is not available
                    try:
                        out2 = subprocess.check_output(["ps", "-p", str(pid), "-o", "args="], text=True, stderr=subprocess.DEVNULL)
                        cmd = out2.strip().lower()
                    except Exception:
                        cmd = None

                if cmd:
                    # Avoid killing pytest runner or py.test
                    if "pytest" in cmd or "py.test" in cmd:
                        continue
                    # Avoid matching ephemeral interactive python like 'python -'
                    if cmd.endswith(" -") or cmd.endswith(" -c"):
                        continue

            except Exception:
                # If anything goes wrong inspecting this pid, conservatively include it
                pass

            filtered.append(pid)

        return filtered



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


# ============================================================================
# GLOBAL STATE RESET FIXTURES
# These fixtures reset singleton/global state between tests to avoid cross-test
# contamination when tests run in parallel or in different order.
# ============================================================================

def _reset_all_global_state():
    """Helper function to reset all known global state."""
    # Reset logging: remove file handlers added during test
    try:
        import logging
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, (logging.FileHandler, logging.handlers.RotatingFileHandler)):
                try:
                    root.removeHandler(handler)
                    handler.close()
                except Exception:
                    pass
    except Exception:
        pass
    
    # Reset auth database
    try:
        from agent_system.auth import database as auth_db_module
        auth_db_module._db = None
    except ImportError:
        pass
    
    # Reset plugin registries.
    #
    # mcp/integration.py aliases this registry at IMPORT time
    # (`from ...mcp_adapter import plugin_mcp_registry`) and MCPIntegration
    # stores that alias as self.plugin_registry. REBINDING the attribute here
    # (the old `plugin_mcp_registry = PluginMCPRegistry()`) therefore does NOT
    # reach the stale alias — it keeps pointing at the fully-discovered
    # registry a prior app-building test populated. A later bare-agent test
    # then discovers every plugin and renders an extra tool-system prompt
    # (regression seen in tests/integration test_message_list_construction and
    # broad writer-test pollution). Clear the registry contents IN PLACE so
    # every alias observes it empty, and unify both module references on one
    # instance.
    try:
        from agent_system.plugins import mcp_adapter
        reg = mcp_adapter.plugin_mcp_registry
        reg.plugin_servers.clear()
        reg.plugin_factories.clear()
        try:
            from agent_system.mcp import integration as _mcp_int_mod
            stale = getattr(_mcp_int_mod, "plugin_mcp_registry", None)
            if stale is not None and stale is not reg:
                stale.plugin_servers.clear()
                stale.plugin_factories.clear()
                _mcp_int_mod.plugin_mcp_registry = reg
        except ImportError:
            pass
    except (ImportError, AttributeError):
        pass
    
    # The web registry likewise: mcp_adapter registers into its import-time
    # alias, the UI catalogue reads web_adapter's -- clear in place so both
    # stay one object.
    try:
        from agent_system.plugins import web_adapter
        web_registry = web_adapter.plugin_web_registry
        web_registry.web_plugins.clear()
        web_registry.active_routers.clear()
        web_registry.static_mounts.clear()
        web_registry.security_configs.clear()
        web_adapter._plugin_security_enforcer = None
    except ImportError:
        pass
    
    # Reset app registry and all app-level globals
    try:
        from agent_system import app as app_module
        app_module._app_registry = None
        app_module._mcp_integration = None
        app_module._mcp_server_handler = None
        app_module._config_service = None
        app_module._mcp_service = None
        app_module._tool_service = None
        app_module._agent_service = None
        app_module._initialization_service = None
        app_module._session_manager = None
        app_module._session_service = None
        app_module._shutdown_event = None
        app_module._app_start_time = None
    except ImportError:
        pass
    
    # Reset hook registry
    try:
        from agent_system.hooks import registry as hooks_registry_module
        hooks_registry_module._global_hook_registry = None
    except ImportError:
        pass
    
    # Reset MCP service module state
    try:
        from agent_system.mcp import service as mcp_service_module
        if hasattr(mcp_service_module, '_service'):
            mcp_service_module._service = None
        if hasattr(mcp_service_module, '_mcp_registry'):
            mcp_service_module._mcp_registry = None
    except ImportError:
        pass
    
    # Reset MCP integration global instance
    try:
        from agent_system.mcp import integration as mcp_integration_module
        mcp_integration_module.mcp_integration = None
    except ImportError:
        pass
    
    # Reset config service singleton
    try:
        from agent_system.services import config_service as config_service_module
        if hasattr(config_service_module, '_config_service'):
            config_service_module._config_service = None
        if hasattr(config_service_module, '_cached_config'):
            config_service_module._cached_config = None
    except ImportError:
        pass
    
    # Reset cancellation manager
    try:
        from agent_system.core import cancellation as cancellation_module
        cancellation_module._cancellation_manager = None
    except ImportError:
        pass
    
    # Reset background job manager
    try:
        from agent_system.services import background_job_manager as bjm_module
        bjm_module._background_job_manager = None
    except ImportError:
        pass
    
    # Reset profiling utils
    try:
        from agent_system.utils import profiling as profiling_module
        profiling_module._request_profiler = None
        profiling_module._task_monitor = None
        profiling_module._loop_monitor = None
    except ImportError:
        pass
    
    # Reset memory profiling
    try:
        from agent_system.utils import memory_profiling as mem_module
        mem_module._leak_detector = None
        mem_module._reference_tracker = None
        mem_module._snapshot_task = None
        # Don't reset _profiling_executor as it may have active threads
    except ImportError:
        pass
    
    # Reset LLM batch components
    try:
        from agent_system.llm.batch import initialization as batch_init_module
        batch_init_module._batch_queue_manager = None
    except ImportError:
        pass
    
    try:
        from agent_system.llm.batch import job_tracker as job_tracker_module
        job_tracker_module._job_tracker = None
    except ImportError:
        pass
    
    try:
        from agent_system.llm import factory as llm_factory_module
        llm_factory_module._batch_queue_manager = None
        llm_factory_module._batch_manager_config = None
    except ImportError:
        pass
    
    # Reset config settings cache
    try:
        from agent_system.config import settings as settings_module
        settings_module._plugins_cache = None
        # Clear inheritance cache
        if hasattr(settings_module, '_inheritance_cache'):
            settings_module._inheritance_cache.clear()
    except ImportError:
        pass
    
    # Reset LLM capabilities registry
    try:
        from agent_system.llm import capabilities as capabilities_module
        if hasattr(capabilities_module, '_capabilities_registry'):
            capabilities_module._capabilities_registry.clear()
    except ImportError:
        pass
    
    # Reset plugin discovery shared modules
    try:
        from agent_system.plugins import discovery as discovery_module
        if hasattr(discovery_module, '_registered_shared_modules'):
            discovery_module._registered_shared_modules.clear()
    except ImportError:
        pass
    
    # Reset tool service locks
    try:
        from agent_system.services import tool_service as tool_service_module
        if hasattr(tool_service_module, '_config_file_locks'):
            tool_service_module._config_file_locks.clear()
    except ImportError:
        pass
    
    # Reset vector store backend cache
    try:
        from agent_system.utils import vector_store as vector_store_module
        vector_store_module._VECTOR_BACKEND = None
        vector_store_module._ONNX_PROVIDERS = None
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset all global state before and after each test."""
    # Reset BEFORE test runs
    _reset_all_global_state()
    yield
    # Reset AFTER test runs  
    _reset_all_global_state()


# End of conftest.py fixtures
