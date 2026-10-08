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
<div id="bar"><select id="runToState"><option value="a">a</option><option value="b">b</option></select>
<select data-frame-choice><option value="x">x</option><option value="y">y</option></select></div>
<script type="module">
import { keepingChoices } from '/src/plugins/stategraph/static/graph.js';
const $ = (id) => document.getElementById(id);
const options = '<option value="approve">approve (accepted now)</option><option value="reject">reject</option>';
$('event').value = 'reject';
keepingChoices($('event'), () => { $('event').innerHTML = options; return true; });
$('bare').value = 'reject';
$('bare').innerHTML = options;  // the browser without the helper: back to the first option
$('runToState').value = 'b';
document.querySelector('[data-frame-choice]').value = 'y';
$('runToState').focus();
keepingChoices($('bar'), () => { $('bar').innerHTML = $('bar').innerHTML.replace('>a<', '>a (here)<'); return true; });
fetch('/__results', { method: 'POST', body: JSON.stringify({ event: $('event').value, bare: $('bare').value,
  runTo: $('runToState').value, frame: document.querySelector('[data-frame-choice]').value,
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


BENDS = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/kit/kit.css">
<link rel="stylesheet" href="/src/plugins/stategraph/static/panel.css">
</head><body style="margin:0"><svg id="canvas" style="width:800px;height:500px;display:block"></svg>
<script type="module">
import { Canvas } from '/src/plugins/stategraph/static/graph.js';
const svg = document.getElementById('canvas');
svg.setPointerCapture = () => {};  // a dispatched pointer is no active one: the browser would refuse to capture it
const routes = [];
const positions = { a: { x: 40, y: 40 }, b: { x: 440, y: 300 } };
let lines = {};
const canvas = new Canvas(svg, { onRoute: (way, line) => {  // as the panel saves it: the canvas draws it anew at once
  routes.push([way, line]);
  lines = { ...lines, [way]: line };
  canvas.setLayout({ positions, lines });
} });
const state = (name) => ({ name, parent: null, type: 'state', composite: false, kind: null, label: '', icon: null });
await canvas.setGraph({ states: [state('a'), state('b')], notes: [],
  transitions: [{ id: 'a#0', source: 'a', index: 0, target: 'b', trigger: 'done', guard: null, effect: null }] },
  { positions });
canvas.select({ kind: 'transition', id: 'a#0' });
const handles = () => [...svg.querySelectorAll('.sg-bend')];
const middle = handles()[1];
const box = middle.getBoundingClientRect();
const [x, y] = [box.left + box.width / 2, box.top + box.height / 2];
const on = document.elementFromPoint(x, y);
const cursor = getComputedStyle(middle).cursor;
const pointer = (type, target, px, py) => target.dispatchEvent(new PointerEvent(type, { bubbles: true, clientX: px, clientY: py, button: 0, pointerId: 1 }));
pointer('pointerdown', middle, x, y);
pointer('pointermove', svg, x + 30, y);
pointer('pointerup', svg, x + 30, y);
const moved = handles()[1].getBoundingClientRect().left - box.left;
handles()[1].focus();
handles()[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
const focusedBend = () => (document.activeElement?.classList?.contains('sg-bend') ? document.activeElement.dataset.bend : null);
const focused = focusedBend();
const keyed = routes.length === 2 ? routes[1][1].at[1] - routes[0][1].at[1] : null;
const seen = routes.map(([way, line]) => [way, line.start, line.at.length]);
const before = document.activeElement;
canvas.setOverlay({});  // a run's poll: it leaves the handles as they are
const polled = document.activeElement === before && before.isConnected ? focusedBend() : null;
// the third of five segments moved into line with the first: the second goes with it, the keyboard stays on the line
// the moved one went into -- the boxes' centres lie 260 apart, so -138 is 8 above the first
lines = { 'a→b': { start: 'x', at: [0, -60, -138, 60, 0] } };
canvas.setLayout({ positions, lines });
handles()[2].focus();
handles()[2].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
const merged = [handles().length, focusedBend(), routes[routes.length - 1][1].at];
// drawn anew out of view: the handle keeps the keyboard, and the page is not scrolled back to it
document.body.style.height = '3000px';
handles()[0].focus();
window.scrollTo(0, 2000);
const top = window.scrollY;
canvas.setLayout({ positions, lines });
const scrolled = [top > 0 && window.scrollY === top, focusedBend()];
fetch('/__results', { method: 'POST', body: JSON.stringify({ handles: handles().length, onTop: on === middle, cursor, moved,
  routes: seen, keyed, focused, polled, merged, scrolled }) });
</script></body></html>"""


def test_a_selected_line_bends_under_a_real_pointer_and_keyboard():
    """The handles lie over the states and lines, say which way they move, follow the pointer, and the one moved by
    the arrow keys keeps the focus though the canvas draws them anew -- after a run's poll too, and on the segment it
    went into when it took out a bend."""
    page = REPO / "tmp" / f"sg_bends_{uuid.uuid4().hex}.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text(BENDS, encoding="utf-8")
    try:
        seen = run_test_page(BROWSER, f"tmp/{page.name}", timeout=60)
    finally:
        page.unlink(missing_ok=True)

    assert seen["onTop"] and seen["cursor"] == "ew-resize", seen
    assert seen["moved"] == 30, seen
    assert seen["routes"] == [["a→b", "x", 3], ["a→b", "x", 3]] and seen["keyed"] == 8 and seen["focused"] == "1", seen
    assert seen["polled"] == "1", seen
    assert seen["merged"] == [3, "0", [0, 60, 0]], seen
    assert seen["scrolled"] == [True, "0"], seen


FRAMED_PANEL = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/kit/kit.css">
<link rel="stylesheet" href="/src/plugins/stategraph/static/panel.css">
</head><body style="margin:0"><svg id="canvas" style="width:800px;height:500px;display:block"></svg>
<script type="module">
import { Canvas } from '/src/plugins/stategraph/static/graph.js';
const svg = document.getElementById('canvas');
const layout = { positions: { a: { x: 40, y: 40 }, b: { x: 440, y: 300 } } };
const canvas = new Canvas(svg, { onRoute: () => {} });
const state = (name) => ({ name, parent: null, type: 'state', composite: false, kind: null, label: '', icon: null });
await canvas.setGraph({ states: [state('a'), state('b')], notes: [],
  transitions: [{ id: 'a#0', source: 'a', index: 0, target: 'b', trigger: 'done', guard: null, effect: null }] }, layout);
canvas.select({ kind: 'transition', id: 'a#0' });
window.bends = {
  focus: (i) => svg.querySelectorAll('.sg-bend')[i].focus(),
  redraw: () => canvas.setLayout(layout),
  focused: () => (document.activeElement?.classList?.contains('sg-bend') ? document.activeElement.dataset.bend : null),
};
parent.postMessage('ready', '*');
</script></body></html>"""

FRAMED_SHELL = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<input id="shell"><iframe id="panel" src="%s" style="width:820px;height:520px"></iframe>
<script>
addEventListener('message', () => {
  const frame = document.getElementById('panel');
  const panel = frame.contentWindow;
  panel.bends.focus(0);
  const entered = document.activeElement === frame;
  panel.document.hasFocus = () => false;  // the browser in the background: the shell's focus still on the panel
  panel.bends.redraw();
  const here = panel.bends.focused();
  document.getElementById('shell').focus();
  const held = panel.bends.focused();  // the handle the panel still holds as its focus
  panel.bends.redraw();
  fetch('/__results', { method: 'POST', body: JSON.stringify({ entered, here, held, away: panel.bends.focused(),
    shell: document.activeElement.id }) });
});
</script></body></html>"""


def test_a_redraw_takes_the_keyboard_back_only_where_the_viewer_left_it():
    """A panel lies in a frame of the shell: a line drawn anew gives its handle the keyboard back while the shell's
    focus is on that frame (the browser may be in the background), not when the viewer is in the shell -- the panel
    lets go of its focus then, and the redraw has none to give back."""
    name = uuid.uuid4().hex
    panel = REPO / "tmp" / f"sg_framed_panel_{name}.html"
    shell = REPO / "tmp" / f"sg_framed_shell_{name}.html"
    panel.parent.mkdir(exist_ok=True)
    panel.write_text(FRAMED_PANEL, encoding="utf-8")
    shell.write_text(FRAMED_SHELL % panel.name, encoding="utf-8")
    try:
        seen = run_test_page(BROWSER, f"tmp/{shell.name}", timeout=60)
    finally:
        panel.unlink(missing_ok=True)
        shell.unlink(missing_ok=True)

    assert seen["entered"] and seen["here"] == "0", seen
    assert seen["held"] is None, seen
    assert seen["away"] is None and seen["shell"] == "shell", seen


def test_a_redraw_keeps_what_the_viewer_chose():
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
    # the debug bar's run-to select keeps the choice and the focus; its frame picker has no id and shows the frame
    # the canvas shows, not one chosen before (panel.js drawDebugBar)
    assert (seen["event"], seen["runTo"], seen["frame"], seen["focused"]) == ("reject", "b", "x", "runToState"), seen
