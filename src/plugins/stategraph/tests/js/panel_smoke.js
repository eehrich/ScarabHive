// The panel in JavaScriptCore, without a browser: static/panel.js runs against fake_kit.js (the kit's names, no
// shell), fake_dom.js (elements, classes, listeners, forms) and the real graph.js and ELK. Every main path runs
// once -- open a machine, select, add, connect, drag, rename, remove, inspect, debug a run, the YAML tab, start a
// run -- and each step prints ok or FAIL. test_plugin_stategraph_js.py prepares the directory it runs in: this
// file, the fakes, fixtures.js (a real graph_view and describe_kinds), paths.js and panel_copy.js (panel.js with
// its two imports pointed at the fake kit and the real graph.js).
import { ELK_PATH } from './paths.js';
import { MACHINE, RUN, RUNS, KINDS } from './fixtures.js';
import { ApiError } from './fake_kit.js';

load('./fake_dom.js');
load(ELK_PATH);

globalThis.RENDERS = []; globalThis.ICONS = new Set(); globalThis.TOASTS = []; globalThis.CALLS = []; globalThis.ASKED = [];
globalThis.TABS = {}; globalThis.ANSWERS = { prompt: [], confirm: true, dialog: null };
let editAnswer = null;
globalThis.SERVER = (method, path, json) => {
  const p = path.replace('/plugins/stategraph/api', '');
  if (p === '/kinds') return KINDS;
  if (p === '/machines' && method === 'GET') return [{ id: 'review', title: 'Review', description: 'd', valid: false, errors: 1, warnings: 2, writable: true, file: '/m/review.yaml' }];
  if (p === '/machines' && method === 'POST') return { ...MACHINE, id: json.id };
  if (p === '/machines/review' && method === 'GET') return MACHINE;
  if (p === '/machines/review' && method === 'PUT') return globalThis.SAVE_ANSWER || { versions: {} };
  if (p === '/machines/review/edit') return editAnswer || MACHINE;
  if (p === '/machines/review/layout') return {};
  if (p === '/validate') return { problems: MACHINE.problems, graph: MACHINE.graph };
  if (p.startsWith('/runs?')) return RUNS;
  if (p === '/runs' && method === 'POST') return { run_id: 'r1' };
  if (p.startsWith('/runs/r1?')) return RUN;
  if (p === '/runs/r1/control') {
    if (json.action === 'evaluate' || json.action === 'set') return { value: { v: 1 } };
    if (json.action === 'fork') return { run_id: 'r2', forked_from: 'r1' };
    return RUN;
  }
  if (p.startsWith('/runs/r2?')) return { ...RUN, id: 'r2', status: 'succeeded', active: false, terminal: true, output: { ok: 1 }, debug: { breakpoints: [], watchpoints: [], watch: {}, paused: null } };
  if (p === '/runs/r1/events') return { accepted: true, frame: 's3/m/' };
  throw new Error(`unexpected ${method} ${path}`);
};

const $ = (id) => document.getElementById(id);
const settle = async () => { for (let i = 0; i < 30; i += 1) await new Promise((r) => setTimeout(r, 0)); };
const button = (data, parent = null) => { const b = new FakeElement('button'); for (const [k, v] of Object.entries(data)) b.setAttribute(k, v); if (parent) parent.appendChild(b); return b; };
const steps = [];
async function step(name, fn) {
  try { await fn(); await settle(); steps.push(`ok   ${name}`); } catch (error) { steps.push(`FAIL ${name}: ${error} ${error.stack?.split('\n')[0] || ''}`); }
}

await step('start: kinds, machines, open from the URL, run from the URL', async () => {
  location.search = '?machine=review&run=r1';
  await import('./panel_copy.js');
});
await step('the machine is drawn', () => {
  if ($('machineView').hidden) throw new Error('machine view still hidden');
  if (!$('canvas').querySelectorAll('.sg-node').length) throw new Error('no nodes on the canvas');
  if ($('canvas').querySelectorAll('.sg-link').length < 4) throw new Error('too few edges');
  if (!$('palette').innerHTML.includes('data-add-kind="agent"')) throw new Error('palette without kinds');
  if (!$('side-inspect').innerHTML.includes('data-form="machine-fields"')) throw new Error('no machine overview');
});
await step('the run is shown: debug bar, pane, history, overlay', () => {
  if ($('debugBar').hidden) throw new Error('debug bar hidden');
  for (const id of ['dbgRun', 'dbgWatch', 'dbgPoints', 'dbgFrames']) if (!$(id).innerHTML) throw new Error(`${id} empty`);
  if (!$('runHistory').innerHTML.includes('draft text')) throw new Error('history without the activity out');
  const current = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'read');
  if (!current.classList.contains('is-paused') || !current.classList.contains('is-active')) throw new Error(`overlay classes: ${current.getAttribute('class')}`);
  const last = $('canvas').querySelectorAll('.sg-link').find((n) => n.dataset.transition === 'write#0');
  if (!last.classList.contains('is-last')) throw new Error('last transition not marked');
});
await step('choose a state: inspector with fragment, breakpoints, transitions', async () => {
  const node = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'write');
  await $('canvas').fire('pointerdown', { button: 0, target: node, clientX: 10, clientY: 10, pointerId: 1 });
  await $('canvas').fire('pointerup', { target: node, clientX: 10, clientY: 10 });
  const html = $('side-inspect').innerHTML;
  if (!html.includes('agent: scene_writer')) throw new Error('fragment missing');
  if (!html.includes('data-breakpoint="enter"') || !html.includes('Transitions')) throw new Error('inspector incomplete');
});
await step('choose a transition by clicking its edge', async () => {
  const link = $('canvas').querySelectorAll('.sg-link').find((n) => n.dataset.transition === 'write#1');
  await $('canvas').fire('pointerdown', { button: 0, target: link, clientX: 10, clientY: 10, pointerId: 1 });
  if (!$('side-inspect').innerHTML.includes('Transition')) throw new Error('no transition editor');
});
await step('add a state from the palette (prompt answers a name)', async () => {
  ANSWERS.prompt.push('judge');
  await $('palette').fire('click', { target: button({ 'data-add-kind': 'agent' }) });
  const call = CALLS.find(([m, p]) => p.endsWith('/edit'));
  if (!call || call[2].op.op !== 'add_state' || call[2].op.do.agent !== '' || call[2].expected_version !== 'v1') throw new Error(JSON.stringify(call));
});
await step('connect two states by dragging from a handle', async () => {
  const handle = new FakeElement('circle'); handle.setAttribute('data-handle', 'done');
  const target = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'write');
  document.elementFromPoint = () => target;
  await $('canvas').fire('pointerdown', { button: 0, target: handle, clientX: 5, clientY: 5, pointerId: 1 });
  await $('canvas').fire('pointermove', { target: handle, clientX: 50, clientY: 50 });
  await $('canvas').fire('pointerup', { target: handle, clientX: 50, clientY: 50 });
  const call = CALLS.filter(([m, p]) => p.endsWith('/edit')).pop();
  if (call[2].op.op !== 'add_transition' || call[2].op.source !== 'done' || call[2].op.target !== 'write') throw new Error(JSON.stringify(call[2]));
});
await step('drag a state: the position goes to the layout', async () => {
  const node = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'review');
  await $('canvas').fire('pointerdown', { button: 0, target: node, clientX: 100, clientY: 100, pointerId: 1 });
  await $('canvas').fire('pointermove', { target: node, clientX: 160, clientY: 130 });
  await $('canvas').fire('pointerup', { target: node, clientX: 160, clientY: 130 });
  const call = CALLS.filter(([m, p]) => p.endsWith('/layout')).pop();
  if (!call || !call[2].layout.positions.review || !call[2].layout.positions.done) throw new Error(JSON.stringify(call));
});
await step('rename (double click) and remove with Delete', async () => {
  const node = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'done');
  ANSWERS.prompt.push('finished');
  await $('canvas').fire('dblclick', { target: node });
  const rename = CALLS.filter(([m, p]) => p.endsWith('/edit')).pop();
  if (rename[2].op.op !== 'rename_state' || rename[2].op.new !== 'finished') throw new Error(JSON.stringify(rename[2]));
  await $('canvas').fire('pointerdown', { button: 0, target: node, clientX: 10, clientY: 10, pointerId: 1 });
  await $('canvas').fire('pointerup', { target: node, clientX: 10, clientY: 10 });
  await $('canvas').fire('keydown', { key: 'Delete', target: $('canvas') });
  const removal = CALLS.filter(([m, p]) => p.endsWith('/edit')).pop();
  if (removal[2].op.op !== 'remove_state') throw new Error(JSON.stringify(removal[2]));
});
await step('inspector: apply a transition and the fragment, toggle a breakpoint on the live run', async () => {
  const node = $('canvas').querySelectorAll('.sg-node').find((n) => n.dataset.state === 'write');
  await $('canvas').fire('pointerdown', { button: 0, target: node, clientX: 10, clientY: 10, pointerId: 1 });
  await $('canvas').fire('pointerup', { target: node, clientX: 10, clientY: 10 });
  const form = new FakeElement('form'); form.setAttribute('data-transition', 'write#0');
  for (const [name, shape, orig, value] of [['trigger', 'enum', 'error', 'done'], ['target', 'enum', 'review', 'review'],
    ['guard', 'line', '', 'ctx.ok'], ['effect', 'code', '', '']]) {
    Object.assign(form.elements[name], { name, value });
    form.elements[name].dataset.shape = shape;
    form.elements[name].dataset.orig = orig;
  }
  form.querySelector = () => new FakeElement('button');
  await $('side-inspect').fire('submit', { target: form });
  const update = CALLS.filter(([m, p]) => p.endsWith('/edit')).pop();
  if (update[2].op.op !== 'update_transition' || JSON.stringify(update[2].op.fields) !== '{"trigger":null,"guard":"ctx.ok"}') throw new Error(JSON.stringify(update[2]));
  const frag = new FakeElement('form'); frag.setAttribute('data-form', 'set-state'); frag.elements.yaml.value = 'type: final';
  frag.querySelector = () => new FakeElement('button');
  await $('side-inspect').fire('submit', { target: frag });
  const set = CALLS.filter(([m, p]) => p.endsWith('/edit')).pop();
  if (set[2].op.op !== 'set_state' || set[2].op.name !== 'write') throw new Error(JSON.stringify(set[2]));
  const box = new FakeElement('input'); box.setAttribute('data-breakpoint', 'exit'); box.checked = true;
  await $('side-inspect').fire('change', { target: box });
  const control = CALLS.filter(([m, p]) => p.endsWith('/control')).pop();
  if (control[2].action !== 'set_breakpoints' || !control[2].breakpoints.some((b) => b.state === 'write' && b.at === 'exit')) throw new Error(JSON.stringify(control[2]));
});
await step('debug bar: continue, run to, fork', async () => {
  await $('debugBar').fire('click', { target: button({ 'data-control': 'continue' }) });
  $('runToState').value = 'review';
  await $('debugBar').fire('click', { target: button({ 'data-control': 'run_to' }) });
  const runTo = CALLS.filter(([m, p]) => p.endsWith('/control')).pop();
  if (runTo[2].action !== 'run_to' || runTo[2].state !== 'review') throw new Error(JSON.stringify(runTo[2]));
  $('forkStep').value = '2';
  await $('debugBar').fire('click', { target: button({ 'data-control': 'fork' }) });
  const fork = CALLS.filter(([m, p]) => p.endsWith('/control')).pop();
  if (fork[2].action !== 'fork' || fork[2].at_step !== 2) throw new Error(JSON.stringify(fork[2]));
});
await step('back to r1; debug pane: watch, evaluate, set, send event', async () => {
  await $('runList').fire('rowselect', { detail: { id: 'r1' } });
  $('watchForm').elements.expr.value = 'len(ctx.draft)';
  $('watchForm').querySelector = () => new FakeElement('button');
  await $('watchForm').fire('submit', {});
  const watch = CALLS.filter(([m, p]) => p.endsWith('/control')).pop();
  if (watch[2].action !== 'set_watchpoints' || watch[2].watchpoints.length !== 2) throw new Error(JSON.stringify(watch[2]));
  $('evalForm').elements.expr.value = 'ctx.round';
  $('evalForm').querySelector = () => new FakeElement('button');
  await $('evalForm').fire('submit', {});
  if (!$('dbgEval').innerHTML.includes('ctx.round')) throw new Error('evaluation not shown');
  $('setForm').elements.path.value = 'ctx.round'; $('setForm').elements.expr.value = '5';
  $('setForm').querySelector = () => new FakeElement('button');
  await $('setForm').fire('submit', {});
  $('eventForm').elements.name.value = 'approve'; $('eventForm').elements.frame.value = ''; $('eventForm').elements.data.value = '{"ok": true}';
  $('eventForm').querySelector = () => new FakeElement('button');
  await $('eventForm').fire('submit', {});
  const sent = CALLS.filter(([m, p]) => p.endsWith('/events')).pop();
  if (!sent || sent[2].name !== 'approve' || sent[2].data.ok !== true) throw new Error(JSON.stringify(sent));
});
await step('YAML tab: type, validate, save with a conflict (reload offered)', async () => {
  $('yamlText').value = MACHINE.files['review.yaml'] + '\n# more\n';
  await $('yamlText').fire('input', {});
  if (!DIRTY) throw new Error('not dirty');
  await $('yamlValidate').fire('click', {});
  if (!$('yamlProblems').innerHTML.includes('SG004')) throw new Error('problems not listed');
  globalThis.SAVE_ANSWER = new ApiError(409, 'the file changed since you read it');
  ANSWERS.dialog = 'reload';
  await $('yamlSave').fire('click', {});
  if (!ASKED.some(([k, t]) => k === 'dialog' && t === 'The file changed')) throw new Error('no conflict dialog');
  if (DIRTY) throw new Error('still dirty after reload');
  globalThis.SAVE_ANSWER = null;
});
await step('a problem click jumps to its state', async () => {
  $('yamlProblems').problems = MACHINE.problems;
  const row = new FakeElement('button'); row.setAttribute('data-problem', String(MACHINE.problems.length - 1));
  await $('yamlProblems').fire('click', { target: row });
  if (TABS.mainTabs !== 'graph') throw new Error(`tab ${TABS.mainTabs}`);
});
await step('start a run from the form', async () => {
  // the param fields are rendered markup, which the fake DOM does not parse: readParams sees none
  $('mocks').value = '{"write": "a draft"}';
  $('mockOnly').checked = true;
  await $('startForm').fire('submit', {});
  const start = CALLS.filter(([m, p]) => m === 'POST' && p.endsWith('/api/runs')).pop();
  if (!start || start[2].mock_only !== true || start[2].mocks.write !== 'a draft') throw new Error(JSON.stringify(start));
});
await step('auto layout and new machine', async () => {
  await $('autoLayout').fire('click', {});
  ANSWERS.prompt.push('other_machine');
  await $('newMachine').fire('click', {});
  const created = CALLS.filter(([m, p]) => m === 'POST' && p.endsWith('/api/machines')).pop();
  if (!created || created[2].id !== 'other_machine') throw new Error(JSON.stringify(created));
});
await step('refresh event', async () => {
  for (const fn of DOC_LISTENERS.refresh || []) await fn({ detail: { auto: true } });
});

for (const line of steps) print(line);
print(`SUMMARY ${steps.filter((l) => l.startsWith('ok')).length}/${steps.length}`);
print('ERRORS', JSON.stringify(ERRORS));
print('TOASTS', JSON.stringify(TOASTS.filter(([k]) => k !== 'ok')));
print('ICONS', [...ICONS].sort().join(' '));
