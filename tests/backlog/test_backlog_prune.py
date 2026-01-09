import os
import time

from scripts.backlog_tool import parser as bl


def make_backups_for(path, count=5, base_time=None):
    p = os.path.abspath(path)
    d = os.path.dirname(p)
    backups_dir = os.path.join(d, '.backups')
    os.makedirs(backups_dir, exist_ok=True)
    files = []
    if base_time is None:
        base_time = time.time() - 10000
    for i in range(count):
        ts = time.strftime('%Y%m%d_%H%M%S', time.gmtime(base_time + i))
        fn = os.path.join(backups_dir, f"{os.path.basename(p)}.{ts}.bak")
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(f'backup {i}')
        # set mtime explicitly
        os.utime(fn, (base_time + i, base_time + i))
        files.append(fn)
    return files


def test_prune_keep(tmp_path):
    p = tmp_path / 'backlog.md'
    p.write_text('# test backlog')
    make_backups_for(str(p), count=5)
    # keep 2 newest -> expect 3 removed
    removed = bl.prune_backups(str(p), keep=2)
    assert len(removed) == 3
    remaining = bl.list_backups(str(p))
    assert len(remaining) == 2


def test_prune_older_than(tmp_path):
    p = tmp_path / 'backlog.md'
    p.write_text('# test backlog')
    # create some very old backups and some recent ones
    old_base = time.time() - (60 * 60 * 24 * 40)  # 40 days ago
    recent_base = time.time() - (60 * 60 * 24 * 2)  # 2 days ago
    old_files = make_backups_for(str(p), count=2, base_time=old_base)
    recent_files = make_backups_for(str(p), count=3, base_time=recent_base)
    # remove older than 30 days
    removed = bl.prune_backups(str(p), older_than_days=30)
    # the two old files should be removed
    assert set(removed) >= set(old_files)
    bl.list_backups(str(p))
    assert all(f not in removed for f in recent_files)