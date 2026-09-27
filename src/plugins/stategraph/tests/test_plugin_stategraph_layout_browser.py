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


CHOICES = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<select id="event"><option value="approve">approve</option><option value="reject">reject</option></select>
<select id="bare"><option value="approve">approve</option><option value="reject">reject</option></select>
<div id="bar"><select id="runTo"><option value="a">a</option><option value="b">b</option></select>
<input id="forkStep" type="number"></div>
<script type="module">
import { keepingChoices } from '/src/plugins/stategraph/static/graph.js';
const $ = (id) => document.getElementById(id);
const options = '<option value="approve">approve (accepted now)</option><option value="reject">reject</option>';
$('event').value = 'reject';
keepingChoices($('event'), () => { $('event').innerHTML = options; return true; });
$('bare').value = 'reject';
$('bare').innerHTML = options;  // the browser without the helper: back to the first option
$('runTo').value = 'b';
$('forkStep').value = '7';
$('forkStep').focus();
keepingChoices($('bar'), () => { $('bar').innerHTML = $('bar').innerHTML.replace('>a<', '>a (here)<'); return true; });
fetch('/__results', { method: 'POST', body: JSON.stringify({ event: $('event').value, bare: $('bare').value,
  runTo: $('runTo').value, forkStep: $('forkStep').value, focused: document.activeElement.id }) });
</script></body></html>"""


def test_a_redraw_keeps_what_the_viewer_chose_and_typed():
    """A poll redraws the event select when a wait state marks its events "(accepted now)": without keepingChoices
    the browser puts the first event back in the viewer's choice, and "Send" sends that one."""
    page = REPO / "tmp" / f"sg_choices_{uuid.uuid4().hex}.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text(CHOICES, encoding="utf-8")
    try:
        seen = run_test_page(BROWSER, f"tmp/{page.name}", timeout=60)
    finally:
        page.unlink(missing_ok=True)

    assert seen["bare"] == "approve", "fixture: a redraw of the options resets a select in this browser"
    assert (seen["event"], seen["runTo"], seen["forkStep"], seen["focused"]) == ("reject", "b", "7", "forkStep"), seen
