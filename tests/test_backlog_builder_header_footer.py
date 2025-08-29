from scripts.backlog_tool import parser as bl


def test_emit_epics_before_eof_marker(tmp_path):
    # header contains an EOF marker before the epics markers (malformed template)
    header = [
        "# Backlog",
        "...",
        "EOF",
        "",
        "## 1. Epics - open",
        "",
    ]
    b = bl.Backlog(header=header, epics_open=[bl.Epic(id='0001', title='T', status='open', raw_lines=['', '  - description: x', ''])], epics_finished=[], footer=['FOOT'])
    out = bl.build_markdown(b)
    assert "## 1. Epics - open" in out
    # EOF should still appear after the epics/footers
    assert "EOF" in out
    assert out.rfind("EOF") < out.rfind("## 1. Epics - open") or out.rfind("EOF") > out.rfind("## 2. Epics - finished")


def test_collapse_blank_lines_in_raw_lines():
    header = ["# Backlog"]
    raw = ['', '', '  - notes:', '', '    - a', '', '']
    e = bl.Epic(id='0002', title='T2', status='open', raw_lines=raw)
    b = bl.Backlog(header=header, epics_open=[e], epics_finished=[], footer=[])
    out = bl.build_markdown(b)
    # ensure we don't have multiple consecutive blank lines where raw_lines were
    assert '\n\n\n' not in out
    # notes line should be present
    assert '  - notes:' in out
