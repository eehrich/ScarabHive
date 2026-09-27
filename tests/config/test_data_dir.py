"""One setting moves the data directory: AGENT_DATA_DIR, else paths.data_dir.

Everything the system writes lived under a relative "data/..." -- in the
code, in config/*.yaml, in plugin schema defaults, in database rows and in
the paths prompts brief the model with. The rule under test: a relative path
whose first part is ``data`` lands in the configured directory, and with
nothing configured nothing changes, not even to an absolute path (tests that
chdir into a temporary directory rely on the relative form).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_system import paths
from agent_system.config.settings import load_settings
from agent_system.core.schema_base_mixin import config_defaults_from_schema
from agent_system.utils.path_sandbox import PathSandbox


@pytest.fixture(autouse=True)
def clean_data_dir(monkeypatch):
    """No data directory from the environment or an earlier load_settings.

    load_settings records paths.data_dir process-wide; a test that loads a
    config naming one must not leave it behind for the next test.
    """
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(paths, "_config_data_dir", None)
    # the root conftest settles it for every other test; these are about it
    monkeypatch.setattr(paths, "_settled", False)


def configure(monkeypatch, directory: Path | str) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(directory))


class TestNothingConfigured:
    def test_data_paths_stay_the_relative_literals_they_replace(self):
        assert paths.configured_data_dir() is None
        assert paths.data_path("writer", "books.db") == Path("data", "writer", "books.db")
        assert paths.resolve_data_path("data/writer/books.db") == Path("data/writer/books.db")

    def test_configuration_is_returned_untouched(self):
        config = {"a": "data/x", "b": ["data/y/"], "c": 3}
        assert paths.relocate_data_paths(config) is config


class TestConfigured:
    def test_a_data_path_lands_in_the_configured_directory(self, monkeypatch, tmp_path):
        configure(monkeypatch, tmp_path)
        assert paths.data_path("writer", "books.db") == tmp_path / "writer" / "books.db"
        assert paths.data_path() == tmp_path
        assert paths.resolve_data_path("data/writer/books.db") == tmp_path / "writer" / "books.db"
        assert paths.resolve_data_path("data") == tmp_path

    def test_other_paths_keep_what_they_say(self, monkeypatch, tmp_path):
        configure(monkeypatch, tmp_path)
        elsewhere = tmp_path.parent / "elsewhere.db"
        assert paths.resolve_data_path("config/config.yaml") == Path("config/config.yaml")
        assert paths.resolve_data_path("database/x") == Path("database/x")
        assert paths.resolve_data_path(elsewhere) == elsewhere

    def test_resolving_twice_changes_nothing(self, monkeypatch, tmp_path):
        """The loader moves configured values; the code that opens them may
        resolve again -- that must be harmless."""
        configure(monkeypatch, tmp_path)
        once = paths.resolve_data_path("data/writer/books.db")
        assert paths.resolve_data_path(once) == once

    def test_a_relative_setting_is_relative_to_the_project(self, monkeypatch):
        configure(monkeypatch, "var/agentdata")
        assert paths.configured_data_dir() == paths.PROJECT_ROOT / "var" / "agentdata"

    def test_the_environment_wins_over_the_configuration(self, monkeypatch, tmp_path):
        paths.set_config_data_dir(str(tmp_path / "from_config"))
        assert paths.configured_data_dir() == tmp_path / "from_config"
        configure(monkeypatch, tmp_path / "from_env")
        assert paths.configured_data_dir() == tmp_path / "from_env"


class TestRelocate:
    def test_only_data_strings_move(self, monkeypatch, tmp_path):
        configure(monkeypatch, tmp_path)
        moved = paths.relocate_data_paths({
            "db": "data/writer/books.db",
            "dirs": ["data/workspace", "src/plugins", "data"],
            "prose": "data/x\nis not a path",
            "near_miss": "database.db",
            "number": 7,
        })
        root = tmp_path.as_posix()
        assert moved == {
            "db": f"{root}/writer/books.db",
            "dirs": [f"{root}/workspace", "src/plugins", root],
            "prose": "data/x\nis not a path",
            "near_miss": "database.db",
            "number": 7,
        }

    def test_a_trailing_slash_survives(self, monkeypatch, tmp_path):
        """An allowlist entry "data/workspace/" must not start admitting
        workspace2 because the separator got lost."""
        configure(monkeypatch, tmp_path)
        assert paths.relocate_data_paths("data/workspace/") == f"{tmp_path.as_posix()}/workspace/"


class TestLoader:
    """Through load_settings, the path every process takes."""

    @staticmethod
    def write_config(tmp_path: Path, data_dir: str | None) -> Path:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        master = {"includes": ["plugins.yaml"],
                  "auth": {"database_path": "data/users.db"}}
        if data_dir is not None:
            master["paths"] = {"data_dir": data_dir}
        (config_dir / "config.yaml").write_text(yaml.safe_dump(master), encoding="utf-8")
        (config_dir / "plugins.yaml").write_text(yaml.safe_dump({"plugins": {"servers": {
            "probe": {"type": "file_ops", "enabled": False,
                      "allowed_directories": ["data/workspace/", "src"]},
        }}}), encoding="utf-8")
        return config_dir / "config.yaml"

    def test_paths_data_dir_moves_every_configured_data_path(self, tmp_path):
        target = tmp_path / "agentdata"
        cfg = load_settings(str(self.write_config(tmp_path, str(target))))

        assert paths.configured_data_dir() == target
        assert cfg.auth.database_path == f"{target.as_posix()}/users.db"
        assert cfg.plugins.servers["probe"].allowed_directories == [
            f"{target.as_posix()}/workspace/", "src"]
        # the setting itself is not read as a path inside itself
        assert cfg.paths.data_dir == str(target)

    def test_the_setting_is_not_read_as_a_path_inside_itself(self, tmp_path):
        """data_dir: data/elsewhere is itself a data/... string; moving it
        into the directory it names would double the tail."""
        cfg = load_settings(str(self.write_config(tmp_path, "data/elsewhere")))

        assert paths.configured_data_dir() == paths.PROJECT_ROOT / "data" / "elsewhere"
        assert cfg.paths.data_dir == "data/elsewhere"
        assert cfg.auth.database_path == (
            paths.PROJECT_ROOT / "data" / "elsewhere" / "users.db").as_posix()

    def test_a_later_load_does_not_move_the_data_directory(self, tmp_path):
        """A helper calling load_settings() bare reads the default config, not
        the one the process started with; a hot reload of a changed setting
        would move half the process. The first load decides."""
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        load_settings(str(self.write_config(first, str(tmp_path / "agentdata"))))
        cfg = load_settings(str(self.write_config(second, None)))

        assert paths.configured_data_dir() == tmp_path / "agentdata"
        assert cfg.auth.database_path == f"{(tmp_path / 'agentdata').as_posix()}/users.db"

    def test_the_master_config_is_read_when_nothing_loaded_it(self, monkeypatch, tmp_path):
        """A process that never calls load_settings (a writer CLI) reads
        paths.data_dir itself -- provisionally: a load after it still decides."""
        monkeypatch.setattr(paths, "_config_data_dir", paths._UNREAD)
        master = self.write_config(tmp_path, str(tmp_path / "from_master"))
        monkeypatch.setenv("AGENT_CONFIG_PATH", str(master))

        assert paths.data_path("x") == tmp_path / "from_master" / "x"

        other = tmp_path / "other"
        other.mkdir()
        load_settings(str(self.write_config(other, str(tmp_path / "from_load"))))
        assert paths.data_path("x") == tmp_path / "from_load" / "x"

    def test_without_the_setting_the_configuration_is_as_written(self, tmp_path):
        cfg = load_settings(str(self.write_config(tmp_path, None)))

        assert paths.configured_data_dir() is None
        assert cfg.auth.database_path == "data/users.db"
        assert cfg.plugins.servers["probe"].allowed_directories == ["data/workspace/", "src"]

    def test_the_real_configuration_moves_exactly_its_data_paths(self, monkeypatch, tmp_path):
        """The shipped config/*.yaml: every leaf that names data/ moves into
        the directory, and no other leaf changes."""
        def leaves(obj, prefix=""):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    yield from leaves(value, f"{prefix}.{key}")
            elif isinstance(obj, list):
                for index, value in enumerate(obj):
                    yield from leaves(value, f"{prefix}[{index}]")
            else:
                yield prefix, obj

        # the shipped config as written, even where it names a paths.data_dir
        monkeypatch.setattr(paths, "_settled", True)
        plain = dict(leaves(load_settings().model_dump()))
        configure(monkeypatch, tmp_path)
        moved = dict(leaves(load_settings().model_dump()))

        changed = {key for key in plain if plain[key] != moved[key]}
        names_data = {key for key, value in plain.items()
                      if isinstance(value, str) and paths._names_data(value)}
        assert names_data, "the shipped config names no data path -- this test measures nothing"
        assert changed == names_data
        root = tmp_path.as_posix()
        assert all(str(moved[key]).startswith(root) for key in changed)


class TestSchemaDefaults:
    def test_a_schema_default_under_data_moves_like_the_same_value_in_plugins_yaml(
            self, monkeypatch, tmp_path):
        configure(monkeypatch, tmp_path)
        defaults = config_defaults_from_schema({
            "storage_path": {"type": "string", "default": "data/context_engineer"},
            "limit": {"type": "integer", "default": 3},
        })
        assert defaults == {"storage_path": f"{tmp_path.as_posix()}/context_engineer", "limit": 3}


class TestModelPaths:
    """Prompts and skills brief the model with data/... paths."""

    def test_the_sandbox_resolves_a_data_path_into_the_data_directory(self, monkeypatch, tmp_path):
        """Root and request both written data/...: a root left behind would
        deny every request (media_ops' own default was ["data"])."""
        configure(monkeypatch, tmp_path / "agentdata")
        sandbox = PathSandbox.from_config(["data/workspace"], base=tmp_path)
        assert sandbox.resolve("data/workspace/notes.md") == (
            tmp_path / "agentdata" / "workspace" / "notes.md").resolve()

    def test_unconfigured_the_sandbox_resolves_against_its_base_as_before(self, tmp_path):
        sandbox = PathSandbox.from_config(["data/workspace"], base=tmp_path)
        assert sandbox.resolve("data/workspace/notes.md") == (
            tmp_path / "data" / "workspace" / "notes.md").resolve()


class TestStoredPaths:
    """Paths written before the move come back as data/... from files and rows."""

    def test_a_stored_media_path_is_found_in_the_data_directory(self, monkeypatch, tmp_path):
        from PIL import Image

        from agent_system.utils.multimodal_tool_content import encode_multimodal_item

        configure(monkeypatch, tmp_path / "agentdata")
        image = tmp_path / "agentdata" / "media" / "x.png"
        image.parent.mkdir(parents=True)
        Image.new("RGB", (4, 4)).save(image)
        monkeypatch.chdir(tmp_path)  # no old data/ next to the process

        encoded = encode_multimodal_item(
            {"type": "image", "path": "data/media/x.png", "mime_type": "image/png"})

        assert encoded is not None

    @pytest.fixture
    def moved_image(self, monkeypatch, tmp_path):
        """An image under the configured directory, stored as data/media/x.png,
        seen from a working directory without an old data/."""
        from PIL import Image

        configure(monkeypatch, tmp_path / "agentdata")
        image = tmp_path / "agentdata" / "media" / "x.png"
        image.parent.mkdir(parents=True)
        Image.new("RGB", (4, 4)).save(image)
        monkeypatch.chdir(tmp_path)
        return {"type": "image", "path": "data/media/x.png", "mime_type": "image/png"}

    @pytest.mark.parametrize("vision", [True, False])
    def test_the_request_sends_a_stored_media_path(self, moved_image, vision):
        """The path every client takes (create_multimodal_injection), with and
        without vision: neither may report the moved file as missing."""
        from types import SimpleNamespace

        from agent_system.utils.multimodal_tool_content import create_multimodal_injection

        injected = create_multimodal_injection(
            SimpleNamespace(name="t", tool_call_id="c", multimodal_content=[moved_image]),
            supports_vision=vision)

        assert injected is not None
        assert "not found" not in str(injected)
        if vision:
            assert "base64" in str(injected)

    def test_the_size_estimates_count_the_file_that_is_sent(self, moved_image):
        """Token and byte estimates read the same file the request encodes --
        at 0 the compaction thresholds would never see it."""
        from agent_system.llm.token_utils import estimate_file_tokens
        from plugins.context_engineer.compaction import LayeredCompactionStrategy

        assert estimate_file_tokens(moved_image["path"]) > 0
        assert LayeredCompactionStrategy._estimate_item_bytes(None, dict(moved_image)) > 0

    def test_a_stored_media_index_finds_its_moved_files(self, monkeypatch, tmp_path):
        """context_engineer's media store keeps data/... paths in its index."""
        from plugins.context_engineer.media_store import MediaStore

        configure(monkeypatch, tmp_path / "agentdata")
        store_dir = tmp_path / "agentdata" / "context_engineer" / "media"
        store_dir.mkdir(parents=True)
        (store_dir / "x.png").write_bytes(b"png")
        monkeypatch.chdir(tmp_path)
        store = MediaStore(store_dir)
        store._metadata["h"] = {"path": "data/context_engineer/media/x.png"}

        assert store.get_path("h") == str(store_dir / "x.png")

    def test_the_batch_store_follows_an_explicit_config_path(self, monkeypatch, tmp_path):
        """The batch initialisation passes Path(config.storage_path) --
        data/batch_jobs from llm.yaml, or the model's own default."""
        from agent_system.llm.batch.job_tracker import BatchJobTracker
        from agent_system.llm.batch.queue_manager import BatchQueueManager

        configure(monkeypatch, tmp_path / "agentdata")
        monkeypatch.chdir(tmp_path)

        tracker = BatchJobTracker(Path("data/batch_jobs"))
        manager = BatchQueueManager(storage_path=Path("data/batch_jobs"))

        assert tracker.storage_path == tmp_path / "agentdata" / "batch_jobs"
        assert manager.storage_path == tmp_path / "agentdata" / "batch_jobs"
        assert not (tmp_path / "data").exists()


class TestSessions:
    def test_the_sessions_directory_moves_with_the_data_directory(self, monkeypatch, tmp_path):
        from agent_system.core.session_presence import sessions_dir

        monkeypatch.delenv("AGENT_SESSION_STORAGE_PATH", raising=False)
        configure(monkeypatch, tmp_path)
        assert sessions_dir() == tmp_path / "sessions"

    def test_unconfigured_it_is_the_checkouts(self, monkeypatch):
        from agent_system.core.session_presence import sessions_dir

        monkeypatch.delenv("AGENT_SESSION_STORAGE_PATH", raising=False)
        assert sessions_dir() == paths.PROJECT_ROOT / "data" / "sessions"

    def test_agent_run_lists_the_sessions_every_other_process_uses(self, monkeypatch, tmp_path):
        """agent-run --list-sessions computed its own directory and ignored
        AGENT_SESSION_STORAGE_PATH, so it listed another store than the API."""
        import asyncio

        from agent_system import agent_run

        class Recorded(BaseException):
            """Past every `except Exception` of the run: stop right there."""

        seen = []

        def recorder(storage_path):
            seen.append(Path(storage_path))
            raise Recorded

        monkeypatch.setattr("agent_system.services.session_manager.SessionManager", recorder)
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "store"))
        with pytest.raises(Recorded):
            asyncio.run(agent_run.main_async("", list_sessions="all"))
        assert seen == [tmp_path / "store"]
