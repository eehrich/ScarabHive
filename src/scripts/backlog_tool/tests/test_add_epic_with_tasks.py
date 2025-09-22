import json
import os
import sys
from pathlib import Path

from scripts.backlog_tool.commands import add as add_cmd

# Ensure package import works when running tests directly
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))


def test_add_epic_with_tasks_bulk(tmp_path):
    # Prepare a minimal backlog file
    backlog_file = tmp_path / "backlog.md"
    tpl = os.path.join(os.path.dirname(__file__), '..', 'template.md')
    with open(tpl, 'r', encoding='utf-8') as f:
        tpl_text = f.read()
    backlog_file.write_text(tpl_text, encoding='utf-8')

    # Create JSON payload with an epic and tasks
    payload = [
        {
            "title": "Test Bulk Epic",
            "tasks": [
                {"title": "Task A", "description": "Desc A"},
                {"title": "Task B", "description": "Desc B"}
            ]
        }
    ]

    jf = tmp_path / "payload.json"
    jf.write_text(json.dumps(payload), encoding='utf-8')

    # Build argparse-like namespace
    class Args:
        from_file = str(jf)
        file = str(backlog_file)
        write = True
        backup_dir = None
        max_backups = None

    args = Args()

    # Run the bulk add epic command
    rc = add_cmd._cmd_add_epic_bulk(args)
    assert rc == 0

    # Read the resulting backlog and assert epic and tasks present
    lines = backlog_file.read_text(encoding='utf-8')
    assert "Test Bulk Epic" in lines
    assert "Task A" in lines
    assert "Task B" in lines
