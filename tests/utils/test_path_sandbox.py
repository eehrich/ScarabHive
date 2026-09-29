"""The shared path boundary — one reading for every plugin.

Every plugin that takes a path from LLM arguments used to have its own. The
rules now live in one place, and the three that carry weight are pinned here:

* **fail closed** — no roots means nothing allowed, never everything.
* **canonicalize first, compare second** — otherwise you judge a path nobody
  will open.
* **read_only is part of the vocabulary** — a write attempt fails at the
  boundary, not at a flag beside it.

The attacks below point at files that REALLY exist; otherwise a green result
could come from "file not found" rather than from the boundary.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_system.utils.path_sandbox import PathSandbox, PathSandboxDenied


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "file.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    return root


@pytest.fixture
def sandbox(workspace: Path) -> PathSandbox:
    return PathSandbox.from_config([str(workspace)], base=workspace.parent)


class TestFailClosed:
    """The costliest mistake would be reading an empty list as 'allow all'."""

    def test_no_roots_denies_everything(self, workspace):
        empty = PathSandbox.from_config([], base=workspace.parent)
        with pytest.raises(PathSandboxDenied):
            empty.resolve(str(workspace / "sub" / "file.txt"))

    def test_none_config_denies_everything(self, workspace):
        empty = PathSandbox.from_config(None, base=workspace.parent)
        with pytest.raises(PathSandboxDenied):
            empty.resolve(str(workspace / "sub" / "file.txt"))

    def test_a_configured_root_is_what_makes_it_pass(self, workspace):
        """Counter-check: the same path WITH a root goes through. Without
        this, the two tests above would also pass on broken resolution."""
        ok = PathSandbox.from_config([str(workspace)], base=workspace.parent)
        assert ok.resolve(str(workspace / "sub" / "file.txt")).exists()


class TestContainment:
    def test_inside_resolves(self, sandbox, workspace):
        assert sandbox.resolve(str(workspace / "sub" / "file.txt")) == \
            (workspace / "sub" / "file.txt").resolve()

    def test_the_root_itself_is_allowed(self, sandbox, workspace):
        assert sandbox.resolve(str(workspace)) == workspace.resolve()

    def test_absolute_path_outside_is_denied(self, sandbox, workspace):
        target = workspace.parent / "outside" / "secret.txt"
        assert target.is_file(), "fixture missing — test would be vacuous"
        with pytest.raises(PathSandboxDenied):
            sandbox.resolve(str(target))

    def test_traversal_out_is_denied(self, sandbox, workspace):
        with pytest.raises(PathSandboxDenied):
            sandbox.resolve(str(workspace / ".." / "outside" / "secret.txt"))

    def test_traversal_that_stays_inside_is_allowed(self, sandbox, workspace):
        """Deliberate choice: ``..`` is resolved, not banned lexically. After
        canonicalization it IS the file that was named."""
        assert sandbox.resolve(str(workspace / "sub" / ".." / "sub" / "file.txt")) == \
            (workspace / "sub" / "file.txt").resolve()

    def test_a_sibling_with_the_same_prefix_is_not_inside(self, tmp_path):
        """``/x/allowed`` must not contain ``/x/allowed_evil`` — a string
        prefix comparison would let that through."""
        (tmp_path / "allowed").mkdir()
        evil = tmp_path / "allowed_evil"
        evil.mkdir()
        (evil / "f.txt").write_text("x")
        box = PathSandbox.from_config([str(tmp_path / "allowed")], base=tmp_path)
        with pytest.raises(PathSandboxDenied):
            box.resolve(str(evil / "f.txt"))

    def test_null_byte_is_denied(self, sandbox, workspace):
        with pytest.raises(PathSandboxDenied):
            sandbox.resolve(str(workspace / "sub") + "\x00.txt")


class TestRelativePathsCountAgainstTheBase:
    def test_relative_input_uses_the_base_not_the_process_cwd(self, workspace):
        box = PathSandbox.from_config([str(workspace)], base=workspace.parent)
        assert box.resolve("allowed/sub/file.txt") == \
            (workspace / "sub" / "file.txt").resolve()

    def test_relative_root_entry_also_counts_against_the_base(self, workspace):
        """``- data`` in the configuration names the same root no matter where
        the process was started."""
        box = PathSandbox.from_config(["allowed"], base=workspace.parent)
        assert box.roots == (workspace.resolve(),)


class TestReadOnlyIsPartOfTheVocabulary:
    def test_write_is_denied(self, workspace):
        box = PathSandbox.from_config([str(workspace)], base=workspace.parent,
                                      read_only=True)
        with pytest.raises(PathSandboxDenied, match="read-only"):
            box.resolve(str(workspace / "sub" / "new.txt"), write=True)

    def test_read_still_works(self, workspace):
        box = PathSandbox.from_config([str(workspace)], base=workspace.parent,
                                      read_only=True)
        assert box.resolve(str(workspace / "sub" / "file.txt")).exists()

    def test_write_passes_when_not_read_only(self, sandbox, workspace):
        assert sandbox.resolve(str(workspace / "sub" / "new.txt"), write=True)


class TestSymlinksAreResolvedBeforeJudgement:
    """The order is the point: ``resolve()`` follows the link, THEN the
    comparison runs. Normalizing lexically first judges a different path than
    the one that gets opened."""

    def test_a_link_pointing_out_is_denied(self, workspace, tmp_path):
        link = workspace / "escape"
        try:
            link.symlink_to(tmp_path / "outside", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("no symlink privileges on this host")
        assert (tmp_path / "outside" / "secret.txt").is_file()
        box = PathSandbox.from_config([str(workspace)], base=workspace.parent)
        with pytest.raises(PathSandboxDenied):
            box.resolve(str(link / "secret.txt"))

    def test_a_link_pointing_in_is_followed(self, workspace, tmp_path):
        link = tmp_path / "shortcut"
        try:
            link.symlink_to(workspace / "sub", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("no symlink privileges on this host")
        box = PathSandbox.from_config([str(workspace)], base=workspace.parent)
        assert box.resolve(str(link / "file.txt")) == \
            (workspace / "sub" / "file.txt").resolve()


class TestLeadingTildeIsRefused:
    """``resolve()`` does not expand ``~`` — it becomes a literal directory.

    With a root that is an ancestor of the base (``allowed_directories: [.]``)
    that lands INSIDE the sandbox: the write succeeds and the model believes it
    wrote to the home directory. Containment cannot catch it, nothing escaped —
    only the meaning did.
    """

    def test_home_shorthand_is_refused(self, tmp_path):
        box = PathSandbox.from_config(["."], base=tmp_path)
        with pytest.raises(PathSandboxDenied, match="~"):
            box.resolve("~/notes.md")

    def test_bare_tilde_is_refused(self, tmp_path):
        box = PathSandbox.from_config(["."], base=tmp_path)
        with pytest.raises(PathSandboxDenied, match="~"):
            box.resolve("~")

    def test_named_home_is_refused(self, tmp_path):
        box = PathSandbox.from_config(["."], base=tmp_path)
        with pytest.raises(PathSandboxDenied, match="~"):
            box.resolve("~root/.ssh/id_rsa")

    def test_a_tilde_inside_a_filename_is_allowed(self, sandbox, workspace):
        """Editor and Office lock files are named this way. Refusing them was
        the bug the old lexical rule actually had."""
        assert sandbox.resolve(str(workspace / "sub" / "~lock.db")) == \
            (workspace / "sub" / "~lock.db").resolve()

    def test_a_leading_lock_file_name_is_refused_on_purpose(self, tmp_path):
        """Known false positive, pinned so nobody "fixes" it silently.

        ``~lock.db`` as the first component cannot be told apart from
        ``~user`` — expanduser reads it that way. Friction the caller can see
        beats a silent write into a directory named ``~``. Naming the
        directory (``sub/~lock.db``) works and is covered above.
        """
        box = PathSandbox.from_config(["."], base=tmp_path)
        with pytest.raises(PathSandboxDenied, match="~"):
            box.resolve("~lock.db")


class TestMessagesNameTheBoundary:
    """The refusal is what the model acts on — it has to say what is allowed."""

    def test_the_refusal_names_the_roots(self, sandbox, workspace):
        with pytest.raises(PathSandboxDenied) as exc:
            sandbox.resolve(str(workspace.parent / "outside" / "secret.txt"))
        assert str(workspace) in str(exc.value)

    def test_relative_reports_the_path_below_its_root(self, sandbox, workspace):
        assert sandbox.relative((workspace / "sub" / "file.txt").resolve()) == \
            str(Path("sub") / "file.txt")

    def test_relative_falls_back_to_absolute_outside(self, sandbox, tmp_path):
        stranger = (tmp_path / "outside" / "secret.txt").resolve()
        assert sandbox.relative(stranger) == str(stranger)


class TestBothPluginsShareOneResolution:
    """The actual point of the exercise: ONE reading, not two.

    Checked by BEHAVIOUR, not by grepping for the class name in the source. An
    earlier version asserted ``"PathSandbox" in inspect.getsource(...)``; a
    mutation that unhooked media_ops from the sandbox entirely left that green,
    because the string still stood in a neighbouring line.
    """

    @staticmethod
    def _media_server(roots, tmp_path):
        from types import SimpleNamespace

        from agent_system.config.models import ToolServerConfig
        from plugins.media_ops.server import MediaOpsServer

        cfg = ToolServerConfig(type="media_ops", enabled=True,
                        config={"allowed_directories": [str(r) for r in roots]})
        return MediaOpsServer("media_ops", SimpleNamespace(), cfg)

    async def test_the_same_escape_is_refused_by_both(self, tmp_path):
        from plugins.file_ops.security import PathValidator, SecurityError

        allowed = tmp_path / "ws"
        allowed.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "secret.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        target = str(outside / "secret.png")

        # file_ops
        with pytest.raises(SecurityError):
            PathValidator([str(allowed)], base=tmp_path).validate_path(target)

        # media_ops — through the REAL tool call, not through a helper
        res = await self._media_server([allowed], tmp_path).call(
            "media_ops_load", {"path": target})
        assert res["status"] == "error", res
        assert res["error_type"] == "PermissionError", res

        # and the shared boundary itself
        with pytest.raises(PathSandboxDenied):
            PathSandbox.from_config([str(allowed)], base=tmp_path).resolve(target)

    async def test_media_ops_load_stays_inside(self, tmp_path):
        """Counter-check: without it the test above would also pass on a
        media_ops that refuses everything."""
        from PIL import Image

        allowed = tmp_path / "ws"
        allowed.mkdir()
        Image.new("RGB", (4, 4), (1, 2, 3)).save(allowed / "ok.png")

        res = await self._media_server([allowed], tmp_path).call(
            "media_ops_load", {"path": str(allowed / "ok.png")})
        assert res["status"] == "success", res

    def test_file_ops_refusal_is_catchable_as_the_shared_error(self, tmp_path):
        """``SecurityError`` keeps its name for the model-facing error_type but
        must stay a ``PathSandboxDenied`` — otherwise a caller that handles the
        shared type silently misses file_ops."""
        from plugins.file_ops.security import PathValidator

        allowed = tmp_path / "ws"
        allowed.mkdir()
        with pytest.raises(PathSandboxDenied):
            PathValidator([str(allowed)], base=tmp_path).validate_path(
                str(tmp_path / "elsewhere.txt"))


@pytest.mark.skipif(sys.platform != "win32", reason="a UNC path is a host only on Windows")
@pytest.mark.parametrize("hostile", [r"\\evil.example\share\x.txt", "//evil.example/share/x.txt",
                                     r"\\?\UNC\evil.example\share\x.txt"])
def test_a_host_path_is_refused_before_anything_opens_it(tmp_path, monkeypatch, hostile):
    """Resolving a share path makes Windows connect and sign in (NTLM) -- before containment could refuse it."""
    touched = []
    real_resolve, real_stat = Path.resolve, Path.stat

    def spy_resolve(self, *args, **kwargs):
        touched.append(str(self))
        return real_resolve(self, *args, **kwargs)

    def spy_stat(self, *args, **kwargs):
        touched.append(str(self))
        return real_stat(self, *args, **kwargs)

    sandbox = PathSandbox.from_config([str(tmp_path)], base=tmp_path)
    monkeypatch.setattr(Path, "resolve", spy_resolve)
    monkeypatch.setattr(Path, "stat", spy_stat)
    with pytest.raises(PathSandboxDenied):
        sandbox.resolve(hostile)
    assert not [path for path in touched if "evil" in path]
