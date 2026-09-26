// The canvas's pure functions, in JavaScriptCore (no DOM):
//   jsc -m src/plugins/stategraph/tests/js/graph_tests.js
// Prints one PASS/FAIL line per test and "SUMMARY <passed>/<total>"; test_plugin_stategraph_js.py runs it.
// The layout test loads the vendored ELK the way the panel does (a classic script defining the global ELK).

import {
  applyPositions, clipToBox, edgeRoute, edgeText, elkInput, layoutFrom, nodeSize, PAD, problemIndex, runOverlay,
  fragmentLock, stateFragment, stateId,
} from '../../static/graph.js';

const results = [];
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function equal(actual, expected, message) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${message}: ${a} !== ${b}`);
}

const GRAPH = {
  id: 'review', initial: 'write',
  states: [
    { name: 'write', parent: null, type: 'state', composite: false, kind: 'agent', label: 'scene_writer', icon: 'brain', path: 'states.write', line: 8 },
    { name: 'review', parent: null, type: 'state', composite: true, initial: 'read', kind: null, label: '', icon: null, path: 'states.review', line: 20 },
    { name: 'read', parent: 'review', type: 'state', composite: false, kind: 'machine', label: 'critique', icon: 'layers', path: 'states.review.states.read', line: 23 },
    { name: 'verdict', parent: 'review', type: 'final', composite: false, kind: null, label: '', icon: null, path: 'states.review.states.verdict', line: 27 },
    { name: 'route', parent: null, type: 'choice', composite: false, kind: null, label: '', icon: null, path: 'states.route', line: 32 },
    { name: 'done', parent: null, type: 'final', composite: false, kind: null, label: '', icon: null, path: 'states.done', line: 38 },
  ],
  transitions: [
    { id: 'write#0', source: 'write', index: 0, target: 'review', trigger: 'done', guard: null, effect: 'ctx.draft = out', path: 'states.write.transitions[0]' },
    { id: 'write#1', source: 'write', index: 1, target: 'done', trigger: 'error', guard: null, effect: null, path: 'states.write.transitions[1]' },
    { id: 'read#0', source: 'read', index: 0, target: 'verdict', trigger: 'done', guard: null, effect: null, path: 'states.review.states.read.transitions[0]' },
    { id: 'review#0', source: 'review', index: 0, target: 'route', trigger: 'done', guard: null, effect: null, path: 'states.review.transitions[0]' },
    { id: 'route#0', source: 'route', index: 0, target: 'done', trigger: 'done', guard: 'ctx.round >= 3', effect: null, path: 'states.route.transitions[0]' },
    { id: 'route#1', source: 'route', index: 1, target: 'write', trigger: 'done', guard: 'else', effect: null, path: 'states.route.transitions[1]' },
    { id: 'route#2', source: 'route', index: 2, target: 'ghost', trigger: 'done', guard: null, effect: null, path: 'states.route.transitions[2]' },
    { id: 'write#2', source: 'write', index: 2, target: null, trigger: 'poke', guard: null, effect: 'ctx.n = 1', path: 'states.write.transitions[2]' },
  ],
};

// ------------------------------------------------------------------ labels and sizes

test('edgeText writes trigger, guard and effect, and leaves the completion trigger out', () => {
  equal(edgeText({ trigger: 'done', guard: null, effect: null }), '', 'plain completion');
  equal(edgeText({ trigger: 'error', guard: "error.type == 'timeout'", effect: 'ctx.x = 1' }),
    "error [error.type == 'timeout'] / ctx.x = 1", 'full label');
  equal(edgeText({ trigger: 'done', guard: 'else', effect: 'a = 1\nb = 2' }), '[else] / a = 1 …', 'several statements');
  assert(edgeText({ trigger: 'done', guard: 'x'.repeat(80) }).length < 45, 'a long guard is shortened');
});

test('nodeSize gives pseudostates their shape size and a labelled state two lines', () => {
  equal(nodeSize({ name: 'c', type: 'choice' }), [30, 30], 'choice');
  equal(nodeSize({ name: 'f', type: 'final' }), [26, 26], 'final');
  equal(nodeSize({ name: 'j', type: 'junction' }), [14, 14], 'junction');
  assert(nodeSize({ name: 'w', type: 'state', label: 'agent_x', icon: 'brain' })[1] > nodeSize({ name: 'w', type: 'state' })[1],
    'a label makes the box taller');
});

// ------------------------------------------------------------------ ELK input

test('elkInput nests composites, adds an initial dot per region and skips edges it cannot draw', () => {
  const input = elkInput(GRAPH);
  const top = input.children.map((n) => n.id);
  equal(top, ['i:', 's:write', 's:review', 's:route', 's:done'], 'top-level nodes in order, initial dot first');
  const review = input.children.find((n) => n.id === 's:review');
  equal(review.children.map((n) => n.id), ['i:review', 's:read', 's:verdict'], 'the composite holds its region');
  assert(!('width' in review), 'ELK sizes a composite from its children');
  const edges = input.edges.map((e) => e.id);
  assert(edges.includes('ie:') && edges.includes('ie:review'), 'an initial edge per region');
  assert(edges.includes('t:read#0') && edges.includes('t:route#1'), 'transitions become edges');
  assert(!edges.includes('t:route#2'), 'an unknown target is not drawn');
  assert(!edges.includes('t:write#2'), 'an internal transition is not an edge');
  equal(input.edges.find((e) => e.id === 't:route#1').labels[0].text, '[else]', 'the label goes to ELK');
  equal(input.layoutOptions['elk.hierarchyHandling'], 'INCLUDE_CHILDREN', 'edges cross composite borders');
});

test('ELK lays the graph out: children inside their composite, every edge routed', async () => {
  globalThis.window = globalThis;
  globalThis.document = globalThis.document || {};
  globalThis.console = globalThis.console || { log: print, warn: print, error: print };
  globalThis.console.err = globalThis.console.error;
  load('../../static/vendor/elkjs/elk.bundled.js');
  const out = await new ELK().layout(elkInput(GRAPH));
  const layout = layoutFrom(out);
  const review = layout.nodes['s:review'];
  const read = layout.nodes['s:read'];
  assert(read.parent === 's:review', 'the child knows its parent');
  assert(read.x >= review.x + PAD.left - 1 && read.y >= review.y + PAD.top - 1, `the child sits in the content area: ${JSON.stringify([review, read])}`);
  assert(read.x + read.w <= review.x + review.w && read.y + read.h <= review.y + review.h, 'the child fits inside');
  for (const id of ['t:write#0', 't:read#0', 't:route#1', 'ie:', 'ie:review']) {
    assert(layout.edges[id] && layout.edges[id].points.length >= 2, `edge ${id} is routed`);
  }
  assert(layout.nodes['s:write'].x < layout.nodes['s:route'].x, 'left to right');
});

// ------------------------------------------------------------------ positions

const AUTO = {
  nodes: {
    's:a': { x: 10, y: 10, w: 100, h: 40, parent: null },
    's:c': { x: 200, y: 10, w: 240, h: 120, parent: null },
    's:c1': { x: 214, y: 44, w: 100, h: 40, parent: 's:c' },
  },
  edges: {},
};

test('applyPositions: a moved composite takes its children along', () => {
  const { nodes, moved } = applyPositions(AUTO, { c: { x: 300, y: 50 } });
  equal([nodes['s:c'].x, nodes['s:c'].y], [300, 50], 'the composite moved');
  equal([nodes['s:c1'].x, nodes['s:c1'].y], [314, 84], 'the child keeps its offset');
  assert(moved.has('s:c') && moved.has('s:c1') && !moved.has('s:a'), 'moved marks exactly what moved');
});

test('applyPositions: a composite grows to hold a child moved to its edge, and a child stays inside', () => {
  const grown = applyPositions(AUTO, { c1: { x: 400, y: 200 } }).nodes;
  assert(grown['s:c'].w >= 400 + 100 + PAD.right && grown['s:c'].h >= 200 + 40 + PAD.bottom, 'the composite grew');
  const clamped = applyPositions(AUTO, { c1: { x: -50, y: 0 } }).nodes;
  equal([clamped['s:c1'].x, clamped['s:c1'].y], [200 + PAD.left, 10 + PAD.top], 'the child stays in the content area');
  const ignored = applyPositions(AUTO, { ghost: { x: 1, y: 1 }, a: { x: 'x', y: 1 } });
  equal(ignored.moved.size, 0, 'unknown names and broken spots are ignored');
});

test('clipToBox and edgeRoute: straight lines leave the boxes at their border', () => {
  const box = { x: 0, y: 0, w: 100, h: 40 };
  equal(clipToBox(box, 300, 20), [100, 20], 'to the right');
  equal(clipToBox(box, 50, -100), [50, 0], 'upwards');
  const route = { points: [[1, 1], [2, 2]], label: { x: 0, y: 0, w: 1, h: 1 } };
  equal(edgeRoute(route, box, box, false).points, [[1, 1], [2, 2]], 'an unmoved edge keeps its route');
  const straight = edgeRoute(route, box, { x: 200, y: 0, w: 100, h: 40 }, true);
  equal(straight.points, [[100, 20], [200, 20]], 'a moved edge is drawn straight');
  equal(edgeRoute(null, box, box, true).points.length, 4, 'a self-transition loops');
});

// ------------------------------------------------------------------ overlays

test('problemIndex pins a problem to its transition and state; other files and the machine stay apart', () => {
  const problems = [
    { level: 'error', code: 'SG004', message: 'x', path: 'states.route.transitions[0].guard', file: '/m/review.yaml', line: 34 },
    { level: 'warning', code: 'SG101', message: 'y', path: 'states.review.states.verdict', file: '/m/review.yaml', line: 27 },
    { level: 'error', code: 'SG002', message: 'z', path: 'initial', file: '/m/review.yaml', line: 3 },
    { level: 'error', code: 'SG004', message: 'w', path: '', file: '/m/review.py', line: 2 },
    { level: 'error', code: 'SG001', message: 'v', path: '', file: '/m/review.yaml', line: 24 },
  ];
  const index = problemIndex(GRAPH, problems, 'review.yaml');
  equal(index.transitions['route#0'].errors, 1, 'the transition');
  equal(index.states.route.errors, 1, 'and its state');
  equal(index.states.verdict.warnings, 1, 'the nested state, not its composite');
  assert(!index.states.review || !index.states.review.warnings, 'the longest path wins');
  equal(index.machine.map((p) => p.message), ['z', 'w'], 'machine-level and other files');
  equal(index.states.read.errors, 1, 'a line alone finds the state whose lines hold it');
});

test('runOverlay reads the root frame, the pause, submachine frames and the last transition', () => {
  const run = {
    view: { frames: [
      { prefix: '', path: '', state: 'read', config: ['review', 'read'], visits: { write: 2, review: 1, read: 1 } },
      { prefix: 's3/m/', path: 'read', state: 'done', config: ['done'], visits: {} },
    ] },
    debug: { paused: { frame: '', state: 'read', hook: 'enter' } },
    journal: [
      { kind: 'trace', status: 'transition', data: { frame: '', from: 'route', to: 'write', index: 1 } },
      { kind: 'trace', status: 'transition', data: { frame: '', from: 'write', to: 'review', index: 0 } },
      { kind: 'trace', status: 'transition', data: { frame: 's3/m/', from: 'x', to: 'y', index: 0 } },
    ],
  };
  const overlay = runOverlay(run);
  assert(overlay.active.has('review') && overlay.active.has('read') && !overlay.active.has('done'), 'root config only');
  equal([overlay.current, overlay.paused, overlay.visits.write], ['read', 'read', 2], 'leaf, pause, visits');
  assert(overlay.submachines.has('read'), 'the state a submachine runs under');
  equal(overlay.lastEdge, 'write#0', 'the last transition of the root frame');
  equal(runOverlay(null).active.size, 0, 'no run, no overlay');
});

// ------------------------------------------------------------------ fragments

test('stateFragment cuts a state body out of the file, dedented, up to its next sibling', () => {
  const text = [
    'states:',            // 1
    '  write:   # w',     // 2
    '    do:',            // 3
    '      agent: a',     // 4
    '',                   // 5
    '    # inner note',   // 6
    '    transitions:',   // 7
    '      - target: b',  // 8
    '',                   // 9
    '  # about b',        // 10
    '  b: {type: final}  # the end', // 11
    '  c:',               // 12
  ].join('\n');
  equal(stateFragment(text, 2), 'do:\n  agent: a\n\n# inner note\ntransitions:\n  - target: b', 'a block body');
  equal(stateFragment(text, 11), '{type: final}  # the end', 'an inline flow mapping, with its comment');
  equal(stateFragment(text, 12), '', 'an empty state');
  equal(stateFragment(text, 99), '', 'a line outside the file');
  equal([2, 11, 12].map((line) => fragmentLock(text, line)), ['', '', ''], 'nothing locked in plain layouts');
});

test('stateFragment and fragmentLock: an anchor, a tag, an alias or a flow value over several lines', () => {
  const text = [
    'states:',                      // 1
    '  a: &base   # shared',        // 2
    '    description: first',       // 3
    '  b: !special',                // 4
    '    type: final',              // 5
    '  c: {type: final,',           // 6
    '      description: end}',      // 7
    '  d: *base',                   // 8
    '  e: {type: final}',           // 9
    '    # a note below',           // 10
  ].join('\n');
  equal(stateFragment(text, 2), 'description: first', 'an anchor alone: the value is the block below');
  equal(stateFragment(text, 4), 'type: final', 'a tag alone: the same');
  equal(stateFragment(text, 6), '{type: final,\ndescription: end}', 'a flow value over two lines, whole');
  equal(stateFragment(text, 9), '{type: final}', 'a comment below an inline value is not part of it');
  equal([2, 4, 8].map((line) => Boolean(fragmentLock(text, line))), [true, true, true], 'anchor, tag, alias: locked');
  assert(fragmentLock(text, 6).includes('goes on below'), 'the flow over two lines is locked');
  equal(fragmentLock(text, 9), '', 'a comment below does not lock');
});

// ------------------------------------------------------------------ run

for (const [name, fn] of tests) {
  try {
    await fn();
    results.push(true);
    print(`PASS ${name}`);
  } catch (error) {
    results.push(false);
    print(`FAIL ${name}: ${error.message}`);
  }
}
print(`SUMMARY ${results.filter(Boolean).length}/${results.length}`);
