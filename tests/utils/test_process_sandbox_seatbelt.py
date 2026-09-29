"""The Seatbelt backend, run for real: ``/usr/bin/sandbox-exec`` on tmp dirs.

``test_process_sandbox.py`` checks the argv bubblewrap gets; these tests check
what the kernel then does, because an SBPL profile is only as good as the
paths it actually matches -- and three things about that were surprises when
measured (``/tmp`` vs ``/private/tmp``, firmlinks, and case folding).

The test area comes from ``seatbelt_rig``, which keeps it out of the user's
temp directory (writable by design, so useless as an "outside").

The selection tests at the bottom run everywhere; the rest need macOS.
"""
from __future__ import annotations

import os
import shlex
import shutil
import uuid
import subprocess
import sys
import termios
from pathlib import Path

import pytest

from agent_system.utils import process_sandbox as ps
from agent_system.utils.process_sandbox import ProcessSandbox, SandboxUnavailable
from seatbelt_rig import Area, on_macos, probe_scratch_in, probed_seatbelt, seatbelt_area

#: A workspace name that rewrites the profile if spliced into its text: it
#: closes the rule, opens "/" for writing, and swallows the rest.
WIDENING_NAME = 'x")) (allow file-write* (subpath "/")) (allow file-write* (subpath "y'
#: One that merely breaks the syntax if spliced in.
BREAKING_NAME = 'q"u)o(te'


@pytest.fixture
def area():
    with seatbelt_area() as made:
        yield made


@pytest.fixture
def seatbelt():
    with probed_seatbelt() as backend:
        yield backend


def short(path: Path) -> str:
    """The same path in the ``/tmp`` spelling."""
    text = str(path)
    assert text.startswith("/private/tmp/")
    return text[len("/private"):]


def run(box: ProcessSandbox, *argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    confined = box.confine(list(argv))
    return subprocess.run(confined.argv, capture_output=True, text=True,
                          timeout=60, cwd=cwd)


def sh(box: ProcessSandbox, script: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return run(box, "/bin/sh", "-c", script, cwd=cwd)


def writes(box: ProcessSandbox, target: str) -> bool:
    """Whether the confined process could create ``target``, and did."""
    result = sh(box, f"echo x > '{target}'")
    created = os.path.exists(target)
    assert (result.returncode == 0) == created, result.stderr
    return created


@on_macos
class TestWorkspaceWrite:
    @pytest.fixture
    def box(self, area, seatbelt):
        return ProcessSandbox(mode="workspace-write", workspace_root=area.ws)

    def test_a_write_inside_the_workspace_succeeds(self, box, area):
        assert writes(box, str(area.ws / "inside.txt"))

    def test_a_write_outside_fails_in_both_spellings(self, box, area):
        assert not writes(box, str(area.out / "long.txt"))
        assert not writes(box, short(area.out / "short.txt"))

    def test_a_workspace_given_in_the_short_spelling_is_writable_in_both(self, area, seatbelt):
        """Seatbelt matches the kernel's path: a profile naming ``/tmp/...``
        matches nothing, and the workspace would not be writable at all."""
        box = ProcessSandbox(mode="workspace-write", workspace_root=Path(short(area.ws)))
        assert writes(box, short(area.ws / "a.txt"))
        assert writes(box, str(area.ws / "b.txt"))
        assert not writes(box, short(area.out / "c.txt"))

    def test_a_firmlinked_spelling_of_the_workspace_is_writable(self, area, seatbelt):
        """``/System/Volumes/Data/private/...`` is the same directory, and
        realpath keeps that spelling -- only the kernel knows the short one."""
        box = ProcessSandbox(mode="workspace-write",
                             workspace_root=Path("/System/Volumes/Data" + str(area.ws)))
        assert writes(box, str(area.ws / "firm.txt"))

    def test_a_child_of_the_confined_process_is_confined_too(self, box, area):
        target = area.out / "from_child.txt"
        code = f"open({str(target)!r}, 'w').write('x')"
        python = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        result = sh(box, f"sh -c {shlex.quote(python)}")
        assert "Operation not permitted" in result.stderr, result.stderr
        assert not target.exists()

    def test_writing_through_a_symlink_to_outside_fails(self, box, area):
        victim = area.out / "victim.txt"
        victim.write_text("original")
        (area.ws / "file_link").symlink_to(victim)
        (area.ws / "dir_link").symlink_to(area.out)

        assert sh(box, f"echo evil > '{area.ws}/file_link'").returncode != 0
        assert sh(box, f"echo evil > '{area.ws}/dir_link/new.txt'").returncode != 0
        assert victim.read_text() == "original"
        assert not (area.out / "new.txt").exists()

    def test_a_symlink_made_inside_the_sandbox_does_not_help(self, box, area):
        victim = area.out / "victim.txt"
        victim.write_text("original")
        result = sh(box, f"ln -s '{victim}' '{area.ws}/made' && echo evil >> '{area.ws}/made'")
        assert result.returncode != 0
        assert victim.read_text() == "original"

    def test_a_hard_link_to_an_outside_file_is_refused(self, box, area):
        """A hard link is the same file under an inside name; bubblewrap stops
        it with EXDEV at the bind mount, Seatbelt at the link itself."""
        victim = area.out / "victim.txt"
        victim.write_text("original")
        result = sh(box, f"ln '{victim}' '{area.ws}/hard' && echo evil >> '{area.ws}/hard'")
        assert result.returncode != 0
        assert victim.read_text() == "original"

    def test_files_cannot_be_moved_across_the_boundary(self, box, area):
        (area.out / "in_out.txt").write_text("o")
        (area.ws / "in_ws.txt").write_text("w")
        assert sh(box, f"mv '{area.out}/in_out.txt' '{area.ws}/'").returncode != 0
        assert sh(box, f"mv '{area.ws}/in_ws.txt' '{area.out}/'").returncode != 0
        assert (area.out / "in_out.txt").exists() and (area.ws / "in_ws.txt").exists()

    def test_the_workspace_root_itself_cannot_be_removed(self, box, area):
        """bubblewrap's root is a mount point (EBUSY); here the rule says so."""
        result = sh(box, f"rmdir '{area.ws}'")
        assert result.returncode != 0
        assert area.ws.is_dir()

    @pytest.mark.parametrize("name", [WIDENING_NAME, BREAKING_NAME])
    def test_a_crafted_workspace_name_cannot_change_the_profile(self, area, seatbelt, name):
        crafted = area.root / name
        crafted.mkdir(parents=True)
        box = ProcessSandbox(mode="workspace-write", workspace_root=crafted)
        assert not writes(box, str(area.out / "escaped.txt"))
        assert writes(box, str(crafted / "inside.txt"))

    def test_enforcement_is_reported_as_partial(self, box):
        """The user temp directory is shared with the user's other processes,
        which bubblewrap's private /tmp is not."""
        confined = box.confine(["/usr/bin/true"])
        assert (confined.backend, confined.enforcement) == ("seatbelt", "partial")

    def test_the_heredocs_of_bin_bash_work_from_inside_the_workspace(self, box, area):
        """bash 3.2 writes a here-document to /var/tmp or /tmp, else to the
        working directory -- the terminal starts inside the workspace, so this
        is the case that has to work."""
        result = run(box, "/bin/bash", "-c", "cat > note.txt <<'EOF'\nfrom heredoc\nEOF",
                     cwd=area.ws)
        assert result.returncode == 0, result.stderr
        assert (area.ws / "note.txt").read_text() == "from heredoc\n"


@on_macos
class TestReadOnly:
    @pytest.fixture
    def box(self, area, seatbelt):
        return ProcessSandbox(mode="read-only", workspace_root=area.ws)

    def test_no_write_goes_through_inside_or_outside(self, box, area):
        assert not writes(box, str(area.ws / "inside.txt"))
        assert not writes(box, str(area.out / "outside.txt"))
        assert not writes(box, short(area.out / "short.txt"))

    def test_reads_still_work_everywhere(self, box, area):
        (area.out / "readable.txt").write_text("content outside")
        result = sh(box, f"cat '{area.out}/readable.txt' && ls / > /dev/null && echo listed")
        assert result.returncode == 0, result.stderr
        assert "content outside" in result.stdout and "listed" in result.stdout

    def test_what_a_process_needs_to_run_stays_writable(self, box):
        result = sh(box, ": > /dev/null && echo to-stdout > /dev/stdout "
                         "&& echo to-fd > /dev/fd/1")
        assert result.returncode == 0, result.stderr
        assert "to-stdout" in result.stdout and "to-fd" in result.stdout

    def test_the_user_temp_directory_is_writable(self, box):
        """mktemp(1) goes there whatever TMPDIR says; Python via TMPDIR."""
        result = sh(box, 'f=$(mktemp) && echo made && rm "$f"')
        assert result.returncode == 0 and "made" in result.stdout, result.stderr
        result = run(box, sys.executable, "-c",
                     "import tempfile; tempfile.TemporaryFile().write(b'x'); print('ok')")
        assert result.returncode == 0 and "ok" in result.stdout, result.stderr


@on_macos
class TestPseudoTerminals:
    """bubblewrap gives a private devpts: only ptys the process opens itself."""

    @pytest.fixture
    def box(self, area, seatbelt):
        return ProcessSandbox(mode="read-only", workspace_root=area.ws)

    def test_a_pty_the_process_opens_is_writable(self, box):
        code = ("import os; m, s = os.openpty(); fd = os.open(os.ttyname(s), os.O_WRONLY); "
                "os.write(fd, b'hi'); print(os.read(m, 2))")
        result = run(box, sys.executable, "-c", code)
        assert result.returncode == 0, result.stderr
        assert "b'hi'" in result.stdout

    def test_the_input_of_a_pty_somebody_else_opened_cannot_be_read(self, box):
        """Keystroke theft from another terminal. bubblewrap's private devpts
        does not even show such a pty."""
        master, slave = os.openpty()
        try:
            name = os.ttyname(slave)
            os.write(master, b"secret\n")
            code = f"import os; fd = os.open({name!r}, os.O_RDONLY | os.O_NONBLOCK); print(os.read(fd, 20))"
            result = run(box, sys.executable, "-c", code)
            os.set_blocking(slave, False)
            left = os.read(slave, 20)
        finally:
            os.close(master)
            os.close(slave)
        assert "Operation not permitted" in result.stderr, result.stdout + result.stderr
        assert left == b"secret\n", "the confined process consumed the pending input"

    def test_a_pty_somebody_else_opened_cannot_be_revoked(self, box):
        """revoke() hangs up every session on that terminal."""
        master, slave = os.openpty()
        try:
            name = os.ttyname(slave)
            code = ("import ctypes, os; libc = ctypes.CDLL(None, use_errno=True); "
                    f"rc = libc.revoke({name!r}.encode()); print(rc, os.strerror(ctypes.get_errno()))")
            result = run(box, sys.executable, "-c", code)
            termios.tcgetattr(slave)  # raises if the pty was revoked
        finally:
            os.close(master)
            os.close(slave)
        assert result.stdout.startswith("-1 Operation not permitted"), result.stdout + result.stderr

    def test_an_inherited_terminal_keeps_working(self, box):
        """A terminal handed over as stdin is the caller's to give. As under
        bubblewrap, the process may use it -- ioctls included."""
        master, slave = os.openpty()
        try:
            code = ("import termios; a = termios.tcgetattr(0); a[3] &= ~termios.ECHO; "
                    "termios.tcsetattr(0, termios.TCSANOW, a); print('set')")
            confined = box.confine([sys.executable, "-c", code])
            result = subprocess.run(confined.argv, stdin=slave, capture_output=True,
                                    text=True, timeout=60)
        finally:
            os.close(master)
            os.close(slave)
        assert result.returncode == 0 and "set" in result.stdout, result.stderr

    def test_a_pty_somebody_else_opened_is_not(self, box):
        master, slave = os.openpty()
        try:
            name = os.ttyname(slave)
            result = sh(box, f"echo intruder > {name}")
            os.set_blocking(master, False)
            try:
                received = os.read(master, 100)
            except BlockingIOError:
                received = b""
        finally:
            os.close(master)
            os.close(slave)
        assert result.returncode != 0
        assert received == b""


#: Pushes a line into the controlling terminal, as if typed: via /dev/tty and
#: via an inherited stdin. Prints what each path answered.
INJECTOR = """
import fcntl, os, termios
def push(fd):
    for ch in b"INJECTED\\n":
        fcntl.ioctl(fd, termios.TIOCSTI, bytes([ch]))
answers = []
for label, opener in (("dev_tty", lambda: os.open("/dev/tty", os.O_RDWR)), ("stdin", lambda: 0)):
    try:
        push(opener())
        answers.append(label + ":pushed")
    except OSError as e:
        answers.append(label + ":" + e.strerror)
print(" ".join(answers))
"""

#: Runs argv with the pty on stdin as its controlling terminal, then reports
#: what is waiting in the terminal's input -- what a shell would read next.
SESSION_LEADER = """
import fcntl, os, subprocess, sys, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
run = subprocess.run(sys.argv[1:], capture_output=True, text=True, timeout=20)
os.set_blocking(0, False)
try:
    queued = os.read(0, 100)
except BlockingIOError:
    queued = b""
print(run.stdout.strip())
print("QUEUED", queued)
"""


def in_a_terminal_session(argv) -> str:
    """Run argv in a session of its own whose terminal is a pty we own."""
    master, slave = os.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO  # nothing queued for output: exit cannot block on it
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        done = subprocess.run([sys.executable, "-c", SESSION_LEADER, *argv], stdin=slave,
                              capture_output=True, text=True, timeout=60,
                              start_new_session=True)
        assert done.returncode == 0, done.stderr
        return done.stdout
    finally:
        os.close(master)
        os.close(slave)


@on_macos
class TestTheCallersTerminal:
    """bubblewrap's --new-session: a confined process must not type into the
    terminal it was started from -- the next reader would run it unconfined."""

    def test_the_rig_sees_an_injection_when_nothing_confines(self):
        """Counter-check: without it the test below would pass on a rig that
        cannot detect an injection at all."""
        out = in_a_terminal_session([sys.executable, "-c", INJECTOR])
        assert "dev_tty:pushed stdin:pushed" in out
        assert "QUEUED b'INJECTED\\n'" in out

    def test_a_confined_process_cannot_push_input_into_it(self, area, seatbelt):
        box = ProcessSandbox(mode="workspace-write", workspace_root=area.ws)
        out = in_a_terminal_session(box.confine([sys.executable, "-c", INJECTOR]).argv)
        assert "dev_tty:Operation not permitted stdin:Operation not permitted" in out
        assert "QUEUED b''" in out


@on_macos
class TestChannelsBeyondFileWrites:
    """macOS lets a process change files by other means than a file write, or
    have a daemon change them for it. Each is refused by a rule of its own."""

    @pytest.fixture
    def box(self, area, seatbelt):
        return ProcessSandbox(mode="read-only", workspace_root=area.ws)

    def test_a_read_only_descriptor_cannot_change_the_data_protection_class(self, box, area):
        """F_SETPROTECTIONCLASS on a file opened read-only; class 2 locks the
        user out of it (EPERM on every open)."""
        victim = area.out / "victim.txt"
        victim.write_text("data")
        code = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDONLY)\n"
                "try:\n    fcntl.fcntl(fd, 64, 2); print('changed')\n"
                "except OSError as e:\n    print('refused', e.strerror)")
        result = run(box, sys.executable, "-c", code, str(victim))
        assert "refused Operation not permitted" in result.stdout, result.stdout + result.stderr
        assert victim.read_text() == "data"

    def test_no_preference_domain_is_written_for_it(self, box):
        """cfprefsd writes on the client's behalf -- unless the profile says no."""
        domain = f"com.scarabhive.sandboxtest.{uuid.uuid4().hex[:12]}"
        plist = Path.home() / "Library" / "Preferences" / f"{domain}.plist"
        try:
            result = sh(box, f"defaults write {domain} key -string confined")
            back = subprocess.run(["defaults", "read", domain, "key"],
                                  capture_output=True, text=True)
        finally:
            subprocess.run(["defaults", "delete", domain], capture_output=True)
            plist.unlink(missing_ok=True)
        assert result.returncode != 0
        assert back.returncode != 0, "the confined process wrote a preference"

    def test_launchservices_is_out_of_reach(self, box):
        """It starts applications unconfined. Both services: with one refused
        it falls back to the other (measured with a throwaway app)."""
        code = ("import ctypes\nlibc = ctypes.CDLL(None)\n"
                "bp = ctypes.c_uint.in_dll(libc, 'bootstrap_port')\n"
                "for n in ('com.apple.coreservices.launchservicesd', "
                "'com.apple.coreservices.quarantine-resolver', 'com.apple.runningboard'):\n"
                "    port = ctypes.c_uint(0)\n"
                "    print(n, libc.bootstrap_look_up(bp, n.encode(), ctypes.byref(port)))")
        result = run(box, sys.executable, "-c", code)
        answers = dict(line.rsplit(" ", 1) for line in result.stdout.splitlines())
        assert len(answers) == 3, result.stdout + result.stderr
        assert all(kr != "0" for kr in answers.values()), answers

    def test_it_cannot_signal_a_process_outside_its_sandbox(self, box):
        """Measured escape before this rule: LaunchServices spawned an app
        suspended, and the confined process resumed it with SIGCONT -- the
        app's code ran unconfined."""
        outsider = subprocess.Popen(["/bin/sleep", "60"])
        try:
            result = sh(box, f"kill -STOP {outsider.pid} && echo stopped; "
                             f"kill -TERM {outsider.pid} && echo terminated")
            still_running = outsider.poll() is None
        finally:
            outsider.kill()
            outsider.wait()
        assert "Operation not permitted" in result.stderr, result.stdout + result.stderr
        assert still_running

    def test_it_can_still_signal_its_own_children(self, box):
        result = run(box, "/bin/bash", "-c", "sleep 30 & kill $! && wait $!; echo rc=$?")
        assert "rc=143" in result.stdout, result.stdout + result.stderr

    def test_launchd_takes_no_job_from_it(self, box):
        """launchd refuses sandboxed clients on its own; pinned, because a
        job it took would run unconfined."""
        label = f"com.scarabhive.sandboxtest.{uuid.uuid4().hex[:12]}"
        try:
            result = run(box, "/bin/launchctl", "submit", "-l", label, "--", "/usr/bin/true")
            listed = subprocess.run(["/bin/launchctl", "list", label],
                                    capture_output=True).returncode == 0
        finally:
            subprocess.run(["/bin/launchctl", "remove", label], capture_output=True)
        assert result.returncode != 0
        assert not listed


def git_available() -> bool:
    git = shutil.which("git")
    if not git:
        return False
    try:
        return subprocess.run([git, "--version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


GIT = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]


@on_macos
@pytest.mark.skipif(not git_available(), reason="needs a working git")
class TestGitMetadata:
    """Hooks and config run code for the NEXT unconfined git: a confined
    process must not write them, while commits keep working."""

    @pytest.fixture
    def repo(self, area, seatbelt):
        subprocess.run([*GIT, "init", "-q", str(area.ws)], check=True)
        subprocess.run([*GIT, "-C", str(area.ws), "commit", "-q", "--allow-empty", "-m", "init"],
                       check=True)
        return area.ws

    @pytest.fixture
    def box(self, repo):
        return ProcessSandbox(mode="workspace-write", workspace_root=repo)

    def test_commits_branches_and_checkouts_still_work(self, box, repo):
        git = " ".join(GIT)
        result = sh(box, f"echo a > f && {git} add f && {git} commit -qm c && "
                         f"{git} checkout -qb feature && {git} checkout -q - && {git} branch config",
                    cwd=repo)
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("command", [
        "echo '[core] fsmonitor = evil' >> .git/config",
        "git config core.hooksPath /elsewhere",
        "echo evil > .git/hooks/pre-commit",
        "rm -rf .git/hooks",
        "echo '[core] pager = evil' > .git/config.worktree",
    ])
    def test_hooks_and_config_are_read_only(self, box, repo, command):
        before = (repo / ".git" / "config").read_text()
        assert sh(box, command, cwd=repo).returncode != 0
        assert (repo / ".git" / "config").read_text() == before
        assert not (repo / ".git" / "hooks" / "pre-commit").exists()
        assert (repo / ".git" / "hooks").is_dir()

    @pytest.mark.parametrize("command", [
        "mv .git .git_old",
        "echo evil > c2 && mv c2 .git/config",
        "ln .git/config cfg && echo evil >> cfg",
        "ln -s .git/config lcfg && echo evil >> lcfg",
    ])
    def test_the_git_directory_cannot_be_swapped_or_reached_through_a_link(self, box, repo, command):
        before = (repo / ".git" / "config").read_text()
        assert sh(box, command, cwd=repo).returncode != 0
        assert (repo / ".git" / "config").read_text() == before

    def test_submodule_and_worktree_metadata_are_read_only(self, box, repo):
        sub = repo / ".git" / "modules" / "libs" / "sub"
        sub.mkdir(parents=True)
        (sub / "HEAD").write_text("ref: refs/heads/main\n")
        (sub / "config").write_text("[core]\n")
        worktree = repo / ".git" / "worktrees" / "wt"
        worktree.mkdir(parents=True)
        (worktree / "HEAD").write_text("ref: refs/heads/wt\n")

        for command in (f"echo evil >> '{sub}/config'",
                        f"mkdir '{sub}/hooks' && echo x > '{sub}/hooks/post-checkout'",
                        f"echo evil > '{worktree}/config.worktree'"):
            assert sh(box, command, cwd=repo).returncode != 0, command
        assert (sub / "config").read_text() == "[core]\n"
        assert sh(box, f"echo fine > '{worktree}/index'", cwd=repo).returncode == 0

    def test_git_init_at_the_root_is_refused(self, area, seatbelt):
        """Else a confined process makes the root a repository with a config
        of its choosing, for the user's next git there."""
        box = ProcessSandbox(mode="workspace-write", workspace_root=area.ws)
        assert sh(box, "git init -q .", cwd=area.ws).returncode != 0
        assert not (area.ws / ".git").exists()


@on_macos
class TestFailClosed:
    """No usable sandbox-exec means an exception, never an unconfined argv."""

    @pytest.fixture(autouse=True)
    def fresh_probe(self, area):
        ps.reset_backend_cache()
        with probe_scratch_in(area.root):
            yield
        ps.reset_backend_cache()

    def _fake(self, area: Area, body: str) -> str:
        fake = area.root / "sandbox-exec"
        fake.write_text("#!/bin/sh\n" + body)
        fake.chmod(0o755)
        return str(fake)

    def test_a_missing_sandbox_exec_raises(self, area, monkeypatch):
        monkeypatch.setattr(ps, "_SANDBOX_EXEC", str(area.root / "absent"))
        with pytest.raises(SandboxUnavailable, match="does not exist"):
            ProcessSandbox(mode="read-only", workspace_root=area.ws).confine(["/usr/bin/true"])

    def test_a_sandbox_exec_that_cannot_apply_a_profile_raises(self, area, monkeypatch):
        """What a nested sandbox looks like: ``sandbox_apply`` fails, exit 71."""
        monkeypatch.setattr(ps, "_SANDBOX_EXEC", self._fake(
            area, "echo 'sandbox-exec: sandbox_apply: Operation not permitted' >&2; exit 71\n"))
        with pytest.raises(SandboxUnavailable, match="failed its probe"):
            ProcessSandbox(mode="read-only", workspace_root=area.ws).confine(["/usr/bin/true"])

    def test_a_sandbox_exec_that_runs_without_enforcing_raises(self, area, monkeypatch):
        """The deprecated tool turned into a pass-through: it starts the
        command and applies nothing. Presence and exit code look fine."""
        monkeypatch.setattr(ps, "_SANDBOX_EXEC", self._fake(
            area, 'while [ "$1" != "--" ]; do shift; done; shift; exec "$@"\n'))
        with pytest.raises(SandboxUnavailable, match="does not enforce"):
            ProcessSandbox(mode="read-only", workspace_root=area.ws).confine(["/usr/bin/true"])

    def test_a_real_profile_that_lets_a_write_through_is_refused(self, area, monkeypatch):
        """The probe tries a forbidden write with the profile commands get,
        not with one of its own: a broken rule is caught before first use."""
        monkeypatch.setattr(ps, "_SEATBELT_TAIL", ps._SEATBELT_TAIL + "(allow file-write*)\n")
        with pytest.raises(SandboxUnavailable, match="does not enforce"):
            ProcessSandbox(mode="read-only", workspace_root=area.ws).confine(["/usr/bin/true"])
        assert not list(area.root.glob("scarabhive-sandbox-probe-*")), "the probe left its scratch"

    def test_a_case_sensitive_user_temp_directory_makes_the_backend_unusable(self, area, monkeypatch):
        real = os.pathconf

        def pathconf(path, name):
            if name == ps._PC_CASE_SENSITIVE and str(path) == "/private/var/folders":
                return 1
            return real(path, name)

        monkeypatch.setattr(os, "pathconf", pathconf)
        with pytest.raises(SandboxUnavailable, match="user temp directory .* by case"):
            ProcessSandbox(mode="read-only", workspace_root=area.ws).confine(["/usr/bin/true"])

    def test_the_temp_directory_itself_cannot_be_removed(self, area, seatbelt):
        """rmdir of the user's temp directory would take it from every other
        program of the user. Checked on a stand-in: never on the real one."""
        stand_in = area.root / "temp"
        stand_in.mkdir()
        backend = ps._Seatbelt(ps._SANDBOX_EXEC, str(stand_in))
        confined = backend.wrap(ProcessSandbox(mode="read-only"),
                                ("/bin/sh", "-c", f"rmdir '{stand_in}'"), None)
        result = subprocess.run(confined.argv, capture_output=True, text=True, timeout=60)
        assert result.returncode != 0
        assert stand_in.is_dir()

    def test_a_workspace_below_a_case_sensitive_directory_raises(self, area, seatbelt, monkeypatch):
        """Seatbelt folds case (measured on a case-sensitive APFS image: beside
        ``ws``, ``WS`` was writable). Where names can differ by case alone,
        the only honest answer is no."""
        real = os.pathconf

        def pathconf(path, name):
            if name == ps._PC_CASE_SENSITIVE and str(path) == str(area.root):
                return 1
            return real(path, name)

        monkeypatch.setattr(os, "pathconf", pathconf)
        with pytest.raises(SandboxUnavailable, match="by case"):
            ProcessSandbox(mode="workspace-write", workspace_root=area.ws).confine(["/usr/bin/true"])

    def test_a_directory_that_cannot_say_counts_as_case_sensitive(self, area, seatbelt, monkeypatch):
        real = os.pathconf

        def pathconf(path, name):
            if name == ps._PC_CASE_SENSITIVE and str(path) == str(area.root):
                raise OSError(22, "Invalid argument")
            return real(path, name)

        monkeypatch.setattr(os, "pathconf", pathconf)
        with pytest.raises(SandboxUnavailable, match="by case"):
            ProcessSandbox(mode="workspace-write", workspace_root=area.ws).confine(["/usr/bin/true"])



class TestTheFirstConfineIsLogged:
    """``enforcement`` has to reach somebody: the operator, once."""

    class Backend:
        name = "stub"

        def __init__(self, enforcement):
            self.enforcement = enforcement

        def wrap(self, sandbox, argv, cwd):
            return ps.ConfinedCommand(argv=("stub", *argv), enforcement=self.enforcement,
                                      backend=self.name)

    @pytest.fixture(autouse=True)
    def fresh(self, monkeypatch):
        monkeypatch.setattr(ps, "_announced", set())

    def test_partial_enforcement_warns_once(self, monkeypatch, caplog):
        monkeypatch.setattr(ps, "_select_backend", lambda: self.Backend("partial"))
        box = ProcessSandbox(mode="workspace-write")
        with caplog.at_level("INFO", logger=ps.logger.name):
            box.confine(["a"])
            box.confine(["b"])
        records = [r for r in caplog.records if "process sandbox" in r.getMessage()]
        assert len(records) == 1
        assert records[0].levelname == "WARNING"
        assert "stub" in records[0].getMessage() and "PARTIAL" in records[0].getMessage()

    def test_full_enforcement_is_information(self, monkeypatch, caplog):
        monkeypatch.setattr(ps, "_select_backend", lambda: self.Backend("full"))
        with caplog.at_level("INFO", logger=ps.logger.name):
            ProcessSandbox(mode="read-only").confine(["a"])
        records = [r for r in caplog.records if "process sandbox" in r.getMessage()]
        assert [r.levelname for r in records] == ["INFO"]
        assert "enforcement full" in records[0].getMessage()


class TestSelectionFollowsThePlatform:
    """Automatic by platform, and a missing backend still raises -- on every host."""

    @pytest.fixture(autouse=True)
    def probes(self, monkeypatch):
        ps.reset_backend_cache()
        calls = []
        monkeypatch.setattr(ps, "_probe_seatbelt", lambda: calls.append("seatbelt") or "SB")
        monkeypatch.setattr(ps, "_probe_bubblewrap", lambda: calls.append("bwrap") or "BW")
        yield calls
        ps.reset_backend_cache()

    def _on(self, monkeypatch, host):
        monkeypatch.setattr(ps, "_host", lambda: host)

    def test_this_host_is_classified_by_its_platform(self):
        expected = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
        assert ps._host() == expected

    def test_macos_probes_seatbelt(self, monkeypatch, probes):
        self._on(monkeypatch, "macos")
        assert ps._select_backend() == "SB"
        assert probes == ["seatbelt"]

    def test_linux_probes_bubblewrap(self, monkeypatch, probes):
        self._on(monkeypatch, "linux")
        assert ps._select_backend() == "BW"
        assert probes == ["bwrap"]

    def test_windows_has_none_and_the_refusal_says_so(self, monkeypatch, probes):
        self._on(monkeypatch, "windows")
        assert ps._select_backend() is None
        assert probes == []
        with pytest.raises(SandboxUnavailable, match="no sandbox backend for Windows"):
            ProcessSandbox(mode="read-only").confine(["x"])

    def test_the_default_still_needs_no_backend(self, monkeypatch, probes):
        """The default stays danger-full-access until Windows has a backend."""
        self._on(monkeypatch, "windows")
        assert ProcessSandbox.from_config(None, base=".").confine(["x"]).argv == ("x",)
        assert probes == []
