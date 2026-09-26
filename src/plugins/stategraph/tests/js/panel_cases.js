// Single cases of the panel in JavaScriptCore, each in a jsc of its own (the panel's state is module state):
// case.js names the case, test_plugin_stategraph_js.py writes it and runs this file once per case. The panel runs
// against fake_kit_held.js (the real kit's timing: answers later or when released, `latest` and abandon() abort) and
// fake_dom.js, without ELK (the canvas falls back to its grid). Prints "PASS <case>" or "FAIL <case>: ...".
import { CASE } from './case.js';
import { HOOKS, MACHINE, RUN, RUNS, KINDS } from './fixtures.js';
import { ApiError } from './fake_kit.js';

load('./fake_dom.js');

globalThis.RENDERS = []; globalThis.ICONS = new Set(); globalThis.TOASTS = []; globalThis.CALLS = []; globalThis.ASKED = [];
globalThis.TABS = {}; globalThis.ANSWERS = { prompt: [], confirm: true, dialog: null };
const OTHER = { ...MACHINE, id: 'other', versions: { 'review.yaml': 'v9', 'review.py': 'p9' } };
let editAnswer = null;
let runAnswer = RUN;
globalThis.SERVER = (method, path, json) => {
  const p = path.replace('/plugins/stategraph/api', '');
  if (p === '/kinds') return KINDS;
  if (p === '/machines' && method === 'GET') {
    return ['review', 'other', 'hooks'].map((id) => ({ id, title: id, errors: 0, warnings: 0, writable: true }));
  }
  if (p === '/machines/review' && method === 'GET') return MACHINE;
  if (p === '/machines/other' && method === 'GET') return OTHER;
  if (p === '/machines/hooks' && method === 'GET') return HOOKS;
  if (p.endsWith('/edit')) return editAnswer || MACHINE;
  if (p.endsWith('/layout')) return {};
  if (p.startsWith('/runs?')) return RUNS;
  if (p === '/runs' && method === 'POST') return { run_id: 'r1' };
  if (p.startsWith('/runs/r1?')) return runAnswer;
  if (p === '/runs/r1/control') return runAnswer;
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
