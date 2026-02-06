import importlib
from pathlib import Path


def test_init_add_and_append_preserve_header(tmp_path):
    mod = importlib.import_module("scripts.backlog")
    tpl = Path(mod.__file__).parent / "backlog_tool" / "template.md"
    assert tpl.exists()

    p = tmp_path / "backlog.md"
    # init from template
    rc = mod.main(["init", "--file", str(p)])
    assert rc == 0
    # header should equal template header prefix until the Epics marker
    tpl_text = tpl.read_text(encoding="utf-8").splitlines()
    p_text = p.read_text(encoding="utf-8").splitlines()

    # find epics marker index in template
    def find_epics_idx(lines):
        for i, ln in enumerate(lines):
            if ln.strip().startswith("## 1. Epics - open"):
                return i
        return None

    tpl_idx = find_epics_idx(tpl_text)
    assert tpl_idx is not None

    # Compare header slices
    tpl_header = tpl_text[:tpl_idx]
    p_header = p_text[:tpl_idx]
    assert tpl_header == p_header

    # Now add an epic and a task and append notes to the task
    # create epic
    rc = mod.main(["add-epic", "--title", "Test Epic", "--write", "--file", str(p)])
    assert rc == 0
    # find newly created epic id by parsing file
    p.read_text(encoding="utf-8")
    # add task under epic 0000 (first available) - use add-task write
    rc = mod.main(["add-task", "--title", "Test Task", "--epic", "0000", "--write", "--file", str(p)])
    assert rc == 0

    # append notes to the task id 0000 (task id will be 0000 or 0001 depending on template)
    # find a task id from file: look for 'Task ' pattern
    import re
    m = re.search(r"Task\s+(\d{4}):", p.read_text(encoding="utf-8"))
    assert m
    task_id = m.group(1)

    rc = mod.main(["edit", task_id, "--set", "notes=appended note line", "--write", "--file", str(p)])
    assert rc == 0

    # After modifications, ensure header prefix still matches template header
    p_text2 = p.read_text(encoding="utf-8").splitlines()
    p_header2 = p_text2[:tpl_idx]
    assert tpl_header == p_header2
