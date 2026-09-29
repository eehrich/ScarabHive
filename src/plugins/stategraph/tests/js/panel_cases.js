// Single cases of the panel in JavaScriptCore, each in a jsc of its own (the panel's state is module state):
// case.js names the case, test_plugin_stategraph_js.py writes it and runs this file once per case. The panel runs
// against fake_kit_held.js (the real kit's timing: answers later or when released, `latest` and abandon() abort) and
// fake_dom.js, without ELK (the canvas falls back to its grid). Prints "PASS <case>" or "FAIL <case>: ...".
import { CASE } from './case.js';
import { FIELDS, HOOKS, MACHINE, RUN, RUNS, KINDS } from './fixtures.js';
import { ApiError } from './fake_kit.js';

load('./fake_dom.js');

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
let journalOf = { r1: RUN.journal, r2: JOURNAL2 };
const CATALOG = { agents: [{ name: 'scene_writer', description: 'Writes one scene' }], tools: [{ name: 'store_put', description: 'Store a value' }],
  profiles: ['fast'] };
let editAnswer = null;
let reviewAnswer = MACHINE;  // GET of the review machine
let layoutsKept = false;  // a layout PUT to review changes what its GET answers, as the server's does
let runAnswer = RUN;
let controlAnswer = null;  // r1's control answer, when it is not runAnswer
let runsAnswer = null;  // (query) -> the runs list, when not the two runs
globalThis.SERVER = (method, path, json) => {
  const p = path.replace('/plugins/stategraph/api', '');
  if (p === '/kinds') return KINDS;
  if (p === '/catalog') return CATALOG;
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
  if (p.endsWith('/layout')) {
    if (layoutsKept && p === '/machines/review/layout') reviewAnswer = { ...reviewAnswer, layout: json.layout };
    return {};
  }
  if (p.startsWith('/runs?')) return runsAnswer ? runsAnswer(new URLSearchParams(p.split('?')[1]))
    : [...RUNS, { ...RUNS[0], id: 'r2', status: 'succeeded', final_state: 'done' }];
  if (p === '/runs/r1/events') return { accepted: true, frame: '' };
  if (p === '/runs' && method === 'POST') return { run_id: 'r1' };
  if (p.startsWith('/runs/r1?')) return runAnswer;
  if (p === '/runs/r1/control') return controlAnswer || runAnswer;
  if (p.startsWith('/runs/r2?')) return RUN2;
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
const headName = () => ($('machineHead').innerHTML.match(/<h2 class="sg-head-name">([^<]*)<\/h2>/) || [])[1];
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
    await $('palette').fire('click', { target: element('button', { 'data-add-type': 'state' }) });
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
    check(/<textarea[^>]*readonly/.test(form) && /<button type="submit"[^>]*disabled/.test(form), 'Apply is offered');
    check(form.includes('description: anchored'), 'the value below the anchor is shown');
    await choose('work');
    const plain = $('side-inspect').innerHTML;
    check(!/<button type="submit"[^>]*disabled/.test(plain.slice(plain.indexOf('data-form="set-state"'))), 'a plain state is locked');
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
    check(/data-add-type="composite"/.test($('palette').innerHTML), 'the palette offers no composite');
    ANSWERS.prompt.push('group');
    await $('palette').fire('click', { target: element('button', { 'data-add-type': 'composite' }) });
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
    check($('side-inspect').innerHTML.includes('2 states'), 'the drag lost the selection');
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

  async a_composite_named_like_its_first_state_gets_another_one() {
    await boot('?machine=review');
    ANSWERS.prompt.push('start');
    await $('palette').fire('click', { target: element('button', { 'data-add-type': 'composite' }) });
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
    await $('palette').fire('click', { target: element('button', { 'data-add-type': 'state' }) });
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
    check(JSON.parse(localStorage.getItem('stategraph:params:review')).premise === 'x', 'params not kept');
    check($('paramFields').innerHTML.includes('data-type="string">x</textarea>'), `form: ${$('paramFields').innerHTML}`);
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
    check(shown.includes('open><summary>agent: block'), 'the block is open where no agent runs it');
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
    $('forkStep').value = '2';
    $('forkPause').checked = true;
    await $('debugBar').fire('click', { target: element('button', { 'data-control': 'fork' }) });
    await settle();
    const sent = CALLS.filter(([, path]) => path.endsWith('/control')).pop();
    check(sent && sent[2].action === 'fork' && sent[2].at_step === 2 && sent[2].pause === true, `sent ${JSON.stringify(sent && sent[2])}`);
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
    check(result.includes('Result of r2'), 'no result card');
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
    check($('runResult').innerHTML.includes('Result of r2'), 'no result of r2');
    HOLD = (method, path) => path.includes('/runs/r1');
    await $('runList').fire('rowselect', { detail: { id: 'r1' } });
    await settle();
    check(!$('runResult').innerHTML.includes('Result of r2'), 'the result of the run left stays under the next one');
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
    await $('yamlSave').fire('click', {});
    await settle();
    check(confirms() === 2 && !CALLS.some(([method]) => method === 'PUT'), 'a YAML save reloaded over the inspector unasked');
  },

  async a_deleted_machine_leaves_nothing_of_itself_behind() {
    localStorage.setItem('stategraph:breakpoints:review', '[{"state":"write","at":"enter"}]');
    localStorage.setItem('stategraph:params:review', '{"premise":"secret"}');
    await boot('?machine=review');
    await choose('write');
    await $('side-inspect').fire('click', { target: element('button', { 'data-act': 'initial' }) });  // an undo step
    await settle();
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'delete-machine' }) });
    await settle();
    check(!$('side-inspect').innerHTML.includes('sg-inspect-name'), 'the inspector still shows the deleted machine');
    check(localStorage.getItem('stategraph:params:review') === null, 'its params stay');
    await clickMachine('review');  // a machine of the same id again (made anew): the old steps are not its own
    await settle();
    check($('undo').disabled, 'its undo steps stay');
    check(localStorage.getItem('stategraph:breakpoints:review') === null, 'its breakpoints wait for a machine of the same id');
  },

  async the_sessions_of_another_users_run_are_not_offered() {
    document.querySelector = (selector) => (selector === '.sg-layout' ? { dataset: { viewer: 'ada' } } : null);
    await boot('?machine=review&run=r2');  // r2 is nobody's: its sessions are not ada's
    check($('runResult').innerHTML.includes('Result of r2'), 'no result of r2');
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
    await $('yamlSave').fire('click', {});
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
    await $('yamlSave').fire('click', {});
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
    check(!lastEdit() && TOASTS.filter(([, text]) => text === 'Nothing changed').length === 2, `sent: ${JSON.stringify(lastEdit())}`);
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
    check(/name="description" data-shape="text"[^>]*>\nline one\nline two/.test($('side-inspect').innerHTML),
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
