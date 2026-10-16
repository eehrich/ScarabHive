// The canvas (graph.js Canvas) against fake_dom.js and the vendored ELK, for the panel items of the review of
// 2026-10-15: a layout kept while ELK's input stays, ELK in a worker that may fail, the view a large machine opens at
// and Find gives, the keyboard on transitions and states, and the names of the run's marks. test_plugin_stategraph_
// r15_panel.py writes canvas_paths.js (GRAPH_JS: graph.js as a module specifier, ELK_PATH) and runs this file.
// Prints "PASS <case>" or "FAIL <case>: ..." per case.
import { ELK_PATH, GRAPH_JS } from './canvas_paths.js';

load('./fake_dom.js');
load(ELK_PATH);
const G = await import(GRAPH_JS);

function check(condition, message) {
  if (!condition) throw new Error(message);
}
/** n plain states in a row, s0 -> s1 -> ... */
const chain = (n) => ({
  initial: 's0', notes: [],
  states: Array.from({ length: n }, (_, i) => ({ name: `s${i}`, parent: null, type: 'state', composite: false, kind: null,
    label: '', icon: null, line: i + 1 })),
  transitions: Array.from({ length: n - 1 }, (_, i) => ({ id: `s${i}#0`, source: `s${i}`, index: 0, target: `s${i + 1}`,
    trigger: 'done', guard: null, effect: null })),
});
const svg = () => document.createElementNS('http://www.w3.org/2000/svg', 'svg');
const middleOf = (canvas, name) => {
  const box = canvas.nodes[G.stateId(name)];
  const { x, y, k } = canvas.view;
  return [(box.x + box.w / 2) * k + x, (box.y + box.h / 2) * k + y];
};
const near = (a, b) => Math.abs(a - b) < 1e-6;

const CASES = {
  async an_answer_whose_elk_input_is_unchanged_is_not_laid_out_again() {  // UI-2
    const canvas = new G.Canvas(svg());
    const real = canvas.elk;
    let layouts = 0;
    canvas.elk = { layout: (graph) => { layouts += 1; return real.layout(graph); } };
    const graph = chain(4);
    await canvas.setGraph(graph, {});
    const first = layouts;
    check(first >= 1 && canvas.nodes[G.stateId('s3')], `laid out ${first} times`);
    // what ELK does not see: a max_visits, a description
    await canvas.setGraph({ ...graph, states: graph.states.map((s) => ({ ...s, max_visits: 3, description: 'more' })) }, {});
    check(layouts === first && canvas.nodes[G.stateId('s3')], `an Apply ELK does not see was laid out again: ${layouts - first}`);
    await canvas.setGraph(chain(5), {});
    check(layouts > first && canvas.nodes[G.stateId('s4')], 'a new state is not laid out');
    const before = layouts;
    await canvas.setGraph(chain(5), { auto: 'classic' });
    check(layouts > before, 'another automatic layout is not laid out');
  },

  async a_worker_answers_the_layouts_and_one_that_fails_hands_them_to_the_page() {  // UI-2
    let failed;
    const answers = [];
    const inWorker = { layout: (graph) => new Promise((resolve) => answers.push(() => resolve({ from: 'worker', graph }))) };
    const page = { layout: async (graph) => ({ from: 'page', graph }) };
    let pages = 0;
    const elk = new G.WorkerElk((fail) => { failed = fail; return inWorker; }, () => { pages += 1; return page; });
    const one = elk.layout({ id: 'one' });
    answers.shift()();
    check((await one).from === 'worker', 'the worker\'s answer is not taken');
    const two = elk.layout({ id: 'two' });  // out when the worker fails
    failed();
    const late = answers.shift();
    const [got] = [await two];
    late();  // a worker answer after the failure is not taken twice
    check(got.from === 'page' && got.graph.id === 'two', `the waiting layout: ${JSON.stringify(got)}`);
    check((await elk.layout({ id: 'three' })).from === 'page' && pages === 1 && !answers.length, 'later layouts go to the worker');
  },

  async a_large_machine_opens_readable_on_a_state_and_find_zooms_while_a_click_does_not() {  // UI-13
    const canvas = new G.Canvas(svg());
    canvas.elk = null;  // a grid: 120 states run far down
    await canvas.setGraph(chain(120), {});
    canvas.fit();
    const whole = canvas.view.k;
    check(whole < 0.5, `fit ${whole}`);
    canvas.fit({ readable: true, on: 's60' });
    const [mx, my] = middleOf(canvas, 's60');
    check(canvas.view.k === 0.7 && near(mx, 450) && near(my, 300), `opened at ${canvas.view.k}, s60 at ${mx},${my}`);
    canvas.fit();
    canvas.reveal('s100');  // a click: whole, every state is in sight
    check(canvas.view.k === whole, 'a reveal without zoom zoomed');
    canvas.reveal('s100', { zoom: true });
    const [fx, fy] = middleOf(canvas, 's100');
    check(canvas.view.k === 1 && near(fx, 450) && near(fy, 300), `found at ${canvas.view.k}, s100 at ${fx},${fy}`);
    await canvas.setGraph(chain(3), {});
    canvas.fit({ readable: true, on: 's0' });
    check(canvas.view.k >= 0.7 && canvas.view.k <= 1.5, `a small machine opens whole: ${canvas.view.k}`);
  },

  async transitions_take_the_keyboard_a_focused_state_moves_by_the_arrows_and_the_marks_have_names() {  // UI-11
    let selected = null;
    let moved = null;
    const area = svg();
    const canvas = new G.Canvas(area, { onSelect: (s) => { selected = s; }, onMove: (spots) => { moved = spots; } });
    canvas.elk = null;
    await canvas.setGraph(chain(3), {});
    const link = () => area.querySelectorAll('.sg-link').find((l) => l.dataset.transition === 's0#0');
    // drawn before the states, a line in the Tab order would put every line before the first state: T reaches them
    check(link().getAttribute('tabindex') === '-1' && link().getAttribute('role') === 'button'
      && link().getAttribute('aria-label') === 'Transition s0 → s1', `the link: ${JSON.stringify(link().attrs)}`);
    const proto = Object.getPrototypeOf(link());
    const focusOf = proto.focus;
    let focused = null;
    proto.focus = function focus() { focused = this; };
    try {
      const s0 = () => area.querySelectorAll('.sg-node').find((n) => n.dataset.state === 's0');
      await area.fire('keydown', { key: 't', target: s0() });
      check(focused?.dataset?.transition === 's0#0', `T on s0: ${focused?.dataset?.transition}`);
      await area.fire('keydown', { key: 's', target: link() });
      check(focused?.dataset?.state === 's0', `S on its line: ${JSON.stringify(focused?.dataset)}`);
    } finally {
      proto.focus = focusOf;
    }
    await area.fire('keydown', { key: 'Enter', target: link() });
    check(selected?.kind === 'transition' && selected.id === 's0#0', `Enter on a line: ${JSON.stringify(selected)}`);
    check(link().getAttribute('aria-pressed') === 'true', 'the selected line does not say so');
    const node = () => area.querySelectorAll('.sg-node').find((n) => n.dataset.state === 's1');
    const at = G.relativeSpot(canvas.nodes, G.stateId('s1'));
    await area.fire('keydown', { key: 'ArrowRight', target: node() });
    check(moved?.s1?.x === at.x + 8 && moved.s1.y === at.y, `ArrowRight: ${JSON.stringify(moved)} from ${JSON.stringify(at)}`);
    await area.fire('keydown', { key: 'ArrowUp', shiftKey: true, target: node() });
    check(moved?.s1?.x === at.x + 8 && moved.s1.y === at.y - 1, `Shift+ArrowUp: ${JSON.stringify(moved)}`);
    canvas.setOverlay({ run: { active: new Set(['s1']), current: 's1', paused: 's1', visits: { s1: 2 }, submachines: new Set(),
      lastEdge: 's0#0' }, breakpoints: new Set(['s1']), problems: { states: {}, transitions: {} } });
    const said = node().getAttribute('aria-description') || '';
    check(['the run is paused here', 'visited 2 times', 'breakpoint'].every((part) => said.includes(part)), `s1: ${said}`);
    check((link().getAttribute('aria-description') || '').includes('the run took it last'), 'the last transition has no name');
  },

  async a_run_id_keeps_its_label_and_hash() {  // UI-3
    check(G.runLabel('9e0tq7uhrt_009_ch795_3c2cac96') === '…_009_ch795_3c2cac96', G.runLabel('9e0tq7uhrt_009_ch795_3c2cac96'));
    check(G.runLabel('so7cus2i2h_015_recheck_0_fa6fe105') === '…_015_recheck_0_fa6fe105', G.runLabel('so7cus2i2h_015_recheck_0_fa6fe105'));
    check(G.runLabel('r1') === 'r1', 'a short id is kept');
    const long = G.runLabel('a'.repeat(40));
    check(long.length === 24 && long.includes('…'), `an id without segments: ${long}`);
  },
};

for (const [name, fn] of Object.entries(CASES)) {
  try {
    await fn();
    print(`PASS ${name}`);
  } catch (error) {
    print(`FAIL ${name}: ${error.message}`);
  }
}
print(`CASES ${Object.keys(CASES).join(' ')}`);
print('ERRORS', JSON.stringify(ERRORS));
