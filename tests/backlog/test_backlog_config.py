import pytest
import tempfile
from pathlib import Path
from scripts.backlog import load_config


class TestBacklogConfiguration:
    """Test .backlogrc configuration file support."""

    def test_load_config_no_file(self, tmp_path, monkeypatch):
        """Test load_config when no .backlogrc file exists."""
        monkeypatch.chdir(tmp_path)
        config = load_config()
        assert config == {}

    def test_load_config_basic_settings(self, tmp_path, monkeypatch):
        """Test loading basic configuration settings."""
        # Create a .backlogrc file
        config_file = tmp_path / ".backlogrc"
        config_file.write_text("""[backlog]
default_file = custom_backlog.md
default_color = true
backup_dir = /tmp/backups
max_backups = 10
""")

        # Mock Path.cwd to return our tmp_path
        mock_cwd = lambda: tmp_path
        monkeypatch.setattr(Path, 'cwd', mock_cwd)

        config = load_config()
        assert config['file'] == 'custom_backlog.md'
        assert config['color'] == True
        assert config['backup_dir'] == '/tmp/backups'
        assert config['max_backups'] == 10

    def test_load_config_color_false(self, tmp_path, monkeypatch):
        """Test loading color configuration set to false."""
        # Create a .backlogrc file
        config_file = tmp_path / ".backlogrc"
        config_file.write_text("""[backlog]
default_color = false
""")

        # Mock Path.cwd to return our tmp_path
        mock_cwd = lambda: tmp_path
        monkeypatch.setattr(Path, 'cwd', mock_cwd)

        config = load_config()
        assert config['color'] == False

    def test_load_config_color_auto(self, tmp_path, monkeypatch):
        """Test loading color configuration set to auto."""
        # Create a .backlogrc file
        config_file = tmp_path / ".backlogrc"
        config_file.write_text("""[backlog]
default_color = auto
""")

        # Mock Path.cwd to return our tmp_path
        mock_cwd = lambda: tmp_path
        monkeypatch.setattr(Path, 'cwd', mock_cwd)

        config = load_config()
        assert config['color'] == 'auto'

    def test_load_config_invalid_max_backups(self, tmp_path, monkeypatch):
        """Test loading invalid max_backups value."""
        # Create a .backlogrc file
        config_file = tmp_path / ".backlogrc"
        config_file.write_text("""[backlog]
max_backups = invalid
""")

        # Mock Path.cwd to return our tmp_path
        mock_cwd = lambda: tmp_path
        monkeypatch.setattr(Path, 'cwd', mock_cwd)

        config = load_config()
        # Should be kept as string since int conversion fails
        assert config['max_backups'] == 'invalid'

    def test_load_config_home_directory(self, tmp_path, monkeypatch):
        """Test loading config from home directory when cwd doesn't have one."""
        # Mock home directory
        home_dir = tmp_path / "home"
        home_dir.mkdir()

        # Create config in home directory
        home_config = home_dir / ".backlogrc"
        home_config.write_text("""[backlog]
default_file = home_backlog.md
""")

        # Change to a different directory without config
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        monkeypatch.chdir(work_dir)

        # Mock Path.home() to return our test home directory
        monkeypatch.setattr(Path, 'home', lambda: home_dir)

        config = load_config()
        assert config['file'] == 'home_backlog.md'

    def test_load_config_cwd_precedence(self, tmp_path, monkeypatch):
        """Test that current directory config takes precedence over home."""
        home_dir = tmp_path / "home"
        home_dir.mkdir()

        # Create config in home directory
        home_config = home_dir / ".backlogrc"
        home_config.write_text("""[backlog]
default_file = home_backlog.md
""")

        # Create config in current directory
        cwd_config = tmp_path / ".backlogrc"
        cwd_config.write_text("""[backlog]
default_file = cwd_backlog.md
""")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, 'home', lambda: home_dir)

        config = load_config()
        assert config['file'] == 'cwd_backlog.md'  # CWD should take precedence

    def test_load_config_malformed_file(self, tmp_path, monkeypatch):
        """Test handling of malformed config file."""
        monkeypatch.chdir(tmp_path)

        config_file = tmp_path / ".backlogrc"
        config_file.write_text("This is not a valid INI file")

        # Should not crash, should return empty config
        config = load_config()
        assert config == {}

    def test_load_config_empty_section(self, tmp_path, monkeypatch):
        """Test config file with empty backlog section."""
        monkeypatch.chdir(tmp_path)

        config_file = tmp_path / ".backlogrc"
        config_file.write_text("""[backlog]
""")

        config = load_config()
        assert config == {}

    def test_load_config_no_backlog_section(self, tmp_path, monkeypatch):
        """Test config file without backlog section."""
        monkeypatch.chdir(tmp_path)

        config_file = tmp_path / ".backlogrc"
        config_file.write_text("""[other_section]
setting = value
""")

        config = load_config()
        assert config == {}
