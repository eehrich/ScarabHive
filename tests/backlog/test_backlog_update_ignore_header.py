import importlib

SAMPLE = """
# Backlog

### Documentation and Guidelines

- ☐ Task/Epic <uniqid 4 digits>: <title>
  - status: <status> (mandatory)

## 1. Epics - open

- ☐ Epic 0001: Sample
  - status: open
  - tasks:
    - ☐ Task 0001: Done task
      - status: done
      - added: 2025-08-01
      - closed: 2025-08-02

## 2. Epics - finished

"""


def test_update_ignores_header_template(tmp_path):
    p = tmp_path / "b.md"
    p.write_text(SAMPLE, encoding="utf-8")
    mod = importlib.import_module("scripts.backlog")
    # Should run update and move the finished epic (none in this simple sample)
    # but must not fail because of the placeholder in the documentation header.
    rc = mod.main(["update", "--file", str(p)])
    assert rc == 0
    # validate that the file still exists and contains the Epics headers
    text = p.read_text(encoding="utf-8")
    assert "## 1. Epics - open" in text
    assert "## 2. Epics - finished" in text
