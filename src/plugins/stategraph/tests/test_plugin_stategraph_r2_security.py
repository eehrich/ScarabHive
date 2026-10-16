"""Second security review: a reader who may not save machines gets no absolute server path from get_machine (API-3)
-- not in a problem's ``file`` and not inside its message, for an import that is unreadable, missing or a cycle."""
from __future__ import annotations

import json

from plugins.stategraph.tests.test_plugin_stategraph_r15_api_tools import server  # noqa: F401
from plugins.stategraph.tests.test_plugin_stategraph_server import _enable_auth, run_tool

LOST = """\
stategraph: 1
id: lost
imports: {bad: gone, nowhere: ./missing.yaml, loop: cyc}
initial: a
states:
  a: {type: final}
"""

CYC = """\
stategraph: 1
id: cyc
imports: {back: lost}
initial: a
states:
  a: {type: final}
"""


async def test_a_reader_sees_import_problems_by_file_name_only(server, monkeypatch, tmp_path):
    (tmp_path / "machines" / "lost.yaml").write_text(LOST, encoding="utf-8")
    (tmp_path / "machines" / "cyc.yaml").write_text(CYC, encoding="utf-8")
    (tmp_path / "machines" / "gone.yaml").write_bytes(b"stategraph: 1\nid: gone\n# \xff\n")
    _enable_auth(server, monkeypatch, {"alice": "USER", "root": "ADMIN"})
    got, _ = await run_tool(server, "stategraph_get_machine", {"machine_id": "lost", "_user_id": "alice"})
    messages = [p["message"] for p in got["problems"]]
    assert any("gone.yaml is not UTF-8" in m for m in messages), messages
    assert any("no such file next to lost.yaml" in m for m in messages), messages
    assert any("import cycle: lost.yaml -> cyc.yaml -> lost.yaml" in m for m in messages), messages
    text = json.dumps(got)
    for root in (str(tmp_path), str(tmp_path).replace("\\", "/"), str(tmp_path).replace("\\", "\\\\")):
        assert root not in text, text
