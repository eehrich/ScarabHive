import importlib
from pathlib import Path


def test_fix_format_ids_only_preserves_header(tmp_path):
    mod = importlib.import_module('scripts.backlog')
    tpl = Path(mod.__file__).parent / 'backlog_tool' / 'template.md'
    assert tpl.exists()

    p = tmp_path / 'backlog.md'
    # init from template
    rc = mod.main(['init', '--file', str(p)])
    assert rc == 0

    # add an epic and a task with canonical writer
    rc = mod.main(['add-epic', '--title', 'IDS Epic', '--write', '--file', str(p)])
    assert rc == 0
    rc = mod.main(['add-task', '--title', 'IDS Task', '--epic', '0000', '--write', '--file', str(p)])
    assert rc == 0

    # Read file and perform a manual textual substitution to change an occurrence
    # of '0000' to a non-padded form '0' to mimic an original authoring style.
    txt = p.read_text(encoding='utf-8')
    # Replace the first occurrence of 'Epic 0000' and 'Task 0000' with non-padded forms
    txt = txt.replace('Epic 0000', 'Epic 0', 1)
    txt = txt.replace('Task 0000', 'Task 0', 1)
    p.write_text(txt, encoding='utf-8')

    # header prefix index in template
    tpl_text = tpl.read_text(encoding='utf-8').splitlines()
    def find_epics_idx(lines):
        for i, ln in enumerate(lines):
            if ln.strip().startswith('## 1. Epics - open'):
                return i
        return None
    tpl_idx = find_epics_idx(tpl_text)
    assert tpl_idx is not None
    tpl_header = tpl_text[:tpl_idx]

    # Run fix-format with ids-only which will do regex replacements on raw text
    rc = mod.main(['fix-format', '--write', '--ids-only', '--file', str(p)])
    assert rc == 0

    # After id-only replacements, header prefix should still match template header
    p_text2 = p.read_text(encoding='utf-8').splitlines()
    p_header2 = p_text2[:tpl_idx]
    assert tpl_header == p_header2
