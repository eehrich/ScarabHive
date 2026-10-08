"""The root conftest kills only processes that test runs spawned.

It used to pick every python process whose command line mentioned the repo,
`.venv/` or `-m agent_system.app` and killed them at the start and at the end
of every run: the developer's API server, other agents' scripts, other pytest
sessions' helpers. Ownership is now the AGENT_SYSTEM_TEST_SESSION marker,
"<pid>:<start time>:<pid namespace>" of the pytest process, in the environment
a child was started with. These tests spawn real processes and run the real
selection over the machine's real process table.

The kill function is called for real only on helpers these tests spawned;
everywhere else a recorder stands in for it, and a pytest these tests start
runs with the cleanup switched off. A mutated copy of this conftest
whose session hooks could really signal once killed two Terminal sessions'
login processes (real uid the user's, effective uid root).
"""
import inspect
import os
import subprocess
import sys
import time
from multiprocessing import resource_tracker
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKER = "AGENT_SYSTEM_TEST_SESSION"
# Printing first lets the spawner wait until the child really runs python --
# only then is the environment it was started with readable.
SLEEPER = "import time; print('up', flush=True); time.sleep(60)"
# A helper with a child of its own, the same sleeper; it reports the child's pid.
PARENT = ("import subprocess, sys; "
          "child = subprocess.Popen([sys.executable, '-c', sys.argv[1]], stdout=subprocess.PIPE, text=True); "
          "assert child.stdout.readline().strip() == 'up'; "
          "print('up', child.pid, flush=True); child.wait()")
INHERIT = object()
POSIX_ONLY = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX only: uids and zombies")


@pytest.fixture
def conftest_module(pytestconfig):
    """The root conftest module pytest loaded. Not a fresh import: that would
    run its module code (marker, patches, atexit handlers) a second time."""
    path = REPO_ROOT / "conftest.py"
    for plugin in pytestconfig.pluginmanager.get_plugins():
        file = getattr(plugin, "__file__", None)
        if file and Path(file).resolve() == path:
            return plugin
    pytest.fail(f"the root conftest {path} is not loaded")


@pytest.fixture
def root_conftest(conftest_module):
    """The root conftest, for tests that watch it select and kill. With the
    cleanup switched off every sweep selects nothing and the kill signals
    nothing: these tests would fail on empty selections, or pass without
    having looked."""
    if conftest_module._REAPING_OFF:
        pytest.skip(f"the process cleanup is switched off in this run "
                    f"({conftest_module._NO_REAP_SWITCH}=1 in its environment)")
    return conftest_module


def token_of(root_conftest, pid):
    """The marker a pytest session with this pid would set."""
    return f"{pid}:{psutil.Process(pid).create_time()!r}:{root_conftest._PID_NAMESPACE}"


@pytest.fixture
def spawn():
    """Starts sleeper helpers -- this session's marker (INHERIT, from
    os.environ, as in production), none (None) or the given one -- and ends
    them, and nothing else, afterwards."""
    spawned = []

    def start(*args, marker=INHERIT, code=SLEEPER):
        env = None
        if marker is not INHERIT:
            env = {name: value for name, value in os.environ.items() if name != MARKER}
            if marker is not None:
                env[MARKER] = marker
        process = subprocess.Popen([sys.executable, "-c", code, *args], env=env,
                                   stdout=subprocess.PIPE, text=True)
        spawned.append(process)
        line = process.stdout.readline().split()
        assert line[:1] == ["up"], "helper did not start"
        process.reported = line[1:]
        return process

    yield start
    for process in spawned:
        process.kill()
        process.wait(timeout=30)
        process.stdout.close()


def ended_token(root_conftest):
    """The marker of a session whose pytest has ended."""
    process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    token = token_of(root_conftest, process.pid)
    process.stdin.close()
    process.wait(timeout=30)
    assert not psutil.pid_exists(process.pid), "the ended owner's pid was reused"
    return token


@pytest.fixture
def helpers(root_conftest, spawn):
    """own: inherits this session's marker, as every test child does.
    unmarked: no marker, but a command line the old matcher took for a test
    server (python, the repo path, `.venv/`, `-m agent_system.app`) -- stands
    in for the developer's API server and other agents' scripts.
    other_owner: an unmarked, live process standing in for another pytest
    session; other_session: its child, marked with its marker.
    orphan: marked with the marker of a session that has ended."""
    orphan_marker = ended_token(root_conftest)
    own = spawn()
    unmarked = spawn("-m", "agent_system.app", str(REPO_ROOT / ".venv" / "x"), marker=None)
    other_owner = spawn(marker=None)
    other_session = spawn(marker=token_of(root_conftest, other_owner.pid))
    orphan = spawn(marker=orphan_marker)

    # The fixture has to be what it claims, or the selection below proves nothing.
    assert os.environ[MARKER] == root_conftest._SESSION_TOKEN
    assert psutil.Process(own.pid).environ()[MARKER] == root_conftest._SESSION_TOKEN
    command_line = " ".join(psutil.Process(unmarked.pid).cmdline())
    assert MARKER not in psutil.Process(unmarked.pid).environ()
    assert str(REPO_ROOT) in command_line and ".venv/" in command_line.replace("\\", "/")
    assert "-m agent_system.app" in command_line

    return {"own": own, "unmarked": unmarked, "other_owner": other_owner,
            "other_session": other_session, "orphan": orphan}


def _names(targets, processes):
    """The helper names of (process, marker) pairs a sweep selected; other processes are left out."""
    by_pid = {process.pid: name for name, process in processes.items()}
    return sorted(by_pid[proc.pid] for proc, _marker in targets if proc.pid in by_pid)


def require_alive(*processes):
    """Every session's start sweep reaps the processes of a session that has
    ended -- the helpers standing in for such orphans too, when another pytest
    session with this conftest starts meanwhile, and rightly so. Whatever the
    selection saw, it was then not the process table the test built."""
    if any(process.poll() is not None for process in processes):
        pytest.skip("an orphan helper ended during the test: another pytest session's start sweep "
                    "reaped it, as it should -- the selection ran on a process table this test did not build")


def still_running(process, seconds=0.5):
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return True
    return False


def recorder(root_conftest, monkeypatch):
    killed = []
    monkeypatch.setattr(root_conftest, "_kill_marked", killed.extend)
    monkeypatch.setattr(root_conftest, "time", SimpleNamespace(sleep=lambda _seconds: None))
    return killed


def pretend_setuid(monkeypatch, pid):
    """The process runs with this user's real uid and root's effective and saved one, as login does. A run as root
    pretends another user's (nobody's): root's own would change nothing, and the helper is this user's for real."""
    original = psutil.Process.uids
    other = 0 if os.getuid() else 65534

    def uids(self):
        ids = original(self)
        return ids._replace(effective=other, saved=other) if self.pid == pid else ids

    monkeypatch.setattr(psutil.Process, "uids", uids)


def pretend_unreadable(monkeypatch, pid):
    original = psutil.Process.environ

    def environ(self):
        if self.pid == pid:
            raise psutil.AccessDenied(pid)
        return original(self)

    monkeypatch.setattr(psutil.Process, "environ", environ)


def pretend_marked(monkeypatch, pid, token):
    original = psutil.Process.environ

    def environ(self):
        env = original(self)
        return {**env, MARKER: token} if self.pid == pid else env

    monkeypatch.setattr(psutil.Process, "environ", environ)


# --- selection ------------------------------------------------------------

def test_the_end_of_a_session_selects_only_its_own_children(root_conftest, helpers):
    assert root_conftest._SESSION_PID == os.getpid()
    assert _names(root_conftest._find_session_leftovers(), helpers) == ["own"]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX only: no resource tracker on Windows")
def test_the_end_of_a_session_spares_its_own_resource_tracker(root_conftest, monkeypatch):
    """The tracker lives as long as this process. Killed at the end of the
    session, Python relaunched it at shutdown with a "resources might leak"
    warning, and the atexit sweep killed the relaunched one. Its marker is
    pretended: Python starts one tracker per process, lazily, with the
    environment of that moment, and an earlier test may have had the marker
    out of it then."""
    resource_tracker.ensure_running()
    tracker_pid = resource_tracker._resource_tracker._pid
    pretend_marked(monkeypatch, tracker_pid, root_conftest._SESSION_TOKEN)
    with monkeypatch.context() as unspared:
        unspared.setattr(root_conftest, "_own_resource_tracker_pid", lambda: None)
        assert tracker_pid in root_conftest._pids(root_conftest._find_session_leftovers()), \
            "the tracker is no candidate at all -- nothing to spare"

    assert tracker_pid not in root_conftest._pids(root_conftest._find_session_leftovers())


def test_the_start_of_a_session_selects_only_orphans_of_a_dead_session(root_conftest, helpers):
    selected = _names(root_conftest._find_orphans(), helpers)
    require_alive(helpers["orphan"])
    assert selected == ["orphan"]


@POSIX_ONLY
def test_an_orphan_whose_owner_is_a_zombie_is_selected(root_conftest, spawn):
    """A zombie is an ended process whose parent has not collected it yet: its
    pid exists and its start time still reads the same."""
    owner = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        orphan = spawn(marker=token_of(root_conftest, owner.pid))
        owner.stdin.close()
        for _ in range(200):
            if psutil.Process(owner.pid).status() == psutil.STATUS_ZOMBIE:
                break
            time.sleep(0.05)
        assert psutil.Process(owner.pid).status() == psutil.STATUS_ZOMBIE, "the owner did not become a zombie"

        selected = [proc.pid for proc, _marker in root_conftest._find_orphans()]
        require_alive(orphan)
        assert orphan.pid in selected
    finally:
        owner.stdin.close()
        owner.wait(timeout=30)


def test_an_orphan_whose_owner_pid_now_names_another_process_is_selected(root_conftest, spawn):
    """The pid lives on in a process that started at another time."""
    stand_in = spawn(marker=None)
    started = psutil.Process(stand_in.pid).create_time()
    orphan = spawn(marker=f"{stand_in.pid}:{started - 100!r}:{root_conftest._PID_NAMESPACE}")
    live = spawn(marker=f"{stand_in.pid}:{started!r}:{root_conftest._PID_NAMESPACE}")

    selected = [proc.pid for proc, _marker in root_conftest._find_orphans()]
    require_alive(orphan)
    assert orphan.pid in selected and live.pid not in selected


@pytest.mark.parametrize("marker", ["garbage", "pid only", "no start time", "infinite start time",
                                    "negative pid", "another pid namespace"])
def test_a_marker_the_sweep_cannot_decide_spares_its_process(root_conftest, spawn, marker):
    """Each names a session whose pytest has ended -- or that cannot be told."""
    ended = ended_token(root_conftest)
    pid, started, namespace = ended.split(":")
    undecidable = {"garbage": "garbage",
                   "pid only": pid,  # the format of the first version of the marker
                   "no start time": f"{pid}:unknown:{namespace}",
                   "infinite start time": f"{pid}:inf:{namespace}",
                   "negative pid": f"-1:{started}:{namespace}",
                   "another pid namespace": f"{pid}:{started}:{namespace}0"}[marker]
    orphan = spawn(marker=ended)
    spared = spawn(marker=undecidable)

    selected = [proc.pid for proc, _marker in root_conftest._find_orphans()]
    require_alive(orphan)
    assert orphan.pid in selected, "the sweep does not reach the ended session's processes"
    assert spared.pid not in selected


def test_the_sweep_spares_every_process_this_one_descends_from(root_conftest, spawn, monkeypatch):
    """A pytest that a test of a since dead session started carries that
    session's marker. Pretending this process is the child of such a parent:
    both carry the dead session's marker."""
    parent = spawn(SLEEPER, marker=ended_token(root_conftest), code=PARENT)
    child = psutil.Process(int(parent.reported[0]))
    try:
        assert child.ppid() == parent.pid
        before = [proc.pid for proc, _marker in root_conftest._find_orphans()]
        require_alive(parent)
        assert {parent.pid, child.pid} <= set(before), "the fixture does not reach the sparing"

        monkeypatch.setattr(root_conftest, "_this_process", lambda: child)
        selected = [proc.pid for proc, _marker in root_conftest._find_orphans()]
        require_alive(parent)
        assert parent.pid not in selected and child.pid not in selected
    finally:
        # The spawn fixture ends the parent only; psutil refuses the pid if it was reused meanwhile.
        try:
            child.kill()
            child.wait(timeout=30)
        except psutil.NoSuchProcess:
            pass


@POSIX_ONLY
@pytest.mark.parametrize("identity", ["setuid", "unreadable environment"])
def test_a_process_this_session_cannot_vouch_for_is_never_selected(root_conftest, spawn, monkeypatch, identity):
    """The incident: login (real uid the user's, effective uid root) was
    unreadable, and a mutated selection took unreadable for this session's."""
    helper = spawn()
    assert helper.pid in root_conftest._pids(root_conftest._find_session_leftovers())
    if identity == "setuid":
        pretend_setuid(monkeypatch, helper.pid)
    else:
        pretend_unreadable(monkeypatch, helper.pid)

    assert helper.pid not in root_conftest._pids(root_conftest._find_session_leftovers())


def test_unreadable_environments_select_nothing(root_conftest, helpers, monkeypatch):
    """No fallback to command lines: without psutil nothing is a candidate --
    not even the unmarked helper the old matcher would have killed."""
    monkeypatch.setitem(sys.modules, "psutil", None)  # `import psutil` now raises ImportError

    assert root_conftest._find_session_leftovers() == []
    assert root_conftest._find_orphans() == []


# --- the hooks that sweep -------------------------------------------------

def test_the_session_fixture_sweeps_orphans_before_and_its_own_children_after(
        root_conftest, helpers, monkeypatch):
    killed = recorder(root_conftest, monkeypatch)
    sweep = inspect.unwrap(root_conftest.ensure_test_servers_terminated)()

    next(sweep)  # before the session
    require_alive(helpers["orphan"])
    assert _names(killed, helpers) == ["orphan"]

    killed.clear()
    with pytest.raises(StopIteration):
        next(sweep)  # after it; the recorder kills nothing, so every attempt finds "own" again
    assert set(_names(killed, helpers)) == {"own"}


@pytest.mark.parametrize("hook", ["atexit", "sessionfinish"])
def test_the_end_hooks_kill_only_this_sessions_children(root_conftest, helpers, request,
                                                        monkeypatch, hook):
    killed = recorder(root_conftest, monkeypatch)

    if hook == "atexit":
        root_conftest._final_emergency_cleanup()
    else:
        root_conftest.pytest_sessionfinish(request.session, 0)

    assert _names(killed, helpers) == ["own"]


def test_the_xdist_controller_leaves_the_end_to_its_workers(root_conftest, helpers, monkeypatch):
    """Its workers carry the controller's marker and still run at its
    sessionfinish; xdist ends them in its own, later one."""
    killed = recorder(root_conftest, monkeypatch)
    controller = SimpleNamespace(config=SimpleNamespace(pluginmanager=SimpleNamespace(
        hasplugin=lambda name: name == "dsession")))
    assert _names(root_conftest._find_session_leftovers(), helpers) == ["own"], \
        "the fixture does not reach the guard"

    root_conftest.pytest_sessionfinish(controller, 0)

    assert killed == []


def test_a_forked_child_leaving_through_atexit_kills_nothing(root_conftest, helpers, monkeypatch):
    """A fork of the session process inherits the atexit handler. Pretending
    this process is such a fork of `other_owner`: its child carries the
    session's marker, so without the guard the handler would pick it."""
    killed = recorder(root_conftest, monkeypatch)
    monkeypatch.setattr(root_conftest, "_SESSION_PID", helpers["other_owner"].pid)
    monkeypatch.setattr(root_conftest, "_SESSION_TOKEN", token_of(root_conftest, helpers["other_owner"].pid))
    assert _names(root_conftest._find_session_leftovers(), helpers) == ["other_session"], \
        "the fixture does not reach the guard"

    root_conftest._final_emergency_cleanup()

    assert killed == []


# --- the kill itself: real signals, to this test's own helpers only ---------

def test_the_kill_ends_a_process_that_carries_the_marker(root_conftest, spawn):
    helper = spawn()

    root_conftest._kill_marked([(psutil.Process(helper.pid), root_conftest._SESSION_TOKEN)])

    assert not still_running(helper, seconds=10)


def test_the_kill_ends_an_orphan_of_a_dead_session(root_conftest, spawn):
    marker = ended_token(root_conftest)
    helper = spawn(marker=marker)

    root_conftest._kill_marked([(psutil.Process(helper.pid), marker)])

    assert not still_running(helper, seconds=10)


def test_the_kill_refuses_a_process_without_the_marker_whatever_the_caller_passed(root_conftest, spawn):
    """A buggy caller: the process carries no marker at all."""
    helper = spawn(marker=None)

    root_conftest._kill_marked([(psutil.Process(helper.pid), root_conftest._SESSION_TOKEN)])

    assert still_running(helper)


def test_the_kill_refuses_a_process_of_another_live_session(root_conftest, spawn):
    """A buggy caller: the right marker, but its session still runs."""
    owner = spawn(marker=None)
    marker = token_of(root_conftest, owner.pid)
    helper = spawn(marker=marker)

    root_conftest._kill_marked([(psutil.Process(helper.pid), marker)])

    assert still_running(helper)


@POSIX_ONLY
@pytest.mark.parametrize("identity", ["setuid", "unreadable environment"])
def test_the_kill_refuses_a_process_this_session_cannot_vouch_for(root_conftest, spawn, monkeypatch, identity):
    helper = spawn()
    if identity == "setuid":
        pretend_setuid(monkeypatch, helper.pid)
    else:
        pretend_unreadable(monkeypatch, helper.pid)

    root_conftest._kill_marked([(psutil.Process(helper.pid), root_conftest._SESSION_TOKEN)])

    assert still_running(helper)


def test_the_kill_refuses_a_process_this_one_descends_from(root_conftest, spawn, monkeypatch):
    """Pretending the helper is this process: it is spared like the real one."""
    helper = spawn()
    monkeypatch.setattr(root_conftest, "_this_process", lambda: psutil.Process(helper.pid))

    root_conftest._kill_marked([(psutil.Process(helper.pid), root_conftest._SESSION_TOKEN)])

    assert still_running(helper)


def test_the_kill_signals_nothing_while_the_cleanup_is_switched_off(conftest_module, spawn, monkeypatch):
    helper = spawn()
    monkeypatch.setattr(conftest_module, "_REAPING_OFF", True)

    conftest_module._kill_marked([(psutil.Process(helper.pid), conftest_module._SESSION_TOKEN)])

    assert still_running(helper)


# --- a pytest a test starts -------------------------------------------------

def processes_marked_by_session(pid):
    """Live processes whose start environment names the session with this pid.
    Reads environments only."""
    found = []
    for proc in psutil.process_iter():
        try:
            if proc.environ().get(MARKER, "").startswith(f"{pid}:"):
                found.append(proc.pid)
        except psutil.Error:
            continue
    return found


def run_nested_pytest(request, tmp_path, source, **env):
    """A pytest of its own on one probe file, loading this conftest the way
    pytest loads it, before any test. Every test that starts a pytest does it
    like this: the inner session's sweeps would send real signals outside
    whatever guards this run, so its cleanup is switched off, and every -p
    plugin of this run (a signal fence, a recorder) goes along. With the
    cleanup off nothing ends what the inner session leaves running -- so it
    must leave nothing."""
    probe = tmp_path / "probe_nested_session.py"
    probe.write_text(source, encoding="utf-8")
    # -p conftest: the probe lies outside the repo, so the repo's conftest is
    # loaded by name (the working directory is on sys.path under -m). -s: what
    # the session fixture prints around the test is part of what is checked.
    command = [sys.executable, "-m", "pytest", str(probe), "-c", str(REPO_ROOT / "pytest.ini"),
               "--rootdir", str(REPO_ROOT), "-p", "conftest", "-p", "no:cacheprovider", "-p", "no:warnings",
               "-q", "-s"]
    for plugin in request.config.option.plugins:
        command += ["-p", plugin]
    # Into a file, not a pipe: a leftover holding the pipe open would keep the
    # read waiting until it ends -- and the check below would find nothing.
    log = tmp_path / "nested_session.log"
    with open(log, "wb") as out:
        nested = subprocess.Popen(command, cwd=REPO_ROOT, env=dict(os.environ, AGENT_SYSTEM_TEST_NO_REAP="1", **env),
                                  stdout=out, stderr=subprocess.STDOUT)
        try:
            nested.wait(timeout=110)
        except subprocess.TimeoutExpired:
            nested.kill()
            nested.wait()
    output = log.read_text(encoding="utf-8", errors="replace")
    assert nested.returncode == 0 and "1 passed" in output, output[-6000:]
    assert "[conftest]" in output, "the nested session did not load the repo's conftest"
    leftovers = processes_marked_by_session(nested.pid)
    assert not leftovers, f"the nested session left processes running, and nothing will end them: {leftovers}"
    return output


def test_a_pytest_a_test_starts_reaps_nothing(conftest_module, spawn, request, tmp_path):
    """Its start sweep would end the planted orphan -- for real, and under a
    mutated copy of this conftest whatever the mutation selects."""
    orphan = spawn(marker=ended_token(conftest_module))

    output = run_nested_pytest(request, tmp_path, "def test_nothing():\n    pass\n")

    assert "process cleanup is off" in output
    assert "terminating orphans" not in output and "Attempting to kill" not in output, output[-6000:]
    require_alive(orphan)


def test_a_pytest_a_test_starts_gets_every_plugin_of_this_run(request, tmp_path, monkeypatch):
    """A plugin given here with -p -- a signal fence around a mutation run --
    has to guard the nested session too."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "forwarded_probe_plugin.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(request.config.option, "plugins", [*request.config.option.plugins, "forwarded_probe_plugin"])
    python_path = os.pathsep.join(filter(None, [str(plugins), os.environ.get("PYTHONPATH")]))

    run_nested_pytest(request, tmp_path, (
        "def test_probe(request):\n"
        "    assert request.config.pluginmanager.hasplugin('forwarded_probe_plugin')\n"), PYTHONPATH=python_path)


NESTED_CONFIG_PATH_PROBE = '''
import os
import sys

import psutil


def test_probe():
    if sys.platform != "win32":  # Windows rewrites the start environment along with os.environ
        assert psutil.Process().environ().get("AGENT_CONFIG_PATH") == os.environ["PROBE_VALUE"], \\
            "this pytest was not started with the variable -- the probe would prove nothing"
    assert "AGENT_CONFIG_PATH" not in os.environ
'''


def test_a_test_never_sees_an_exported_agent_config_path(request, tmp_path):
    """build_app() and load_settings() without a path follow AGENT_CONFIG_PATH;
    the tests assume the repo's config. A pytest of its own, started with the
    variable."""
    elsewhere = str(tmp_path / "elsewhere" / "config.yaml")

    run_nested_pytest(request, tmp_path, NESTED_CONFIG_PATH_PROBE, AGENT_CONFIG_PATH=elsewhere,
                      PROBE_VALUE=elsewhere)


# --- temporary directories -------------------------------------------------

def test_temporary_directories_are_removed_only_by_the_process_that_made_them(
        conftest_module, tmp_path, monkeypatch):
    assert (os.getpid(), conftest_module._TEST_SESSION_DIR) in conftest_module._TEMP_DIRS, \
        "the session's storage directory is not registered for removal"
    own, foreign = tmp_path / "own", tmp_path / "foreign"
    own.mkdir()
    foreign.mkdir()
    # The session's own list stays untouched: removing it here would take the running session's storage.
    monkeypatch.setattr(conftest_module, "_TEMP_DIRS", [(os.getpid(), own), (os.getpid() + 1, foreign)])

    conftest_module._remove_temp_dirs()

    assert not own.exists() and foreign.exists()
