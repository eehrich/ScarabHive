// Single cases of the panel in JavaScriptCore, each in a jsc of its own (the panel's state is module state):
// case.js names the case, test_plugin_stategraph_js.py writes it and runs this file once per case. The panel runs
// against fake_kit_held.js (the real kit's timing: answers later or when released, `latest` and abandon() abort) and
// fake_dom.js, without ELK (the canvas falls back to its grid). Prints "PASS <case>" or "FAIL <case>: ...".
import { CASE } from './case.js';
import { FIELDS, HOOKS, MACHINE, RUN, RUNS, KINDS } from './fixtures.js';
import { ApiError } from './fake_kit.js';

load('./fake_dom.js');
// the cases edit the file at once, as with auto-save; those without it take the setting back before they boot
localStorage.setItem('stategraph:autosave', 'true');
// as panel.html draws it: a start error shows only once there is one (the start form's fold reads it)
document.getElementById('startError').hidden = true;

globalThis.RENDERS = []; globalThis.ICONS = new Set(); globalThis.TOASTS = []; globalThis.CALLS = []; globalThis.ASKED = [];
globalThis.TABS = {}; globalThis.ANSWERS = { prompt: [], confirm: true, dialog: null };
const OTHER = { ...MACHINE, id: 'other', versions: { 'review.yaml': 'v9', 'review.py': 'p9' } };
const READ_ONLY = { ...MACHINE, id: 'ro', writable: false };
// a machine without a companion module, and the same after a save that added one
const PLAIN_TEXT = 'stategraph: 1\nid: plain\ntitle: Plain\ninitial: a\nstates:\n  a: {type: final}\n';
const PLAIN = { ...MACHINE, id: 'plain', root_file: 'plain.yaml', files: { 'plain.yaml': PLAIN_TEXT }, versions: { 'plain.yaml': 'v1' } };
let plainAnswer = PLAIN;
let fieldsAnswer = FIELDS;
const plainErrors = [];  // answers of the next PUTs of plain, before the good one
const EMPTY = { ...MACHINE, id: 'empty', graph: { ...MACHINE.graph, initial: null, states: [], transitions: [] },
  problems: [{ level: 'error', code: 'SG001', message: 'a machine needs at least one state', path: 'states', file: '/m/empty.yaml', line: 1 }] };
// a second run of the machine, ended: r1 is the paused one
const RUN2 = { ...RUN, id: 'r2', status: 'succeeded', active: false, final_state: 'done',
  debug: { ...RUN.debug, paused: null, breakpoints: [{ state: 'read', at: 'enter', machine: null, condition: null, enabled: true, id: 'b9' }] } };
const JOURNAL2 = [
  { seq: 3, kind: 'activity', key: 's1', state: 'write', status: 'done',
    data: { kind: 'agent', path: 'write', out: 'the whole draft, not only its first line', meta: { agent: 'scene_writer', instance_id: 'inst-1', duration_s: 12.5 } } },
  { seq: 5, kind: 'activity', key: 's2', state: 'read', status: 'error',
    data: { kind: 'machine', path: 'review/read', error: { type: 'timeout', message: 'slow' }, meta: {} } },
  { seq: 8, kind: 'trace', key: 's3:final:verdict', state: 'verdict', status: 'final', data: { frame: 's2/m/', machine: 'critique', status: 'failed' } },
  { seq: 9, kind: 'trace', key: 's4:final:done', state: 'done', status: 'final', data: { frame: '', status: 'succeeded' } },
];
let journalOf = { r1: RUN.journal, r2: JOURNAL2, r3: [] };
// a third run of the machine, running beside r1
const RUN3 = { ...RUN, id: 'r3', status: 'running', debug: { ...RUN.debug, paused: null } };
const CATALOG = { agents: [{ name: 'scene_writer', description: 'Writes one scene' }], tools: [{ name: 'store_put', description: 'Store a value' }],
  profiles: ['fast'] };
let editAnswer = null;
// review and loop, both composites; loop holds one state, inner
const stateLike = (name, from, fields) => ({ ...MACHINE.graph.states.find((s) => s.name === from), name, ...fields });
const TWO_COMPOSITES = { ...MACHINE, graph: { ...MACHINE.graph, states: [...MACHINE.graph.states,
  stateLike('loop', 'review', { parent: null, initial: 'inner', line: 90 }),
  stateLike('inner', 'verdict', { parent: 'loop', line: 93 })] } };
let reviewAnswer = MACHINE;  // GET of the review machine
const WITH_NOTE = { ...MACHINE, graph: { ...MACHINE.graph, notes: [{ name: 'why', text: 'Because the review\nneeds a second look.\n' }] } };  // a | block: a line break at the end
let layoutsKept = false;  // a layout PUT to review changes what its GET answers, as the server's does
let runAnswer = RUN;
let controlAnswer = null;  // r1's control answer, when it is not runAnswer
let runsAnswer = null;  // (query) -> the runs list, when not the two runs
let catalogDown = null;  // (path) -> true: the catalog answers 503
let startRefused = null;  // the answer to a run's start, when it is refused
globalThis.SERVER = (method, path, json) => {
  const p = path.replace('/plugins/stategraph/api', '');
  if (p === '/kinds') return KINDS;
  if (p.split('?')[0] === '/catalog') return catalogDown?.(p) ? new ApiError(503, 'restarting') : CATALOG;
  if (p === '/machines' && method === 'GET') {
    const groups = { review: 'Writer/v6', other: 'Writer', ro: 'stategraph' };
    return ['review', 'other', 'hooks', 'ro', 'empty', 'plain', 'fields'].map((id) => ({ id, title: id, errors: 0, warnings: 0, writable: id !== 'ro',
      group: groups[id] || 'My machines' }));
  }
  if (p === '/machines/review' && method === 'GET') return reviewAnswer;
  if (p === '/machines/other' && method === 'GET') return OTHER;
  if (p === '/machines/hooks' && method === 'GET') return HOOKS;
  if (p === '/machines/ro' && method === 'GET') return READ_ONLY;
  if (p === '/machines/empty' && method === 'GET') return EMPTY;
  if (p === '/machines/plain' && method === 'GET') return plainAnswer;
  if (p === '/machines/fields' && method === 'GET') return fieldsAnswer;
  if (p === '/machines/plain' && method === 'PUT') {
    if (plainErrors.length) return plainErrors.shift();
    plainAnswer = { ...PLAIN, files: { ...PLAIN.files, ...json.files }, versions: { 'plain.yaml': 'v2', 'plain.py': 'p1' } };
    return { machine_id: 'plain', versions: plainAnswer.versions, problems: [], graph: PLAIN.graph };
  }
  if (p === '/machines/review' && method === 'PUT') return { machine_id: 'review', versions: MACHINE.versions, problems: [], graph: MACHINE.graph };
  const copy = p.match(/^\/machines\/(\w+_copy)$/);
  if (copy) return method === 'PUT' ? { machine_id: copy[1], versions: {}, problems: [], graph: MACHINE.graph } : { ...MACHINE, id: copy[1] };
  if (p === '/machines/review' && method === 'DELETE') return { deleted: 'review', files: ['review.yaml'], kept_module: null };
  if (p.endsWith('/edit')) return editAnswer || MACHINE;
  if (p === '/validate') return { machine_id: 'review', problems: [], graph: MACHINE.graph };
  if (p.endsWith('/layout')) {
    if (layoutsKept && p === '/machines/review/layout') reviewAnswer = { ...reviewAnswer, layout: json.layout };
    return {};
  }
  if (p.startsWith('/runs?')) return runsAnswer ? runsAnswer(new URLSearchParams(p.split('?')[1]))
    : [...RUNS, { ...RUNS[0], id: 'r2', status: 'succeeded', final_state: 'done' }];
  if (p === '/runs/r1/events') return { accepted: true, frame: '' };
  if (p === '/runs' && method === 'POST') return startRefused || { run_id: 'r1' };
  if (p.startsWith('/runs/r1?')) return runAnswer;
  if (p === '/runs/r1/control') return controlAnswer || runAnswer;
  if (p.startsWith('/runs/r2?')) return RUN2;
  if (p.startsWith('/runs/r3?')) return RUN3;
  const journal = p.match(/^\/runs\/(r\d)\/journal\?(.*)$/);
  if (journal) {
    const query = new URLSearchParams(journal[2]);
    const kinds = (query.get('kinds') || '').split(',');
    return journalOf[journal[1]].filter((row) => row.seq > Number(query.get('after') || 0) && kinds.includes(row.kind))
      .slice(0, Number(query.get('limit') || 200));
  }
  throw new Error(`unexpected ${method} ${path}`);
};

const $ = (id) => document.getElementById(id);
const settle = async () => { for (let i = 0; i < 40; i += 1) await new Promise((r) => setTimeout(r, 0)); };
async function release(match = () => true) {
  for (let round = 0; round < 50; round += 1) {
    const at = PENDING.findIndex((call) => match(call.path));
    if (at < 0) return;
    PENDING.splice(at, 1)[0].resolve();
    await settle();
  }
}
function check(condition, message) {
  if (!condition) throw new Error(message);
}
async function boot(search) {
  location.search = search;
  await import('./panel_copy.js');
  await settle();
  await release();
  await settle();
}
const element = (tag, attrs) => {
  const el = new FakeElement(tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  return el;
};
const clickMachine = (id) => $('machineList').fire('click', { target: element('button', { 'data-machine': id }) });
/** The canvas as the pointer sees it: a state's node, its box in canvas units, and a canvas point in client pixels
 * through the view (translate, scale) the canvas drew with. */
function canvasGeometry() {
  const transform = $('canvas').querySelector('.sg-viewport').getAttribute('transform') || 'translate(0 0) scale(1)';
  const [tx, ty, k] = transform.match(/-?\d*\.?\d+(?:e[-+]?\d+)?/gi).map(Number);
  const node = (name) => $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === name);
  const boxOf = (name) => {
    const rect = node(name).querySelector('.sg-box');
    return { x: Number(rect.getAttribute('x')), y: Number(rect.getAttribute('y')), w: Number(rect.getAttribute('width')),
      h: Number(rect.getAttribute('height')) };
  };
  return { k, node, boxOf, client: (x, y) => ({ clientX: x * k + tx, clientY: y * k + ty }) };
}
async function choose(name) {
  const node = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === name);
  check(node, `no node ${name} on the canvas`);
  await $('canvas').fire('pointerdown', { button: 0, target: node, clientX: 10, clientY: 10, pointerId: 1 });
  await $('canvas').fire('pointerup', { target: node, clientX: 10, clientY: 10 });
}
const offered = () => [...$('side-inspect').innerHTML.matchAll(/data-breakpoint="(\w+)"/g)].map((m) => m[1]);
const checked = (at) => new RegExp(`data-breakpoint="${at}" checked`).test($('side-inspect').innerHTML);
const toggle = (at, on) => {
  const box = element('input', { 'data-breakpoint': at });
  box.checked = on;
  return $('side-inspect').fire('change', { target: box });
};
const submit = async (form, values) => {
  for (const [key, value] of Object.entries(values)) form.elements[key].value = value;
  form.querySelector = () => new FakeElement('button');
  await form.fire('submit', {});
  await settle();
};
const confirms = () => ASKED.filter(([kind]) => kind === 'confirm').length;
const runGets = () => CALLS.filter(([, path]) => path.includes('/runs/r1?')).length;
/** The head's auto-save switch: 'on', 'off', or null when it is not drawn. */
const autosaveSwitch = () => {
  const box = $('machineHead').innerHTML.match(/<input\b[^>]*\bdata-act="autosave"[^>]*>/)?.[0];
  return box ? (/\bchecked\b/.test(box) ? 'on' : 'off') : null;
};
const headName = () => ($('machineHead').innerHTML.match(/<h2 class="sg-head-name[^"]*">([^<]*)<\/h2>/) || [])[1];
/** A submitted inspector form: {name: [shape, shown, typed]} (shape 'kind' for the kind select). */
function formOf(kind, controls) {
  const form = element('form', { 'data-form': kind });
  for (const [name, [shape, orig, value]] of Object.entries(controls)) {
    const control = form.elements[name];
    control.name = name;
    control.value = value;
    if (shape !== 'kind') {
      control.dataset.shape = shape;
      control.dataset.orig = orig;
    }
  }
  return form;
}
const lastEdit = () => CALLS.filter(([, path]) => path.endsWith('/edit')).map(([, , json]) => json.op).pop();
async function typeDraft() {
  $('yamlText').value = `${MACHINE.files['review.yaml']}\n# unsaved\n`;
  await $('yamlText').fire('input', {});
  check(DIRTY, 'typing made no draft');
}

const CASES = {
  async a_click_on_the_open_machine_asks_before_it_drops_the_drafts() {
    await boot('?machine=review');
    await typeDraft();
    ANSWERS.confirm = false;
    await clickMachine('review');
    await settle();
    check(confirms() === 1, `not asked: ${JSON.stringify(ASKED)}`);
    check(DIRTY && $('yamlText').value.includes('# unsaved'), 'a no must keep the drafts');
    ANSWERS.confirm = true;
    await clickMachine('review');
    await settle();
    check(!DIRTY && !$('yamlText').value.includes('# unsaved'), 'a yes discards them');
  },

  async a_reload_after_an_edit_conflict_asks_about_the_drafts_once() {
    await boot('?machine=review');
    await typeDraft();
    editAnswer = new ApiError(409, 'the file changed since you read it');
    ANSWERS.prompt.push('judge');
    ANSWERS.dialog = 'reload';
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-type': 'state' }) });
    await settle();
    check(ASKED.some(([kind, text]) => kind === 'dialog' && text === 'The file changed'), 'no conflict dialog');
    check(confirms() === 1, `asked ${confirms()} times: ${JSON.stringify(ASKED)}`);
    check(!DIRTY, 'the reload discards the drafts the author gave up');
  },

  async breakpoints_and_watches_of_the_next_run_carry_the_open_machine() {
    await boot('?machine=review');
    await choose('write');
    await toggle('enter', true);
    const stored = JSON.parse(localStorage.getItem('stategraph:breakpoints:review'));
    check(JSON.stringify(stored) === '[{"state":"write","at":"enter","machine":"review"}]', `stored ${JSON.stringify(stored)}`);
    await submit($('watchForm'), { expr: 'ctx.round' });
    await $('startForm').fire('submit', {});
    await settle();
    const start = CALLS.find(([method, path]) => method === 'POST' && path.endsWith('/api/runs'));
    check(start, 'no run started');
    check(JSON.stringify(start[2].breakpoints) === '[{"state":"write","at":"enter","machine":"review"}]', JSON.stringify(start[2].breakpoints));
    check(JSON.stringify(start[2].watchpoints) === '[{"expr":"ctx.round","machine":"review"}]', JSON.stringify(start[2].watchpoints));
  },

  async a_renamed_state_takes_the_next_runs_breakpoint_along() {
    await boot('?machine=review');
    await choose('write');
    await toggle('enter', true);
    ANSWERS.prompt = ['drafting'];
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'rename' }) });
    await settle();
    check(lastEdit()?.op === 'rename_state', `sent: ${JSON.stringify(lastEdit())}`);
    const stored = JSON.parse(localStorage.getItem('stategraph:breakpoints:review'));
    check(JSON.stringify(stored) === '[{"state":"drafting","at":"enter","machine":"review"}]', `stored ${JSON.stringify(stored)}`);
  },

  async a_breakpoint_the_machine_cannot_stop_at_any_more_is_dropped_at_the_start() {
    localStorage.setItem('stategraph:breakpoints:review', JSON.stringify([
      { state: 'write', at: 'enter', machine: 'review' }, { state: 'gone', at: 'enter', machine: 'review' },
      { state: 'inner', at: 'enter', machine: 'critique' }]));
    await boot('?machine=review');
    await $('startForm').fire('submit', {});
    await settle();
    const start = CALLS.find(([method, path]) => method === 'POST' && path.endsWith('/api/runs'));
    check(start && JSON.stringify(start[2].breakpoints.map((p) => p.state)) === '["write","inner"]',
      `sent: ${JSON.stringify(start && start[2].breakpoints)}`);
    check(TOASTS.some(([kind, text]) => kind === 'warn' && text.includes('gone@enter')), `toasts: ${JSON.stringify(TOASTS)}`);
  },

  async a_live_run_shows_and_changes_only_the_open_machines_breakpoints() {
    const point = (state, at, machine, id) => ({ state, at, machine, condition: null, enabled: true, id });
    runAnswer = { ...RUN, debug: { ...RUN.debug, breakpoints: [point('write', 'enter', 'critique', 'b1'), point('read', 'enter', null, 'b2')] } };
    await boot('?machine=review&run=r1');
    await choose('write');
    check(!checked('enter'), 'the submachine\'s write@enter is not this machine\'s');
    const dots = $('canvas').querySelectorAll('.sg-breakpoint').map((dot) => dot.parentNode.parentNode.dataset.state);
    check(JSON.stringify(dots) === '["read"]', `breakpoint dots on ${JSON.stringify(dots)}`);
    await toggle('exit', true);
    await settle();
    const sent = CALLS.filter(([, path]) => path.endsWith('/control')).pop();
    check(sent && sent[2].action === 'set_breakpoints', 'no set_breakpoints');
    const mine = sent[2].breakpoints.find((p) => p.state === 'write' && p.at === 'exit');
    check(mine && mine.machine === 'review', `new point ${JSON.stringify(mine)}`);
    check(sent[2].breakpoints.some((p) => p.id === 'b1' && p.machine === 'critique'), 'the other machine\'s point stays');
    await submit($('watchForm'), { expr: 'len(ctx.draft)' });
    const watch = CALLS.filter(([, path]) => path.endsWith('/control')).pop();
    check(watch[2].action === 'set_watchpoints' && watch[2].watchpoints.at(-1).machine === 'review', JSON.stringify(watch[2]));
  },

  async the_inspector_offers_only_hooks_that_can_stop() {
    await boot('?machine=hooks');
    const expected = { work: 'enter,exit,error', route: '', box: '', loop: 'error', inner_end: 'exit', done: '' };
    const seen = {};
    for (const name of Object.keys(expected)) {
      await choose(name);
      seen[name] = offered().join(',');
    }
    check(JSON.stringify(seen) === JSON.stringify(expected), JSON.stringify(seen));
  },

  async a_state_the_inspector_cannot_stand_in_for_is_shown_not_applied() {
    await boot('?machine=hooks');
    await choose('shared');
    const inspector = $('side-inspect').innerHTML;
    const form = inspector.slice(inspector.indexOf('data-form="set-state"'));
    check(/<textarea[^>]*readonly/.test(form) && !/<button type="submit"/.test(form), 'Apply is offered');
    check(form.includes('description: anchored'), 'the value below the anchor is shown');
    await choose('work');
    const plain = $('side-inspect').innerHTML.slice($('side-inspect').innerHTML.indexOf('data-form="set-state"'));
    check(!/<textarea[^>]*readonly/.test(plain) && /<button type="submit"[^>]*data-apply/.test(plain), 'a plain state is locked');
  },

  async a_poll_tick_waits_for_the_answer_that_is_out() {
    await boot('?machine=review&run=r1');
    HOLD = (method, path) => path.includes('/runs/r1?');
    const poller = POLLERS.find((p) => p.ms === 1000);
    check(poller && poller.running, 'the poller does not run for a paused run');
    const before = runGets();
    poller.fn();
    await settle();
    check(runGets() === before + 1, 'a tick asks');
    poller.fn();
    await settle();
    poller.fn();
    await settle();
    check(runGets() === before + 1, `ticks asked again while the answer was out (${runGets() - before})`);
    check(!ABORTED.length, `aborted: ${ABORTED}`);
    await release();
    poller.fn();
    await settle();
    check(runGets() === before + 2, 'the tick after the answer asks again');
    await $('runList').fire('rowselect', { detail: { id: 'r1' } });
    await settle();
    check(runGets() === before + 3 && ABORTED.length === 1, 'a run the author picks is asked for at once');
    await release();
  },

  async a_selected_line_is_bent_by_its_handles_and_kept_in_the_layout_for_its_way() {
    await boot('?machine=review');
    const handles = () => $('canvas').querySelectorAll('.sg-bend');
    check(!handles().length, 'handles with no line selected');
    const link = $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === 'write#0');
    await $('canvas').fire('pointerdown', { button: 0, target: link, clientX: 10, clientY: 10, pointerId: 1 });
    await settle();
    check(handles().length >= 2 && handles().every((h) => h.dataset.bendOf === 'write#0'), `handles: ${handles().length}`);
    const shown = $('side-inspect').innerHTML;
    check(shown.includes('data-act="add-bend"') && !shown.includes('reset-line'), 'the inspector offers no Add bend');
    const { k } = canvasGeometry();
    const puts = () => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout'));
    const lineOf = () => puts().pop()?.[2].layout.lines?.['write→review'];
    const spot = (h) => [Number(h.getAttribute('x')), Number(h.getAttribute('y'))];
    // the middle handle, dragged across its segment: a pointercancel first leaves the line as it was
    const i = Math.floor(handles().length / 2);
    const handle = handles()[i];
    const level = handle.classList.contains('sg-bend--level');
    const before = spot(handle);
    const along = (d) => (level ? { clientX: 10, clientY: 10 + d } : { clientX: 10 + d, clientY: 10 });
    await $('canvas').fire('pointerdown', { button: 0, target: handle, pointerId: 1, ...along(0) });
    await $('canvas').fire('pointermove', { target: handle, ...along(40) });
    await $('canvas').fire('pointercancel', { target: handle, ...along(40) });
    await settle();
    check(!puts().length && JSON.stringify(spot(handles()[i])) === JSON.stringify(before), 'a cancelled bend changed the line');
    await $('canvas').fire('pointerdown', { button: 0, target: handles()[i], pointerId: 1, ...along(0) });
    await $('canvas').fire('pointermove', { target: handles()[i], ...along(20) });
    await $('canvas').fire('pointermove', { target: handles()[i], ...along(40) });
    await $('canvas').fire('pointerup', { target: handles()[i], ...along(40) });
    await settle();
    const bent = lineOf();
    check(bent && ['x', 'y'].includes(bent.start) && bent.at.length === handles().length, `stored: ${JSON.stringify(bent)}`);
    const after = spot(handles()[i]);
    const moved = level ? after[1] - before[1] : after[0] - before[0];
    check(Math.abs(moved - 40 / k) < 0.6 && (level ? after[0] === before[0] : after[1] === before[1]),
      `moved ${moved}, the pointer ${40 / k}: ${before} -> ${after}`);
    check($('line-tools-write#0').innerHTML.includes('reset-line'), 'no Reset line for a line drawn by hand');
    // a focused handle: the arrows across it move it by 8
    await $('canvas').fire('keydown', { target: handles()[i], key: level ? 'ArrowDown' : 'ArrowRight', preventDefault() {} });
    await settle();
    const stepped = lineOf();
    check(stepped.at[i] - bent.at[i] === 8, `an arrow moved it by ${stepped.at[i] - bent.at[i]}`);
    await $('canvas').fire('keydown', { target: handles()[i], key: level ? 'ArrowLeft' : 'ArrowUp', preventDefault() {} });
    check(puts().length === 2, 'an arrow along the segment moved it');
    // the inspector's buttons, in the transition's form
    const formButton = (act) => element('form', { 'data-transition': 'write#0' }).appendChild(element('button', { 'data-act': act }));
    await $('side-inspect').fire('click', { target: formButton('add-bend') });
    await settle();
    check(lineOf().at.length === stepped.at.length + 2 && handles().length === stepped.at.length + 2, `a bend more: ${JSON.stringify(lineOf())}`);
    // its button goes with it: the keyboard goes to Add bend beside it (the fake DOM draws no buttons: one stands in)
    let keyboard = null;
    $('line-tools-write#0').querySelector = (selector) => ({ focus: () => { keyboard = selector; } });
    await $('side-inspect').fire('click', { target: formButton('reset-line') });
    await settle();
    check(!('write→review' in (puts().pop()[2].layout.lines || {})) && !$('line-tools-write#0').innerHTML.includes('reset-line'),
      'Reset line kept the line drawn by hand');
    check(keyboard === '[data-act="add-bend"]', `the keyboard after Reset line: ${keyboard}`);
  },

  async a_line_in_a_lane_is_bent_where_it_is_drawn_and_a_states_forms_show_its_tools() {
    const write = MACHINE.graph.transitions.find((t) => t.id === 'write#0');
    reviewAnswer = { ...MACHINE, graph: { ...MACHINE.graph, transitions: [...MACHINE.graph.transitions,  // a pair: two lanes
      { ...write, id: 'review#9', source: 'review', index: 9, target: 'write', path: 'states.review.transitions[9]' }] } };
    await boot('?machine=review');
    const link = () => $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === 'write#0');
    const path = () => link().querySelector('.sg-edge').getAttribute('d').match(/-?[\d.]+/g).map(Number)
      .reduce((points, v, k, all) => (k % 2 ? points : [...points, [v, all[k + 1]]]), []);
    const near = (p, q) => Math.abs(p - q) < 0.6;
    const onPath = ([x, y]) => path().slice(1).some(([bx, by], k) => {
      const [ax, ay] = path()[k];
      return near(ax, bx) ? near(x, ax) && y > Math.min(ay, by) - 0.6 && y < Math.max(ay, by) + 0.6
        : near(y, ay) && x > Math.min(ax, bx) - 0.6 && x < Math.max(ax, bx) + 0.6;
    });
    const handles = () => $('canvas').querySelectorAll('.sg-bend');
    const centre = (h) => [Number(h.getAttribute('x')) + 5, Number(h.getAttribute('y')) + 5];
    await $('canvas').fire('pointerdown', { button: 0, target: link(), clientX: 10, clientY: 10, pointerId: 1 });
    await settle();
    check(handles().length >= 2 && handles().every((h) => onPath(centre(h))),
      `handles off the line in its lane: ${handles().map(centre)} on ${path()}`);
    const i = Math.floor(handles().length / 2);
    const level = handles()[i].classList.contains('sg-bend--level');
    const along = (d) => (level ? { clientX: 10, clientY: 10 + d } : { clientX: 10 + d, clientY: 10 });
    const drawn = path();
    await $('canvas').fire('pointerdown', { button: 0, target: handles()[i], pointerId: 1, ...along(0) });
    await $('canvas').fire('pointermove', { target: handles()[i], ...along(10) });
    await $('canvas').fire('pointerup', { target: handles()[i], ...along(10) });
    await settle();
    const puts = () => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout'));
    const lineOf = () => puts().pop()?.[2].layout.lines?.['write→review'];
    const ends = (points) => JSON.stringify([points[0], points[points.length - 1]]);
    check(String(path()) !== String(drawn) && handles().every((h) => onPath(centre(h))) && ends(path()) === ends(drawn),
      `the bent line left its lane: ${drawn} -> ${path()}`);
    // a state's inspector shows its transitions' line tools: a bend added there shows its Reset line
    await choose('write');
    await settle();
    const other = element('form', { 'data-transition': 'write#1' }).appendChild(element('button', { 'data-act': 'add-bend' }));
    await $('side-inspect').fire('click', { target: other });
    await settle();
    check(puts().pop()?.[2].layout.lines?.['write→failed']?.at?.length >= 5, 'Add bend in the state\'s form bent nothing');
    const tools = $('line-tools-write#1').innerHTML;
    // keyed per transition: redrawn with Reset line, the button clicked keeps the keyboard -- not another form's
    check(tools.includes('data-act="reset-line" data-key="reset-line:write#1"') && tools.includes('data-act="add-bend" data-key="add-bend:write#1"')
      && tools.includes('longest segment'), `the state's form of write#0: ${tools}`);
    // right-angled for a line drawn by hand: it is one already, its bends stay
    const bent = lineOf();
    const select = element('select', { 'data-line': 'write#0' });
    element('form', { 'data-transition': 'write#0' }).appendChild(select);
    select.value = 'orthogonal';
    await $('side-inspect').fire('change', { target: select });
    await settle();
    check(JSON.stringify(lineOf()) === JSON.stringify(bent), `Right-angled undid the bends: ${JSON.stringify(lineOf())}`);
    select.value = 'straight';
    await $('side-inspect').fire('change', { target: select });
    await settle();
    check(lineOf() === 'straight', `Straight kept the bends: ${JSON.stringify(lineOf())}`);
  },

  async a_bump_in_a_lane_dropped_in_line_with_its_far_side_goes() {
    const write = MACHINE.graph.transitions.find((t) => t.id === 'write#0');
    // up, across, down: in a lane the two upright segments go opposite ways, drawn apart from where they lie as one
    reviewAnswer = { ...MACHINE, layout: { ...MACHINE.layout, lines: { 'write→review': { start: 'x', at: [0, -60, -300, 60, 0] } } },
      graph: { ...MACHINE.graph, transitions: [...MACHINE.graph.transitions,
        { ...write, id: 'review#9', source: 'review', index: 9, target: 'write', path: 'states.review.transitions[9]' }] } };
    await boot('?machine=review');
    const link = $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === 'write#0');
    await $('canvas').fire('pointerdown', { button: 0, target: link, clientX: 10, clientY: 10, pointerId: 1 });
    await settle();
    const handles = () => $('canvas').querySelectorAll('.sg-bend');
    check(handles().length === 5, `handles: ${handles().length}`);
    const { k } = canvasGeometry();
    const x = (h) => Number(h.getAttribute('x'));
    const by = (x(handles()[3]) - x(handles()[1])) * k;  // the second upright segment onto the fourth, as drawn
    await $('canvas').fire('pointerdown', { button: 0, target: handles()[1], pointerId: 1, clientX: 10, clientY: 10 });
    await $('canvas').fire('pointermove', { target: handles()[1], clientX: 10 + by, clientY: 10 });
    await $('canvas').fire('pointerup', { target: handles()[1], clientX: 10 + by, clientY: 10 });
    await settle();
    const line = CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).pop()?.[2].layout.lines?.['write→review'];
    check(line?.at.length === 3 && handles().length === 3, `the bump stayed: ${JSON.stringify(line)}`);
  },

  async a_line_whose_transition_changed_under_the_pointer_is_neither_bent_nor_kept_as_the_pointer_had_it() {
    await boot('?machine=review');
    const link = (id) => $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === id);
    const path = (id) => link(id).querySelector('.sg-edge').getAttribute('d');
    await $('canvas').fire('pointerdown', { button: 0, target: link('write#0'), clientX: 10, clientY: 10, pointerId: 1 });
    await settle();
    const handles = () => $('canvas').querySelectorAll('.sg-bend');
    const handle = handles()[Math.floor(handles().length / 2)];
    const along = (d) => (handle.classList.contains('sg-bend--level') ? { clientX: 10, clientY: 10 + d } : { clientX: 10 + d, clientY: 10 });
    const before = path('write#0');
    await $('canvas').fire('pointerdown', { button: 0, target: handle, pointerId: 1, ...along(0) });
    await $('canvas').fire('pointermove', { target: handle, ...along(40) });
    check(path('write#0') !== before, 'the drag did not bend the line');
    // the file changed meanwhile: write#0 goes to failed now, write#1 the way write#0 went
    const [first, second] = ['write#0', 'write#1'].map((id) => MACHINE.graph.transitions.find((t) => t.id === id));
    const swapped = (t, other) => ({ ...other, id: t.id, index: t.index, path: t.path });
    reviewAnswer = { ...MACHINE, versions: { ...MACHINE.versions, 'review.yaml': 'v2' }, graph: { ...MACHINE.graph,
      transitions: MACHINE.graph.transitions.map((t) => (t.id === first.id ? swapped(t, second) : t.id === second.id ? swapped(t, first) : t)) } };
    await Promise.all(DOC_LISTENERS.refresh.map((fn) => fn({ detail: { auto: true } })));
    await settle();
    check(link('write#1') && link('write#0'), 'the changed machine is not drawn');
    const bentMeanwhile = path('write#1');
    await $('canvas').fire('pointerup', { target: handle, ...along(40) });
    await settle();
    check(!CALLS.some(([method, p]) => method === 'PUT' && p.endsWith('/layout')), 'the drag bent the line of another way');
    check(path('write#1') !== bentMeanwhile, 'the way is still drawn as the pointer had it');
  },

  async runs_live_at_once_are_all_offered_in_the_bar_and_picked_there() {
    let r1 = 'paused';
    runsAnswer = () => [{ ...RUNS[0], id: 'r3', status: 'running' }, { ...RUNS[0], id: 'r2', status: 'succeeded', final_state: 'done' },
      { ...RUNS[0], status: r1 }];
    await boot('?machine=review&run=r1');
    const picks = () => [...$('debugBar').innerHTML.matchAll(/data-pick-run="(r\d)" data-key="[^"]+"\s+aria-pressed="(true|false)"/g)]
      .map(([, id, on]) => (on === 'true' ? `[${id}]` : id)).join(',');
    check(picks() === '[r1],r3', `offered: ${picks()}`);  // in the order they started; the ended r2 is not
    const lists = POLLERS.find((p) => p.ms === 3000);
    check(lists?.running, 'the list is not asked again while another run is live');
    HOLD = (method, path) => path.includes('/runs?');
    const asked = () => CALLS.filter(([, path]) => path.includes('/runs?')).length;
    const before = asked();
    lists.fn();
    await settle();
    lists.fn();
    await settle();
    check(asked() === before + 1 && !ABORTED.length, `ticks while the list was out: ${asked() - before}, aborted: ${ABORTED}`);
    HOLD = () => false;
    await release();
    await $('debugBar').fire('click', { target: element('button', { 'data-pick-run': 'r3' }) });
    await settle();
    await release();
    check(picks() === 'r1,[r3]', `after the pick: ${picks()}`);
    check(CALLS.some(([, path]) => path.includes('/runs/r3?')), 'the run picked is not asked for');
    r1 = 'succeeded';
    lists.fn();
    await settle();
    await release();
    const bar = $('debugBar').innerHTML;
    check(!bar.includes('data-pick-run') && bar.includes('title="r3"'), `a run that ended is still offered: ${picks()}`);
    check(!lists.running, 'the list is still asked with no other run live');
    await $('runList').fire('rowselect', { detail: { id: 'r2' } });
    await settle();
    check(picks() === '[r2],r3' && lists.running, `an ended run shown beside a live one: ${picks()}, followed: ${lists.running}`);
    const listed = $('runList').innerHTML;
    runsAnswer = () => new ApiError(503, 'restarting');
    lists.fn();
    await settle();
    check($('runList').innerHTML === listed && !TOASTS.length && lists.running, `a failed tick: ${TOASTS}`);
    await $('debugBar').fire('click', { target: element('button', { 'data-control': 'close' }) });
    await settle();
    check(!lists.running, 'the list is still asked with no run shown');
  },

  async a_run_shown_the_list_does_not_hold_is_offered_beside_the_live_ones_by_their_ends() {
    runsAnswer = () => [{ ...RUNS[0], id: 'k3j9x0p2qa_m01x9z_sgzz98yy', status: 'running' }];
    await boot('?machine=review&run=r1');
    const bar = $('debugBar').innerHTML;
    const picks = [...bar.matchAll(/data-pick-run="([^"]+)" data-key="[^"]+"\s+aria-pressed="true"/g)].map(([, id]) => id);
    check(bar.includes('data-pick-run="k3j9x0p2qa_m01x9z_sgzz98yy"') && picks.join() === 'r1', `pressed: ${picks}`);
    check(bar.includes('…_sgzz98yy</span>'), 'a run a machine tool started is not told by its end');
  },

  async a_refresh_neither_drops_a_machine_click_nor_draws_the_machine_left() {
    await boot('?machine=review');
    HOLD = (method, path) => /\/machines(\/other|\/review)?$/.test(path);
    await clickMachine('other');
    await settle();
    DOC_LISTENERS.refresh.forEach((fn) => fn({ detail: { auto: true } }));
    await settle();
    await release((path) => path.endsWith('/api/machines'));
    const pending = PENDING.map((call) => call.path.split('/').pop()).sort().join(',');
    check(pending === 'other,review', `pending: ${pending}`);
    await release((path) => path.endsWith('/other'));
    await release();
    check(headName() === 'other', `open: ${headName()}`);
    check(!TOASTS.length, JSON.stringify(TOASTS));
  },

  async a_control_answer_for_a_run_left_does_not_take_the_view() {
    await boot('?machine=review&run=r1');
    HOLD = (method, path) => path.endsWith('/runs/r1/control');
    controlAnswer = { ...RUN, status: 'cancelled', active: false };
    const clicked = $('debugBar').fire('click', { target: element('button', { 'data-control': 'terminate' }) });
    await settle();
    check(PENDING.length === 1, 'the terminate is not out');
    await $('runList').fire('rowselect', { detail: { id: 'r2' } });
    await settle();
    await release();
    await clicked;
    await settle();
    const bar = $('debugBar').innerHTML;
    check(bar.includes('title="r2"') && !bar.includes('title="r1"'), `the bar shows ${(bar.match(/title="(r\d)"/) || [])[1]}`);
    const row = $('runList').innerHTML.match(/data-id="r1"[\s\S]*?<\/tr>/)?.[0] || '';
    check(row.includes('cancelled'), `the list does not know r1 ended: ${row}`);
    check(!POLLERS.find((p) => p.ms === 3000).running, 'the list is still asked though no other run is live');
  },

  async the_run_list_follows_a_terminate() {
    await boot('?machine=review&run=r1');
    controlAnswer = { ...RUN, status: 'cancelled', active: false };
    await $('debugBar').fire('click', { target: element('button', { 'data-control': 'terminate' }) });
    await settle();
    const row = $('runList').innerHTML.match(/data-id="r1"[\s\S]*?<\/tr>/)?.[0] || '';
    check(row.includes('cancelled') && !row.includes('paused'), `row: ${row}`);
  },

  async a_failed_poll_goes_on_polling_and_says_so() {
    await boot('?machine=review&run=r1');
    const poller = POLLERS.find((p) => p.ms === 1000);
    runAnswer = new ApiError(503, 'restarting');
    poller.fn();
    await settle();
    check(poller.running, 'one failed poll stopped the polling');
    check($('debugBar').innerHTML.includes('not refreshed'), 'the bar does not say the run is not refreshed');
    poller.fn();
    await settle();
    check(TOASTS.filter(([, text]) => text.includes('not refreshed')).length === 1, `toasted per tick: ${JSON.stringify(TOASTS)}`);
    runAnswer = RUN;
    poller.fn();
    await settle();
    check(!$('debugBar').innerHTML.includes('not refreshed'), 'a poll that got through still says not refreshed');
  },

  async text_typed_into_the_inspector_is_not_dropped_unasked() {
    await boot('?machine=review');
    await choose('write');
    const form = element('form', { 'data-form': 'set-state' });
    const area = form.appendChild(element('textarea', { name: 'yaml' }));
    await $('side-inspect').fire('input', { target: area });
    check(DIRTY, 'typing into the inspector marks the panel unsaved');
    ANSWERS.confirm = false;
    await choose('done');
    await settle();
    check(confirms() === 1, `not asked: ${JSON.stringify(ASKED)}`);
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">write</h3>'), 'a no must keep the inspector on write');
    ANSWERS.confirm = true;
    await choose('done');
    await settle();
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">done</h3>') && !DIRTY, 'a yes moves on');
  },

  async tab_in_a_read_only_file_types_nothing() {
    await boot('?machine=ro');
    const before = $('yamlText').value;
    await $('yamlText').fire('keydown', { key: 'Tab', target: $('yamlText') });
    await settle();
    check($('yamlText').value === before && !DIRTY, 'Tab changed a read-only file');
  },

  async a_run_that_ended_leaves_the_debug_lists_to_the_next_run() {
    await boot('?machine=review');
    await choose('write');
    await toggle('enter', true);
    await $('runList').fire('rowselect', { detail: { id: 'r2' } });
    await settle();
    const points = $('dbgPoints').innerHTML;
    check(points.includes('write@enter') && !points.includes('read@enter'), `debug pane: ${points}`);
    check($('dbgPointsScope').textContent === '(next run)', `scope: ${$('dbgPointsScope').textContent}`);
  },

  async an_interrupted_run_can_be_terminated() {
    runAnswer = { ...RUN, status: 'interrupted', active: false, debug: { ...RUN.debug, paused: null } };
    await boot('?machine=review&run=r1');
    const bar = $('debugBar').innerHTML;
    check(bar.includes('data-control="terminate"') && !/data-control="terminate" disabled/.test(bar), 'Terminate is off');
  },

  async a_run_of_another_process_can_be_paused_continued_and_terminated_but_not_run_to_a_state() {
    const enabled = (bar, control) => bar.includes(`data-control="${control}"`)
      && !new RegExp(`data-control="${control}" disabled`).test(bar);
    runAnswer = { ...RUN, status: 'running', active: false, debug: { ...RUN.debug, paused: null } };
    await boot('?machine=review&run=r1');
    let bar = $('debugBar').innerHTML;
    check(enabled(bar, 'pause') && enabled(bar, 'terminate') && !enabled(bar, 'run_to') && bar.includes('in another process'),
      `running elsewhere: ${bar}`);
    runAnswer = { ...runAnswer, status: 'paused' };
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    bar = $('debugBar').innerHTML;
    check(enabled(bar, 'continue') && enabled(bar, 'step') && !enabled(bar, 'pause'), `paused elsewhere: ${bar}`);
  },

  async a_click_on_a_states_handle_connects_nothing() {
    await boot('?machine=review');
    const node = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'write');
    const handle = node.querySelector('[data-handle]');
    document.elementFromPoint = () => node;
    try {
      await $('canvas').fire('pointerdown', { button: 0, target: handle, clientX: 10, clientY: 10, pointerId: 1 });
      await $('canvas').fire('pointerup', { type: 'pointerup', target: handle, clientX: 10, clientY: 10 });
      await settle();
      check(!CALLS.some(([, path]) => path.endsWith('/edit')), 'a click wrote a transition');
      await $('canvas').fire('pointerdown', { button: 0, target: handle, clientX: 10, clientY: 10, pointerId: 1 });
      await $('canvas').fire('pointermove', { target: handle, clientX: 80, clientY: 40 });
      await $('canvas').fire('pointerup', { type: 'pointerup', target: handle, clientX: 10, clientY: 10 });
      await settle();
      check(CALLS.some(([, path, json]) => path.endsWith('/edit') && json.op.op === 'add_transition'), 'a drag out and back is a self-transition');
    } finally {
      document.elementFromPoint = () => null;
    }
  },

  async a_double_click_renames_the_state_under_the_pointer() {
    await boot('?machine=review');
    const node = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'write');
    document.elementFromPoint = () => node;
    try {
      // the pointer is captured by the svg: the click events arrive with the svg as their target
      await $('canvas').fire('dblclick', { target: $('canvas'), clientX: 10, clientY: 10 });
      await settle();
    } finally {
      document.elementFromPoint = () => null;
    }
    check(ASKED.some(([kind, text]) => kind === 'prompt' && text.startsWith('New name for write')), `asked: ${JSON.stringify(ASKED)}`);
  },

  async a_composite_is_drawn_under_the_transitions_inside_it() {
    await boot('?machine=review');
    const drawn = $('canvas').descendants();  // document order = painting order
    const composite = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'review');
    const inner = MACHINE.graph.transitions.find((t) => t.source === 'read' && t.target === 'verdict');
    const link = $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === inner.id);
    const leaf = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'read');
    check(composite && link && leaf, 'the composite, its inner transition or its state is not drawn');
    check(drawn.indexOf(composite) < drawn.indexOf(link), 'the composite covers the transition inside it');
    check(drawn.indexOf(link) < drawn.indexOf(leaf), 'the transition covers the state it leaves');
    await choose('review');
    check(composite.classList.contains('is-selected'), 'a selected composite is not marked');
  },

  async a_composite_from_the_palette_is_one_edit_with_its_first_state() {
    await boot('?machine=review');
    check(/data-add-type="composite"/.test($('paletteMenu').innerHTML), 'the palette offers no composite');
    ANSWERS.prompt.push('group');
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-type': 'composite' }) });
    await settle();
    const sent = lastEdit();
    check(sent?.op === 'batch' && JSON.stringify(sent.ops) === JSON.stringify([
      { op: 'add_state', name: 'group', type: 'state', parent: null },
      { op: 'add_state', name: 'start', type: 'state', parent: 'group' }]), `sent: ${JSON.stringify(sent)}`);
  },

  async ctrl_and_shift_click_select_several_states_and_delete_removes_them_in_one_edit() {
    await boot('?machine=review');
    const node = (name) => $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === name);
    const click = async (name, keys) => {
      await $('canvas').fire('pointerdown', { button: 0, target: node(name), clientX: 10, clientY: 10, pointerId: 1, ...keys });
      await $('canvas').fire('pointerup', { target: node(name), clientX: 10, clientY: 10 });
      await settle();
    };
    await choose('write');
    await click('review', { ctrlKey: true });
    await click('read', { shiftKey: true });  // inside review: it goes with review
    await click('done', { ctrlKey: true });
    await click('done', { ctrlKey: true });  // a second click takes it out again
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">3 states</h3>'), 'the inspector does not show 3 states');
    check(['write', 'review', 'read'].every((name) => node(name).classList.contains('is-selected'))
      && !node('done').classList.contains('is-selected'), 'the selected states are not marked');
    ANSWERS.confirm = true;
    await $('canvas').fire('keydown', { key: 'Delete', target: $('canvas') });
    await settle();
    const sent = lastEdit();
    check(sent?.op === 'batch' && JSON.stringify(sent.ops) === JSON.stringify([
      { op: 'remove_state', name: 'write' }, { op: 'remove_state', name: 'review' }]), `sent: ${JSON.stringify(sent)}`);
  },

  async a_selected_state_gone_after_a_reload_leaves_the_selection() {
    await boot('?machine=review');
    const node = (name) => $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === name);
    await choose('write');
    const link = $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === 'write#1');  // write → failed
    for (const target of [node('failed'), link]) {
      await $('canvas').fire('pointerdown', { button: 0, target, ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
      await $('canvas').fire('pointerup', { target, clientX: 10, clientY: 10 });
      await settle();
    }
    check($('side-inspect').innerHTML.includes('2 states, 1 transition'), 'not two states and a transition selected');
    reviewAnswer = { ...MACHINE, versions: { ...MACHINE.versions, 'review.yaml': 'v2' }, graph: { ...MACHINE.graph,
      states: MACHINE.graph.states.filter((s) => s.name !== 'failed'),
      transitions: MACHINE.graph.transitions.filter((t) => t.target !== 'failed') } };
    DOC_LISTENERS.refresh.forEach((fn) => fn({ detail: { auto: true } }));
    await settle();
    await release();
    await settle();
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">write</h3>'), 'the one state left is not shown as a state');
  },

  async a_drag_of_a_selected_state_moves_every_selected_one() {
    await boot('?machine=review');
    const node = (name) => $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === name);
    await choose('write');
    await $('canvas').fire('pointerdown', { button: 0, target: node('failed'), ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: node('failed'), clientX: 10, clientY: 10 });
    await $('canvas').fire('pointerdown', { button: 0, target: node('write'), clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointermove', { target: node('write'), clientX: 60, clientY: 30 });
    await $('canvas').fire('pointerup', { target: node('write'), clientX: 60, clientY: 30 });
    await settle();
    const saved = CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).pop();
    const moved = saved && Object.keys(saved[2].layout.positions).sort();
    check(JSON.stringify(moved) === JSON.stringify(['done', 'failed', 'write']), `positions saved: ${JSON.stringify(moved)}`);
    check(saved[2].layout.auto === 'classic', `dragged against the classic layout, it stays: ${JSON.stringify(saved[2].layout)}`);
    check($('side-inspect').innerHTML.includes('2 states'), 'the drag lost the selection');
  },

  async the_first_drag_keeps_a_machine_laid_out_flow() {
    reviewAnswer = { ...MACHINE, layout: { version: 1, positions: {} } };
    await boot('?machine=review');
    const node = (name) => $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === name);
    await $('canvas').fire('pointerdown', { button: 0, target: node('write'), clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointermove', { target: node('write'), clientX: 60, clientY: 30 });
    await $('canvas').fire('pointerup', { target: node('write'), clientX: 60, clientY: 30 });
    await settle();
    const saved = CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).pop()?.[2].layout;
    check(saved?.auto === 'flow' && Object.keys(saved.positions).join() === 'write',
      `the first position turned the layout classic: ${JSON.stringify(saved)}`);
  },

  async a_shift_drag_on_the_empty_canvas_selects_the_states_inside_the_band() {
    await boot('?machine=review');
    await choose('write');
    await $('canvas').fire('pointerdown', { button: 0, target: $('canvas'), shiftKey: true, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: $('canvas'), clientX: 10, clientY: 10 });
    await settle();
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">write</h3>'), 'a Shift+click on the canvas dropped the selection');
    await $('canvas').fire('pointerdown', { button: 0, target: $('canvas'), shiftKey: true, clientX: -9000, clientY: -9000, pointerId: 1 });
    await $('canvas').fire('pointermove', { target: $('canvas'), clientX: 9000, clientY: 9000 });
    check($('canvas').querySelectorAll('.sg-band').length === 1, 'no band is drawn');
    await $('canvas').fire('pointerup', { target: $('canvas'), clientX: 9000, clientY: 9000 });
    await settle();
    const all = MACHINE.graph.states.length;
    check($('side-inspect').innerHTML.includes(`<h3 class="sg-inspect-name">${all} states</h3>`), `not all ${all} states selected`);
    check(!$('canvas').querySelectorAll('.sg-band').length, 'the band stays after the drag');
  },

  async a_band_from_any_corner_selects_only_what_lies_wholly_inside_it() {
    await boot('?machine=review');
    const { client, boxOf, node } = canvasGeometry();
    const box = boxOf('write');
    // from below right to above left, around write only: canvas units through the view, as the pointer gives them
    await $('canvas').fire('pointerdown', { button: 0, target: $('canvas'), shiftKey: true, pointerId: 1, ...client(box.x + box.w + 3, box.y + box.h + 3) });
    await $('canvas').fire('pointermove', { target: $('canvas'), ...client(box.x - 3, box.y - 3) });
    await $('canvas').fire('pointerup', { target: $('canvas'), ...client(box.x - 3, box.y - 3) });
    await settle();
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">write</h3>'), 'the band did not select write alone');
    // a Ctrl+drag that starts on a state draws a band as well: here around nothing whole -- no toggle of that state
    const composite = boxOf('review');
    await $('canvas').fire('pointerdown', { button: 0, target: node('review'), ctrlKey: true, pointerId: 1, ...client(composite.x + 2, composite.y + 2) });
    await $('canvas').fire('pointermove', { target: node('review'), ...client(composite.x + 12, composite.y + 12) });
    await $('canvas').fire('pointerup', { target: node('review'), ...client(composite.x + 12, composite.y + 12) });
    await settle();
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">write</h3>'), 'a Ctrl+drag on a state toggled it');
  },

  async a_selected_composite_moves_with_its_states_by_the_pointers_distance() {
    await boot('?machine=review');
    for (let i = 0; i < 5; i += 1) await $('zoomIn').fire('click', {});  // zoomed in: one pixel is less than a unit
    const { k, boxOf, node } = canvasGeometry();
    const before = boxOf('review');
    await choose('review');
    await $('canvas').fire('pointerdown', { button: 0, target: node('read'), ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: node('read'), clientX: 10, clientY: 10 });
    await settle();
    await $('canvas').fire('pointerdown', { button: 0, target: node('read'), clientX: 10, clientY: 10, pointerId: 1 });
    for (let x = 11; x <= 50; x += 1) await $('canvas').fire('pointermove', { target: node('read'), clientX: x, clientY: 10 });
    await $('canvas').fire('pointerup', { target: node('read'), clientX: 50, clientY: 10 });
    await settle();
    const saved = CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).pop()?.[2].layout.positions;
    check(saved && !('read' in saved), `read moved on its own too: ${JSON.stringify(saved)}`);
    check(Math.abs(saved.review.x - (before.x + 40 / k)) <= 1 && Math.abs(saved.review.y - before.y) <= 1,
      `review at ${JSON.stringify(saved.review)}, expected x ${before.x + 40 / k}, y ${before.y}`);
  },

  async a_state_dropped_on_a_composite_goes_into_it_and_keeps_its_place() {
    await boot('?machine=review');
    const { client, boxOf, node } = canvasGeometry();
    const read = boxOf('read');
    await $('canvas').fire('pointerdown', { button: 0, target: node('read'), pointerId: 1, ...client(read.x + 5, read.y + 5) });
    await $('canvas').fire('pointermove', { target: node('read'), ...client(read.x + 25, read.y + 5) });
    await $('canvas').fire('pointerup', { target: node('read'), ...client(read.x + 25, read.y + 5) });
    await settle();
    check(!CALLS.some(([, path]) => path.endsWith('/edit')), `a move inside its own composite went into it again: ${JSON.stringify(lastEdit())}`);
    const from = boxOf('write');
    const into = boxOf('review');
    const at = client(into.x + into.w / 2, into.y + into.h - 6);
    await $('canvas').fire('pointerdown', { button: 0, target: node('write'), pointerId: 1, ...client(from.x + 5, from.y + 5) });
    await $('canvas').fire('pointermove', { target: node('review'), ...at });
    check(node('review').getAttribute('class').includes('sg-node--drop'), 'the composite under the pointer is not marked');
    await $('canvas').fire('pointerup', { target: node('review'), ...at });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'move_state', name: 'write', into: 'review' }), `sent: ${JSON.stringify(lastEdit())}`);
    const saved = CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).pop()?.[2].layout.positions;
    check(saved?.write && saved.write.x >= 0 && saved.write.y >= 0 && saved.write.x < into.w,
      `its place counts from the composite now: ${JSON.stringify(saved?.write)}`);
    check(!$('canvas').querySelectorAll('.sg-node--drop').length, 'the drop mark stays');
  },

  async a_refused_drop_keeps_the_state_where_it_was_dropped() {
    await boot('?machine=review');
    editAnswer = new ApiError(422, "'write' cannot go there");
    const { client, boxOf, node } = canvasGeometry();
    const from = boxOf('write');
    const into = boxOf('review');
    const at = client(into.x + into.w / 2, into.y + into.h - 6);
    await $('canvas').fire('pointerdown', { button: 0, target: node('write'), pointerId: 1, ...client(from.x + 5, from.y + 5) });
    await $('canvas').fire('pointermove', { target: node('review'), ...at });
    await $('canvas').fire('pointerup', { target: node('review'), ...at });
    await settle();
    const saved = CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).pop()?.[2].layout.positions;
    check(saved?.write && Math.abs(saved.write.x - (into.x + into.w / 2 - 5)) <= 1,
      `a refused move is a plain move: ${JSON.stringify(saved?.write)}`);
  },

  async a_drop_on_a_read_only_machine_is_a_plain_move() {
    await boot('?machine=ro');
    const { client, boxOf, node } = canvasGeometry();
    const from = boxOf('write');
    const into = boxOf('review');
    const at = client(into.x + into.w / 2, into.y + into.h - 6);
    await $('canvas').fire('pointerdown', { button: 0, target: node('write'), pointerId: 1, ...client(from.x + 5, from.y + 5) });
    await $('canvas').fire('pointermove', { target: node('review'), ...at });
    await $('canvas').fire('pointerup', { target: node('review'), ...at });
    await settle();
    check(!CALLS.some(([, path]) => path.endsWith('/edit')) && !TOASTS.some(([, text]) => text.includes('read-only')),
      `a read-only machine was edited or refused a drag: ${JSON.stringify(TOASTS)}`);
    check(!$('canvas').querySelectorAll('.sg-node--drop').length, 'the drop mark stays');
  },

  async the_inspector_moves_a_state_to_another_composite_or_the_top_level() {
    await boot('?machine=review');
    await choose('review');
    const choices = ($('side-inspect').innerHTML.match(/data-act="parent"[^>]*>([\s\S]*?)<\/select>/) || [])[1] || '';
    check(choices.includes('(top level)') && !choices.includes('value="review"'), `a composite goes into itself: ${choices}`);
    await choose('read');
    const select = element('select', { 'data-act': 'parent' });
    select.value = '';
    await $('side-inspect').fire('change', { target: select });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'move_state', name: 'read', into: null }), `sent: ${JSON.stringify(lastEdit())}`);
    editAnswer = new ApiError(422, "'read' cannot go there");
    await choose('read');
    const refused = element('select', { 'data-act': 'parent' });
    refused.value = '';
    await $('side-inspect').fire('change', { target: refused });
    await settle();
    check(refused.value === 'review', `a refused move leaves the choice at ${JSON.stringify(refused.value)}`);
  },

  async a_composite_named_like_its_first_state_gets_another_one() {
    await boot('?machine=review');
    ANSWERS.prompt.push('start');
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-type': 'composite' }) });
    await settle();
    check(JSON.stringify(lastEdit()?.ops?.[1]) === JSON.stringify({ op: 'add_state', name: 'start_2', type: 'state', parent: 'start' }),
      `sent: ${JSON.stringify(lastEdit())}`);
  },

  async the_remove_button_and_its_question_name_what_goes() {
    await boot('?machine=review');
    await choose('write');
    await $('canvas').fire('keydown', { key: 'Enter', shiftKey: true, target: canvasGeometry().node('review') });  // keyboard adds
    await $('canvas').fire('keydown', { key: ' ', ctrlKey: true, target: canvasGeometry().node('read') });
    await settle();
    check($('side-inspect').innerHTML.includes('Remove 2 states'), 'the button does not count what goes (read goes with review)');
    ANSWERS.confirm = true;
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove-selection' }) });
    await settle();
    const asked = ASKED.filter(([kind]) => kind === 'confirm').pop()?.[1] || '';
    check(asked.startsWith('Remove 2 states write, review with the states inside?'), `asked: ${asked}`);
    check(JSON.stringify(lastEdit()?.ops) === JSON.stringify([{ op: 'remove_state', name: 'write' }, { op: 'remove_state', name: 'review' }]),
      `sent: ${JSON.stringify(lastEdit())}`);
  },

  async several_states_of_a_read_only_machine_are_not_asked_about_and_the_last_ones_are_kept() {
    await boot('?machine=ro');
    const { node } = canvasGeometry();
    await choose('write');
    await $('canvas').fire('pointerdown', { button: 0, target: node('done'), ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: node('done'), clientX: 10, clientY: 10 });
    await settle();
    const asked = ASKED.length;
    await $('canvas').fire('keydown', { key: 'Delete', target: $('canvas') });
    await settle();
    check(ASKED.length === asked && TOASTS.some(([, text]) => text.includes('read-only')), 'a read-only machine asked before it refused');
    await clickMachine('review');
    await settle();
    await release();
    await settle();
    await $('canvas').fire('pointerdown', { button: 0, target: $('canvas'), shiftKey: true, clientX: -9000, clientY: -9000, pointerId: 1 });
    await $('canvas').fire('pointermove', { target: $('canvas'), clientX: 9000, clientY: 9000 });
    await $('canvas').fire('pointerup', { target: $('canvas'), clientX: 9000, clientY: 9000 });
    await settle();
    const edits = CALLS.filter(([, path]) => path.endsWith('/edit')).length;
    await $('canvas').fire('keydown', { key: 'Delete', target: $('canvas') });
    await settle();
    check(CALLS.filter(([, path]) => path.endsWith('/edit')).length === edits && TOASTS.some(([, text]) => text.includes('at least one state')),
      'removing every state was sent');
  },

  async a_double_click_with_ctrl_renames_nothing() {
    await boot('?machine=review');
    const { node } = canvasGeometry();
    document.elementFromPoint = () => node('write');
    try {
      await $('canvas').fire('dblclick', { target: $('canvas'), ctrlKey: true, clientX: 10, clientY: 10 });
      await settle();
    } finally {
      document.elementFromPoint = () => null;
    }
    check(!ASKED.some(([kind]) => kind === 'prompt'), `asked: ${JSON.stringify(ASKED)}`);
  },

  async ctrl_click_on_transitions_selects_them_with_states_and_delete_removes_them_in_one_edit() {
    await boot('?machine=review');
    const { node } = canvasGeometry();
    const link = (id) => $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === id);
    const click = async (target) => {
      await $('canvas').fire('pointerdown', { button: 0, target, ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
      await $('canvas').fire('pointerup', { target, clientX: 10, clientY: 10 });
      await settle();
    };
    await choose('done');
    for (const id of ['write#0', 'write#1', 'review#0', 'write#1']) await click(link(id));  // write#1 on and off again
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">1 state, 2 transitions</h3>'),
      'the inspector does not show 1 state and 2 transitions');
    check(link('write#0').classList.contains('is-selected') && !link('write#1').classList.contains('is-selected')
      && node('done').classList.contains('is-selected'), 'the selected transitions are not marked');
    // review → done goes with done anyway: it is not counted, nor sent
    check($('side-inspect').innerHTML.includes('Remove 1 state and 1 transition'), 'the button does not count what goes');
    // a band around no state keeps the transitions
    await $('canvas').fire('pointerdown', { button: 0, target: $('canvas'), shiftKey: true, clientX: -9000, clientY: -9000, pointerId: 1 });
    await $('canvas').fire('pointermove', { target: $('canvas'), clientX: -8000, clientY: -8000 });
    await $('canvas').fire('pointerup', { target: $('canvas'), clientX: -8000, clientY: -8000 });
    await settle();
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">1 state, 2 transitions</h3>'), 'the band dropped the transitions');
    ANSWERS.confirm = true;
    await $('canvas').fire('keydown', { key: 'Delete', target: $('canvas') });
    await settle();
    const asked = ASKED.filter(([kind]) => kind === 'confirm').pop()?.[1] || '';
    check(asked === 'Remove the state done and the transition write → review? 1 transition into it goes too.', `asked: ${asked}`);
    check(JSON.stringify(lastEdit()?.ops) === JSON.stringify([{ op: 'remove_transition', source: 'write', index: 0 },
      { op: 'remove_state', name: 'done' }]), `sent: ${JSON.stringify(lastEdit())}`);
    await $('side-inspect').fire('click', { target: element('button', { 'data-select-transition': 'write#0' }) });
    await settle();
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">Transition</h3>'), 'the inspector\'s button did not open the transition');
  },

  async transitions_of_one_state_are_removed_from_its_last_one_on() {
    await boot('?machine=review');
    const link = (id) => $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === id);
    await $('canvas').fire('pointerdown', { button: 0, target: link('write#0'), clientX: 10, clientY: 10, pointerId: 1 });
    await settle();
    for (const id of ['read#0', 'write#1']) {
      await $('canvas').fire('pointerdown', { button: 0, target: link(id), shiftKey: true, clientX: 10, clientY: 10, pointerId: 1 });
      await $('canvas').fire('pointerup', { target: link(id), clientX: 10, clientY: 10 });
      await settle();
    }
    ANSWERS.confirm = true;
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove-selection' }) });
    await settle();
    check(JSON.stringify(lastEdit()?.ops) === JSON.stringify([{ op: 'remove_transition', source: 'read', index: 0 },
      { op: 'remove_transition', source: 'write', index: 1 }, { op: 'remove_transition', source: 'write', index: 0 }]),
    `sent: ${JSON.stringify(lastEdit())}`);
  },

  async group_puts_the_selected_states_into_a_composite_where_they_are_and_an_undo_puts_them_back() {
    layoutsKept = true;
    await boot('?machine=review');
    const { node, boxOf } = canvasGeometry();
    const circle = node('done').querySelector('.sg-shape');  // a final is drawn as circles in its box
    const [cx, cy, r] = ['cx', 'cy', 'r'].map((key) => Number(circle.getAttribute(key)));
    const drawn = { write: boxOf('write'), done: { x: cx - r, y: cy - r } };
    await choose('write');
    await $('canvas').fire('pointerdown', { button: 0, target: node('done'), ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: node('done'), clientX: 10, clientY: 10 });
    await settle();
    ANSWERS.prompt.push('drafting');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'group' }) });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'group_states', names: ['write', 'done'], name: 'drafting' }),
      `sent: ${JSON.stringify(lastEdit())}`);
    const layouts = () => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).map(([, , json]) => json.layout.positions);
    const spots = layouts().pop();
    for (const name of ['write', 'done']) {
      const at = { x: spots.drafting.x + spots[name].x, y: spots.drafting.y + spots[name].y };
      check(Math.abs(at.x - drawn[name].x) <= 1 && Math.abs(at.y - drawn[name].y) <= 1,
        `${name} at ${JSON.stringify(at)} in the composite, drawn at ${JSON.stringify(drawn[name])}`);
    }
    // another state dragged after the group keeps its place through the undo and the redo
    await $('canvas').fire('pointerdown', { button: 0, target: node('failed'), clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointermove', { target: node('failed'), clientX: 60, clientY: 30 });
    await $('canvas').fire('pointerup', { target: node('failed'), clientX: 60, clientY: 30 });
    await settle();
    const failed = layouts().pop().failed;
    const sorted = (spotsByName) => JSON.stringify(Object.keys(spotsByName).sort().map((key) => [key, spotsByName[key]]));
    await $('undo').fire('click', {});
    await settle();
    check(sorted(layouts().pop()) === sorted({ ...MACHINE.layout.positions, failed }), `undone to ${JSON.stringify(layouts().pop())}`);
    await $('redo').fire('click', {});
    await settle();
    check(sorted(layouts().pop()) === sorted({ ...spots, failed }), `redone to ${JSON.stringify(layouts().pop())}`);
  },

  async states_of_two_levels_are_not_grouped_a_composite_takes_its_own_along_and_unplaced_ones_get_no_position() {
    await boot('?machine=review');
    const { node } = canvasGeometry();
    const add = async (name) => {
      await $('canvas').fire('pointerdown', { button: 0, target: node(name), ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
      await $('canvas').fire('pointerup', { target: node(name), clientX: 10, clientY: 10 });
      await settle();
    };
    await choose('write');
    await add('read');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'group' }) });
    await settle();
    check(!ASKED.some(([kind]) => kind === 'prompt') && TOASTS.some(([, text]) => text.includes('side by side')),
      `asked ${JSON.stringify(ASKED)}, told ${JSON.stringify(TOASTS)}`);
    await add('read');
    await add('failed');
    ANSWERS.prompt.push('ends');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'group' }) });
    await settle();
    check(lastEdit()?.op === 'group_states' && !CALLS.some(([method, path]) => method === 'PUT' && path.endsWith('/layout')),
      'states laid out by ELK got positions');
    await choose('review');
    await add('read');  // inside review: it goes along, the two are one level
    ANSWERS.prompt.push('outer');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'group' }) });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'group_states', names: ['review'], name: 'outer' }),
      `sent: ${JSON.stringify(lastEdit())}`);
  },

  async a_renamed_states_position_goes_back_with_an_undo() {
    await boot('?machine=review');
    ANSWERS.prompt.push('finished');
    document.elementFromPoint = () => canvasGeometry().node('done');
    try {
      await $('canvas').fire('dblclick', { target: $('canvas'), clientX: 10, clientY: 10 });
      await settle();
    } finally {
      document.elementFromPoint = () => null;
    }
    const layouts = () => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).map(([, , json]) => json.layout.positions);
    check(JSON.stringify(layouts().pop()) === JSON.stringify({ finished: MACHINE.layout.positions.done }), `renamed: ${JSON.stringify(layouts())}`);
    await $('undo').fire('click', {});
    await settle();
    check(JSON.stringify(layouts().pop()) === JSON.stringify(MACHINE.layout.positions), `undone: ${JSON.stringify(layouts())}`);
  },

  async a_transition_back_between_two_states_is_drawn_beside_the_other() {
    const there = MACHINE.graph.transitions.find((t) => t.source === 'write' && t.target === 'review');
    reviewAnswer = { ...MACHINE, layout: { ...MACHINE.layout, line: 'straight' },  // straight lanes; right-angled: graph_tests
      graph: { ...MACHINE.graph, transitions: [...MACHINE.graph.transitions,
        { ...there, id: 'review#9', source: 'review', target: 'write', index: 9, path: 'states.review.transitions[9]' }] } };
    await boot('?machine=review');
    const middle = (id) => {
      const d = $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === id).querySelector('.sg-edge').getAttribute('d');
      const [x1, y1, x2, y2] = d.match(/-?\d+(?:\.\d+)?/g).map(Number);
      return [(x1 + x2) / 2, (y1 + y2) / 2];
    };
    const [a, b] = [middle(there.id), middle('review#9')];
    const apart = Math.hypot(a[0] - b[0], a[1] - b[1]);
    check(Math.abs(apart - 12) < 0.5, `the two lines are ${apart} apart, not 12: ${JSON.stringify([a, b])}`);
    // one without a transition back goes through the middles of its states: write → failed (a box, a circle)
    const { boxOf, node } = canvasGeometry();
    const box = boxOf('write');
    const circle = node('failed').querySelector('.sg-shape');
    const [p, q] = [[box.x + box.w / 2, box.y + box.h / 2], [Number(circle.getAttribute('cx')), Number(circle.getAttribute('cy'))]];
    const m = middle('write#1');
    const off = Math.abs((q[0] - p[0]) * (m[1] - p[1]) - (q[1] - p[1]) * (m[0] - p[0])) / Math.hypot(q[0] - p[0], q[1] - p[1]);
    check(off < 0.5, `a transition without one back is drawn ${off} beside the middle line`);
  },

  async a_moved_states_transition_stays_right_angled_and_its_line_is_set_at_once_in_its_inspector() {
    await boot('?machine=review');
    const points = (id) => {
      const d = $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === id).querySelector('.sg-edge').getAttribute('d');
      const numbers = d.match(/-?\d+(?:\.\d+)?/g).map(Number);
      return numbers.reduce((out, n, i) => (i % 2 ? out : [...out, [n, numbers[i + 1]]]), []);
    };
    const square = (ps) => ps.slice(1).every((p, i) => p[0] === ps[i][0] || p[1] === ps[i][1]);
    check(points('review#0').length === 4 && square(points('review#0')), `review → done (done moved) is not right-angled at first: ${JSON.stringify(points('review#0'))}`);
    await $('canvas').fire('pointerdown', { button: 0, target: $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === 'review#0'),
      clientX: 10, clientY: 10, pointerId: 1 });
    await settle();
    check($('side-inspect').innerHTML.includes('data-line="review#0"'), 'the transition inspector offers no line style');
    check($('side-inspect').innerHTML.includes('As the machine: Right-angled'), 'the machine\'s default is not named right-angled');
    const select = element('select', { 'data-line': 'review#0' });
    element('form', { 'data-transition': 'review#0' }).appendChild(select);  // in the transition's form, as drawn
    select.value = 'straight';
    await $('side-inspect').fire('input', { target: select });
    check(!DIRTY, 'choosing a line style made a draft to apply');
    await $('side-inspect').fire('change', { target: select });
    await settle();
    const saved = CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).pop()?.[2].layout;
    check(JSON.stringify(saved?.lines) === JSON.stringify({ 'review→done': 'straight' })
      && JSON.stringify(saved.positions) === JSON.stringify(MACHINE.layout.positions), `saved ${JSON.stringify(saved)}`);
    check(points('review#0').length === 2 && !square(points('review#0')), `not straight: ${JSON.stringify(points('review#0'))}`);
    select.value = '';
    await $('side-inspect').fire('change', { target: select });
    await settle();
    check(points('review#0').length === 4 && square(points('review#0')), 'the machine\'s style (right-angled) is not back');
  },

  async the_start_line_stays_right_angled_when_its_composite_grows() {
    load((await import('./paths.js')).ELK_PATH);  // ELK draws the start lines: the grid has none
    // review is the start state; a state placed deep inside it grows it, and the start dot stays where ELK put it
    reviewAnswer = { ...MACHINE, graph: { ...MACHINE.graph, initial: 'review' },
      layout: { ...MACHINE.layout, positions: { ...MACHINE.layout.positions, verdict: { x: 20, y: 400 } } } };
    await boot('?machine=review');
    await settle();
    const lines = $('canvas').querySelectorAll('.sg-edge--initial').map((line) => line.getAttribute('d'));
    const square = (d) => {
      const n = d.match(/-?\d+(?:\.\d+)?/g).map(Number);
      const ps = n.reduce((out, v, i) => (i % 2 ? out : [...out, [v, n[i + 1]]]), []);
      return ps.slice(1).every((p, i) => p[0] === ps[i][0] || p[1] === ps[i][1]);
    };
    check(lines.length === 2 && lines.every(square), `a start line is not right-angled: ${JSON.stringify(lines)}`);
    await $('side-inspect').fire('change', { target: Object.assign(element('select', { 'data-line-default': '' }), { value: 'straight' }) });
    await settle();
    const straight = $('canvas').querySelectorAll('.sg-edge--initial').map((line) => line.getAttribute('d'));
    check(straight.some((d) => !square(d)), `the start lines do not follow the machine's straight line: ${JSON.stringify(straight)}`);
  },

  async a_machine_without_positions_is_drawn_top_down() {
    load((await import('./paths.js')).ELK_PATH);  // the direction is ELK's: the grid has none
    reviewAnswer = { ...MACHINE, layout: { version: 1, positions: {} } };
    await boot('?machine=review');
    await settle();
    const [write, review] = ['write', 'review'].map(canvasGeometry().boxOf);
    // above it, not beside it: left to right puts it left of review, and higher up than review's top as well
    check(write.y + write.h <= review.y && write.x < review.x + review.w && review.x < write.x + write.w,
      `write is not above review: ${JSON.stringify([write, review])}`);
  },

  async a_machine_dragged_before_flow_came_stays_left_to_right() {
    load((await import('./paths.js')).ELK_PATH);
    await boot('?machine=review');  // MACHINE's layout: done placed by hand, no auto
    await settle();
    const [write, review] = ['write', 'review'].map(canvasGeometry().boxOf);
    check(write.x + write.w <= review.x, `write is not left of review: ${JSON.stringify([write, review])}`);
  },

  async a_stored_line_style_no_longer_offered_is_named_right_angled_as_drawn() {
    reviewAnswer = { ...MACHINE, layout: { ...MACHINE.layout, line: 'auto', lines: { 'review→done': 'auto', 'write→review': 'orthogonal' } } };
    await boot('?machine=review');
    check($('side-inspect').innerHTML.includes('value="orthogonal" selected'), 'the machine\'s line is not shown right-angled');
    await $('canvas').fire('pointerdown', { button: 0, target: $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === 'review#0'),
      clientX: 10, clientY: 10, pointerId: 1 });
    await settle();
    const shown = $('side-inspect').innerHTML;
    check(shown.includes('As the machine: Right-angled') && shown.includes('value="orthogonal" selected'),
      `the transition's line is not shown right-angled: ${shown.slice(shown.indexOf('data-line='), shown.indexOf('data-line=') + 400)}`);
    const other = $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === 'write#0');
    await $('canvas').fire('pointerdown', { button: 0, target: other, ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: other, clientX: 10, clientY: 10 });
    await settle();
    const both = $('side-inspect').innerHTML;
    check(both.includes('data-line="*"') && both.includes('value="orthogonal" selected') && !both.includes('Several styles'),
      'with a right-angled one, it is not one right-angled style');
  },

  async the_machines_line_and_a_selections_lines_are_set_from_the_inspector() {
    await boot('?machine=review');
    check($('side-inspect').innerHTML.includes('data-line-default'), 'the machine overview offers no line style');
    const layouts = () => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).map(([, , json]) => json.layout);
    await $('side-inspect').fire('change', { target: Object.assign(element('select', { 'data-line-default': '' }), { value: 'straight' }) });
    await settle();
    check(layouts().pop()?.line === 'straight', `machine line: ${JSON.stringify(layouts())}`);
    const link = (id) => $('canvas').querySelectorAll('.sg-link').find((l) => l.dataset.transition === id);
    const d = link('review#0').querySelector('.sg-edge').getAttribute('d');
    check((d.match(/L/g) || []).length === 1, `review → done is not drawn straight by the machine's line: ${d}`);
    await $('canvas').fire('pointerdown', { button: 0, target: link('write#0'), clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerdown', { button: 0, target: link('write#1'), ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: link('write#1'), clientX: 10, clientY: 10 });
    await settle();
    check($('side-inspect').innerHTML.includes('data-line="*"'), 'a selection of transitions offers no line style');
    await $('side-inspect').fire('change', { target: Object.assign(element('select', { 'data-line': '*' }), { value: 'orthogonal' }) });
    await settle();
    const last = layouts().pop();
    check(JSON.stringify(last?.lines) === JSON.stringify({ 'write→review': 'orthogonal', 'write→failed': 'orthogonal' }) && last.line === 'straight',
      `selection lines: ${JSON.stringify(last)}`);
    ANSWERS.confirm = true;
    await $('autoLayout').fire('click', {});
    await settle();
    const laid = layouts().pop();
    check(JSON.stringify(laid?.positions) === '{}' && laid.line === 'straight' && Object.keys(laid.lines || {}).length === 2,
      `auto layout dropped the line styles: ${JSON.stringify(laid)}`);
    check(laid.auto === 'flow', `auto layout lays the classic machine out flow: ${JSON.stringify(laid)}`);
  },

  async a_renamed_state_takes_its_line_styles_along_and_an_undo_puts_them_back() {
    layoutsKept = true;
    reviewAnswer = { ...MACHINE, layout: { ...MACHINE.layout, lines: { 'write→failed': 'orthogonal', 'review→done': 'straight' } } };
    editAnswer = reviewAnswer;
    await boot('?machine=review');
    ANSWERS.prompt.push('writer');
    document.elementFromPoint = () => canvasGeometry().node('write');
    try {
      await $('canvas').fire('dblclick', { target: $('canvas'), clientX: 10, clientY: 10 });
      await settle();
    } finally {
      document.elementFromPoint = () => null;
    }
    const layouts = () => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).map(([, , json]) => json.layout);
    const sorted = (lines) => JSON.stringify(Object.entries(lines || {}).sort());
    check(sorted(layouts().pop()?.lines) === sorted({ 'writer→failed': 'orthogonal', 'review→done': 'straight' }),
      `renamed: ${JSON.stringify(layouts())}`);
    await $('undo').fire('click', {});
    await settle();
    check(sorted(layouts().pop()?.lines) === sorted({ 'review→done': 'straight', 'write→failed': 'orthogonal' }),
      `undone: ${JSON.stringify(layouts())}`);
  },

  async a_duplicate_takes_the_line_styles_along_without_positions() {
    plainAnswer = { ...PLAIN, layout: { version: 1, positions: {}, line: 'orthogonal', lines: { 'a→b': 'straight' } } };
    await boot('?machine=plain');
    ANSWERS.prompt.push('plain_copy');
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'duplicate-machine' }) });
    await settle();
    const layout = CALLS.find(([method, path]) => method === 'PUT' && path.endsWith('/machines/plain_copy/layout'))?.[2].layout;
    check(layout?.line === 'orthogonal' && layout.lines['a→b'] === 'straight', `copied layout: ${JSON.stringify(layout)}`);
  },

  async an_internal_transition_offers_no_line() {
    const write = MACHINE.graph.transitions.find((t) => t.source === 'write');
    reviewAnswer = { ...MACHINE, graph: { ...MACHINE.graph, transitions: [...MACHINE.graph.transitions,
      { ...write, id: 'write#7', index: 7, target: null, trigger: 'approve', path: 'states.write.transitions[7]' }] } };
    await boot('?machine=review');
    await choose('write');
    const shown = $('side-inspect').innerHTML;
    check(shown.includes('data-transition="write#7"') && !shown.includes('data-line="write#7"') && shown.includes('data-line="write#0"'),
      'an internal transition offers a line style, or the others none');
    // chosen in the inspector (it is not drawn), then a state added: a selection with no line to set
    await $('side-inspect').fire('click', { target: element('button', { 'data-select-transition': 'write#7' }) });
    await settle();
    const done = canvasGeometry().node('done');
    await $('canvas').fire('pointerdown', { button: 0, target: done, ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: done, clientX: 10, clientY: 10 });
    await settle();
    check($('side-inspect').innerHTML.includes('1 state, 1 transition') && !$('side-inspect').innerHTML.includes('data-line="*"'),
      'a selection whose transitions have no line offers one');
  },

  async a_click_zoomed_out_selects_and_moves_nothing() {
    await boot('?machine=review');
    for (let i = 0; i < 9; i += 1) await $('zoomOut').fire('click', {});
    const node = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'write');
    await $('canvas').fire('pointerdown', { button: 0, target: node, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointermove', { target: node, clientX: 12, clientY: 11 });
    await $('canvas').fire('pointerup', { target: node, clientX: 12, clientY: 11 });
    await settle();
    check(!CALLS.some(([method, path]) => method === 'PUT' && path.endsWith('/layout')), 'a click of 3 pixels became a drag');
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">write</h3>'), 'the click did not select');
  },

  async a_bad_state_name_is_asked_again_with_what_was_typed_and_an_unchanged_one_is_no_error() {
    await boot('?machine=review');
    ANSWERS.prompt.push('Bad Name', 'write', 'fresh_one');
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-type': 'state' }) });
    await settle();
    const prompts = ASKED.filter(([kind]) => kind === 'prompt');
    check(prompts.length === 3, `asked ${JSON.stringify(prompts)}`);
    check(prompts[1][1].startsWith('"Bad Name" is no state name') && prompts[1][2] === 'Bad Name', `second: ${JSON.stringify(prompts[1])}`);
    check(prompts[2][1].startsWith('A state "write" exists already') && prompts[2][2] === 'write', `third: ${JSON.stringify(prompts[2])}`);
    check(lastEdit()?.op === 'add_state' && lastEdit().name === 'fresh_one', `edit ${JSON.stringify(lastEdit())}`);
    await choose('write');
    const edits = CALLS.filter(([, path]) => path.endsWith('/edit')).length;
    const toasts = TOASTS.length;
    const asked = ASKED.length;
    ANSWERS.prompt.push('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'rename' }) });
    await settle();
    check(CALLS.filter(([, path]) => path.endsWith('/edit')).length === edits && TOASTS.length === toasts && ASKED.length === asked + 1,
      `the same name renamed, complained or asked again: ${JSON.stringify(ASKED.slice(asked))} ${JSON.stringify(TOASTS.slice(toasts))}`);
  },

  async the_problem_badge_opens_the_overview_that_lists_every_problem() {
    await boot('?machine=review');
    await choose('write');
    check($('machineHead').innerHTML.includes('data-act="show-problems"'), `no problem button: ${$('machineHead').innerHTML}`);
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'show-problems' }) });
    await settle();
    const shown = $('side-inspect').innerHTML;
    const at = MACHINE.problems.findIndex((p) => p.code === 'SG004');
    check(shown.includes('data-form="machine-fields"') && shown.includes('unknown name x') && shown.includes(`data-problem="${at}"`),
      `overview: ${shown}`);
    await $('side-inspect').fire('click', { target: element('button', { 'data-problem': String(at) }) });
    await settle();
    check($('side-inspect').innerHTML.includes('data-transition="write#0"') && $('side-inspect').innerHTML.includes('>Transition<'),
      `not at the transition: ${$('side-inspect').innerHTML.slice(0, 400)}`);
  },

  async an_agent_field_offers_the_catalog_s_agents_and_says_what_it_is() {
    await boot('?machine=review');
    await choose('write');
    const shown = $('side-inspect').innerHTML;
    check(/list="sgAgents"[^>]*id="af-agent"/.test(shown), `agent field: ${shown}`);
    check($('sgAgents').innerHTML.includes('value="scene_writer"') && $('sgTools').innerHTML.includes('value="store_put"')
      && $('sgProfiles').innerHTML.includes('value="fast"'), `offered: ${$('sgAgents').innerHTML} ${$('sgTools').innerHTML}`);
    check($('sgMachines').innerHTML.includes('value="review"') && $('sgMachines').innerHTML.includes('value="fields"'), 'machines offered');
    check(shown.includes('class="pk-help sg-field-help"'), 'no description shown');
  },

  async a_new_event_from_the_trigger_select_is_declared_and_becomes_the_trigger() {
    await boot('?machine=review');
    await choose('write');
    check($('side-inspect').innerHTML.includes('New event…'), 'no new event option');
    ANSWERS.prompt.push('retry_now', 'second');
    // the answer: a machine whose events key is written without a value (events:), as the next one reads it
    editAnswer = { ...MACHINE, graph: { ...MACHINE.graph, events: {}, yaml: { ...MACHINE.graph.yaml, events: 'null' } } };
    const newEvent = async () => {
      const form = element('form', { 'data-transition': 'write#0' });
      const select = element('select', { 'data-orig': 'done' });
      select.name = 'trigger';
      select.value = '+new-event';
      form.appendChild(select);
      await $('side-inspect').fire('change', { target: select });
      await settle();
      return select;
    };
    const select = await newEvent();
    const edits = () => CALLS.filter(([, path]) => path.endsWith('/edit')).map(([, , json]) => json.op);
    check(edits().length === 1 && edits()[0].op === 'update_machine'
      && edits()[0].fields.events.$yaml === 'approve: {description: a human says yes}\nretry_now: {description: ""}',
      `declared: ${JSON.stringify(edits())}`);
    check(select.value === 'done', 'the select still says New event');
    await newEvent();
    check(edits().length === 2 && JSON.stringify(edits()[1].fields.events) === '{"second":{"description":""}}',
      `events: without a value: ${JSON.stringify(edits()[1])}`);
  },

  async the_machine_overview_puts_its_settings_first_without_the_tables_they_repeat() {
    await boot('?machine=review');
    const shown = $('side-inspect').innerHTML;
    check(shown.indexOf('data-form="machine-fields"') < shown.indexOf('As an agent'), 'settings not first');
    check(!shown.includes('<th>Type</th>') && !shown.includes('<th>Description</th>'), 'a params or events table');
  },

  async a_waiting_run_offers_its_events_in_the_bar_and_the_form_chooses_and_explains_one() {
    runAnswer = { ...RUN, status: 'waiting', debug: { ...RUN.debug, paused: null }, accepts: [{ frame: '', state: 'read', events: ['approve'] }] };
    await boot('?machine=review&run=r1');
    const bar = $('debugBar').innerHTML;
    check(bar.includes('data-send-event="approve"') && bar.includes('title="a human says yes"'), `bar: ${bar}`);
    check($('eventForm').elements.name.value === 'approve' && $('eventHelp').textContent === 'a human says yes',
      `form: ${$('eventForm').elements.name.value} / ${$('eventHelp').textContent}`);
    await $('debugBar').fire('click', { target: element('button', { 'data-send-event': 'approve' }) });
    await settle();
    const sent = CALLS.filter(([, path]) => path.endsWith('/runs/r1/events')).map(([, , json]) => json);
    check(sent.length === 1 && sent[0].name === 'approve' && sent[0].data === null && sent[0].frame === null, `sent ${JSON.stringify(sent)}`);
  },

  async an_event_two_frames_wait_for_goes_to_the_form_to_pick_one() {
    runAnswer = { ...RUN, status: 'waiting', debug: { ...RUN.debug, paused: null },
      accepts: [{ frame: '', state: 'read', events: ['approve'] }, { frame: 's3/m/', state: 'done', events: ['approve'] }] };
    await boot('?machine=review&run=r1');
    check($('debugBar').innerHTML.includes('2 frames wait for it'), `bar: ${$('debugBar').innerHTML}`);
    $('eventForm').elements.name.value = '';
    await $('debugBar').fire('click', { target: element('button', { 'data-send-event': 'approve' }) });
    await settle();
    check(!CALLS.some(([, path]) => path.endsWith('/events')), 'sent without a frame');
    check(TABS.sideTabs === 'debug' && $('eventForm').elements.name.value === 'approve', `form: ${$('eventForm').elements.name.value}`);
  },

  async the_runs_list_pages_back_and_filters_by_status() {
    runsAnswer = (query) => (query.get('before') ? [{ ...RUNS[0], id: 'old1' }]
      : Array.from({ length: Number(query.get('limit')) }, (_, i) => ({ ...RUNS[0], id: `n${i}` })));
    await boot('?machine=review');
    check(!$('olderRuns').hidden && $('runCount').textContent === '50+', `first page: ${$('runCount').textContent}`);
    const last = () => {
      const paths = CALLS.filter(([, path]) => path.includes('/runs?')).map(([, path]) => path);
      return new URLSearchParams(paths[paths.length - 1].split('?')[1]);
    };
    await $('olderRuns').fire('click', {});
    await settle();
    check(last().get('before') === 'n49', `older: ${last()}`);
    check($('runCount').textContent === '51' && $('olderRuns').hidden, `after: ${$('runCount').textContent}`);
    await Promise.all(DOC_LISTENERS.refresh.map((fn) => fn({ detail: { auto: true } })));
    await settle();
    check(last().get('limit') === '51' && !last().get('before'), `a refresh dropped the older page: ${last()}`);
    $('runStatus').value = 'failed';
    await $('runStatus').fire('change', {});
    await settle();
    check(last().get('status') === 'failed' && !last().get('before') && last().get('limit') === '50', `status: ${last()}`);
    await $('olderRuns').fire('click', {});
    await settle();
    await clickMachine('other');
    await settle();
    check(last().get('machine_id') === 'other' && last().get('limit') === '50', `another machine reads on: ${last()}`);
  },

  async run_again_starts_with_the_run_s_inputs_and_the_form_keeps_them_for_the_machine() {
    runAnswer = { ...RUN, mocks: { mocks: { write: 'a draft' }, mock_only: true } };
    await boot('?machine=review&run=r1');
    check($('runResult').innerHTML.includes('data-act="rerun"'), `no run again: ${$('runResult').innerHTML.slice(0, 300)}`);
    await $('runResult').fire('click', { target: element('button', { 'data-act': 'rerun' }) });
    await settle();
    const started = CALLS.filter(([method, path]) => method === 'POST' && path.endsWith('/runs')).map(([, , json]) => json);
    check(started.length === 1 && JSON.stringify(started[0].params) === '{"premise":"x"}'
      && JSON.stringify(started[0].mocks) === '{"write":"a draft"}' && started[0].mock_only === true, `started ${JSON.stringify(started)}`);
    check(JSON.parse(localStorage.getItem('stategraph:start-params:review')).values.premise === 'x', 'params not kept');
    check($('paramFields').innerHTML.includes('data-type="string">x</textarea>'), `form: ${$('paramFields').innerHTML}`);
  },

  async a_kept_default_follows_the_machine_s_default_and_a_choice_stays() {
    // A value kept as what its field sent untouched then is no choice: the form shows the param's default now. A
    // machine's engine param went from agent to claude_code, and a browser that kept agent sent it again at every
    // start. The values kept before (params:<id>) held every default sent, a choice and a default alike: unread.
    const engine = { type: 'string', default: 'claude_code', enum: ['agent', 'claude_code'] };
    const ratio = { type: 'number' };
    const named = { type: 'string' };  // a param named like an Object property finds nothing kept
    reviewAnswer = { ...MACHINE, graph: { ...MACHINE.graph, params: { ...MACHINE.graph.params, engine, ratio,
      constructor: named } } };
    OTHER.graph = { ...MACHINE.graph, params: { ...MACHINE.graph.params, engine, rounds: { type: 'integer', default: 4 },
      quiet: { type: 'boolean', default: true } } };
    localStorage.setItem('stategraph:params:review', '{"premise":"old","engine":"agent"}');
    localStorage.setItem('stategraph:params:other', '{"premise":"older"}');
    localStorage.setItem('stategraph:start-params:other', JSON.stringify({
      values: { premise: 'x', engine: 'agent', rounds: 7, quiet: false }, defaults: { engine: 'agent', rounds: 3, quiet: false } }));
    await boot('?machine=review');
    let form = $('paramFields').innerHTML;
    check(form.includes('value="1" selected>claude_code') && !form.includes('selected>agent'), `the old key's engine: ${form}`);
    check(form.includes('data-type="string"></textarea>'), `the old key's premise: ${form}`);
    check(localStorage.getItem('stategraph:params:review') === null, 'the old key stays');
    check(!form.includes('native code'), `an Object property in the form: ${form}`);
    // a step is an attribute, not text the kit escapes: step=&quot;any&quot; is no step, the browser takes 1
    check(form.includes('value="3" step="1">') && form.includes('data-type="number" value="" step="any">'),
      `the number fields' steps: ${form}`);
    await clickMachine('other');
    await settle();
    form = $('paramFields').innerHTML;
    check(form.includes('value="1" selected>claude_code') && !form.includes('selected>agent'), `a kept default: ${form}`);
    check(form.includes('data-type="boolean" checked'), `an unchecked box kept as the machine's default now: ${form}`);
    check(form.includes('data-type="integer" value="7"') && form.includes('data-type="string">x</textarea>'),
      `the choices are not shown: ${form}`);
    await $('startForm').fire('submit', {});
    await settle();
    const kept = JSON.parse(localStorage.getItem('stategraph:start-params:other'));
    check(JSON.stringify(kept.defaults) === '{"rounds":4,"engine":"claude_code","quiet":true}', `kept under: ${JSON.stringify(kept)}`);
  },

  async the_form_sends_no_default_so_the_run_takes_the_machine_s_own() {
    // A default sent as a value outlives a change of the machine's default: the run binds the old one, and a tab
    // drawn before the change sends it still. Sent only what differs, the run takes the default the machine has as
    // it starts. A box without a default is sent unchecked all the same (false, not none) and kept as untouched.
    const engine = { type: 'string', default: 'claude_code', enum: ['agent', 'claude_code'] };
    reviewAnswer = { ...MACHINE, graph: { ...MACHINE.graph, params: { ...MACHINE.graph.params, engine,
      teaser: { type: 'boolean', default: true }, quiet: { type: 'boolean' },
      mode: { type: 'string', enum: ['a', 'b'], required: true }, tags: { type: 'array', default: [] },
      // a boolean with an enum is drawn as a select (paramField asks enum first): untouched, it sends its first
      // option when required and nothing when not -- not a checkbox's false
      strict: { type: 'boolean', enum: [true, false], required: true }, loose: { type: 'boolean', enum: [true, false] } } } };
    await boot('?machine=review');
    const field = (param, type, value, checked = false) => {
      const input = element('input', { 'data-param': param, 'data-type': type });
      input.value = value;
      input.checked = checked;
      return $('paramFields').appendChild(input);
    };
    const fields = { premise: field('premise', 'string', 'p'), rounds: field('rounds', 'integer', '3'),
      engine: field('engine', 'enum', '1'), teaser: field('teaser', 'boolean', '', true), quiet: field('quiet', 'boolean', ''),
      mode: field('mode', 'enum', '0'), tags: field('tags', 'json', '[]') };
    const start = async () => {
      await $('startForm').fire('submit', {});
      await settle();
      return CALLS.filter(([method, path]) => method === 'POST' && path.endsWith('/runs')).map(([, , json]) => json.params).pop();
    };
    let sent = await start();
    check(JSON.stringify(sent) === '{"premise":"p","quiet":false,"mode":"a"}', `the defaults are sent: ${JSON.stringify(sent)}`);
    fields.engine.value = '0';
    fields.rounds.value = '5';
    fields.teaser.checked = false;
    sent = await start();
    check(JSON.stringify(sent) === '{"premise":"p","rounds":5,"engine":"agent","teaser":false,"quiet":false,"mode":"a"}',
      `the choices: ${JSON.stringify(sent)}`);
    const kept = JSON.parse(localStorage.getItem('stategraph:start-params:review'));
    check(JSON.stringify(kept.values) === JSON.stringify(sent) && kept.defaults.quiet === false
      && kept.defaults.engine === 'claude_code' && kept.defaults.mode === 'a' && kept.defaults.strict === true
      && !('loose' in kept.defaults), `kept: ${JSON.stringify(kept)}`);
  },

  async with_drafts_the_form_sends_what_it_shows() {
    // Unsaved, the form draws the draft's defaults while the run starts from the saved file: left out, a default
    // the draft changed would not be the one the run takes. So with drafts every value shown is sent.
    localStorage.removeItem('stategraph:autosave');
    await boot('?machine=review');
    await typeDraft();
    const input = element('input', { 'data-param': 'rounds', 'data-type': 'integer' });
    input.value = '3';
    $('paramFields').appendChild(input);
    await $('startForm').fire('submit', {});
    await settle();
    const sent = CALLS.filter(([method, path]) => method === 'POST' && path.endsWith('/runs')).map(([, , json]) => json.params).pop();
    check(JSON.stringify(sent) === '{"rounds":3}', `not what the form shows: ${JSON.stringify(sent)}`);
  },

  async the_run_box_says_which_defaults_the_run_took() {
    // The form sends only choices: the run's own params leave out what it ran with by default. Its root frame's
    // bound params show it.
    const root = { ...RUN.view.frames[0], params: { premise: 'x', rounds: 3, engine: 'claude_code', none: null } };
    runAnswer = { ...RUN, params: { premise: 'x' }, view: { ...RUN.view, frames: [root, ...RUN.view.frames.slice(1)] } };
    await boot('?machine=review&run=r1');
    const box = $('dbgRun').innerHTML;
    const defaults = box.split('<dt>Defaults</dt>')[1] || '';
    check(defaults.includes('claude_code') && defaults.includes('rounds') && !defaults.includes('premise')
      && !defaults.includes('none'), `the run box: ${box}`);
  },

  async run_again_leaves_out_what_was_the_run_s_default_then() {
    // A run a panel started before held every default it sent. Run again sends only what differed from the
    // defaults the run had (param_defaults, from its definition): the machine's default now applies, a changed one
    // too, and the form does not keep the old default as a choice.
    const engine = { type: 'string', default: 'claude_code', enum: ['agent', 'claude_code'] };
    reviewAnswer = { ...MACHINE, graph: { ...MACHINE.graph, params: { ...MACHINE.graph.params, engine } } };
    runAnswer = { ...RUN, params: { premise: 'x', rounds: 7, engine: 'agent' }, param_defaults: { rounds: 3, engine: 'agent' } };
    await boot('?machine=review&run=r1');
    await $('runResult').fire('click', { target: element('button', { 'data-act': 'rerun' }) });
    await settle();
    const started = CALLS.filter(([method, path]) => method === 'POST' && path.endsWith('/runs')).map(([, , json]) => json);
    check(started.length === 1 && JSON.stringify(started[0].params) === '{"premise":"x","rounds":7}',
      `started ${JSON.stringify(started)}`);
    const form = $('paramFields').innerHTML;
    check(form.includes('value="1" selected>claude_code') && !form.includes('selected>agent'), `the form's engine: ${form}`);
    check(form.includes('data-type="string">x</textarea>') && form.includes('data-type="integer" value="7"'),
      `the run's choices are not in the form: ${form}`);
  },

  async an_apply_keeps_what_another_field_form_holds_without_asking() {
    await boot('?machine=review');
    await choose('write');
    const fields = element('form', { 'data-form': 'state-fields' });
    await $('side-inspect').fire('input', { target: fields.appendChild(element('input', { name: 'description' })) });
    const other = formOf('transition', { target: ['enum', 'review', 'write'] });
    other.setAttribute('data-transition', 'write#0');
    await $('side-inspect').fire('submit', { target: other });
    await settle();
    check(!confirms() && lastEdit()?.op === 'update_transition', `asked ${confirms()}, sent ${JSON.stringify(lastEdit())}`);
  },

  async an_edit_is_undone_with_the_file_as_it_was_and_auto_layout_asks_first() {
    editAnswer = { ...MACHINE, files: { ...MACHINE.files, 'review.yaml': 'changed' }, versions: { ...MACHINE.versions, 'review.yaml': 'v2' } };
    await boot('?machine=review');
    check($('undo').disabled, 'undo offered before any edit');
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    check(!$('undo').disabled, 'no undo after an edit');
    editAnswer = null;
    await $('undo').fire('click', {});
    await settle();
    const put = CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/machines/review')).map(([, , json]) => json);
    check(put.length === 1 && put[0].files['review.yaml'] === MACHINE.files['review.yaml']
      && put[0].expected_versions['review.yaml'] === 'v2' && put[0].force === true, `put ${JSON.stringify(put)}`);
    check($('undo').disabled && !$('redo').disabled, `after the undo: undo ${$('undo').disabled}, redo ${$('redo').disabled}`);
    await $('canvas').fire('keydown', { key: 'y', ctrlKey: true, target: $('canvas'), preventDefault() {} });
    await settle();
    const puts = () => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/machines/review')).map(([, , json]) => json);
    const redone = puts()[1];
    check(redone && redone.files['review.yaml'] === 'changed' && redone.expected_versions['review.yaml'] === 'v1'
      && redone.force === true, `redo put ${JSON.stringify(redone)}`);
    check(!$('undo').disabled && $('redo').disabled, 'after the redo: undo offered, redo not');
    await $('canvas').fire('keydown', { key: 'z', ctrlKey: true, target: $('canvas'), preventDefault() {} });
    await settle();
    check(puts().length === 3 && puts()[2].files['review.yaml'] === MACHINE.files['review.yaml'] && !$('redo').disabled,
      `Ctrl+Z: ${JSON.stringify(puts()[2])}`);
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    check($('redo').disabled, 'a new edit left the undone step to redo over it');
    const before = puts().length;
    await Promise.all([1, 2].map(() => $('canvas').fire('keydown', { key: 'z', ctrlKey: true, target: $('canvas'), preventDefault() {} })));
    await settle();
    check(puts().length === before + 1, `a repeated Ctrl+Z undid ${puts().length - before} times at once`);
    const layouts = () => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/layout')).length;
    const asked = confirms();
    ANSWERS.confirm = false;
    await $('autoLayout').fire('click', {});
    await settle();
    check(confirms() === asked + 1 && layouts() === 0, `laid out unasked: ${confirms()} ${layouts()}`);
    ANSWERS.confirm = true;
    await $('autoLayout').fire('click', {});
    await settle();
    check(layouts() === 1, 'not laid out after yes');
  },

  async the_wheel_scrolls_the_graph_ctrl_wheel_zooms_and_the_search_finds_a_state() {
    await boot('?machine=review');
    const view = () => $('canvas').querySelector('.sg-viewport').getAttribute('transform');
    const scale = () => Number(view().match(/scale\(([^)]+)\)/)[1]);
    const place = () => view().match(/translate\(([-\d.e]+) ([-\d.e]+)\)/).slice(1).map(Number);
    const [x, y] = place();
    const k = scale();
    await $('canvas').fire('wheel', { deltaX: 0, deltaY: 120, deltaMode: 0, clientX: 100, clientY: 100 });
    check(place()[0] === x && place()[1] === y - 120 && scale() === k, `scrolled: ${view()}`);
    await $('canvas').fire('wheel', { deltaX: 0, deltaY: -120, deltaMode: 0, ctrlKey: true, clientX: 100, clientY: 100 });
    check(scale() > k, `zoomed: ${view()}`);
    check($('sgStates').innerHTML.includes('value="verdict"'), 'states offered');
    $('stateSearch').value = 'verd';
    await $('stateSearch').fire('change', {});
    await settle();
    check($('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">verdict</h3>'), 'not at verdict');
    $('stateSearch').value = 'e';
    await $('stateSearch').fire('change', {});
    check(TOASTS.some(([, text]) => /^\d+ states match: /.test(text)), `no word on several: ${JSON.stringify(TOASTS)}`);
  },

  async a_machine_is_duplicated_under_a_new_id_with_its_own_module() {
    await boot('?machine=ro');
    ANSWERS.prompt.push('ro_copy');
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'duplicate-machine' }) });
    await settle();
    const puts = (id) => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith(`/machines/${id}`)).map(([, , json]) => json);
    const prompts = ASKED.filter(([kind]) => kind === 'prompt');
    check(prompts[0][2] === 'ro_copy', `offered ${JSON.stringify(prompts)}`);
    const ro = puts('ro_copy')[0];
    check(ro && Object.keys(ro.files).join() === 'ro_copy.yaml', `read-only copy: ${JSON.stringify(ro && Object.keys(ro.files))}`);
    check(ro.files['ro_copy.yaml'].includes('\nid: ro_copy\n') && !ro.files['ro_copy.yaml'].includes('id: review'), 'the id stays');
    check(CALLS.some(([method, path]) => method === 'PUT' && path.endsWith('/machines/ro_copy/layout')), 'layout not copied');
    plainAnswer = { ...PLAIN, graph: { ...PLAIN.graph, python: 'plain.py', imports: { sub: './sub.yaml' } },
      files: { 'plain.yaml': PLAIN_TEXT.replace('title: Plain\n', 'title: Plain\npython: plain.py   # its module\nimports: {sub: ./sub.yaml}\n'),
        'plain.py': 'X = 1\n', 'sub.yaml': 'stategraph: 1\n' } };
    await clickMachine('plain');
    await settle();
    ANSWERS.prompt.push('plain_copy');
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'duplicate-machine' }) });
    await settle();
    const plain = puts('plain_copy')[0];
    check(plain && Object.keys(plain.files).sort().join() === 'plain_copy.py,plain_copy.yaml'
      && plain.files['plain_copy.yaml'].includes('\npython: plain_copy.py\n') && plain.files['plain_copy.py'] === 'X = 1\n'
      && plain.files['plain_copy.yaml'].includes('imports: {sub: sub}\n'), `writable copy: ${JSON.stringify(plain)}`);
  },

  async a_narrow_panel_folds_the_machine_list_once_a_machine_is_open_and_offers_the_palette_as_a_menu() {
    let narrow = false;
    globalThis.matchMedia = (query) => ({ matches: narrow && query === '(max-width: 900px)' });
    await boot('?machine=review');
    check(!$('machinesPane').hidden, 'folded wide');
    narrow = true;
    await clickMachine('other');
    await settle();
    check($('machinesPane').hidden === true, 'not folded narrow');
    check($('paletteMenu').innerHTML.includes('data-add-kind="agent"') && $('paletteMenu').innerHTML.includes('class="pk-menu-item"'), 'no menu');
    ANSWERS.prompt.push('from_menu');
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-type': 'final' }) });
    await settle();
    check(lastEdit()?.op === 'add_state' && lastEdit().type === 'final' && lastEdit().name === 'from_menu', `edit ${JSON.stringify(lastEdit())}`);
  },

  async a_renamed_state_stays_shown_with_what_its_forms_hold() {
    const renamed = (name) => (name === 'write' ? 'written' : name);
    editAnswer = { ...MACHINE, graph: { ...MACHINE.graph,
      initial: 'written',
      states: MACHINE.graph.states.map((s) => ({ ...s, name: renamed(s.name) })),
      transitions: MACHINE.graph.transitions.map((t) => ({ ...t, source: renamed(t.source), target: renamed(t.target),
        id: t.id.replace(/^write#/, 'written#') })) } };
    await boot('?machine=review');
    await choose('write');
    ANSWERS.prompt.push('written');
    const drawn = RENDERS.length;
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'rename' }) });
    await settle();
    check(lastEdit()?.op === 'rename_state' && $('side-inspect').innerHTML.includes('<h3 class="sg-inspect-name">written</h3>'),
      `after the rename: ${$('side-inspect').innerHTML.slice(0, 300)}`);
    // the edit's own redraw shows the renamed state -- not the overview, whose redraw drops what the forms held
    check(!RENDERS.slice(drawn).some(([id, , shown]) => id === 'side-inspect' && shown.includes('data-form="machine-fields"')),
      'the overview was drawn over the renamed state');
  },

  async a_timer_state_says_so_and_offers_its_after() {
    fieldsAnswer = { ...FIELDS, graph: { ...FIELDS.graph,
      states: FIELDS.graph.states.map((s) => (s.name === 'idle' ? { ...s, after: '10m' } : s)) } };
    await boot('?machine=fields');
    await choose('idle');
    const shown = $('side-inspect').innerHTML;
    check(shown.includes('timer 10m') && /id="sf-after"[^>]*value="10m"|value="10m"[^>]*id="sf-after"/.test(shown), `the timer: ${shown}`);
  },

  async a_machine_without_an_agent_offers_the_entry_that_makes_one() {
    await boot('?machine=review');
    const shown = $('side-inspect').innerHTML;
    check(shown.includes('As an agent') && shown.includes('No agent runs this machine'), 'no agent section');
    const block = (shown.match(/id="agentEntry">([^<]*)<\/pre>/) || [])[1] || '';
    check(block.startsWith('agent:\n  name: review_agent') && block.includes('input: json') && !block.includes('task_param')
      && block.includes('on_wait: ask') && block.includes('visibility: tool'), `the block: ${block || shown}`);
    check(shown.includes('<details class="pk-details"><summary>agent: block'), 'the block is not there, or not folded');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'copy-agent-entry' }) });
    check((globalThis.COPIED || []).some((text) => text.startsWith('agent:\n  name: review_agent')),
      `copied ${JSON.stringify(globalThis.COPIED)}`);
  },

  async a_machine_whose_block_is_not_declared_yet_says_a_restart_offers_it() {
    fieldsAnswer = { ...FIELDS, offer: { name: 'fields_agent', declared: false } };
    await boot('?machine=fields');
    const shown = $('side-inspect').innerHTML;
    check(shown.includes('offers it as') && shown.includes('fields_agent') && shown.includes('after a restart')
      && !shown.includes('summary>agent: block'), `not yet declared: ${shown}`);
    check(/id="mf-agent"/.test(shown), 'the settings have no agent field');
  },

  async a_machine_that_offers_itself_says_under_which_name() {
    fieldsAnswer = { ...FIELDS, offer: { name: 'fields_agent', declared: true },
      agents: [{ name: 'fields_agent', visibility: 'tool', input: 'text', on_wait: 'ask', problems: [] }] };
    await boot('?machine=fields');
    const shown = $('side-inspect').innerHTML;
    check(shown.includes('offers it as') && shown.includes('fields_agent') && !shown.includes('after a restart')
      && !shown.includes('summary>agent: block'), `declared: ${shown}`);
  },

  async a_machine_with_one_param_takes_the_message_as_it_and_lists_its_agents() {
    fieldsAnswer = { ...FIELDS, agents: [{ name: 'fields_agent', visibility: 'private', input: 'text', on_wait: 'block',
      problems: ['task_param task is no param of fields'] }] };
    await boot('?machine=fields');
    const shown = $('side-inspect').innerHTML;
    check(shown.includes('input: text') && shown.includes('task_param: text'), `one param: ${shown}`);
    check(shown.includes('fields_agent') && shown.includes('task_param task is no param of fields')
      && shown.includes('A private agent is reached only by its name'), 'the agent, its problem and what private means');
    check(!shown.includes('open><summary>agent: block') && shown.includes('summary>agent: block'),
      'the block is folded where an agent runs it');
  },

  async a_machine_whose_one_param_is_no_text_takes_a_json_message() {
    fieldsAnswer = { ...FIELDS, graph: { ...FIELDS.graph, params: { n: { type: 'integer', required: true } } } };
    await boot('?machine=fields');
    const shown = $('side-inspect').innerHTML;
    const block = (shown.match(/id="agentEntry">([^<]*)<\/pre>/) || [])[1] || '';
    check(block.includes('input: json') && !block.includes('task_param'), `a number is no message: ${block || shown}`);
    check(block.includes('called as a tool it waits'), 'what ask does as a tool');
  },

  async an_activitys_error_shows_its_traceback_input_failed_attempts_and_a_copyable_request() {
    journalOf = { ...journalOf, r2: [{ seq: 3, kind: 'activity', key: 's1', state: 'write', status: 'error',
      data: { kind: 'call', path: 'write', inputs: { call: 'explode', args: { n: 7 } },
        error: { type: 'call_failed', message: "KeyError: 'missing'", data: { traceback: 'File "m.py", line 13, in explode' },
          cause: { type: 'tool_failed', message: 'deeper' } },
        meta: { request_id: 'r2_004', attempts: 2, failures: [{ attempt: 1, type: 'call_failed', message: 'ValueError: first' }] } } }] };
    await boot('?machine=review&run=r2');
    const details = element('details', { 'data-result': '3' });
    details.open = true;
    const body = details.appendChild(element('div', { class: 'sg-result-body' }));
    await $('runResult').fire('toggle', { target: details });
    const shown = body.innerHTML;
    check(shown.includes('File &quot;m.py&quot;, line 13, in explode') || shown.includes('File "m.py", line 13, in explode'),
      `no traceback: ${shown}`);
    check(shown.includes('caused by tool_failed: deeper'), 'no cause');
    check(shown.includes('<summary>Input</summary>') && shown.includes('explode'), 'no input');
    check(shown.includes('Failed attempts (1)') && shown.includes('ValueError: first'), 'no failed attempts');
    check(shown.includes('data-copy="r2_004"'), 'the request id cannot be copied');
    await $('runResult').fire('click', { target: element('button', { 'data-copy': 'r2_004' }) });
    await settle();
    check(JSON.stringify(globalThis.COPIED) === '["r2_004"]', `copied ${JSON.stringify(globalThis.COPIED)}`);
  },

  async a_run_that_took_no_transition_shows_the_guards_it_evaluated() {
    runAnswer = { ...RUN, status: 'failed', error: { type: 'no_transition', message: 'read completed and no completion transition is enabled',
      state: 'read', data: { guards: [{ at: 'read.transitions', guard: 'ctx.round > 3', result: false }] } } };
    await boot('?machine=review&run=r1');
    const result = $('runResult').innerHTML;
    check(result.includes('Guards evaluated') && result.includes('ctx.round &gt; 3') && result.includes('false'),
      `the guards: ${result}`);
  },

  async the_history_shows_the_time_and_one_kind_of_row_on_request() {
    await boot('?machine=review&run=r1');
    check($('runHistory').innerHTML.includes('<th>Time</th>'), 'no time column');
    const traces = RUN.journal.filter((row) => row.kind === 'trace').length;
    check(traces > 0 && traces < RUN.journal.length, 'fixture: the run has trace rows and others');
    await $('runHistory').fire('change', { target: Object.assign(element('select', { id: 'historyKind' }), { value: 'trace' }) });
    const rows = ($('runHistory').innerHTML.match(/<tr>/g) || []).length - 1;  // less the head row
    check(rows === traces, `${rows} rows shown, ${traces} traces`);
  },

  async a_fork_can_be_held_at_its_fork_point() {
    await boot('?machine=review&run=r1');
    ANSWERS.prompt.push('two', '2');  // a step that is no number is asked again
    ANSWERS.dialog = 'hold';
    await $('debugBar').fire('click', { target: element('button', { 'data-control': 'fork' }) });
    await settle();
    const sent = CALLS.filter(([, path]) => path.endsWith('/control')).pop();
    check(sent && sent[2].action === 'fork' && sent[2].at_step === 2 && sent[2].pause === true && sent[2].definition === 'snapshot',
      `sent ${JSON.stringify(sent && sent[2])}`);
    check(ASKED.filter(([kind]) => kind === 'prompt').length === 2, `asked ${JSON.stringify(ASKED)}`);
    const forks = () => CALLS.filter(([, path, json]) => path.endsWith('/control') && json.action === 'fork').length;
    ANSWERS.prompt.push('2');
    ANSWERS.dialog = null;  // cancelled at the second question
    await $('debugBar').fire('click', { target: element('button', { 'data-control': 'fork' }) });
    await settle();
    check(forks() === 1, 'a cancelled fork was sent');
  },

  async a_waiting_frame_says_what_it_waits_for_and_since_when() {
    const frames = RUN.view.frames.map((f) => ({ ...f, accepts: ['approve'], waiting_since: '2026-09-27T10:00:00.000+00:00',
      deadline: '2026-09-27T11:00:00.000+00:00' }));
    runAnswer = { ...RUN, status: 'waiting', view: { ...RUN.view, frames } };
    await boot('?machine=review&run=r1');
    const shown = $('dbgFrames').innerHTML;
    check(shown.includes('waits for approve since') && shown.includes('until'), `frames: ${shown}`);
  },

  async the_result_shows_every_activitys_answer_and_the_end_states() {
    await boot('?machine=review&run=r2');
    const result = $('runResult').innerHTML;
    check(result.includes('data-run="r2"'), 'no result card');
    check(result.includes('data-open-session="sg_r2"'), 'the session of the run is not offered');
    check(/End states[\s\S]*critique · verdict[\s\S]*failed[\s\S]*done/.test(result), `end states: ${result}`);
    check(result.includes('data-result="3"') && result.includes('data-result="5"') && !/data-result="[89]"/.test(result),
      'two finished activities, the traces are not activities');
    check(result.includes('scene_writer') && result.includes('timeout'), 'who ran and how it failed');
    const details = element('details', { 'data-result': '3' });
    details.open = true;
    const body = details.appendChild(element('div', { class: 'sg-result-body' }));
    await $('runResult').fire('toggle', { target: details });
    check(body.innerHTML.includes('the whole draft, not only its first line'), `the answer in full: ${body.innerHTML}`);
    check(body.innerHTML.includes('data-open-session="inst-1"'), 'the agent\'s session is offered');
    await $('runResult').fire('click', { target: element('button', { 'data-open-session': 'inst-1' }) });
    check(JSON.stringify(globalThis.OPENED) === '["inst-1"]', `opened ${JSON.stringify(globalThis.OPENED)}`);
  },

  async a_live_runs_result_reads_on_from_where_it_stopped() {
    runAnswer = { ...RUN, mocks: { mock_only: true } };
    await boot('?machine=review&run=r1');
    check(!$('runResult').innerHTML.includes('data-open-session="sg_r1"'), 'a mock-only run has no session to open');
    const reads = () => CALLS.filter(([, path]) => path.includes('/runs/r1/journal')).map(([, path]) => path);
    check(reads().length === 1 && reads()[0].includes('after=0'), `first read: ${JSON.stringify(reads())}`);
    const next = { seq: 7, kind: 'activity', key: 's5', state: 'write', status: 'done',
      data: { kind: 'agent', path: 'write', out: 'a second draft', meta: {} } };
    journalOf = { ...journalOf, r1: [...RUN.journal, next] };
    runAnswer = { ...RUN, journal: [...RUN.journal, next] };
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    check(reads().length === 2 && reads()[1].includes('after=4'), `read on: ${JSON.stringify(reads())}`);
    check($('runResult').innerHTML.includes('a second draft'), 'the new activity is not shown');
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    check(reads().length === 2, 'a poll with nothing new read the journal again');
  },

  async machines_sit_in_their_folders_and_a_closed_folder_stays_closed() {
    await boot('?machine=hooks');
    const list = () => $('machineList').innerHTML;
    const folder = (path) => new RegExp(`<details class="sg-folder" data-folder="${path}" ?(open)?>`).exec(list());
    check(folder('Writer') && folder('Writer/v6') && folder('My machines') && folder('stategraph'), `folders: ${list()}`);
    check(/data-folder="Writer\/v6"[\s\S]*data-machine="review"[\s\S]*<\/details>[\s\S]*data-machine="other"/.test(list()),
      'review sits in Writer/v6, other in Writer after it');
    check(list().indexOf('data-folder="Writer"') < list().indexOf('data-folder="Writer/v6"'), 'v6 is a folder inside Writer');
    check(folder('Writer/v6')[1] === 'open', 'folders start open');
    const closing = element('details', { 'data-folder': 'Writer/v6' });
    closing.open = false;
    await $('machineList').fire('toggle', { target: closing });
    check(localStorage.getItem('stategraph:closed-folders') === '["Writer/v6"]', `kept: ${localStorage.getItem('stategraph:closed-folders')}`);
    await $('search').fire('input', {});
    check(!folder('Writer/v6')[1], 'the closed folder opened again');
    $('search').value = 'review';
    await $('search').fire('input', {});
    check(folder('Writer/v6')[1] === 'open' && !list().includes('data-machine="hooks"'), 'a search opens what it finds, and only that');
  },

  async a_machine_the_author_made_can_be_deleted_and_a_shipped_one_offers_no_delete() {
    await boot('?machine=ro');
    check(!$('machineHead').innerHTML.includes('data-act="delete-machine"'), 'a read-only machine offers delete');
    await clickMachine('review');
    await settle();
    check($('machineHead').innerHTML.includes('data-act="delete-machine"'), 'no delete for an own machine');
    ANSWERS.confirm = false;
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'delete-machine' }) });
    await settle();
    check(!CALLS.some(([method]) => method === 'DELETE'), 'deleted without a yes');
    const lists = () => CALLS.filter(([method, path]) => method === 'GET' && path.endsWith('/api/machines')).length;
    const listed = lists();
    ANSWERS.confirm = true;
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'delete-machine' }) });
    await settle();
    const sent = CALLS.find(([method]) => method === 'DELETE');
    check(sent && sent[1].endsWith('/machines/review') && sent[2].expected_version === 'v1', `sent ${JSON.stringify(sent)}`);
    check($('machineView').hidden && !$('placeholder').hidden, 'the deleted machine is still shown');
    check(lists() === listed + 1, 'the list was not reloaded');
  },

  async an_activity_read_while_it_ran_is_read_again_when_it_ends() {
    // the journal rewrites an activity's row in place: started, then done under the same seq
    const started = { seq: 7, kind: 'activity', key: 's5', state: 'write', status: 'started', data: { kind: 'agent', path: 'write' } };
    const done = { ...started, status: 'done', data: { ...started.data, out: 'the answer it gave at last', meta: {} } };
    journalOf = { ...journalOf, r1: [...RUN.journal, started] };
    runAnswer = { ...RUN, journal: [...RUN.journal, started] };
    await boot('?machine=review&run=r1');
    check(!$('runResult').innerHTML.includes('data-result="7"'), 'a running activity is shown as finished');
    journalOf = { ...journalOf, r1: [...RUN.journal, done] };
    runAnswer = { ...RUN, journal: [...RUN.journal, done] };
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    check($('runResult').innerHTML.includes('the answer it gave at last'), 'the activity that ended is never read again');
  },

  async a_poll_leaves_a_result_read_that_is_out_alone() {
    await boot('?machine=review&run=r1');
    const next = { seq: 7, kind: 'activity', key: 's5', state: 'write', status: 'done', data: { kind: 'agent', out: 'x', meta: {} } };
    journalOf = { ...journalOf, r1: [...RUN.journal, next] };
    runAnswer = { ...RUN, journal: [...RUN.journal, next] };
    HOLD = (method, path) => path.includes('/runs/r1/journal');
    const reads = () => CALLS.filter(([, path]) => path.includes('/runs/r1/journal')).length;
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    check(reads() === 2 && PENDING.length === 1, `reads out: ${reads()}`);
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    check(reads() === 2 && !ABORTED.some((path) => path.includes('/journal')), `polls restarted the read: ${reads()}, ${ABORTED}`);
    await release();
    check($('runResult').innerHTML.includes('data-result="7"'), 'the read that was left alone did not finish');
  },

  async a_run_switched_to_takes_the_result_of_the_one_left_away_at_once() {
    await boot('?machine=review&run=r2');
    check($('runResult').innerHTML.includes('data-run="r2"'), 'no result of r2');
    HOLD = (method, path) => path.includes('/runs/r1');
    await $('runList').fire('rowselect', { detail: { id: 'r1' } });
    await settle();
    check(!$('runResult').innerHTML.includes('data-run="r2"'), 'the result of the run left stays under the next one');
    await release();
  },

  async text_typed_into_one_inspector_form_is_asked_about_by_another() {
    await boot('?machine=review');
    await choose('write');
    const stateForm = element('form', { 'data-form': 'set-state' });
    await $('side-inspect').fire('input', { target: stateForm.appendChild(element('textarea', { name: 'yaml' })) });
    const drawn = RENDERS.filter(([id]) => id === 'side-inspect').length;
    await choose('write');  // the same state again: a click, Enter, the first click of a double click
    await settle();
    check(RENDERS.filter(([id]) => id === 'side-inspect').length === drawn && !confirms(), 'choosing it again redrew over the text');
    ANSWERS.confirm = false;
    const other = formOf('transition', { target: ['enum', 'review', 'write'] });
    other.setAttribute('data-transition', 'write#0');
    await $('side-inspect').fire('submit', { target: other });
    await settle();
    check(confirms() === 1 && !CALLS.some(([, path]) => path.endsWith('/edit')), 'another form applied over the text unasked');
    $('yamlText').value = `${MACHINE.files['review.yaml']}\n# unsaved\n`;
    await $('yamlText').fire('input', {});
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'save' }) });
    await settle();
    check(confirms() === 2 && !CALLS.some(([method]) => method === 'PUT'), 'a YAML save reloaded over the inspector unasked');
  },

  async a_deleted_machine_leaves_nothing_of_itself_behind() {
    localStorage.setItem('stategraph:breakpoints:review', '[{"state":"write","at":"enter"}]');
    localStorage.setItem('stategraph:params:review', '{"premise":"secret"}');
    localStorage.setItem('stategraph:start-params:review', '{"values":{"premise":"secret"},"defaults":{}}');
    await boot('?machine=review');
    check($('paramFields').innerHTML.includes('>secret</textarea>'), 'the kept params are not in the form');
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'initial' }) });  // an undo step
    await settle();
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'delete-machine' }) });
    await settle();
    check(!$('side-inspect').innerHTML.includes('sg-inspect-name'), 'the inspector still shows the deleted machine');
    check(localStorage.getItem('stategraph:params:review') === null
      && localStorage.getItem('stategraph:start-params:review') === null, 'its params stay');
    await clickMachine('review');  // a machine of the same id again (made anew): the old steps are not its own
    await settle();
    check(!$('paramFields').innerHTML.includes('secret'), 'its form keeps the params');
    check($('undo').disabled, 'its undo steps stay');
    check(localStorage.getItem('stategraph:breakpoints:review') === null, 'its breakpoints wait for a machine of the same id');
  },

  async the_sessions_of_another_users_run_are_not_offered() {
    document.querySelector = (selector) => (selector === '.sg-layout' ? { dataset: { viewer: 'ada' } } : null);
    await boot('?machine=review&run=r2');  // r2 is nobody's: its sessions are not ada's
    check($('runResult').innerHTML.includes('data-run="r2"'), 'no result of r2');
    check(!$('runResult').innerHTML.includes('data-open-session'), 'a session the chat cannot open is offered');
  },

  async a_running_composite_does_not_make_every_poll_read_again() {
    const composite = { seq: 7, kind: 'activity', key: 's5', state: 'read', status: 'started', data: { kind: 'machine', path: 'read' } };
    const child = { seq: 8, kind: 'activity', key: 's5/a1/m/s0', state: 'done', status: 'done', data: { kind: 'agent', path: 'read/done', out: 'c', meta: {} } };
    journalOf = { ...journalOf, r1: [...RUN.journal, composite, child] };
    runAnswer = { ...RUN, journal: [...RUN.journal, composite, child] };
    await boot('?machine=review&run=r1');
    const reads = () => CALLS.filter(([, path]) => path.includes('/runs/r1/journal')).length;
    for (let i = 0; i < 3; i += 1) {
      POLLERS.find((p) => p.ms === 1000).fn();
      await settle();
    }
    check(reads() === 1, `polls read again while the composite ran: ${reads()}`);
    const ended = { ...composite, status: 'done', data: { ...composite.data, out: 'the submachine is done', meta: {} } };
    journalOf = { ...journalOf, r1: [...RUN.journal, ended, child] };
    runAnswer = { ...RUN, journal: [...RUN.journal, ended, child] };
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    const last = CALLS.filter(([, path]) => path.includes('/runs/r1/journal')).pop()[1];
    check(last.includes('kinds=activity&after=6&limit=1'), `the ended composite not read alone: ${last}`);
    check($('runResult').innerHTML.includes('the submachine is done'), 'the composite that ended is not shown');
  },

  async a_run_that_ends_while_its_result_is_read_is_read_to_its_end() {
    await boot('?machine=review&run=r1');
    const done = { seq: 7, kind: 'activity', key: 's5', state: 'write', status: 'done', data: { kind: 'agent', out: 'x', meta: {} } };
    const final = { seq: 9, kind: 'trace', key: 's6:final:done', state: 'done', status: 'final', data: { frame: '', status: 'succeeded' } };
    journalOf = { ...journalOf, r1: [...RUN.journal, done] };
    runAnswer = { ...RUN, journal: [...RUN.journal, done] };
    HOLD = (method, path) => path.includes('/runs/r1/journal');
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    runAnswer = { ...RUN, status: 'succeeded', active: false, final_state: 'done', journal: [...RUN.journal, done, final] };
    POLLERS.find((p) => p.ms === 1000).fn();  // the run ended: the poller stops after this answer
    await settle();
    // the read that was out is served as the journal stood before the end ...
    PENDING.shift().resolve();
    await settle();
    // ... and only a read after it can find the end
    journalOf = { ...journalOf, r1: [...RUN.journal, done, final] };
    HOLD = () => false;
    await release();
    await settle();
    check($('runResult').innerHTML.includes('End states'), 'the end of the run is never read');
  },

  async a_conflict_says_that_a_reload_drops_the_inspectors_text() {
    await boot('?machine=review');
    await choose('write');
    const form = element('form', { 'data-form': 'set-state' });
    form.elements.yaml.value = 'description: typed';
    await $('side-inspect').fire('input', { target: form.appendChild(element('textarea', { name: 'yaml' })) });
    editAnswer = new ApiError(409, 'the file changed since you read it');
    ANSWERS.dialog = null;
    await $('side-inspect').fire('submit', { target: form });
    await settle();
    const asked = ASKED.find(([kind, title]) => kind === 'dialog' && title === 'The file changed');
    check(asked && asked[2].includes('drops what you typed'), `dialog: ${JSON.stringify(asked)}`);
    check(DIRTY, 'Cancel lost the typed text');
  },

  async a_run_that_ended_reads_the_activities_it_still_had_running() {
    const running = { seq: 7, kind: 'activity', key: 's5', state: 'write', status: 'started', data: { kind: 'agent', path: 'write' } };
    journalOf = { ...journalOf, r1: [...RUN.journal, running] };
    runAnswer = { ...RUN, journal: [...RUN.journal, running] };
    await boot('?machine=review&run=r1');
    const ended = { ...running, status: 'done', data: { ...running.data, out: 'ended past the window', meta: {} } };
    journalOf = { ...journalOf, r1: [...RUN.journal, ended] };
    // the run's answer carries its last rows only: the activity's end lies before them
    runAnswer = { ...RUN, status: 'succeeded', active: false, final_state: 'done', journal: RUN.journal.slice(0, 2) };
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    check($('runResult').innerHTML.includes('ended past the window'), 'an activity that ended out of the window stays running');
  },

  async a_companion_module_is_added_as_two_drafts_and_saved_with_them() {
    await boot('?machine=plain');
    check(!$('yamlAddModule').hidden, 'a writable machine without a module offers none');
    await $('yamlAddModule').fire('click', {});
    await settle();
    const options = $('yamlFile').innerHTML;
    check(options.includes('plain.py (unsaved)') && options.includes('plain.yaml (unsaved)'), `the drafts are not offered: ${options}`);
    check($('yamlText').value.startsWith('"""Companion module of plain.yaml.') && $('yamlText').dataset.lang === 'python',
      `the new module is not open as Python: ${$('yamlText').value.slice(0, 60)}`);
    check(DIRTY && $('yamlAddModule').hidden, 'the draft already names a module: no second one');
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'save' }) });
    await settle();
    const put = (CALLS.find(([method, path]) => method === 'PUT' && path.endsWith('/machines/plain')) || [])[2];
    check(put, 'not saved');
    check(put.files['plain.yaml'] === PLAIN_TEXT.replace('id: plain\n', 'id: plain\npython: plain.py\n')
      && put.files['plain.py'].includes('Companion module'), `saved without the module: ${JSON.stringify(put.files)}`);
    check(put.expected_versions['plain.yaml'] === 'v1' && !('plain.py' in put.expected_versions), 'a new file claims a version');
    check(!DIRTY && $('yamlFile').innerHTML.includes('plain.py') && !$('yamlFile').innerHTML.includes('(unsaved)'),
      'after the save the module is not a file of the machine');
  },

  async a_python_file_is_coloured_as_python_and_indented_by_four() {
    globalThis.Prism = { languages: { python: {} }, highlight: (text) => `<i class="token">${text.replace(/</g, '&lt;')}</i>` };
    const view = new FakeElement('pre');
    view.classList.add('sg-code-view');
    $('yamlText').previousElementSibling = view;
    await boot('?machine=review');
    check(!view.innerHTML.includes('class="token"'), 'the YAML file is coloured as Python');
    $('yamlFile').value = 'review.py';
    await $('yamlFile').fire('change', {});
    check(view.innerHTML.includes('<i class="token">def f():'), `the module is not coloured as Python: ${view.innerHTML.slice(0, 80)}`);
    await $('yamlText').fire('keydown', { key: 'Tab', target: $('yamlText'), preventDefault() {} });
    check($('yamlText').value.endsWith('\n    '), 'Tab in Python does not type four spaces');
    $('yamlFile').value = 'review.yaml';
    await $('yamlFile').fire('change', {});
    check(!view.innerHTML.includes('class="token"'), 'back in the YAML file, it stays coloured as Python');
  },

  async a_machine_that_names_its_module_or_cannot_be_written_offers_none() {
    await boot('?machine=ro');
    check($('yamlAddModule').hidden, 'a read-only machine offers a module');
    plainAnswer = { ...PLAIN, files: { 'plain.yaml': PLAIN_TEXT.replace('title:', 'python: plain.py\ntitle:') } };
    await clickMachine('plain');
    await settle();
    await release();
    await settle();
    check($('yamlAddModule').hidden, 'a machine that names its module offers another');
  },

  async a_module_line_typed_into_the_yaml_or_a_missing_id_line_adds_nothing() {
    await boot('?machine=plain');
    for (const typed of [`${PLAIN_TEXT}python: mine.py\n`, PLAIN_TEXT.replace('id: plain', '{id: plain}'),
      PLAIN_TEXT.replace('id: plain', 'id:\n  plain')]) {
      $('yamlText').value = typed;
      await $('yamlText').fire('input', {});
      await $('yamlAddModule').fire('click', {});
      await settle();
      check(!$('yamlFile').innerHTML.includes('plain.py') && $('yamlText').value === typed, `a module was added over: ${typed}`);
    }
    check(TOASTS.filter(([kind]) => kind === 'warn').length === 3, `not told why: ${JSON.stringify(TOASTS)}`);
  },

  async a_module_whose_name_a_file_has_already_is_used_as_it_is() {
    plainErrors.push(new ApiError(409, 'plain.py exists already next to the machine but is not one of its files'));
    await boot('?machine=plain');
    await $('yamlAddModule').fire('click', {});
    await settle();
    ANSWERS.dialog = 'use';
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'save' }) });
    await settle();
    await release();
    await settle();
    const puts = CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/machines/plain')).map(([, , json]) => json);
    check(ASKED.some(([kind, title]) => kind === 'dialog' && title === 'The file exists already'), `asked: ${JSON.stringify(ASKED)}`);
    check(puts.length === 2 && 'plain.py' in puts[0].files && !('plain.py' in puts[1].files)
      && puts[1].files['plain.yaml'].includes('python: plain.py'), `not saved with the file as it is: ${JSON.stringify(puts)}`);
    check(!DIRTY, 'the drafts outlived the save');
  },

  async a_decision_state_shows_its_fields_from_the_kinds_schema() {
    await boot('?machine=fields');
    await choose('judge');
    const shown = $('side-inspect').innerHTML;
    check(/<option value="decide" selected>/.test(shown), 'the kind is not chosen');
    check(/name="decide" data-shape="enum" data-orig="choice"/.test(shown) && /<option value="choice" selected>/.test(shown), 'the decision kind is not shown');
    check(/name="question" data-shape="text" data-orig="Is it done\?"/.test(shown), 'the question is not shown');
    check(/name="criteria" data-shape="yaml"/.test(shown) && shown.includes('done: finished'), 'the criteria are not YAML text');
    check(/name="timeout" data-shape="duration" data-orig="5m"/.test(shown), 'the timeout is not shown');
    const at = (name) => shown.indexOf(`name="${name}"`);
    check(at('decide') < at('input') && at('input') < at('question') && at('question') < at('timeout'),
      'the kind first, then what it needs, the common fields last');
    check(/name="max_visits"/.test(shown) && /name="entry" data-shape="code"/.test(shown), 'the state settings are missing');
  },

  async an_activity_apply_sends_only_what_changed() {
    await boot('?machine=fields');
    await choose('judge');
    const form = formOf('activity', { kind: ['kind', 'decide', 'decide'], question: ['text', 'Is it done?', 'Finished?'],
      input: ['text', '{{ params.text }}', '{{ params.text }}'], criteria: ['yaml', 'done: finished\n', 'done: finished\n'],
      timeout: ['duration', '5m', '30'] });
    await $('side-inspect').fire('submit', { target: form });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'update_state', name: 'judge', do: { question: 'Finished?', timeout: 30 } }),
      `sent: ${JSON.stringify(lastEdit())}`);
  },

  async another_kind_keeps_what_survives_and_drops_the_rest() {
    await boot('?machine=fields');
    await choose('judge');
    const kind = element('select', { name: 'kind' });
    kind.name = 'kind';
    kind.value = 'agent';
    element('form', { 'data-form': 'activity' }).appendChild(kind);
    await $('side-inspect').fire('change', { target: kind });
    check(/name="task" data-shape="text" data-orig=""/.test($('activityFields').innerHTML), 'the agent kind has no empty task field');
    check(/name="timeout" data-shape="duration" data-orig="5m"/.test($('activityFields').innerHTML), 'the timeout the file keeps is not shown');
    check(DIRTY, 'another kind is an unapplied change');
    const form = formOf('activity', { kind: ['kind', 'agent', 'agent'], agent: ['line', '', 'critic'], task: ['text', '', 'Judge it'],
      timeout: ['duration', '5m', '5m'] });
    await $('side-inspect').fire('submit', { target: form });
    await settle();
    check(JSON.stringify(lastEdit()?.do) === JSON.stringify({ agent: 'critic', task: 'Judge it', decide: null, input: null,
      question: null, criteria: null }), `sent: ${JSON.stringify(lastEdit())}`);
  },

  async no_kind_removes_the_activity() {
    await boot('?machine=fields');
    await choose('judge');
    await $('side-inspect').fire('submit', { target: formOf('activity', { kind: ['kind', 'decide', ''] }) });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'update_state', name: 'judge', fields: { do: null } }),
      `sent: ${JSON.stringify(lastEdit())}`);
  },

  async a_final_s_settings_send_status_and_output_as_yaml() {
    await boot('?machine=fields');
    await choose('done');
    const shown = $('side-inspect').innerHTML;
    check(/name="status" data-shape="enum"/.test(shown) && /name="output" data-shape="yaml"/.test(shown)
      && shown.includes('verdict:'), 'a final shows no status or output');
    check(!/data-form="activity"/.test(shown), 'a final has no activity');
    await $('side-inspect').fire('submit', { target: formOf('state-fields', { status: ['enum', '', 'failed'],
      output: ['yaml', 'verdict: x\n', 'verdict: y\n'], description: ['line', '', ''] }) });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'update_state', name: 'done',
      fields: { status: 'failed', output: { $yaml: 'verdict: y\n' } } }), `sent: ${JSON.stringify(lastEdit())}`);
  },

  async the_machine_settings_are_an_update_machine() {
    await boot('?machine=fields');
    const shown = $('side-inspect').innerHTML;
    check(/name="group" data-shape="line" data-orig="Writer\/demo"/.test(shown) && /name="params" data-shape="yaml"/.test(shown),
      'the machine settings are not shown');
    await $('side-inspect').fire('submit', { target: formOf('machine-fields', { title: ['line', 'Fields', 'Judge'],
      group: ['line', 'Writer/demo', ''] }) });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'update_machine', fields: { title: 'Judge', group: null } }),
      `sent: ${JSON.stringify(lastEdit())}`);
  },

  async an_apply_without_a_change_sends_nothing() {
    await boot('?machine=fields');
    await choose('judge');
    await $('side-inspect').fire('submit', { target: formOf('state-fields', { description: ['line', '', ''] }) });
    await $('side-inspect').fire('submit', { target: formOf('activity', { kind: ['kind', 'decide', 'decide'], question: ['text', 'q', 'q'] }) });
    await settle();
    check(!lastEdit() && !TOASTS.length, `sent: ${JSON.stringify(lastEdit())}, said ${JSON.stringify(TOASTS)}`);
  },

  async typing_offers_apply_and_an_unchanged_apply_takes_it_back() {
    await boot('?machine=fields');
    await choose('judge');
    // the form as drawn: its Apply disabled, not primary (rendered markup is a string here: the form is built)
    const form = $('side-inspect').appendChild(element('form', { 'data-form': 'state-fields' }));
    const input = form.appendChild(element('input', { name: 'max_visits' }));
    const apply = form.appendChild(element('button', { type: 'submit', 'data-apply': '', class: 'pk-btn pk-btn--sm' }));
    apply.disabled = true;
    const other = $('side-inspect').appendChild(element('form', { 'data-form': 'activity' }));
    const otherApply = other.appendChild(element('button', { type: 'submit', 'data-apply': '', class: 'pk-btn pk-btn--sm' }));
    otherApply.disabled = true;
    await $('side-inspect').fire('input', { target: input });
    check(DIRTY && !apply.disabled && apply.classList.contains('pk-btn--primary'), `typing did not offer Apply: disabled ${apply.disabled}, ${apply.getAttribute('class')}`);
    check(otherApply.disabled && !otherApply.classList.contains('pk-btn--primary'), 'typing into one form offered another form\'s Apply');
    // typed back to what it showed: Apply sends nothing and is taken back
    form.elements.max_visits.name = 'max_visits';
    form.elements.max_visits.value = '';
    form.elements.max_visits.dataset.shape = 'number';
    form.elements.max_visits.dataset.orig = '';
    await $('side-inspect').fire('submit', { target: form });
    await settle();
    check(!lastEdit() && !TOASTS.length, `sent ${JSON.stringify(lastEdit())}, said ${JSON.stringify(TOASTS)}`);
    check(apply.disabled && !apply.classList.contains('pk-btn--primary') && !DIRTY,
      `an unchanged Apply stays offered: disabled ${apply.disabled}, ${apply.getAttribute('class')}, dirty ${DIRTY}`);
  },

  async a_run_again_that_fails_unfolds_the_form_and_the_test_options_say_what_they_hold() {
    localStorage.setItem('stategraph:mocks:review', JSON.stringify('{"write": "kept"}'));
    await boot('?machine=review&run=r1');  // r1 was started without mocks
    check($('testOptions').open === true && $('testOptionsHeld').textContent === '· mocks',
      `the remembered mocks are folded away unsaid: open ${$('testOptions').open}, "${$('testOptionsHeld').textContent}"`);
    check($('startBox').open === false, 'the start form is not folded with runs listed');
    // refused, its inputs empty: only the refusal's word on mocks can unfold the test options
    $('testOptions').open = false;
    startRefused = new ApiError(422, 'mocks: nope is no state path');
    await $('runResult').fire('click', { target: element('button', { 'data-act': 'rerun' }) });
    await settle();
    check($('startBox').open === true && !$('startError').hidden && $('startError').textContent.includes('nope is no state path'),
      `the refusal is not shown: open ${$('startBox').open}, "${$('startError').textContent}"`);
    check((globalThis.KEPT_IN_SIGHT || []).includes('startError'), `the refusal is not brought into sight: ${globalThis.KEPT_IN_SIGHT}`);
    check($('testOptions').open === true && $('testOptionsHeld').textContent === '',
      `a refusal about mocks leaves the test options folded: open ${$('testOptions').open}, "${$('testOptionsHeld').textContent}"`);
    // started, with the run's mocks: only Run again itself can unfold them
    runAnswer = { ...RUN, mocks: { mocks: { write: 'a draft' }, mock_only: true } };
    POLLERS.find((p) => p.ms === 1000).fn();
    await settle();
    startRefused = null;
    $('testOptions').open = false;
    await $('runResult').fire('click', { target: element('button', { 'data-act': 'rerun' }) });
    await settle();
    check(CALLS.filter(([method, path]) => method === 'POST' && path.endsWith('/api/runs')).length === 2, 'the second run was not started');
    check($('testOptions').open === true && $('testOptionsHeld').textContent === '· mocks, mock only',
      `Run again's inputs are not shown: open ${$('testOptions').open}, "${$('testOptionsHeld').textContent}"`);
  },

  async a_status_filter_on_another_machine_does_not_unfold_new_run() {
    runsAnswer = (query) => (query.get('status') ? [] : [...RUNS]);
    await boot('?machine=review');
    check($('startBox').open === false, 'the start form is not folded with runs listed');
    $('runStatus').value = 'failed';
    await $('runStatus').fire('change', {});
    await settle();
    await clickMachine('other');
    await settle();
    check(headName() === 'other' && $('runList').innerHTML.includes('No failed run'), `not other's failed runs: ${headName()}`);
    check($('startBox').open === false, 'no runs of the filtered status unfolded New run');
    // opened by hand under the filter: the whole list that comes with every status does not fold it under the typing
    $('startBox').open = true;
    await $('startBox').fire('toggle', {});
    $('runStatus').value = '';
    await $('runStatus').fire('change', {});
    await settle();
    check($('runList').innerHTML.includes('data-id="r1"'), 'the whole list did not come');
    check($('startBox').open === true, 'the whole list folded New run the viewer opened');
  },

  async another_machines_start_error_is_gone_with_it() {
    await boot('?machine=review');
    startRefused = new ApiError(422, 'premise is required');
    await $('startForm').fire('submit', {});
    await settle();
    check(!$('startError').hidden, `the refusal is not shown: ${$('startError').textContent}`);
    startRefused = null;
    await clickMachine('other');
    await settle();
    check(headName() === 'other' && $('startError').hidden, `review's refusal shows under other: "${$('startError').textContent}"`);
  },

  async a_start_error_before_the_first_run_list_keeps_new_run_open() {
    await import('./fake_kit_held.js');  // it sets HOLD up as it loads: before the panel loads, then held
    HOLD = (method, path) => path.includes('/runs?');  // the first run list is late
    location.search = '?machine=review';
    await import('./panel_copy.js');
    await settle();
    check(headName() === 'review' && PENDING.some((call) => call.path.includes('/runs?')), 'the run list is not held');
    startRefused = new ApiError(422, 'premise is required');
    await $('startForm').fire('submit', {});
    await settle();
    check($('startBox').open === true && !$('startError').hidden, `the refusal is not shown: ${$('startError').textContent}`);
    HOLD = () => false;
    await release();
    await settle();
    check($('runList').innerHTML.includes('data-id="r1"'), 'the run list did not come');
    check($('startBox').open === true, 'the run list folded New run over the refusal it shows');
  },

  async an_apply_on_its_way_stays_disabled_while_another_form_is_typed_into() {
    editAnswer = FIELDS;
    await boot('?machine=fields');
    await choose('judge');
    const formWith = (kind, name) => {
      const form = $('side-inspect').appendChild(element('form', { 'data-form': kind }));
      const input = form.appendChild(element('input', { name }));
      const apply = form.appendChild(element('button', { type: 'submit', 'data-apply': '', class: 'pk-btn pk-btn--sm' }));
      apply.disabled = true;
      return { form, input, apply };
    };
    const sending = formWith('state-fields', 'max_visits');
    const other = formWith('activity', 'question');
    await $('side-inspect').fire('input', { target: sending.input });
    check(!sending.apply.disabled, 'typing did not offer Apply');
    Object.assign(sending.form.elements.max_visits, { name: 'max_visits', value: '3' });
    sending.form.elements.max_visits.dataset.shape = 'number';
    sending.form.elements.max_visits.dataset.orig = '';
    HOLD = (method, path) => path.endsWith('/edit');
    const submitted = $('side-inspect').fire('submit', { target: sending.form });
    await settle();
    check(PENDING.length === 1 && sending.apply.disabled, `the edit is not on its way: ${PENDING.length}, disabled ${sending.apply.disabled}`);
    await $('side-inspect').fire('input', { target: other.input });
    check(!other.apply.disabled && sending.apply.disabled,
      `typing elsewhere freed the Apply on its way: other ${other.apply.disabled}, sending ${sending.apply.disabled}`);
    HOLD = () => false;
    await release();
    await submitted;
    await settle();
    check(lastEdit()?.op === 'update_state' && JSON.stringify(lastEdit().fields) === '{"max_visits":3}', `sent ${JSON.stringify(lastEdit())}`);
    check(sending.apply.disabled && !sending.apply.classList.contains('pk-btn--primary'), 'applied, its Apply is still offered');
    await $('side-inspect').fire('input', { target: sending.input });
    check(!sending.apply.disabled && sending.apply.classList.contains('pk-btn--primary'),
      `after the edit its Apply no longer follows what is typed: disabled ${sending.apply.disabled}`);
  },

  async a_read_only_machine_shows_its_fields_disabled() {
    fieldsAnswer = { ...FIELDS, writable: false };
    await boot('?machine=fields');
    await choose('judge');
    const shown = $('side-inspect').innerHTML;
    check(/name="question" data-shape="text" data-orig="[^"]*" disabled/.test(shown) && /id="af-kind" name="kind" data-orig="[^"]*" disabled/.test(shown),
      'a read-only machine offers its fields for editing');
  },

  async a_new_decision_takes_its_criteria_as_yaml() {
    await boot('?machine=fields');
    await choose('idle');
    const kind = element('select', { name: 'kind' });
    kind.name = 'kind';
    kind.value = 'decide';
    element('form', { 'data-form': 'activity' }).appendChild(kind);
    await $('side-inspect').fire('change', { target: kind });
    check(/name="criteria" data-shape="yaml" data-orig=""/.test($('activityFields').innerHTML), 'empty criteria are taken as text');
  },

  async a_number_the_browser_cannot_read_is_refused_not_removed() {
    await boot('?machine=fields');
    await choose('judge');
    for (const orig of ['3', '']) {  // the browser's value is '' for "abc": the old value, or none, is kept
      const form = formOf('state-fields', { max_visits: ['number', orig, ''] });
      form.elements.max_visits.validity = { badInput: true };
      TOASTS.length = 0;
      await $('side-inspect').fire('submit', { target: form });
      await settle();
      check(!lastEdit() && TOASTS.some(([kind, text]) => kind === 'warn' && text.includes('not a number')),
        `orig ${orig}: sent ${JSON.stringify(lastEdit())}, toasts ${JSON.stringify(TOASTS)}`);
    }
  },

  async a_number_field_is_a_number_input_and_a_typo_is_refused() {
    await boot('?machine=fields');
    await choose('judge');
    check(/ type="number"\s+value="" id="sf-max_visits"/.test($('side-inspect').innerHTML), 'max_visits is no number input');
    await $('side-inspect').fire('submit', { target: formOf('state-fields', { max_visits: ['number', '', '3,5'] }) });
    await settle();
    check(!lastEdit() && TOASTS.some(([kind, text]) => kind === 'warn' && text.includes('not a number')), `sent: ${JSON.stringify(lastEdit())}`);
  },

  async a_shared_activity_is_locked_with_a_hint() {
    await boot('?machine=fields');
    await choose('share_b');
    const shown = $('side-inspect').innerHTML;
    check(/id="af-kind" name="kind" data-orig="[^"]*" disabled/.test(shown) && /name="task" data-shape="text" data-orig="t" disabled/.test(shown),
      'the aliased activity is offered for editing');
    check(shown.includes('shared with another place'), 'no word why');
  },

  async another_kind_without_its_key_is_refused() {
    await boot('?machine=fields');
    await choose('judge');
    await $('side-inspect').fire('submit', { target: formOf('activity', { kind: ['kind', 'agent', 'agent'], agent: ['line', '', ''],
      task: ['text', '', 'Judge it'] }) });
    await settle();
    check(!lastEdit() && TOASTS.some(([kind, text]) => kind === 'warn' && text.startsWith('Set agent first')),
      `sent: ${JSON.stringify(lastEdit())}`);
  },

  async a_transition_sends_only_what_changed_and_a_guard_over_lines_keeps_them() {
    await boot('?machine=fields');
    await choose('idle');
    check(/<textarea[^>]*id="tr-guard-idle#0" name="guard"[^>]*data-shape="code"/.test($('side-inspect').innerHTML),
      'a guard over two lines sits in a one-line input, which drops its line breaks');
    const guard = '(ctx.verdict is None\n and True)';
    const form = formOf('transition', { trigger: ['enum', 'done', 'done'], target: ['enum', 'done', 'judge'],
      guard: ['code', guard, guard], effect: ['code', '', ''] });
    form.setAttribute('data-transition', 'idle#0');
    await $('side-inspect').fire('submit', { target: form });
    await settle();
    const sent = lastEdit();
    check(sent?.op === 'update_transition' && JSON.stringify(sent.fields) === '{"target":"judge"}', `sent: ${JSON.stringify(sent)}`);
  },

  async a_text_over_lines_is_a_text_area() {
    await boot('?machine=fields');
    await choose('idle');
    check(/name="description" data-shape="prose"[^>]*>\nline one\nline two/.test($('side-inspect').innerHTML),
      'a description over two lines sits in a one-line field');
  },

  async an_empty_machine_asks_for_a_first_state() {
    await boot('?machine=empty');
    check($('canvasHint').textContent.startsWith('No states yet'), `hint: ${$('canvasHint').textContent}`);
  },

  async opening_a_machine_abandons_the_refresh_in_flight() {
    await boot('?machine=review');
    HOLD = (method, path) => /\/machines\/(other|review)$/.test(path);
    DOC_LISTENERS.refresh.forEach((fn) => fn({ detail: { auto: true } }));
    await settle();
    check(PENDING.some((call) => call.path.endsWith('/review')), 'the refresh asks for the open machine');
    await clickMachine('other');
    await settle();
    check(ABORTED.some((path) => path.endsWith('/review')), 'the refresh was not abandoned');
    await release();
    check(headName() === 'other', `open: ${headName()}`);
  },

  async without_auto_save_an_edit_and_a_move_wait_for_save() {
    localStorage.removeItem('stategraph:autosave');
    const drafted = MACHINE.files['review.yaml'].replace('    max_visits: 5\n', '    max_visits: 5  # drafted\n');
    editAnswer = { machine_id: 'review', problems: [], graph: MACHINE.graph, draft: drafted };
    await boot('?machine=review');
    const module = 'def f():\n    return 2\n';
    $('yamlFile').value = 'review.py';
    await $('yamlFile').fire('change', {});
    $('yamlText').value = module;
    await $('yamlText').fire('input', {});
    $('yamlFile').value = 'review.yaml';
    await $('yamlFile').fire('change', {});
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    check(!ASKED.some(([, text]) => String(text).includes('graph edits change the saved file')), 'a second edit asked about the first');
    await choose('write');
    check($('side-inspect').innerHTML.includes('# drafted'), 'the state\'s YAML is not cut from the draft the graph shows');
    await $('autoLayout').fire('click', {});
    await settle();
    const edits = CALLS.filter(([, path]) => path.endsWith('/edit')).map(([, , json]) => json);
    check(edits.length === 2 && !('expected_version' in edits[0]) && edits[0].drafts['review.yaml'] === MACHINE.files['review.yaml']
      && edits[1].drafts['review.yaml'] === drafted && edits[0].drafts['review.py'] === module, `edits ${JSON.stringify(edits)}`);
    check($('yamlProblems').innerHTML.includes('Unsaved text'), 'the draft\'s problems are called the saved file\'s');
    const writes = () => CALLS.filter(([method]) => method === 'PUT');
    check(!writes().length, `written before Save: ${JSON.stringify(writes())}`);
    check(DIRTY && $('yamlText').value === drafted, `dirty ${DIRTY}`);
    check(!/data-act="save"[^>]*disabled/.test($('machineHead').innerHTML), 'Save not offered');
    ANSWERS.confirm = false;
    await $('startForm').fire('submit', {});
    await settle();
    check(!CALLS.some(([method, path]) => method === 'POST' && path.endsWith('/api/runs')), 'a run started from the saved file unasked');
    ANSWERS.confirm = true;
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'duplicate-machine' }) });
    await settle();
    check(!CALLS.some(([, path]) => path.includes('_copy')) && TOASTS.some(([, text]) => text.includes('Save or revert')),
      'a copy of the saved machine made while the open one has unsaved changes');
    DOC_LISTENERS.keydown.forEach((fn) => fn({ key: 's', ctrlKey: true, preventDefault() {} }));
    DOC_LISTENERS.keydown.forEach((fn) => fn({ key: 's', ctrlKey: true, preventDefault() {} }));  // a held key
    await settle();
    const [file, layout, ...more] = writes();
    check(file && file[1].endsWith('/machines/review') && file[2].files['review.yaml'] === drafted
      && file[2].expected_versions['review.yaml'] === MACHINE.versions['review.yaml'], `save ${JSON.stringify(file)}`);
    check(layout && layout[1].endsWith('/machines/review/layout'), `the layout after the text: ${JSON.stringify(layout)}`);
    check(!more.length, `saved twice: ${JSON.stringify(more)}`);
    check(!DIRTY, 'still unsaved after Save');
  },

  async without_auto_save_undo_and_redo_move_the_draft_and_write_nothing() {
    localStorage.removeItem('stategraph:autosave');
    editAnswer = { machine_id: 'review', problems: [], graph: MACHINE.graph, draft: 'drafted' };
    await boot('?machine=review');
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    editAnswer = { ...editAnswer, draft: 'drafted twice' };
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    const shown = () => CALLS.filter(([, path]) => path.endsWith('/validate')).map(([, , json]) => json.files['review.yaml']);
    await $('undo').fire('click', {});
    await settle();
    check(DIRTY && shown().pop() === 'drafted' && $('yamlText').value === 'drafted', `one undo: drawn from ${shown().pop()}`);
    await $('undo').fire('click', {});
    await settle();
    check(!DIRTY && shown().pop() === MACHINE.files['review.yaml'] && $('yamlText').value === MACHINE.files['review.yaml'],
      `two undos: dirty ${DIRTY}, drawn from ${shown().pop()}`);
    await $('redo').fire('click', {});
    await settle();
    check(DIRTY && shown().pop() === 'drafted' && $('yamlText').value === 'drafted', `after the redo: dirty ${DIRTY}`);
    check(!CALLS.some(([method]) => method === 'PUT'), 'an undo without auto-save wrote');
    await $('autoLayout').fire('click', {});
    await settle();
    const gets = () => CALLS.filter(([method, path]) => method === 'GET' && path.endsWith('/machines/review')).length;
    const before = gets();
    await $('yamlRevert').fire('click', {});
    await settle();
    check(gets() === before + 1 && !DIRTY && $('yamlText').value === MACHINE.files['review.yaml'], `revert: dirty ${DIRTY}`);
    check($('undo').disabled && $('redo').disabled, 'steps of the reverted drafts are still offered');
    check(/data-act="save"[^>]*disabled/.test($('machineHead').innerHTML), 'Save still offered after the revert');
    check(!CALLS.some(([method]) => method === 'PUT'), 'a revert wrote');
    await $('autoLayout').fire('click', {});
    await settle();
    await choose('write');
    check(DIRTY, 'a click on a state forgot the unsaved move');
    ANSWERS.confirm = false;
    await clickMachine('other');
    await settle();
    check(headName() === 'review' && ASKED.pop()[1].includes('unsaved changes'), 'a move alone let another machine open unasked');
  },

  async turning_auto_save_on_saves_the_drafts_first_and_is_kept() {
    localStorage.removeItem('stategraph:autosave');
    editAnswer = { machine_id: 'review', problems: [], graph: MACHINE.graph, draft: 'drafted' };
    await boot('?machine=review');
    check(autosaveSwitch() === 'off', `auto-save ${autosaveSwitch()} by default`);
    await $('autoLayout').fire('click', {});
    await settle();
    check(!/data-act="save"[^>]*disabled/.test($('machineHead').innerHTML) && !$('yamlRevert').disabled, 'no Save or Revert for a move');
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'save' }) });
    await settle();
    check(CALLS.some(([method, path]) => method === 'PUT' && path.endsWith('/machines/review/layout')) && !DIRTY,
      'the YAML tab\'s Save left the layout unsaved');
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'autosave' }) });
    await settle();
    const put = CALLS.find(([method, path]) => method === 'PUT' && path.endsWith('/machines/review'));
    check(put && put[2].files['review.yaml'] === 'drafted', `not saved first: ${JSON.stringify(put)}`);
    check(localStorage.getItem('stategraph:autosave') === 'true'
      && autosaveSwitch() === 'on', 'auto-save not on, or not kept');
    editAnswer = null;
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    const edit = CALLS.filter(([, path]) => path.endsWith('/edit')).pop();
    check(edit[2].expected_version && !edit[2].drafts, `with auto-save the edit went to ${JSON.stringify(edit[2])}`);
  },

  async with_auto_save_an_undo_writes_back_the_saved_text_not_discarded_yaml() {
    editAnswer = { ...MACHINE, versions: { ...MACHINE.versions, 'review.yaml': 'v2' } };
    await boot('?machine=review');
    await typeDraft();
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });  // discards the draft
    await settle();
    await $('undo').fire('click', {});
    await settle();
    const put = CALLS.find(([method, path]) => method === 'PUT' && path.endsWith('/machines/review'));
    check(put && put[2].files['review.yaml'] === MACHINE.files['review.yaml'], `undo wrote ${JSON.stringify(put?.[2])}`);
  },
  async without_auto_save_text_the_graph_does_not_show_is_asked_about_and_validate_draws_it() {
    localStorage.removeItem('stategraph:autosave');
    editAnswer = { machine_id: 'review', problems: [], graph: MACHINE.graph, draft: 'drafted' };
    await boot('?machine=review');
    await typeDraft();
    check(!/data-act="save"[^>]*disabled/.test($('machineHead').innerHTML) && !$('yamlDot').classList.contains('sg-invisible'), 'typing offers no Save');
    ANSWERS.confirm = false;
    ANSWERS.prompt.push('judge');
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-type': 'state' }) });
    await settle();
    check(!CALLS.some(([, path]) => path.endsWith('/edit')) && ASKED.some(([, text]) => String(text).includes('does not show yet')),
      'an edit on the graph drawn before the typed text went on unasked');
    await $('yamlValidate').fire('click', {});
    await settle();
    const validated = CALLS.filter(([, path]) => path.endsWith('/validate')).pop();
    check(validated && validated[2].files['review.yaml'].includes('# unsaved'), 'Validate did not draw the typed text');
    const asked = confirms();
    ANSWERS.prompt.push('judge');
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-type': 'state' }) });
    await settle();
    const edit = CALLS.filter(([, path]) => path.endsWith('/edit')).pop();
    check(confirms() === asked && edit && edit[2].drafts['review.yaml'].includes('# unsaved'),
      `after Validate: asked ${confirms() - asked}, sent ${JSON.stringify(edit?.[2]?.drafts)}`);
    $('yamlText').value = 'drafted\n# again\n';
    await $('yamlText').fire('input', {});
    ANSWERS.confirm = true;
    editAnswer = new ApiError(422, 'refused');
    ANSWERS.prompt.push('judge');
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-type': 'state' }) });
    await settle();
    $('yamlFile').value = 'review.yaml';
    await $('yamlFile').fire('change', {});  // the text drawn anew from the drafts
    check($('yamlText').value.includes('# again'), 'a refused edit dropped the typed text');
    editAnswer = { machine_id: 'review', problems: [], graph: MACHINE.graph, draft: 'drafted twice' };
    ANSWERS.prompt.push('judge');
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-type': 'state' }) });
    await settle();
    const made = CALLS.filter(([, path]) => path.endsWith('/edit')).pop();
    check(made[2].drafts['review.yaml'] === 'drafted' && $('yamlText').value === 'drafted twice',
      `a discarding edit sent ${JSON.stringify(made[2].drafts)}, shows ${$('yamlText').value}`);
  },

  async without_auto_save_undo_steps_go_when_the_file_changes_under_them() {
    localStorage.removeItem('stategraph:autosave');
    editAnswer = { machine_id: 'review', problems: [], graph: MACHINE.graph, draft: 'drafted' };
    await boot('?machine=review');
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    await $('undo').fire('click', {});
    await settle();
    check(!DIRTY && !$('redo').disabled, 'no redo after the undo');
    reviewAnswer = { ...MACHINE, versions: { ...MACHINE.versions, 'review.yaml': 'v-other' } };  // another editor saved
    DOC_LISTENERS.refresh.forEach((fn) => fn({ detail: { auto: true } }));
    await settle();
    check($('redo').disabled && $('undo').disabled, 'steps drafted from the older file are offered over the new one');
  },

  async an_edit_answered_after_another_machine_opened_is_dropped() {
    localStorage.removeItem('stategraph:autosave');
    editAnswer = { machine_id: 'review', problems: [], graph: MACHINE.graph, draft: 'drafted' };
    await boot('?machine=review');
    HOLD = (method, path) => path.endsWith('/edit');
    await choose('write');
    const removing = $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    await clickMachine('plain');
    await settle();
    await release();
    await removing;
    check($('machineHead').innerHTML.includes('>plain<') && !DIRTY && !$('yamlFile').innerHTML.includes('(unsaved)'),
      'the answer for review went into plain');
  },
  async without_auto_save_an_edit_answered_after_a_revert_is_dropped() {
    localStorage.removeItem('stategraph:autosave');
    editAnswer = { machine_id: 'review', problems: [], graph: MACHINE.graph, draft: 'drafted' };
    await boot('?machine=review');
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    HOLD = (method, path) => path.endsWith('/edit');
    editAnswer = { ...editAnswer, draft: 'drafted twice' };
    await choose('write');
    const removing = $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    await $('yamlRevert').fire('click', {});
    await settle();
    await release();
    await removing;
    check(!DIRTY && $('yamlText').value === MACHINE.files['review.yaml'] && TOASTS.some(([, text]) => text.includes('not applied')),
      `the answer brought the reverted drafts back: dirty ${DIRTY}, ${$('yamlText').value.slice(0, 20)}`);
  },

  async without_auto_save_text_typed_while_an_edit_is_on_its_way_is_kept() {
    localStorage.removeItem('stategraph:autosave');
    editAnswer = { machine_id: 'review', problems: [], graph: MACHINE.graph, draft: 'drafted' };
    await boot('?machine=review');
    HOLD = (method, path) => path.endsWith('/edit');
    await choose('write');
    const removing = $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    await typeDraft();
    await release();
    await removing;
    $('yamlFile').value = 'review.yaml';
    await $('yamlFile').fire('change', {});
    check($('yamlText').value.includes('# unsaved') && TOASTS.some(([, text]) => text.includes('YAML tab changed')),
      `the answer overwrote the typed text: ${$('yamlText').value.slice(-20)}`);
  },

  async auto_save_is_not_switched_under_an_edit_on_its_way() {
    localStorage.removeItem('stategraph:autosave');
    editAnswer = { machine_id: 'review', problems: [], graph: MACHINE.graph, draft: 'drafted' };
    await boot('?machine=review');
    HOLD = (method, path) => path.endsWith('/edit');
    await choose('write');
    const removing = $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'autosave' }) });
    await settle();
    await release();
    await removing;
    check(localStorage.getItem('stategraph:autosave') !== 'true' && autosaveSwitch() === 'off'
      && DIRTY && $('yamlText').value === 'drafted', 'auto-save switched while the edit was on its way, or the edit lost');
  },
  async a_selection_dragged_by_a_state_inside_its_composite_moves_the_composite_only() {
    reviewAnswer = TWO_COMPOSITES;
    await boot('?machine=review');
    const { client, boxOf, node } = canvasGeometry();
    await choose('review');
    await $('canvas').fire('pointerdown', { button: 0, target: node('read'), ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: node('read'), clientX: 10, clientY: 10 });
    await settle();
    const read = boxOf('read');
    const loop = boxOf('loop');
    const at = client(loop.x + loop.w / 2, loop.y + loop.h - 6);
    await $('canvas').fire('pointerdown', { button: 0, target: node('read'), pointerId: 1, ...client(read.x + 5, read.y + 5) });
    await $('canvas').fire('pointermove', { target: node('loop'), ...at });
    await $('canvas').fire('pointerup', { target: node('loop'), ...at });
    await settle();
    check(!CALLS.some(([, path]) => path.endsWith('/edit')), `the state it was dragged by went into loop: ${JSON.stringify(lastEdit())}`);
  },

  async a_cancelled_drag_puts_nothing_into_a_composite() {
    await boot('?machine=review');
    const { client, boxOf, node } = canvasGeometry();
    const from = boxOf('write');
    const into = boxOf('review');
    const at = client(into.x + into.w / 2, into.y + into.h - 6);
    await $('canvas').fire('pointerdown', { button: 0, target: node('write'), pointerId: 1, ...client(from.x + 5, from.y + 5) });
    await $('canvas').fire('pointermove', { target: node('review'), ...at });
    await $('canvas').fire('pointercancel', { target: node('review'), ...at });
    await settle();
    check(!CALLS.some(([, path]) => path.endsWith('/edit')), `a cancelled drag moved write into review: ${JSON.stringify(lastEdit())}`);
  },

  async a_drop_answered_after_another_machine_opened_leaves_that_ones_layout_alone() {
    await boot('?machine=review');
    const { client, boxOf, node } = canvasGeometry();
    const from = boxOf('write');
    const into = boxOf('review');
    const at = client(into.x + into.w / 2, into.y + into.h - 6);
    HOLD = (method, path) => path.endsWith('/edit');
    await $('canvas').fire('pointerdown', { button: 0, target: node('write'), pointerId: 1, ...client(from.x + 5, from.y + 5) });
    await $('canvas').fire('pointermove', { target: node('review'), ...at });
    const dropping = $('canvas').fire('pointerup', { target: node('review'), ...at });
    await settle();
    await clickMachine('other');
    await settle();
    await release();
    await dropping;
    await settle();
    check(!CALLS.some(([method, path]) => method === 'PUT' && path.endsWith('/machines/other/layout')),
      'the drop went into the layout of the machine opened meanwhile');
  },

  async a_composites_last_state_is_not_removed_after_asking() {
    reviewAnswer = TWO_COMPOSITES;
    await boot('?machine=review');
    await choose('inner');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'remove' }) });
    await settle();
    check(!confirms() && TOASTS.some(([, text]) => text.includes('loop keeps at least one state')), `inner: ${JSON.stringify(TOASTS)}`);
    await choose('read');
    const { node } = canvasGeometry();
    await $('canvas').fire('pointerdown', { button: 0, target: node('verdict'), ctrlKey: true, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: node('verdict'), clientX: 10, clientY: 10 });
    await settle();
    await $('canvas').fire('keydown', { key: 'Delete', target: $('canvas'), preventDefault() {} });
    await settle();
    check(!confirms() && TOASTS.some(([, text]) => text.includes('review keeps at least one state')), `read and verdict: ${JSON.stringify(TOASTS)}`);
    check(!CALLS.some(([, path]) => path.endsWith('/edit')), 'an edit went out');
  },
  async a_note_from_the_bar_goes_into_the_file_and_the_middle_of_the_view() {
    await boot('?machine=review');
    editAnswer = { ...MACHINE, graph: { ...MACHINE.graph, notes: [{ name: 'note_1', text: 'New note' }] } };
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-note': '' }) });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'set_note', name: 'note_1', text: 'New note' }), `sent: ${JSON.stringify(lastEdit())}`);
    const puts = () => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/review/layout')).map(([, , json]) => json.layout.positions);
    const { k, client } = canvasGeometry();
    const [tx, ty] = [client(0, 0).clientX, client(0, 0).clientY];
    const middle = { x: Math.round((450 - tx) / k) - 110, y: Math.round((300 - ty) / k) - 30 };  // the fake canvas: 900 x 600
    check(JSON.stringify(puts().pop()?.['note:note_1']) === JSON.stringify(middle), `placed: ${JSON.stringify(puts().pop())}, the middle ${JSON.stringify(middle)}`);
    check($('side-inspect').innerHTML.includes('data-form="note"'), 'the new note is not the one shown');
    check($('canvas').querySelectorAll('.sg-note').length === 1, 'the note is not drawn');
    await $('undo').fire('click', {});
    await settle();
    check(puts().length === 2 && !('note:note_1' in puts().pop()), `an undo keeps the new note's place: ${JSON.stringify(puts())}`);
    reviewAnswer = editAnswer;  // the redo's reload reads the file with the note again
    await $('redo').fire('click', {});
    await settle();
    check($('canvas').querySelectorAll('.sg-note').length === 1, 'the redo did not bring the note back');
    check(!$('side-inspect').innerHTML.includes('data-form="note"'), 'the note the undo took stayed chosen, and the redo showed it again');
  },

  async a_note_is_added_once_and_asked_about_only_as_it_is() {
    await boot('?machine=review');
    await choose('write');
    const form = element('form', { 'data-form': 'set-state' });
    await $('side-inspect').fire('input', { target: form.appendChild(element('textarea', { name: 'yaml' })) });  // not applied
    editAnswer = { ...MACHINE, graph: { ...MACHINE.graph, notes: [{ name: 'note_1', text: 'New note' }] } };
    ANSWERS.confirm = false;  // keep the state's text
    const click = () => $('paletteMenu').fire('click', { target: element('button', { 'data-add-note': '' }) });
    await Promise.all([click(), click()]);  // a double click
    await release();
    await settle();
    const sent = CALLS.filter(([, path, json]) => path.endsWith('/edit') && json.op?.op === 'set_note');
    check(sent.length === 1, `a double click sent ${sent.length} notes`);
    check(!ASKED.some(([, message]) => String(message).includes('this edit changes the state')),
      `a note's edit asked about the state: ${JSON.stringify(ASKED)}`);
  },

  async a_shared_notes_block_is_read_only_in_the_inspector() {
    reviewAnswer = { ...WITH_NOTE, graph: { ...WITH_NOTE.graph, locked: ['notes'] } };
    await boot('?machine=review');
    const note = $('canvas').querySelectorAll('.sg-note').find((n) => n.dataset.note === 'why');
    await $('canvas').fire('keydown', { key: 'Enter', target: note, preventDefault() {} });
    await settle();
    const shown = $('side-inspect').innerHTML;
    check(shown.includes('YAML anchor') && /sg-note-input"[^>]*readonly/.test(shown) && !shown.includes('remove-note'), shown);
    await $('paletteMenu').fire('click', { target: element('button', { 'data-add-note': '' }) });
    await settle();
    check(!CALLS.some(([, path]) => path.endsWith('/edit')) && TOASTS.some(([, message]) => String(message).startsWith('Notes:')),
      `a note was added to a shared block: ${JSON.stringify(TOASTS)}`);
  },

  async a_note_s_text_is_applied_and_an_emptied_note_is_removed_after_asking() {
    reviewAnswer = WITH_NOTE;
    editAnswer = WITH_NOTE;
    await boot('?machine=review');
    const note = $('canvas').querySelectorAll('.sg-note').find((n) => n.dataset.note === 'why');
    check(note, 'the note is not drawn');
    await $('canvas').fire('keydown', { key: 'Enter', target: note, preventDefault() {} });
    await settle();
    check($('side-inspect').innerHTML.includes('data-form="note"'), 'Enter on the note does not show it');
    await $('canvas').fire('keydown', { key: 'Escape', target: $('canvas'), preventDefault() {} });
    await settle();
    await $('canvas').fire('dblclick', { target: note, clientX: 10, clientY: 10 });
    await settle();
    check($('side-inspect').innerHTML.includes('data-form="note"'), 'a double click on the note does not open it');
    await $('canvas').fire('keydown', { key: 'Escape', target: $('canvas'), preventDefault() {} });
    await settle();
    check(!$('side-inspect').innerHTML.includes('data-form="note"'), 'Escape left the note chosen');
    await $('canvas').fire('pointerdown', { button: 0, target: note, clientX: 10, clientY: 10, pointerId: 1 });
    await $('canvas').fire('pointerup', { target: note, clientX: 10, clientY: 10 });
    await settle();
    check($('side-inspect').innerHTML.includes('data-form="note"') && $('side-inspect').innerHTML.includes('needs a second look'),
      'a click on the note shows its text');
    await $('side-inspect').fire('submit', { target: formOf('note', { text: ['kind', '', 'Because the review\nneeds a second look.\n'] }) });
    await settle();
    check(!lastEdit() && !TOASTS.length, `sent: ${JSON.stringify(lastEdit())}, said ${JSON.stringify(TOASTS)}`);
    await $('side-inspect').fire('submit', { target: formOf('note', { text: ['kind', '', 'Look twice.'] }) });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'set_note', name: 'why', text: 'Look twice.' }), `sent: ${JSON.stringify(lastEdit())}`);
    ANSWERS.confirm = false;
    await $('side-inspect').fire('submit', { target: formOf('note', { text: ['kind', '', '  \n'] }) });
    await settle();
    check(confirms() === 1 && lastEdit().text === 'Look twice.', 'an emptied note went without asking');
    ANSWERS.confirm = true;
    await $('side-inspect').fire('submit', { target: formOf('note', { text: ['kind', '', ''] }) });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'set_note', name: 'why', text: null }), `sent: ${JSON.stringify(lastEdit())}`);
  },

  async a_note_dragged_keeps_its_place_and_delete_removes_it_with_its_place() {
    reviewAnswer = { ...WITH_NOTE, layout: { version: 1, positions: { 'note:why': { x: 500, y: 50 } } } };
    await boot('?machine=review');
    const { client } = canvasGeometry();
    const note = () => $('canvas').querySelectorAll('.sg-note').find((n) => n.dataset.note === 'why');
    await $('canvas').fire('pointerdown', { button: 0, target: note(), pointerId: 1, ...client(510, 60) });
    await $('canvas').fire('pointermove', { target: note(), ...client(610, 100) });
    await $('canvas').fire('pointerup', { target: note(), ...client(610, 100) });
    await settle();
    const put = () => CALLS.filter(([method, path]) => method === 'PUT' && path.endsWith('/review/layout')).map(([, , json]) => json.layout.positions).pop();
    check(JSON.stringify(put()?.['note:why']) === JSON.stringify({ x: 600, y: 90 }), `kept: ${JSON.stringify(put())}`);
    check(!$('side-inspect').innerHTML.includes('data-form="note"'), 'a drag chose the note');
    editAnswer = MACHINE;
    await $('canvas').fire('pointerdown', { button: 0, target: note(), pointerId: 1, ...client(600, 100) });
    await $('canvas').fire('pointerup', { target: note(), ...client(600, 100) });
    await settle();
    await $('canvas').fire('keydown', { key: 'Delete', target: $('canvas'), preventDefault() {} });
    await settle();
    check(confirms() === 1 && JSON.stringify(lastEdit()) === JSON.stringify({ op: 'set_note', name: 'why', text: null }),
      `sent: ${JSON.stringify(lastEdit())}`);
    check(put() && !('note:why' in put()), `the removed note's place stays: ${JSON.stringify(put())}`);
    await $('undo').fire('click', {});
    await settle();
    check(JSON.stringify(put()?.['note:why']) === JSON.stringify({ x: 600, y: 90 }), `an undo does not put the place back: ${JSON.stringify(put())}`);
  },

  async a_state_s_description_is_free_text_at_the_top_and_applies_alone() {
    await boot('?machine=fields');
    await choose('judge');
    const shown = $('side-inspect').innerHTML;
    check(/data-form="state-description"[^]*?<textarea[^>]*id="sd-description" name="description" data-shape="prose"[^]*?<\/form>/.test(shown)
      && shown.indexOf('id="sd-description"') < shown.indexOf('data-form="activity"'), 'no description box at the top');
    check(!/id="sf-description"/.test(shown), 'the description is in the settings as well');
    await $('side-inspect').fire('submit', { target: formOf('state-description', { description: ['prose', '', 'Judges\nthe draft.'] }) });
    await settle();
    check(JSON.stringify(lastEdit()) === JSON.stringify({ op: 'update_state', name: 'judge', fields: { description: 'Judges\nthe draft.' } }),
      `sent: ${JSON.stringify(lastEdit())}`);
  },
  async the_overview_names_the_machine_s_runner_and_the_catalog_is_its() {
    reviewAnswer = { ...MACHINE, runner: 'v6_machine_runner', runner_problem: 'runners a, b both claim /m in runs_machines_in' };
    await boot('?machine=review');
    const shown = $('side-inspect').innerHTML;
    check(/<dt>Runner<\/dt><dd[^>]*>v6_machine_runner<\/dd>/.test(shown), 'the runner is not shown');
    check(shown.includes('both claim /m'), 'the runner problem is not shown');
    const asked = () => CALLS.filter(([, path]) => path.includes('/catalog')).map(([, path]) => path.split('/api')[1]);
    check(JSON.stringify(asked()) === JSON.stringify(['/catalog?machine_id=review']), `catalog asked: ${JSON.stringify(asked())}`);
    await clickMachine('other');
    await settle();
    check(asked().pop() === '/catalog?machine_id=other', `another machine keeps the first one's tools: ${JSON.stringify(asked())}`);
  },

  async a_submachine_lists_the_runs_it_ran_in_and_its_canvas_shows_the_frame_picked() {
    const asked = [];
    runsAnswer = (query) => {
      asked.push(`${query.get('machine_id')}:${query.get('nested')}`);
      return query.get('machine_id') === 'other' ? [{ ...RUNS[0], machine_id: 'review' }] : RUNS;
    };
    // r1 is a run of review; other ran in it twice: s3/m/ runs now, s5/m/ has ended -- only its trace says where
    const live = { machine: 'other', prefix: 's3/m/', path: 'read', step: 2, state: 'write', config: ['write'],
      visits: { write: 4 }, ctx: {}, params: {}, accepts: [] };
    runAnswer = { ...RUN, view: { ...RUN.view, frames: [RUN.view.frames[0], live] },
      frames_started: [{ prefix: 's3/m/', machine: 'other', path: 'read' }, { prefix: 's5/m/', machine: 'other', path: 'read' }] };
    journalOf = { ...journalOf, r1: [...RUN.journal,
      { seq: 50, kind: 'trace', key: 's5/m/s1:enter:done', state: 'done', status: 'enter', data: { machine: 'other', frame: 's5/m/', visit: 1 } },
      { seq: 51, kind: 'trace', key: 's5/m/s1:final:done', state: 'done', status: 'final', data: { machine: 'other', frame: 's5/m/', status: 'succeeded' } }] };
    await boot('?machine=other&run=r1');
    const current = () => $('canvas').querySelectorAll('.sg-node').filter((n) => n.classList.contains('is-current')).map((n) => n.dataset.state);
    const bar = () => $('debugBar').innerHTML;
    check(asked.includes('other:true'), `the submachine's list did not ask for the runs it ran in: ${asked}`);
    check($('runList').innerHTML.includes('in review'), 'the run is not named as one of review');
    check(JSON.stringify(current()) === '["write"]', `the canvas shows ${current()}, not the live frame's state`);
    check(!$('canvas').querySelectorAll('.sg-node').some((n) => n.classList.contains('is-paused')), 'the pause of the root is drawn on the submachine');
    check(bar().includes('data-frame-choice') && bar().includes('under read #1 · running') && bar().includes('under read #2 · ended in done'), bar());
    const picker = element('select', { 'data-frame-choice': '' });
    picker.value = 's5/m/';
    await $('debugBar').fire('change', { target: picker });
    await settle();
    check(JSON.stringify(current()) === '["done"]', `the ended frame picked: the canvas shows ${current()}`);
    controlAnswer = { ...runAnswer, frames_started: undefined };  // a control's answer lists no frames
    await $('debugBar').fire('click', { target: element('button', { 'data-control': 'step' }) });
    await release();
    await settle();
    check(JSON.stringify(current()) === '["done"]', `after a control the frame picked is lost: ${current()}`);
    check(bar().includes('under read #2'), `the recorded frames are lost with a control's answer: ${bar()}`);
    const nowhere = element('button', { 'data-open-frame': 'zz/m/', 'data-machine': 'gone' });  // its GET fails
    await Promise.all(DOC_LISTENERS.click.map((fn) => fn({ target: nowhere })));
    await release();
    await settle();
    POLLERS.find((p) => p.ms === 1000).fn();  // drawn again: the frame shown is the one picked still
    await release();
    await settle();
    check(headName() === 'other' && JSON.stringify(current()) === '["done"]', `a switch that failed moved the frame: ${current()}`);
    // a pick while a switch is out: the switch that fails does not take it back
    HOLD = (method, path) => path.endsWith('/machines/gone');
    await Promise.all(DOC_LISTENERS.click.map((fn) => fn({ target: nowhere })));
    await settle();
    const picker2 = element('select', { 'data-frame-choice': '' });
    picker2.value = 's3/m/';
    await $('debugBar').fire('change', { target: picker2 });
    HOLD = () => false;
    await release();
    await settle();
    POLLERS.find((p) => p.ms === 1000).fn();
    await release();
    await settle();
    check(JSON.stringify(current()) === '["write"]', `the pick made meanwhile was taken back: ${current()}`);
    const back =element('button', { 'data-open-frame': '', 'data-machine': 'review' });
    await Promise.all(DOC_LISTENERS.click.map((fn) => fn({ target: back })));
    await release();
    await settle();
    check(headName() === 'review' && JSON.stringify(current()) === '["read"]', `back on review: ${headName()}, ${current()}`);
    check(!bar().includes('data-frame-choice') && bar().includes('r1'), 'the run is not kept on its own machine');
  },

  async a_state_that_ran_a_submachine_lists_its_runs_in_the_inspector() {
    // review's state read ran other twice (s3 ended, s5 runs); other's own child (s3/m/s1/m/) ran under it, not under read
    const live = { machine: 'other', prefix: 's5/m/', path: 'read', step: 1, state: 'write', config: ['write'], visits: {},
      ctx: {}, params: {}, accepts: [] };
    runAnswer = { ...RUN, view: { ...RUN.view, frames: [RUN.view.frames[0], live] },
      frames_started: [{ prefix: 's3/m/', machine: 'other', path: 'read' }, { prefix: 's3/m/s1/m/', machine: 'other', path: 'read/write' },
        { prefix: 's5/m/', machine: 'other', path: 'read' }] };
    journalOf = { ...journalOf, r1: [...RUN.journal,
      { seq: 50, kind: 'trace', key: 's3/m/s1:final:done', state: 'done', status: 'final', data: { machine: 'other', frame: 's3/m/', status: 'succeeded' } }] };
    await boot('?machine=review&run=r1');
    const badges = (name) => $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === name)
      .querySelectorAll('.sg-badge-text').map((text) => text.textContent);
    check(badges('read').includes('2 runs') && !badges('write').some((text) => / runs?$/.test(text)),
      `the canvas does not say where submachines ran: read ${badges('read')}, write ${badges('write')}`);
    await choose('read');
    await settle();
    const box = () => $('stateFrames').innerHTML;
    check(!$('stateFrames').hidden && box().includes('other #1') && box().includes('other #2'), box());
    const pane = $('side-inspect').innerHTML;
    check(pane.indexOf('id="stateFrames"') > pane.indexOf('sg-fragment'), 'the list is not last in the inspector');
    check(box().includes('Submachine runs (2)') && !/data-state-frames\s+open/.test(box()), `not folded: ${box()}`);
    const fold = element('details', { 'data-state-frames': '' });
    fold.open = true;
    await $('side-inspect').fire('toggle', { target: fold });
    check(box().includes('ended in done') && box().includes('running'), `how they stand: ${box()}`);
    check(box().includes('data-open-frame="s3/m/"') && box().includes('data-open-frame="s5/m/"')
      && !box().includes('data-open-frame="s3/m/s1/m/"'), `a grandchild is listed under read: ${box()}`);
    // s5 ends while the list is open: the next poll's journal says how
    journalOf.r1.push({ seq: 60, kind: 'trace', key: 's5/m/s2:final:failed', state: 'failed', status: 'final',
      data: { machine: 'other', frame: 's5/m/', status: 'failed' } });
    runAnswer = { ...runAnswer, view: { ...runAnswer.view, frames: [RUN.view.frames[0]] }, journal: journalOf.r1.slice(-5) };
    POLLERS.find((p) => p.ms === 1000).fn();
    await release();
    await settle();
    check(box().includes('ended in failed') && !box().includes('running'), `the ended frame is not followed: ${box()}`);
    check(/data-state-frames\s+open/.test(box()), 'opened, the list folds again on a poll');
    const third = { machine: 'other', prefix: 's7/m/', path: 'read', step: 1, state: 'write', config: ['write'], visits: {},
      ctx: {}, params: {}, accepts: [] };
    runAnswer = { ...runAnswer, view: { ...runAnswer.view, frames: [RUN.view.frames[0], third] } };  // no new result row
    POLLERS.find((p) => p.ms === 1000).fn();
    await release();
    await settle();
    check(box().includes('other #3') && box().includes('running'), `a frame started meanwhile is not listed: ${box()}`);
    await choose('write');
    await settle();
    check($('stateFrames').hidden && !box().includes('other'), 'a state that ran none lists runs');
    // on other's graph, its frame s3/m/: the child its state write started, by the path past the frame's own
    const show = element('button', { 'data-open-frame': 's3/m/', 'data-machine': 'other' });
    await Promise.all(DOC_LISTENERS.click.map((fn) => fn({ target: show })));
    await release();
    await settle();
    await choose('write');
    await settle();
    check(headName() === 'other' && box().includes('data-open-frame="s3/m/s1/m/"') && !box().includes('data-open-frame="s5/m/"'), box());
    check(badges('write').includes('1 run'), `on other's graph: write ${badges('write')}`);
    check(!/data-state-frames\s+open/.test(box()), `opened for read, the list of another state starts open: ${box()}`);
  },

  async a_catalog_that_fails_leaves_no_other_machine_s_tools_and_is_asked_again() {
    await boot('?machine=review');
    check($('sgTools').innerHTML.includes('store_put'), 'the catalog of review is not offered');
    catalogDown = (path) => path.includes('machine_id=other');
    await clickMachine('other');
    await settle();
    check(!$('sgTools').innerHTML.includes('store_put'), 'the tools of review are offered for other');
    catalogDown = null;
    await clickMachine('other');
    await settle();
    const asked = CALLS.filter(([, path]) => path.includes('/catalog?machine_id=other')).length;
    check(asked === 2 && $('sgTools').innerHTML.includes('store_put'), `the catalog of other is not asked again (${asked})`);
  },

  async a_run_from_before_the_frame_record_finds_its_frames_in_the_journal_and_shows_the_last() {
    runsAnswer = () => [{ ...RUNS[0], machine_id: 'review' }];
    // no frames_started (a run from before the record), no live frame of other: only the journal names them
    runAnswer = { ...RUN, view: { ...RUN.view, frames: RUN.view.frames } };
    const trace = (seq, frame, state, status, data = {}) => ({ seq, kind: 'trace', key: `${frame}s1:${status}:${state}`, state, status,
      data: { machine: 'other', frame, ...data } });
    // s7's second attempt ran it (s7/a2/m/), s9 once; their paths are their activities'
    const started = (seq, key) => ({ seq, kind: 'activity', key, state: 'read', status: 'done', data: { kind: 'machine', path: 'read' } });
    journalOf = { ...journalOf, r1: [...RUN.journal, started(49, 's7'), trace(50, 's7/a2/m/', 'write', 'enter', { visit: 1 }),
      trace(51, 's7/a2/m/', 'write', 'end', { reason: 'failed' }), started(59, 's9'), trace(60, 's9/m/', 'done', 'enter', { visit: 1 }),
      trace(61, 's9/m/', 'done', 'final', { status: 'succeeded' })] };
    await boot('?machine=other&run=r1');
    const current = () => $('canvas').querySelectorAll('.sg-node').filter((n) => n.classList.contains('is-current')).map((n) => n.dataset.state);
    const bar = $('debugBar').innerHTML;
    check(bar.includes('under read #1 · failed in write') && bar.includes('under read #2 · ended in done'), bar);
    check(JSON.stringify(current()) === '["done"]', `none live, none picked: the last frame, not ${current()}`);
    const frames = $('dbgFrames').innerHTML;
    check(frames.includes('data-machine="review"') && !frames.includes('data-machine="critique"'),
      'a machine without a graph of its own (not in the list) is offered to be shown');
  },

  async the_run_s_own_graph_counts_the_frames_only_its_journal_names() {
    // a run from before the record: no frames_started, no live frame of other -- its journal names one under read
    runAnswer = { ...RUN, view: { ...RUN.view, frames: [RUN.view.frames[0]] } };
    journalOf = { ...journalOf, r1: [...RUN.journal,
      { seq: 40, kind: 'activity', key: 's3', state: 'read', status: 'done', data: { kind: 'machine', path: 'read' } },
      { seq: 41, kind: 'trace', key: 's3/m/s1:final:done', state: 'done', status: 'final', data: { machine: 'other', frame: 's3/m/', status: 'succeeded' } }] };
    await boot('?machine=review&run=r1');
    const badges = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'read')
      .querySelectorAll('.sg-badge-text').map((text) => text.textContent);
    check(badges.includes('1 run'), `read's badge: ${badges}`);
  },

  async the_frames_of_an_interrupted_run_run_no_more() {
    runsAnswer = () => [{ ...RUNS[0], machine_id: 'review' }];
    const left = { machine: 'other', prefix: 's3/m/', path: 'read', step: 1, state: 'write', config: ['write'], visits: {},
      ctx: {}, params: {}, accepts: [] };  // the view the crash left
    runAnswer = { ...RUN, status: 'interrupted', active: false, debug: { ...RUN.debug, paused: null },
      view: { ...RUN.view, frames: [RUN.view.frames[0], left] }, frames_started: [{ prefix: 's3/m/', machine: 'other', path: 'read' }] };
    await boot('?machine=other&run=r1');
    const bar = $('debugBar').innerHTML;
    check(bar.includes('under read · interrupted') && !bar.includes('running'), bar);
  },
};

if (CASE === '*') {
  print(`CASES ${Object.keys(CASES).join(' ')}`);
} else {
  try {
    check(CASES[CASE], `no case ${CASE}`);
    await CASES[CASE]();
    print(`PASS ${CASE}`);
  } catch (error) {
    print(`FAIL ${CASE}: ${error.message}`);
  }
  print('ERRORS', JSON.stringify(ERRORS));
}
