import importlib
from pathlib import Path


def test_init_creates_backlog_from_template(tmp_path):
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    # ensure missing
    if p.exists():
        p.unlink()
    rc = mod.main(["init", "--file", str(p)])
    assert rc == 0
    assert p.exists()
    # compare with bundled template
    tpl = Path(mod.__file__).parent / "backlog_tool" / "template.md"
    assert tpl.exists()
    assert p.read_text(encoding="utf-8") == tpl.read_text(encoding="utf-8")


def test_init_fails_if_exists(tmp_path):
    mod = importlib.import_module("scripts.backlog")
    p = tmp_path / "backlog.md"
    p.write_text("existing", encoding="utf-8")
    rc = mod.main(["init", "--file", str(p)])
    # returns 1 when file already exists
    assert rc == 1
