"""The local git side against real repositories on disk: a bare repository
plays the platform. What is pinned down: where the token goes, that a push is
never forced, and which remote URLs count as the same repository."""
import subprocess

import pytest

from plugins.forge import gitops
from plugins.forge.http import ForgeError


@pytest.fixture(autouse=True)
def quiet_git(tmp_path, monkeypatch):
    """No global or system git config: the global git-lfs filter costs seconds per call."""
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text):
    (repo / name).write_text(text)
    sh(repo, "add", name)
    sh(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)


@pytest.fixture
def platform(tmp_path):
    """A bare repository with one commit on main, and a clone of it."""
    bare = tmp_path / "platform.git"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    sh(tmp_path, "clone", "-q", str(bare), str(seed))
    sh(seed, "switch", "-q", "-c", "main")
    commit(seed, "a.txt", "a\n")
    sh(seed, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    gitops.clone(str(bare), clone, None, "main")
    return bare, seed, clone


def fill(env, host, protocol="https"):
    """What git's credential machinery answers for a host, as git-lfs asks it."""
    done = subprocess.run(["git", "credential", "fill"], input=f"protocol={protocol}\nhost={host}\n\n",
                          env=env, capture_output=True, text=True)
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


def test_the_token_is_a_credential_for_the_platform_origin_only(tmp_path, monkeypatch):
    """A credential, not a header: git-lfs sent our header next to GitLab's
    upload Authorization and got 400 (F-GL10). Another host gets nothing --
    also not from a helper in the user's config, which is reset."""
    config = tmp_path / "global"
    # A store answers with a user AND a password, for every host -- like the real one.
    config.write_text('[credential]\n\thelper = "!f() { echo username=me; echo password=from-the-user-store; }; f"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    env = gitops.auth_env("https://git.example.com:8443/team/app.git", "oauth2", "tok-123")
    assert fill(env, "git.example.com:8443") == {"protocol": "https", "host": "git.example.com:8443",
                                                 "username": "oauth2", "password": "tok-123"}
    assert "password" not in fill(env, "evil.example.com")
    assert "password" not in fill(env, "git.example.com")               # another port is another origin
    assert "tok-123" not in "".join(env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"])))
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_certificates_are_checked_whatever_the_git_config_says(tmp_path, monkeypatch):
    """The development machine had a global http.sslVerify false (review S1):
    the origin-bound key must win over it, and GIT_SSL_NO_VERIFY must go."""
    config = tmp_path / "global"
    config.write_text("[http]\n\tsslVerify = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    url = "https://git.example.com/team/app.git"
    env = gitops.auth_env(url, "oauth2", "t")
    assert "GIT_SSL_NO_VERIFY" not in env
    effective = subprocess.run(["git", "config", "--get-urlmatch", "http.sslVerify", url], env=env,
                               capture_output=True, text=True).stdout.strip()
    assert effective == "true"
    off = gitops.auth_env(url, "oauth2", "t", verify=False)
    assert subprocess.run(["git", "config", "--get-urlmatch", "http.sslVerify", url], env=off,
                          capture_output=True, text=True).stdout.strip() == "false"


def test_auth_env_refuses_a_non_http_clone_url():
    with pytest.raises(ForgeError, match="not HTTP"):
        gitops.auth_env("git@host:team/app.git", "oauth2", "t")


@pytest.mark.parametrize("a,b,same", [
    ("https://git.example.com/team/app.git", "git@git.example.com:team/app.git", True),
    ("https://git.example.com:8929/Team/App", "ssh://git@git.example.com:2222/team/app.git", True),
    ("https://git.example.com/team/app.git", "https://git.example.com/team/other.git", False),
    ("https://git.example.com/team/app.git", "https://evil.example.com/team/app.git", False),
    ("", "", False),
])
def test_same_repo(a, b, same):
    assert gitops.same_repo(a, b) is same


@pytest.mark.parametrize("name,ok", [
    ("scarabhive/42-fix", True), ("main", True), ("-x", False), ("a..b", False), ("a/", False),
    ("a.lock", False), ("a b", False), ("a@{1}", False), ("a//b", False), ("x/.hidden", False), ("", False),
])
def test_valid_branch(name, ok):
    assert gitops.valid_branch(name) is ok


def test_push_is_never_forced(platform):
    bare, seed, clone = platform
    gitops.switch(clone, "scarabhive/x", "refs/remotes/origin/main")
    commit(clone, "b.txt", "b\n")
    gitops.push(clone, str(bare), "scarabhive/x", "scarabhive/x", None)
    assert sh(bare, "rev-parse", "refs/heads/scarabhive/x") == sh(clone, "rev-parse", "HEAD")

    # Someone else pushes to the branch; the local one now lacks that commit.
    sh(seed, "fetch", "-q", "origin")
    sh(seed, "switch", "-q", "-c", "other", "origin/scarabhive/x")
    commit(seed, "c.txt", "c\n")
    sh(seed, "push", "-q", "origin", "other:scarabhive/x")
    theirs = sh(bare, "rev-parse", "refs/heads/scarabhive/x")
    sh(clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--amend", "-m", "rewritten")
    with pytest.raises(gitops.GitError, match="nothing is forced"):
        gitops.push(clone, str(bare), "scarabhive/x", "scarabhive/x", None)
    assert sh(bare, "rev-parse", "refs/heads/scarabhive/x") == theirs


def test_a_push_carries_no_tags_whatever_the_config_says(platform):
    """push.followTags would push refs/tags/* -- outside the prefix (review S6)."""
    bare, _, clone = platform
    sh(clone, "config", "push.followTags", "true")
    gitops.switch(clone, "scarabhive/t", "refs/remotes/origin/main")
    commit(clone, "t.txt", "t\n")
    sh(clone, "-c", "user.name=t", "-c", "user.email=t@t", "tag", "-a", "v9", "-m", "tag")
    gitops.push(clone, str(bare), "scarabhive/t", "scarabhive/t", None)
    assert sh(bare, "tag") == ""
    assert sh(bare, "rev-parse", "refs/heads/scarabhive/t")


def test_a_url_rewrite_in_the_config_is_refused(tmp_path, monkeypatch):
    """insteadOf to SSH would push with the user's identity, not the bot's token."""
    config = tmp_path / "global"
    config.write_text('[url "git@git.example.com:"]\n\tinsteadOf = https://git.example.com/\n'
                      '[url "https://mirror.example.com/"]\n\tpushInsteadOf = https://other.example.com/\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    with pytest.raises(ForgeError, match="redirects https://git.example.com/team/app.git"):
        gitops.refuse_rewrites("https://git.example.com/team/app.git", None, tmp_path)
    gitops.refuse_rewrites("https://unrelated.example.com/team/app.git", None, tmp_path)


URL = "https://git.example.com/team/app.git"


@pytest.mark.parametrize("config", [
    '[url "file:///elsewhere/"]\n\tinsteadOf =\n',                         # an empty prefix rewrites every URL
    '[url "file:///C:/evil dir/"]\n\tinsteadOf = https://git.example.com/\n',   # a space in the base
    f'[remote "{URL}"]\n\tpushurl = file:///elsewhere.git\n',             # git takes the URL as that remote
    '[http "https://git.example.com/team/app.git"]\n\tsslVerify = false\n',  # outranks forge's key for the origin
    '[http "https://git.example.com/team/app.git"]\n\tsslVerify =\n',       # git reads an empty value as false
    '[http "https://git.example.com/team/app.git/info/lfs"]\n\tsslVerify = 00\n',   # below the URL: git-lfs
], ids=["empty-insteadof", "space-in-base", "remote-named-by-url", "path-sslverify", "empty-sslverify",
        "lfs-path-sslverify"])
def test_config_that_redirects_or_unchecks_the_platform_is_refused(tmp_path, monkeypatch, config):
    """Ways past the first guards (reviews of the fix rounds)."""
    (tmp_path / "global").write_text(config)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global"))
    with pytest.raises(ForgeError, match="redirects"):
        gitops.refuse_rewrites(URL, gitops.auth_env(URL, "oauth2", "t"), tmp_path)


def test_a_rule_that_applies_only_inside_the_clone_is_refused_before_the_first_fetch(tmp_path, monkeypatch):
    """includeIf "gitdir:" holds for the new clone, not for its parent: checked
    from there, git clone would have fetched elsewhere (review of the fix round)."""
    target = tmp_path / "work" / "app"
    (tmp_path / "work.inc").write_text('[url "file:///elsewhere/"]\n\tinsteadOf = https://git.example.com/\n')
    (tmp_path / "global").write_text(f'[includeIf "gitdir/i:{(tmp_path / "work").as_posix()}/"]\n'
                                     f'\tpath = {(tmp_path / "work.inc").as_posix()}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global"))
    assert gitops.rewrites(URL, None, None) == []                          # not seen from outside
    with pytest.raises(ForgeError, match="redirects"):
        gitops.clone(URL, target, gitops.auth_env(URL, "oauth2", "t"), "main")
    assert not gitops.has_ref(target, "refs/remotes/origin/main")


def test_the_tls_guard_leaves_alone_what_git_ranks_below_forges_key(tmp_path, monkeypatch):
    """A per-host exception is the usual way to write one; forge's key for the
    origin outranks it, git checks anyway -- refusing would only push the
    operator towards tls_verify: false (review of the fix round)."""
    (tmp_path / "global").write_text('[http "https://other.example.org/"]\n\tsslVerify = false\n'
                                     '[http "https://broken.example.org/"]\n\tsslVerify = maybe\n'  # not forge's
                                     '[http "https://git.example.com"]\n\tsslVerify = false\n'
                                     '[http "https://*.example.com/"]\n\tsslVerify = false\n'
                                     '[http "https://git.example.com/other/"]\n\tsslVerify = false\n'
                                     '[http "https://git.example.com/team/app.git"]\n\tsslVerify = true\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global"))
    gitops.refuse_rewrites(URL, gitops.auth_env(URL, "oauth2", "t"), tmp_path)
    gitops.refuse_rewrites(URL, gitops.auth_env(URL, "oauth2", "t", verify=False), tmp_path, verify=False)


def test_uses_lfs_reads_the_branchs_attributes(platform):
    _, _, clone = platform
    assert not gitops.uses_lfs(clone, "HEAD")
    (clone / "assets").mkdir()
    commit(clone, "assets/.gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    assert gitops.uses_lfs(clone, "HEAD")


def test_lfs_objects_go_up_first_with_the_same_credential(tmp_path, monkeypatch):
    """Hooks are off, so git-lfs's pre-push never runs (review S9); measured live in F-GL10."""
    calls = []
    monkeypatch.setattr(gitops, "uses_lfs", lambda path, ref: True)
    monkeypatch.setattr(gitops, "git", lambda path, *args, env=None, **kw: calls.append((args[:2], env)) or "")
    env = {"marker": "1"}
    gitops.push(tmp_path, "https://gl.test/team/app.git", "scarabhive/x", "scarabhive/x", env)
    assert [a for a, _ in calls] == [("lfs", "push"), ("push", "--quiet")] and all(e is env for _, e in calls)


def test_a_missing_git_lfs_is_named(tmp_path, monkeypatch):
    def git(path, *args, **kw):
        raise gitops.GitError("git lfs: failed", "git: 'lfs' is not a git command. See 'git --help'.")
    monkeypatch.setattr(gitops, "uses_lfs", lambda path, ref: True)
    monkeypatch.setattr(gitops, "git", git)
    with pytest.raises(gitops.GitError, match="git-lfs is not installed"):
        gitops.push(tmp_path, "https://gl.test/team/app.git", "scarabhive/x", "scarabhive/x", {})


def test_sync_reports_each_state(platform):
    bare, seed, clone = platform
    upstream = "refs/remotes/origin/main"
    assert gitops.sync(clone, upstream) == "up to date"
    commit(seed, "b.txt", "b\n")
    sh(seed, "push", "-q", "origin", "main")
    gitops.fetch(clone, str(bare), None)
    assert gitops.sync(clone, upstream) == "fast-forwarded"
    assert sh(clone, "rev-parse", "HEAD") == sh(bare, "rev-parse", "main")
    commit(clone, "mine.txt", "m\n")
    assert gitops.sync(clone, upstream) == "ahead"
    commit(seed, "c.txt", "c\n")
    sh(seed, "push", "-q", "origin", "main")
    gitops.fetch(clone, str(bare), None)
    before = sh(clone, "rev-parse", "HEAD")
    assert gitops.sync(clone, upstream) == "diverged"
    assert sh(clone, "rev-parse", "HEAD") == before


def test_no_hook_runs_during_a_push(platform, tmp_path):
    """A hook would be a program started with the token in its environment."""
    bare, _, clone = platform
    marker = tmp_path / "hook-ran"
    hook = clone / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n")
    hook.chmod(0o755)
    gitops.switch(clone, "scarabhive/h", "refs/remotes/origin/main")
    commit(clone, "h.txt", "h\n")
    gitops.push(clone, str(bare), "scarabhive/h", "scarabhive/h", None)
    assert not marker.exists()


def test_a_platform_refusal_names_the_platforms_reason(monkeypatch):
    """Output as GitLab 19.4.1 gave it for the bot's push to protected main
    (F-GL8). A local bare repository cannot play this: forge's own hook pin
    reaches its receive-pack too."""
    stdout = ("To http://gl.test/team/app.git\n!\trefs/heads/main:refs/heads/main\t"
              "[remote rejected] (pre-receive hook declined)\nDone\n")
    stderr = ("remote: GitLab: You are not allowed to push code to protected branches on this project.\n"
              "To http://gl.test/team/app.git\n ! [remote rejected] main -> main (pre-receive hook declined)\n"
              "error: failed to push some refs to 'http://gl.test/team/app.git'\n")
    def run(cmd, **kw):
        if "grep" in cmd:                                   # uses_lfs: no LFS attributes
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(gitops.subprocess, "run", run)
    with pytest.raises(gitops.GitError) as caught:
        gitops.push(gitops.Path("."), "http://gl.test/team/app.git", "main", "main", {"X": "1"})
    assert str(caught.value) == ("the platform refused the push to main: GitLab: You are not allowed to push "
                                 "code to protected branches on this project.")


def test_a_refused_token_says_so(monkeypatch):
    """git asks for a user name when the platform refuses the header (F-GL9)."""
    stderr = "fatal: could not read Username for 'http://gl.test': terminal prompts disabled\n"
    monkeypatch.setattr(gitops.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 128, stdout="", stderr=stderr))
    with pytest.raises(gitops.GitError, match="refused the token over git"):
        gitops.fetch(gitops.Path("."), "http://gl.test/team/app.git", {"X": "1"})


def test_the_header_value_never_reaches_an_error(monkeypatch):
    stderr = "trace: http.extraHeader=Authorization: Basic b2F1dGgyOnNlY3JldA==\nfatal: boom\n"
    monkeypatch.setattr(gitops.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr=stderr))
    with pytest.raises(gitops.GitError) as caught:
        gitops.git(gitops.Path("."), "status")
    assert "b2F1dGgyOnNlY3JldA" not in str(caught.value) + caught.value.output


def test_dirty_ignores_untracked_files(platform):
    _, _, clone = platform
    (clone / "new.txt").write_text("x")
    assert gitops.dirty(clone) == []
    (clone / "a.txt").write_text("changed")
    assert len(gitops.dirty(clone)) == 1
