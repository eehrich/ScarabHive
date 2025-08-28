import importlib
import sys


def test_backlog_version_and_help(capfd):
    mod = importlib.import_module("scripts.backlog")
    # test version
    rc = mod.main(["--version"])
    assert rc == 0
    out, err = capfd.readouterr()
    assert out.strip() != ""


def test_backlog_validate_missing_temp(tmp_path, capfd):
    mod = importlib.import_module("scripts.backlog")
    # point to a non-existent file
    rc = mod.main(["validate", "--file", str(tmp_path / "nope.md")])
    assert rc != 0
    out, err = capfd.readouterr()
    assert "not found" in err


def test_backlog_add_task_dryrun(capfd):
    mod = importlib.import_module("scripts.backlog")
    rc = mod.main(["add-task", "--title", "Example task", "--notes", "line1\nline2"]) 
    assert rc == 0
    out, err = capfd.readouterr()
    assert "Dry-run: task entry to insert:" in out
