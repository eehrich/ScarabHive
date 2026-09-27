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
<input id="forkStep" type="number"><input id="forkPause" type="checkbox"></div>
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
$('forkPause').checked = true;
$('forkStep').focus();
keepingChoices($('bar'), () => { $('bar').innerHTML = $('bar').innerHTML.replace('>a<', '>a (here)<'); return true; });
fetch('/__results', { method: 'POST', body: JSON.stringify({ event: $('event').value, bare: $('bare').value,
  runTo: $('runTo').value, forkStep: $('forkStep').value, forkPause: $('forkPause').checked,
  focused: document.activeElement.id }) });
</script></body></html>"""


TYPED = """<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root">
<form data-key="a"><input name="x" data-orig="1" value="1"><select name="kind" data-orig="agent"><option value="agent">agent</option><option value="tool">tool</option></select></form>
<form data-key="b"><input name="y" data-orig="5" value="5"><input name="w" data-orig="3" value="3"></form>
<form data-key="c"><input name="z" data-orig="9" value="9"></form>
<form data-key="d"><input name="guard" data-orig="" value=""><input name="target" data-orig="failed" value="failed"></form></div>
<script type="module">
import { putTyped, typedIn } from '/src/plugins/stategraph/static/graph.js';
const root = document.getElementById('root');
const key = (form) => form.dataset.key;
const at = (name) => root.querySelector(`[name="${name}"]`);
at('x').value = '2';
at('kind').value = 'tool';
at('y').value = '6';
at('z').value = '10';
at('guard').value = 'x > 1';
const typed = typedIn(root, new Set(['a', 'b', 'd']), key);
root.innerHTML = root.innerHTML.replace('name="y" data-orig="5" value="5"', 'name="y" data-orig="7" value="7"')
  .replace('name="w" data-orig="3" value="3"', 'name="w" data-orig="4" value="4"')  // w untyped: nothing to give back
  .replace('data-orig="failed" value="failed"', 'data-orig="review" value="review"');  // another transition under d now
const { restored, dropped } = putTyped(root, typed, key);
fetch('/__results', { method: 'POST', body: JSON.stringify({ x: at('x').value, kind: at('kind').value, y: at('y').value,
  z: at('z').value, guard: at('guard').value, restored: [...restored], dropped }) });
</script></body></html>"""


def test_an_apply_gives_the_other_forms_what_was_typed_into_them_unless_the_edit_changed_it():
    """An edit redraws the inspector: what the other forms held comes back where the field still shows what it
    showed; a field the edit changed underneath shows the new value, and says so."""
    page = REPO / "tmp" / f"sg_typed_{uuid.uuid4().hex}.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text(TYPED, encoding="utf-8")
    try:
        seen = run_test_page(BROWSER, f"tmp/{page.name}", timeout=60)
    finally:
        page.unlink(missing_ok=True)

    assert seen == {"x": "2", "kind": "tool", "y": "7", "z": "9", "guard": "", "restored": ["a"],
                    "dropped": ["b: y", "d: guard"]}, seen


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
    assert (seen["event"], seen["runTo"], seen["forkStep"], seen["forkPause"], seen["focused"]) == (
        "reject", "b", "7", True, "forkStep"), seen
