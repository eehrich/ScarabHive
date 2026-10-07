"""install.sh, run for real against stubs: the optional part may fail, the core may not.

A fresh Mac stopped in `pip install -e .`: pycairo has no wheel there and
needs cairo and pkg-config to build. pycairo moved to
requirements/optional.txt; install.sh installs cairo where it can (Homebrew,
apt-get through sudo), installs the optional part best effort, and otherwise
says what is missing and how to add it -- the core install never stops on it.

Every program the script reaches is a stub on a PATH this test builds: the
venv's python (pip, its version and headers, the signing key, the admin
password, the browser wait), agent-api, uname, id, pkg-config, cc,
xcode-select, brew, apt-get, dnf, pacman, sudo. Each logs its argv,
so the test reads what the script would have run. Nothing reaches the network,
the real pip or the system's package manager; the only real program besides
the shell is dirname.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(sys.platform == "win32" or not Path("/bin/sh").exists(),
                                reason="install.sh needs a POSIX sh")

#: /bin/sh is bash on macOS and dash on Debian/Ubuntu; both run the script.
SHELLS = [sh for sh in ("/bin/sh", "/bin/dash")
          if Path(sh).exists() and (sh == "/bin/sh" or Path(sh).resolve() != Path("/bin/sh").resolve())]

#: The stub venv python has its headers unless PY_HEADERS_RC says otherwise;
#: then it is 3.12 and needs python3.12-dev.
APT_ARGS = "install -y build-essential libcairo2-dev pkg-config"
APT_CMD = f"sudo apt-get update && sudo apt-get {APT_ARGS}"
OPTIONAL = "-m pip install -r requirements/optional.txt"
SUMMARY = "SVG layers in images are off:"

# A stub logs "<name> <argv>" and answers what its environment variable says.
LOGGING_STUB = '#!/bin/sh\necho "{name} $*" >> "$STUB_LOG"\n'
STUBS = {
    "python": LOGGING_STUB.format(name="python") + (
        'case "$*" in\n'
        '  *"version_info[:2]"*) echo "${PY_VERSION:-3.12}" ;;\n'
        '  *Python.h*) exit "${PY_HEADERS_RC:-0}" ;;\n'
        # rlPyCairo imports once a successful optional install (or the test) put it there.
        '  *"import rlPyCairo"*) [ -e "$STUB_STATE/rlPyCairo" ]; exit ;;\n'
        '  *"install -r requirements/optional.txt"*)\n'
        '    [ "${PIP_OPTIONAL_RC:-0}" = 0 ] && [ -z "$PIP_OPTIONAL_BROKEN" ] && : > "$STUB_STATE/rlPyCairo"\n'
        '    exit "${PIP_OPTIONAL_RC:-0}" ;;\n'
        '  *"install -e ."*) exit "${PIP_CORE_RC:-0}" ;;\n'
        "esac\n"),
    "python3": LOGGING_STUB.format(name="python3"),
    "agent-api": LOGGING_STUB.format(name="agent-api") + 'echo "agent-api sees OS=$OS" >> "$STUB_LOG"\n',
    "uname": LOGGING_STUB.format(name="uname") + 'echo "${STUB_OS:-Darwin}"\n',
    "id": 'echo "${STUB_UID:-1000}"\n',
    "pkg-config": LOGGING_STUB.format(name="pkg-config") + 'exit "${PKG_CONFIG_RC:-1}"\n',
    "cc": LOGGING_STUB.format(name="cc"),
    "xcode-select": LOGGING_STUB.format(name="xcode-select") + 'exit "${XCODE_RC:-0}"\n',
    "brew": LOGGING_STUB.format(name="brew") + (
        # shellenv: what Homebrew prints for the profile -- its bin directory first on PATH.
        'if [ "$1" = shellenv ]; then echo "export PATH=\\"${0%/*}:\\$PATH\\""; exit 0; fi\n'
        'exit "${BREW_RC:-0}"\n'),
    "apt-get": LOGGING_STUB.format(name="apt-get") + (
        'if [ "$1" = update ]; then exit "${APT_UPDATE_RC:-0}"; fi\n'
        'exit "${APT_RC:-0}"\n'),
    "dnf": LOGGING_STUB.format(name="dnf"),
    "pacman": LOGGING_STUB.format(name="pacman"),
    # sudo -n without a cached password fails; otherwise it runs the command.
    "sudo": LOGGING_STUB.format(name="sudo") + (
        'if [ "$1" = -n ]; then\n'
        '  shift\n'
        '  if [ -n "$SUDO_NEEDS_PASSWORD" ]; then echo "sudo: a password is required" >&2; exit 1; fi\n'
        "fi\n"
        'exec "$@"\n'),
}


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, log: Path):
        self.rc = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr
        self.calls = [c.rstrip() for c in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def called(self, prefix: str) -> list[str]:
        return [c for c in self.calls if c.startswith(prefix)]

    @property
    def optional_installed(self) -> bool:
        return any(OPTIONAL in c for c in self.called("python "))


def run_install(tmp_path: Path, shell: str = "/bin/sh", *, tools=("pkg-config", "brew", "xcode-select"), windows=False,
                tty=False, rlpycairo=False, brew_off_path=False, **env_overrides) -> Run:
    """Copy install.sh into tmp_path with a stubbed .venv and PATH, run it, return what it did."""
    work = tmp_path / "checkout"
    work.mkdir()
    shutil.copy(REPO / "install.sh", work / "install.sh")
    venv = work / ".venv" / ("Scripts" if windows else "bin")
    venv.mkdir(parents=True)
    path = tmp_path / "path"
    path.mkdir()

    def stub(target: Path, body: str) -> None:
        target.write_text(body if body.startswith("#!") else "#!/bin/sh\n" + body, encoding="utf-8")
        target.chmod(0o755)

    stub(venv / "python", STUBS["python"])
    if windows:
        # install.sh finds the venv by python.exe and runs "$BIN/python",
        # which Git Bash resolves to the .exe; here both names are the stub.
        stub(venv / "python.exe", STUBS["python"])
    stub(venv / "agent-api", STUBS["agent-api"])
    for name in ("python3", "uname", "id", *tools):
        stub(path / name, STUBS[name])
    (path / "dirname").symlink_to(shutil.which("dirname"))

    state = tmp_path / "state"
    state.mkdir()
    if rlpycairo:
        (state / "rlPyCairo").touch()
    # The script also looks for Homebrew off PATH, at its usual places -- on a Mac
    # that would be the real one. The probe gets a path of the test's instead.
    probe = tmp_path / "homebrew" / "bin" / "brew"
    if brew_off_path:
        probe.parent.mkdir(parents=True)
        stub(probe, STUBS["brew"])
        stub(probe.parent / "pkg-config", STUBS["pkg-config"])

    log = tmp_path / "calls.log"
    env = dict(os.environ)  # keeps the root conftest's session marker
    for name in ("SCARABHIVE_NO_SYSTEM_PACKAGES", "PORT", "SUDO_NEEDS_PASSWORD", "PIP_OPTIONAL_RC",
                 "PIP_CORE_RC", "PKG_CONFIG_RC", "BREW_RC", "APT_RC", "APT_UPDATE_RC", "STUB_OS",
                 "STUB_UID", "PY_VERSION", "PY_HEADERS_RC", "XCODE_RC", "PIP_OPTIONAL_BROKEN"):
        env.pop(name, None)
    env.update(PATH=str(path), STUB_LOG=str(log), STUB_STATE=str(state), DISPLAY="", WAYLAND_DISPLAY="",
               _SCARABHIVE_BREW_PROBE=str(probe))
    env.update({k: str(v) for k, v in env_overrides.items()})

    if tty:
        master, slave = os.openpty()
        try:
            proc = subprocess.run([shell, "install.sh"], cwd=work, env=env, stdin=slave,
                                  capture_output=True, text=True, timeout=60)
        finally:
            os.close(slave)
            os.close(master)
    else:
        proc = subprocess.run([shell, "install.sh"], cwd=work, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
    return Run(proc, log)


def assert_went_on(run: Run) -> None:
    assert run.rc == 0, (run.rc, run.out, run.err)
    assert "agent-api" in run.calls, run.calls
    assert run.called("python -m agent_system.auth.first_admin"), run.calls


def assert_svg_off(run: Run, command: str) -> None:
    """The note during the install and the summary line at the end both name the command."""
    assert not run.optional_installed, run.calls
    assert "Going on without SVG layers" in run.err and command in run.err, run.err
    summary = [line for line in run.out.splitlines() if line.startswith(SUMMARY)]
    assert len(summary) == 1 and command in summary[0] and "./install.sh again" in summary[0], run.out


@pytest.mark.parametrize("shell", SHELLS)
def test_with_cairo_present_the_optional_part_is_installed_and_nothing_else_is_asked(tmp_path, shell):
    run = run_install(tmp_path, shell, PKG_CONFIG_RC=0)
    assert_went_on(run)
    assert run.called("pkg-config --exists cairo"), "fixture: the script never asked pkg-config"
    assert run.optional_installed, run.calls
    assert not run.called("brew") and "[Y/n]" not in run.out
    assert SUMMARY not in run.out and "Going on without SVG layers" not in run.err


@pytest.mark.parametrize("shell", SHELLS)
def test_macos_without_cairo_installs_it_with_homebrew_unasked(tmp_path, shell):
    run = run_install(tmp_path, shell)
    assert_went_on(run)
    assert run.called("brew") == ["brew install cairo pkg-config"], run.calls
    assert "Installing cairo and pkg-config with Homebrew" in run.out
    assert run.optional_installed and SUMMARY not in run.out, (run.calls, run.out)
    brew_at = run.calls.index("brew install cairo pkg-config")
    optional_at = next(i for i, c in enumerate(run.calls) if OPTIONAL in c)
    assert brew_at < optional_at, "the optional part was built before cairo was there"


def test_the_opt_out_leaves_homebrew_alone_and_says_what_is_missing(tmp_path):
    run = run_install(tmp_path, SCARABHIVE_NO_SYSTEM_PACKAGES=1)
    assert_went_on(run)
    assert not run.called("brew"), run.calls
    assert_svg_off(run, "brew install cairo pkg-config")


def test_a_failing_homebrew_leaves_svg_off_and_the_install_goes_on(tmp_path):
    run = run_install(tmp_path, BREW_RC=1)
    assert_went_on(run)
    assert run.called("brew install cairo pkg-config"), run.calls
    assert_svg_off(run, "brew install cairo pkg-config")


def test_macos_without_homebrew_points_to_it(tmp_path):
    # Neither brew nor pkg-config; an apt-get on a Mac (Fink has one) is not Debian's.
    run = run_install(tmp_path, tools=("apt-get", "sudo", "xcode-select"))
    assert_went_on(run)
    assert not run.called("apt-get") and not run.called("sudo"), run.calls
    assert_svg_off(run, "brew install cairo pkg-config")
    assert "https://brew.sh" in run.err and "xcode-select --install" not in run.err


def test_a_failing_optional_install_leaves_svg_off_and_the_install_goes_on(tmp_path):
    run = run_install(tmp_path, PKG_CONFIG_RC=0, PIP_OPTIONAL_RC=1)
    assert_went_on(run)
    assert any(OPTIONAL in c for c in run.calls), "fixture: the optional part was never tried"
    assert "Going on without SVG layers" in run.err, run.err
    assert [line for line in run.out.splitlines() if line.startswith(SUMMARY)], run.out


def test_a_failing_core_install_stops_the_script(tmp_path):
    run = run_install(tmp_path, PKG_CONFIG_RC=0, PIP_CORE_RC=1)
    assert run.rc != 0
    assert run.called("python -m pip install -e ."), "fixture: the core install never ran"
    assert not run.optional_installed and "agent-api" not in run.calls, run.calls


def test_a_windows_python_takes_pycairo_as_a_wheel_without_cairo(tmp_path):
    run = run_install(tmp_path, tools=(), windows=True, STUB_OS="MINGW64_NT-10.0", OS="Windows_NT")
    assert_went_on(run)
    assert run.optional_installed, run.calls
    # Git Bash exports OS=Windows_NT; the script must not hand its own value on.
    assert "agent-api sees OS=Windows_NT" in run.calls, run.calls


# ── Debian/Ubuntu ─────────────────────────────────────────────────────────

LINUX = dict(tools=("pkg-config", "apt-get", "sudo", "cc"), STUB_OS="Linux")
SUDO_N = ["sudo -n apt-get update", f"sudo -n apt-get {APT_ARGS}"]
APT_BOTH = ["apt-get update", f"apt-get {APT_ARGS}"]


@pytest.mark.parametrize("shell", SHELLS)
def test_apt_without_a_terminal_asks_sudo_for_no_password(tmp_path, shell):
    run = run_install(tmp_path, shell, **LINUX)
    assert_went_on(run)
    assert run.called("sudo") == SUDO_N, run.calls
    assert run.called("apt-get") == APT_BOTH, run.calls  # update first
    assert run.optional_installed and SUMMARY not in run.out


def test_apt_without_a_terminal_and_a_sudo_password_leaves_svg_off(tmp_path):
    run = run_install(tmp_path, SUDO_NEEDS_PASSWORD=1, **LINUX)
    assert_went_on(run)
    assert run.called("sudo") == SUDO_N[:1] and not run.called("apt-get"), run.calls
    assert_svg_off(run, APT_CMD)


@pytest.mark.parametrize("shell", SHELLS)
def test_apt_in_a_terminal_lets_sudo_ask_for_the_password(tmp_path, shell):
    run = run_install(tmp_path, shell, tty=True, SUDO_NEEDS_PASSWORD=1, **LINUX)
    assert_went_on(run)
    assert run.called("sudo") == ["sudo apt-get update", f"sudo apt-get {APT_ARGS}"], run.calls
    assert run.called("apt-get") == APT_BOTH and run.optional_installed, run.calls


def test_apt_as_root_needs_no_sudo(tmp_path):
    run = run_install(tmp_path, STUB_UID=0, **LINUX)
    assert_went_on(run)
    assert not run.called("sudo") and run.called("apt-get") == APT_BOTH, run.calls


def test_a_failing_apt_get_leaves_svg_off(tmp_path):
    run = run_install(tmp_path, APT_RC=100, **LINUX)
    assert_went_on(run)
    assert run.called("apt-get") == APT_BOTH, run.calls
    assert_svg_off(run, APT_CMD)


def test_a_failing_apt_get_update_leaves_svg_off_without_installing(tmp_path):
    run = run_install(tmp_path, APT_UPDATE_RC=100, **LINUX)
    assert_went_on(run)
    assert run.called("apt-get") == ["apt-get update"], run.calls
    assert_svg_off(run, APT_CMD)


def test_without_python_h_the_headers_package_is_the_venv_pythons(tmp_path):
    run = run_install(tmp_path, PY_VERSION="3.13", PY_HEADERS_RC=1, **LINUX)
    assert_went_on(run)
    assert run.called("apt-get") == [
        "apt-get update", "apt-get install -y build-essential libcairo2-dev pkg-config python3.13-dev"], run.calls


def test_with_python_h_no_headers_package_is_named(tmp_path):
    """A pyenv or uv Python has its headers and no pythonX.Y-dev package: a
    name apt does not know would fail the whole call."""
    run = run_install(tmp_path, PY_VERSION="3.13", **LINUX)
    assert_went_on(run)
    assert any("Python.h" in c for c in run.calls), "fixture: the headers were never asked for"
    assert run.called("apt-get") == APT_BOTH, run.calls
    assert "-dev pkg-config python" not in run.out and "python3.13-dev" not in run.out + run.err


@pytest.mark.parametrize("missing", ["compiler", "headers"])
def test_apt_installs_the_build_tools_when_only_cairo_is_there(tmp_path, missing):
    """cairo alone does not build pycairo: without a compiler or Python.h the
    full package list goes in, as when nothing was there."""
    tools = ("pkg-config", "apt-get", "sudo") + (() if missing == "compiler" else ("cc",))
    run = run_install(tmp_path, tools=tools, STUB_OS="Linux", PKG_CONFIG_RC=0,
                      PY_HEADERS_RC=1 if missing == "headers" else 0)
    assert_went_on(run)
    expected = APT_BOTH if missing == "compiler" else ["apt-get update", f"apt-get {APT_ARGS} python3.12-dev"]
    assert run.called("apt-get") == expected and run.optional_installed, run.calls


def test_linux_with_cairo_a_compiler_and_headers_installs_nothing(tmp_path):
    run = run_install(tmp_path, PKG_CONFIG_RC=0, **LINUX)
    assert_went_on(run)
    assert any("Python.h" in c for c in run.calls), "fixture: the headers were never asked for"
    assert not run.called("apt-get") and not run.called("sudo") and run.optional_installed, run.calls


def test_macos_without_the_command_line_tools_names_them_and_runs_nothing(tmp_path):
    run = run_install(tmp_path, XCODE_RC=2, PKG_CONFIG_RC=0)
    assert_went_on(run)
    assert run.called("xcode-select") and not any(c.startswith("xcode-select --install") for c in run.calls)
    assert not run.called("brew"), run.calls
    assert_svg_off(run, "xcode-select --install")
    assert "brew install cairo pkg-config" in run.err


@pytest.mark.parametrize("tool, command", [
    ("dnf", "sudo dnf install -y cairo-devel pkgconf-pkg-config gcc python3-devel"),
    ("pacman", "sudo pacman -S --needed cairo pkgconf gcc"),
    (None, "install cairo's development files, pkg-config, a C compiler and Python's headers"),
])
def test_other_linux_systems_get_the_command_and_nothing_is_run(tmp_path, tool, command):
    run = run_install(tmp_path, tools=("pkg-config", "sudo") + ((tool,) if tool else ()), STUB_OS="Linux")
    assert_went_on(run)
    assert not run.called("sudo") and not (tool and run.called(tool)), run.calls
    assert_svg_off(run, command)


# ── how a clone gets it ───────────────────────────────────────────────────

def test_install_sh_is_executable_in_the_repository():
    """A clone takes the mode from the index: 100644 made `./install.sh` fail."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    listed = subprocess.run(["git", "ls-files", "-s", "install.sh"], cwd=REPO,
                            capture_output=True, text=True, timeout=30)
    if listed.returncode != 0 or not listed.stdout:
        pytest.skip("not a git checkout")
    assert listed.stdout.split()[0] == "100755", listed.stdout


# ── what is already there ─────────────────────────────────────────────────

def test_homebrew_off_path_is_found_and_used(tmp_path):
    """Apple Silicon puts brew in /opt/homebrew/bin, which a shell without the
    profile line does not have on PATH."""
    run = run_install(tmp_path, tools=("xcode-select",), brew_off_path=True)
    assert_went_on(run)
    assert run.called("brew") == ["brew shellenv", "brew install cairo pkg-config"], run.calls
    assert run.optional_installed and "https://brew.sh" not in run.err, (run.calls, run.err)


@pytest.mark.parametrize("why", ["opt-out", "no pkg-config"])
def test_an_installed_backend_keeps_svg_on(tmp_path, why):
    """An earlier run installed rlPyCairo: neither the opt-out nor a missing
    pkg-config turns SVG layers off, and nothing is installed for them."""
    env = {"SCARABHIVE_NO_SYSTEM_PACKAGES": 1} if why == "opt-out" else {}
    run = run_install(tmp_path, tools=("brew", "xcode-select") if why == "no pkg-config" else ("pkg-config", "brew", "xcode-select"),
                      rlpycairo=True, **env)
    assert_went_on(run)
    assert any("import rlPyCairo" in c for c in run.calls), "fixture: the backend was never asked for"
    assert not run.called("brew install"), run.calls
    assert "Going on without SVG layers" not in run.err and SUMMARY not in run.out, (run.err, run.out)


def test_an_optional_install_that_leaves_the_backend_unimportable_says_svg_is_off(tmp_path):
    """pip answers 0, yet rlPyCairo does not import (a cairo library gone since
    pycairo was built): what counts is the import, not pip's exit code."""
    run = run_install(tmp_path, PKG_CONFIG_RC=0, PIP_OPTIONAL_BROKEN=1)
    assert_went_on(run)
    assert run.optional_installed, "fixture: the optional part was not tried"
    assert "Going on without SVG layers" in run.err and SUMMARY in run.out, (run.err, run.out)
