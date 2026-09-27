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
let editAnswer = null;
let runAnswer = RUN;
let controlAnswer = null;  // r1's control answer, when it is not runAnswer
globalThis.SERVER = (method, path, json) => {
  const p = path.replace('/plugins/stategraph/api', '');
  if (p === '/kinds') return KINDS;
  if (p === '/machines' && method === 'GET') {
    const groups = { review: 'Writer/v6', other: 'Writer', ro: 'stategraph' };
    return ['review', 'other', 'hooks', 'ro', 'empty', 'plain', 'fields'].map((id) => ({ id, title: id, errors: 0, warnings: 0, writable: id !== 'ro',
      group: groups[id] || 'My machines' }));
  }
  if (p === '/machines/review' && method === 'GET') return MACHINE;
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
  if (p === '/machines/review' && method === 'DELETE') return { deleted: 'review', files: ['review.yaml'], kept_module: null };
  if (p.endsWith('/edit')) return editAnswer || MACHINE;
  if (p.endsWith('/layout')) return {};
  if (p.startsWith('/runs?')) return [...RUNS, { ...RUNS[0], id: 'r2', status: 'succeeded', final_state: 'done' }];
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
    await $('side-inspect').fire('submit', { target: element('form', { 'data-transition': 'write#0' }) });
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
    await boot('?machine=review');
    await choose('write');
    await $('machineHead').fire('click', { target: element('button', { 'data-act': 'delete-machine' }) });
    await settle();
    check(!$('side-inspect').innerHTML.includes('sg-inspect-name'), 'the inspector still shows the deleted machine');
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
    check(/name="question" data-shape="text" data-orig="[^"]*" disabled/.test(shown) && /id="af-kind" name="kind" disabled/.test(shown),
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
    check(/id="af-kind" name="kind" disabled/.test(shown) && /name="task" data-shape="text" data-orig="t" disabled/.test(shown),
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
