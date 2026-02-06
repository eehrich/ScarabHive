import importlib


def test_add_epic_write_creates_backlog_from_template(tmp_path):
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    if p.exists():
        p.unlink()
    rc = mod.main(["add-epic", "--title", "New Epic", "--file", str(p), "--write"])
    assert rc == 0
    assert p.exists()
    # verify backup folder created
    backups = (p.parent / ".backups")
    assert backups.exists()
    files = list(backups.glob(p.name + ".*.bak"))
    assert files
