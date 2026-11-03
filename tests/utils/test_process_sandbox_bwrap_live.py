"""bubblewrap, live: git keeps working, its metadata that runs code does not
move -- present, or missing at spawn (except ``commondir``, the documented
gap) -- and nothing left behind breaks git.

Runs only where a working bwrap is installed (skipped otherwise). First run
2026-11-03 on bubblewrap 0.9.0 and git 2.43.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_system.utils import process_sandbox as ps
from agent_system.utils.process_sandbox import ProcessSandbox

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("git") is None
    or ps._probe_bubblewrap() is None,
    reason="needs Linux with a working bwrap and git")

ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_ALLOW_PROTOCOL": "file"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True,
                          capture_output=True, text=True).stdout


def confined(root: Path, script: str) -> subprocess.CompletedProcess:
    argv = ps._Bubblewrap(shutil.which("bwrap") or "bwrap").wrap(
        ProcessSandbox(mode="workspace-write", workspace_root=root),
        ("/bin/sh", "-c", script), root).argv
    return subprocess.run(argv, env=ENV, capture_output=True, text=True, timeout=60)


@pytest.fixture
def repo(tmp_path):
    """A workspace repository with a submodule and an upstream to fetch.

    The upstream lives inside the workspace: the sandbox's /tmp is private,
    and tmp_path is below it.
    """
    root = tmp_path / "ws"
    up = root / "vendor" / "up"
    up.mkdir(parents=True)
    git(up, "init", "-q", "-b", "main")
    (up / "u").write_text("u\n")
    git(up, "add", "u")
    git(up, "commit", "-qm", "up")
    git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("vendor/\n")
    (root / "f").write_text("1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "c1")
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(up), "libs/up")
    git(root, "commit", "-qm", "sub")
    git(root, "remote", "add", "origin", str(up))
    shutil.rmtree(root / ".git" / "hooks")  # missing, like after a bare clone
    return root


def test_normal_git_work_goes_on(repo):
    ran = confined(repo, """set -e
        echo 2 >> f; git add f; git commit -qm c2
        git status --short; git branch feat; git checkout -q feat
        echo 3 > g; git add g; git commit -qm c3; git checkout -q main
        git merge -q --no-edit feat
        echo 4 >> f; git stash -q; git stash pop -q; git commit -qam c4
        git fetch -q origin; git tag v1; git pack-refs --all
        git -C libs/up log --oneline >/dev/null
        echo 5 > libs/up/v; git -C libs/up add v; git -C libs/up commit -qm v
        echo OK""")
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip().endswith("OK")
    assert "c4" in git(repo, "log", "--oneline", "-1")


ESCAPES = {
    "the config": "echo '[core]' >> .git/config",
    "a hook": "echo x > .git/hooks/pre-commit",
    "a missing hooks directory": "mkdir .git/hooks/x",
    "a missing config.worktree": "echo '[core]' > .git/config.worktree",
    "config.worktree by rename": "echo x > .git/cw && mv -f .git/cw .git/config.worktree",
    "a submodule's config": "echo '[core]' >> .git/modules/libs/up/config",
    "a submodule's config.worktree": "echo x > .git/modules/libs/up/config.worktree",
    "the git directory": "mv .git .git-old",
}


@pytest.mark.parametrize("what", sorted(ESCAPES))
def test_git_metadata_that_runs_code_stays_put(repo, what):
    git_dir = repo / ".git"
    before = {p: p.read_bytes() for p in (git_dir / "config",
                                          git_dir / "modules/libs/up/config")}
    ran = confined(repo, ESCAPES[what])
    assert ran.returncode != 0, f"{what} was writable"
    for path, content in before.items():
        assert path.read_bytes() == content
    assert not (git_dir / "commondir").exists()
    for name in ("config.worktree", "modules/libs/up/config.worktree"):
        assert (git_dir / name).read_bytes() == b""
    assert list((git_dir / "hooks").iterdir()) == []


def test_what_is_left_behind_changes_nothing_for_git(repo):
    """The traces: an empty hooks directory and config.worktree, mount points
    bwrap leaves. No commondir is written."""
    assert confined(repo, "true").returncode == 0
    git_dir = repo / ".git"
    assert not (git_dir / "commondir").exists()
    assert (git_dir / "config.worktree").read_bytes() == b""
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "a.b", "c")
    assert git(repo, "config", "--worktree", "a.b").strip() == "c"
    (repo / "f").write_text("unconfined\n")
    git(repo, "commit", "-qam", "after")
    git(repo, "worktree", "add", "-q", str(repo / "vendor" / "wt"))
    assert "after" in git(repo / "vendor" / "wt", "log", "--oneline", "-1")
    git(repo, "worktree", "remove", str(repo / "vendor" / "wt"))
    assert git(repo, "rev-parse", "--show-toplevel").strip() == str(repo)
    assert git(repo, "fsck", "--no-dangling") == ""


@pytest.mark.parametrize("gitdir", [".git", ".git/modules/libs/up"])
def test_a_missing_commondir_stays_creatable(repo, gitdir):
    """Documented residual, main repository and submodule alike: no stand-in
    for a missing commondir leaves git working, so none is put there."""
    ran = confined(repo, f"echo evil > {gitdir}/commondir")
    assert ran.returncode == 0
    assert (repo / gitdir / "commondir").read_text() == "evil\n"
