from pathlib import Path
from scripts.backlog_tool import parser as bl


def test_backup_and_restore(tmp_path):
    p = tmp_path / 'b.md'
    p.write_text('# Backlog\n', encoding='utf-8')
    # create initial backup
    bak = bl.make_backup(str(p))
    assert Path(bak).exists()
    # modify file and then restore
    p.write_text('# Backlog\nmodified\n', encoding='utf-8')
    bl.restore_backup(str(p), bak)
    txt = p.read_text(encoding='utf-8')
    assert 'modified' not in txt
