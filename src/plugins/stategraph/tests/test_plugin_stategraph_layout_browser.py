"""The panel's layout in a real browser: what the fake DOM of the JS tests cannot measure.

The markup comes from the real template (its Jinja expressions dropped), styled by the real kit.css and panel.css.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from tests.ui.browser import find_browser, run_test_page

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed")

PLUGIN = Path(__file__).resolve().parent.parent
REPO = PLUGIN.parents[2]

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/kit/kit.css">
<link rel="stylesheet" href="/src/plugins/stategraph/static/panel.css">
</head><body><div class="sg-side" style="width: 320px"><div class="sg-side-body">%s</div></div>
<script>
const form = document.getElementById('eventForm');
form.elements.name.innerHTML = '<option>approve (accepted now)</option><option>reject</option>';
const width = (el) => Math.round(el.getBoundingClientRect().width);
fetch('/__results', { method: 'POST', body: JSON.stringify({ name: width(form.elements.name), frame: width(form.elements.frame) }) });
</script></body></html>"""


def test_the_event_name_is_not_squeezed_to_its_arrow_by_the_frame_select():
    """A waiting run's events are chosen in a select beside the frame's: the kit makes selects 100% wide."""
    template = (PLUGIN / "templates" / "panel.html").read_text(encoding="utf-8")
    section = re.search(r'<div class="sg-section" id="eventSection" hidden>.*?</form>\s*</div>', template, re.S).group(0)
    section = re.sub(r"\{\{.*?\}\}", "", section).replace(" hidden>", ">", 1)
    page = REPO / "tmp" / f"sg_layout_{uuid.uuid4().hex}.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text(PAGE % section, encoding="utf-8")
    try:
        widths = run_test_page(BROWSER, f"tmp/{page.name}", timeout=60)
    finally:
        page.unlink(missing_ok=True)

    assert widths["name"] > 120 and widths["name"] > widths["frame"], widths
