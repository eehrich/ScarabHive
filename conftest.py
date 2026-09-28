import os
import math
import shutil
import socket
import pytest
import subprocess
import sys
import time
import atexit
from pathlib import Path
import tempfile

# Ownership marker for the process cleanup further down: "<pid>:<start>:<pid
# namespace>" of this pytest process. A child a test starts with the inherited
# environment carries it -- and nothing else on the machine does: not the
# developer's API server, not another agent's scripts, not another pytest
# session's helpers (they carry their own session's marker). A child started
# with an environment of its own does NOT carry it and is never reaped: an
# allowlisted environment (coding_cli's child_env, an MCP stdio server's
# default environment) leaves it out unless the test passes it on.
# The start time tells this session apart from a later process that got the
# same pid; the namespace keeps a pid of another Linux pid namespace from being
# looked up here. Set unconditionally and before anything can spawn: a pytest
# started BY a test is a session of its own and must not claim its parent
# session's helpers.
_SESSION_MARKER = "AGENT_SYSTEM_TEST_SESSION"
_SESSION_PID = os.getpid()


def _pid_namespace() -> str:
    """The Linux pid namespace this process lives in; empty elsewhere."""
    try:
        return str(os.stat("/proc/self/ns/pid").st_ino)
    except OSError:
        return ""


def _session_token() -> str:
    try:
        import psutil
        started = repr(psutil.Process(_SESSION_PID).create_time())
    except Exception:
        # No start time: another session cannot tell whether this one still
        # runs, so it never takes this session's children for orphans.
        started = "unknown"
    return f"{_SESSION_PID}:{started}:{_PID_NAMESPACE}"


_PID_NAMESPACE = _pid_namespace()
_SESSION_TOKEN = _session_token()
os.environ[_SESSION_MARKER] = _SESSION_TOKEN

# Off switch for the process cleanup: with it set to 1 no sweep selects and
# nothing is signalled, in this process and in everything it starts. A test
# that starts a pytest of its own has to set it for that run: the inner
# session's sweeps would send real signals outside whatever guards the outer
# run (tests/other/test_conftest_process_cleanup.py, run_nested_pytest). This
# conftest cannot tell such a run apart itself -- an xdist worker, too, carries
# the marker of a live owner and must reap.
_NO_REAP_SWITCH = "AGENT_SYSTEM_TEST_NO_REAP"
_REAPING_OFF = os.environ.get(_NO_REAP_SWITCH) == "1"

# A developer's exported AGENT_CONFIG_PATH would send build_app() and
# load_settings() without a path to another config; the tests assume the
# repo's. Gone for the whole session, children and session fixtures included;
# a test that needs it sets it itself.
os.environ.pop("AGENT_CONFIG_PATH", None)

# Temporary directories this conftest creates, with the pid that created them.
# Removed when that process exits -- after the leftover sweep, atexit runs in
# reverse order -- and only by it: a fork leaving through sys.exit removes its
# own, never the session's.
_TEMP_DIRS: list[tuple[int, Path]] = []


def _make_temp_dir(prefix: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _TEMP_DIRS.append((os.getpid(), path))
    return path


def _remove_temp_dirs() -> None:
    me = os.getpid()
    for creator, path in _TEMP_DIRS:
        if creator == me:
            shutil.rmtree(path, ignore_errors=True)


atexit.register(_remove_temp_dirs)

# Disable WAL mode for writer plugins in tests (avoids Windows file locking issues)
os.environ["WRITER_DISABLE_WAL"] = "true"

# Set test session storage path BEFORE any imports of agent_system
# This ensures all tests use a temporary directory for sessions
_TEST_SESSION_DIR = _make_temp_dir("agent_test_sessions_")
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
            test_path = _make_temp_dir("agent_test_session_")
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
    if session.config.pluginmanager.hasplugin("dsession"):
        # pytest-xdist controller: its workers carry this session's marker and
        # are still up here -- xdist shuts them down in its own, later
        # sessionfinish. Each worker cleans up after its own tests; the atexit
        # sweep runs once the workers are gone.
        return
    print("\n[conftest] pytest_sessionfinish: Final cleanup check...")
    # Wait a bit longer for any processes to settle
    time.sleep(2.0)
    leftovers = _find_session_leftovers()
    if leftovers:
        print(f"[conftest] Final cleanup: found remaining processes {_pids(leftovers)}")
        _kill_marked(leftovers)
        remaining = _find_session_leftovers()
        if remaining:
            print(f"[conftest] WARNING: Some processes still running after final cleanup: {_pids(remaining)}")
    else:
        print("[conftest] Final cleanup: no remaining processes found")


def _final_emergency_cleanup():
    """Emergency cleanup function registered with atexit."""
    if os.getpid() != _SESSION_PID:
        # A forked child leaving through sys.exit runs the atexit handlers it
        # inherited; it must not take the whole session's helpers with it.
        return
    leftovers = _find_session_leftovers()
    if leftovers:
        print(f"[conftest] Emergency cleanup: killing {_pids(leftovers)}")
        _kill_marked(leftovers)


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


def _own_resource_tracker_pid() -> int | None:
    """The multiprocessing resource tracker this interpreter started, if any.

    It carries the marker like any child, but it is no leftover: it lives as
    long as this process and exits with it. Killed at the end of the session,
    Python relaunched it at shutdown ("resource_tracker: process died
    unexpectedly, relaunching. Some resources might leak.") and the atexit
    sweep then killed the new one.
    """
    tracker = sys.modules.get("multiprocessing.resource_tracker")
    return getattr(getattr(tracker, "_resource_tracker", None), "_pid", None)


def _this_process():
    import psutil
    return psutil.Process(os.getpid())


def _spared_pids() -> set:
    """Never candidates, whatever they carry: this process, every process it
    descends from, and its resource tracker. An ancestor can carry a marker: a
    pytest a test started carries its session's, and once that session has
    died, the orphan sweep of a pytest started under it would end its own
    parent."""
    me = _this_process()
    return {me.pid, *(parent.pid for parent in me.parents()), _own_resource_tracker_pid()}


def _runs_as_this_user(proc) -> bool:
    """Real, effective and saved uid all this user's. A setuid program (login,
    su, sudo) runs with this user's real uid and is never a test child; root
    alone reads its environment, and a selection that once took unreadable
    environments for marked killed two Terminal sessions' login processes.
    Windows has no uids: the account."""
    if _is_windows():
        return proc.username() == _this_process().username()
    ids = proc.uids()
    me = os.getuid()
    return ids.real == me and ids.effective == me and ids.saved == me


# Two readings of one process's start time may differ by this much and still
# name the same process: Linux derives start times from the boot time, which it
# re-derives from the wall clock in whole seconds.
_SAME_START_S = 2.0


def _owner_gone(marker: str) -> bool:
    """Whether the session a marker names has provably ended.

    Gone: no process with its pid, a zombie, or a process that started at
    another time (the pid was reused). Whatever this cannot decide counts as
    alive, and that session's processes are spared: a marker that does not
    parse, one from another pid namespace (a container-local pid names some
    other process here, or none), an owner whose start time is unreadable.
    Not covered: on Linux a wall-clock step of more than _SAME_START_S between
    the owner's start and this check makes a live owner look gone.
    """
    import psutil
    try:
        pid_text, started_text, namespace = marker.split(":")
        pid, started = int(pid_text), float(started_text)
    except ValueError:
        return False
    if pid <= 0 or not math.isfinite(started) or namespace != _PID_NAMESPACE:
        return False
    try:
        owner = psutil.Process(pid)
        if owner.status() == psutil.STATUS_ZOMBIE:
            return True
        return abs(owner.create_time() - started) > _SAME_START_S
    except psutil.NoSuchProcess:  # ZombieProcess included
        return True
    except psutil.Error:
        return False


def _marked_processes() -> list:
    """(process, marker) of every process of this user carrying a session marker.

    Reads the environment each process was started with, through psutil:
    Linux /proc/<pid>/environ, macOS sysctl KERN_PROCARGS2 (which keeps argv
    and the environment apart -- `ps -E` joins them, so a marker that merely
    appears in a command line would count), Windows the process's PEB. A
    process of another user or with a setuid identity is skipped, and so is
    one whose environment cannot be read. If environments cannot be read at
    all, nothing is returned: killing nothing is safe, guessing from command
    lines is not.
    """
    if _REAPING_OFF:
        print(f"[conftest] process cleanup is off ({_NO_REAP_SWITCH}=1): nothing selected")
        return []
    try:
        import psutil
    except ImportError:
        print("[conftest] psutil is not installed: cannot read process environments, "
              "no process cleanup")
        return []

    try:
        spared = _spared_pids()
    except psutil.Error:
        print("[conftest] cannot read the processes this one descends from: no process cleanup")
        return []
    marked = []
    for proc in psutil.process_iter():
        if proc.pid in spared:
            continue
        try:
            if not _runs_as_this_user(proc):
                continue
            marker = proc.environ().get(_SESSION_MARKER)
        except Exception:
            # AccessDenied, NoSuchProcess, ZombieProcess, ...
            continue
        if marker:
            marked.append((proc, marker))
    return marked


def _find_session_leftovers() -> list:
    """Processes THIS session spawned -- directly or further down -- that still run."""
    return [(proc, marker) for proc, marker in _marked_processes() if marker == _SESSION_TOKEN]


def _find_orphans() -> list:
    """Processes an earlier test session spawned and left behind when it died.

    Their owning pytest is gone. A process whose owner still runs belongs to a
    live session -- another agent's run, a pytest a test started -- and is left
    alone; that session cleans up after itself.
    """
    return [(proc, marker) for proc, marker in _marked_processes()
            if marker != _SESSION_TOKEN and _owner_gone(marker)]


def _pids(targets: list) -> list:
    return [proc.pid for proc, _marker in targets]


# How long a terminated process gets to exit before it is killed.
_TERM_GRACE_S = 3.0


def _may_signal(proc, marker: str) -> bool:
    """Checked again right before every signal, whatever the caller selected:
    the marker is this session's or names one that has ended, the process is
    none this one must spare, runs as this user and still carries exactly
    that marker. Anything unreadable: no."""
    try:
        if marker != _SESSION_TOKEN and not _owner_gone(marker):
            return False
        if proc.pid in _spared_pids() or not _runs_as_this_user(proc):
            return False
        return proc.environ().get(_SESSION_MARKER) == marker
    except Exception:
        return False


def _kill_marked(targets: list) -> None:
    """End the (process, marker) pairs a sweep selected: terminate, wait, kill the rest.

    Trusts no caller: _may_signal decides right before each signal. Only
    psutil.Process objects are signalled, never a bare pid -- psutil compares
    the start time it saw at selection and refuses a pid reused since. Nothing
    is killed by process tree or command line: a descendant ends here only if
    it carries the marker itself.
    """
    if not targets:
        return
    if _REAPING_OFF:
        print(f"[conftest] process cleanup is off ({_NO_REAP_SWITCH}=1): {_pids(targets)} not signalled")
        return
    import psutil

    print(f"[conftest] Attempting to kill PIDs: {_pids(targets)}")
    terminated, refused = [], []
    for proc, marker in targets:
        if not _may_signal(proc, marker):
            refused.append(proc.pid)
            continue
        try:
            proc.terminate()
        except psutil.Error:
            continue
        terminated.append((proc, marker))
    if refused:
        print(f"[conftest] Not signalled, no longer verifiable as a leftover of a test run: {refused}")
    if not terminated:
        return
    _gone, alive = psutil.wait_procs([proc for proc, _marker in terminated], timeout=_TERM_GRACE_S)
    stubborn = [(proc, marker) for proc, marker in terminated if proc in alive]
    for proc, marker in stubborn:
        if not _may_signal(proc, marker):
            continue
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs([proc for proc, _marker in stubborn], timeout=_TERM_GRACE_S)


@pytest.fixture(scope="session", autouse=True)
def ensure_test_servers_terminated():
    """Terminate processes that test runs spawned and left behind.

    Ownership is the AGENT_SYSTEM_TEST_SESSION marker in the environment a
    process was started with -- never a command line. Before the session: only
    orphans of an earlier run that died (their owning pytest is gone). After
    it: whatever THIS session spawned and is still running. A process without
    the marker (the developer's API server, another agent's scripts), one of
    another live session and one of another user or a setuid identity are
    never touched.
    """
    # Pre-test cleanup (best-effort)
    orphans = _find_orphans()
    if orphans:
        print("[conftest] terminating orphans of an earlier test run:", _pids(orphans))
        _kill_marked(orphans)

    yield

    # Post-test cleanup (more aggressive)
    print("[conftest] Starting post-test cleanup...")
    for attempt in range(3):  # Multiple cleanup attempts
        leftovers = _find_session_leftovers()
        if not leftovers:
            break
        print(f"[conftest] Cleanup attempt {attempt + 1}: terminating leftover processes of this test run:",
              _pids(leftovers))
        _kill_marked(leftovers)
        time.sleep(1.0)

    # Final check
    final = _find_session_leftovers()
    if final:
        print(f"[conftest] WARNING: Some processes may still be running after cleanup: {_pids(final)}")
    else:
        print("[conftest] All processes of this test run terminated")


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
def mock_server_config():
    """Create a mock ToolServerConfig object for plugin tests with agent_config."""
    from agent_system.config.models import ToolServerConfig, AgentConfig
    agent_config = AgentConfig()
    return ToolServerConfig(type="test", enabled=True, agent_config=agent_config)


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
    # tools/integration.py aliases this registry at IMPORT time
    # (`from ...tool_adapter import plugin_tool_registry`) and ToolServerIntegration
    # stores that alias as self.plugin_registry. REBINDING the attribute here
    # (the old `plugin_tool_registry = PluginToolRegistry()`) therefore does NOT
    # reach the stale alias — it keeps pointing at the fully-discovered
    # registry a prior app-building test populated. A later bare-agent test
    # then discovers every plugin and renders an extra tool-system prompt
    # (regression seen in tests/integration test_message_list_construction and
    # broad writer-test pollution). Clear the registry contents IN PLACE so
    # every alias observes it empty, and unify both module references on one
    # instance.
    try:
        from agent_system.plugins import tool_adapter
        reg = tool_adapter.plugin_tool_registry
        reg.plugin_servers.clear()
        reg.plugin_factories.clear()
        try:
            from agent_system.tools import integration as _tools_int_mod
            stale = getattr(_tools_int_mod, "plugin_tool_registry", None)
            if stale is not None and stale is not reg:
                stale.plugin_servers.clear()
                stale.plugin_factories.clear()
                _tools_int_mod.plugin_tool_registry = reg
        except ImportError:
            pass
    except (ImportError, AttributeError):
        pass
    
    # The web registry likewise: tool_adapter registers into its import-time
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
        app_module._tool_integration = None
        app_module._config_service = None
        app_module._tool_server_service = None
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
    
    # Reset tool integration global instance
    try:
        from agent_system.tools import integration as tool_integration_module
        tool_integration_module.tool_integration = None
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
        profiling_module._loop_monitor = None
    except ImportError:
        pass
    
    # Reset memory profiling
    try:
        from agent_system.utils import memory_profiling as mem_module
        mem_module._leak_detector = None
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
    except ImportError:
        pass
    
    # Reset LLM capabilities registry
    try:
        from agent_system.llm import capabilities as capabilities_module
        if hasattr(capabilities_module, '_capabilities_registry'):
            capabilities_module._capabilities_registry.clear()
    except ImportError:
        pass
    
    # Reset the backend each agent type was last served by
    try:
        from agent_system.llm import backend_affinity
        backend_affinity.clear()
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
