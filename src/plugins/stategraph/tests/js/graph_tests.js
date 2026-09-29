// The canvas's pure functions, in JavaScriptCore (no DOM):
//   jsc -m src/plugins/stategraph/tests/js/graph_tests.js
// Prints one PASS/FAIL line per test and "SUMMARY <passed>/<total>"; test_plugin_stategraph_js.py runs it.
// The layout test loads the vendored ELK the way the panel does (a classic script defining the global ELK).

import {
  applyPositions, clipToBox, compositeTitleWidth, edgeRoute, edgeText, elkInput, gridLayout, layoutFrom, nodeSize, PAD,
  posixPath, problemIndex, runOverlay, fragmentLock, stateFragment, stateId, outermost, sameSelection, selectedStates,
  groupedSpots, labelSpot, selectedTransitions, selectionOf, statesWithin, toggled,
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
  equal(Object.entries(layout.initials).sort(), [['i:', 's:write'], ['i:review', 's:read']], 'each initial dot knows the state it points to');
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

test('applyPositions: an initial dot sits left of its state once that one is placed, else where ELK put it', () => {
  const auto = {
    nodes: { 'i:': { x: 400, y: 300, w: 14, h: 14, parent: null }, ...AUTO.nodes,
      'i:c': { x: 380, y: 100, w: 14, h: 14, parent: 's:c' } },
    edges: {}, initials: { 'i:': 's:a', 'i:c': 's:c1' },
  };
  const placed = applyPositions(auto, { a: { x: 500, y: 60 }, c1: { x: 120, y: 50 } }).nodes;
  equal([placed['i:'].x, placed['i:'].y], [500 - 40 - 14, 60 + (40 - 14) / 2], 'left of a, at its middle');
  equal([placed['i:c'].x, placed['i:c'].y], [200 + 120 - 40 - 14, 10 + 50 + 13], 'in its composite, left of c1');
  const unplaced = applyPositions(auto, { c: { x: 300, y: 50 } }).nodes;
  equal([unplaced['i:'].x, unplaced['i:c'].x], [400, 300 + 180], 'the dots of states ELK placed stay where ELK put them');
  const edge = applyPositions(auto, { c1: { x: 20, y: 50 } }).nodes;
  equal(edge['i:c'].x, 200 + PAD.left, 'a state at its composite\'s left edge keeps the dot inside the box');
  const foreign = applyPositions({ ...auto, initials: { 'i:c': 's:a' } }, { a: { x: 500, y: 60 } }).nodes;
  equal([foreign['i:c'].x, foreign['i:c'].y, foreign['s:c'].w], [380, 100, 240], 'an initial outside its composite (SG002) moves no dot');
  const grown = applyPositions({ ...auto, initials: { 'i:': 's:c' } }, { c: { x: 300, y: 50 }, c1: { x: 20, y: 300 } }).nodes;
  equal(grown['i:'].y, 50 + (300 + 40 + PAD.bottom - 14) / 2, 'centred on the composite as it grew');
});

test('edgeRoute / labelSpot: a transition back between the same two is drawn beside the other, its label on its side', () => {
  const a = { x: 0, y: 0, w: 100, h: 40 };
  const b = { x: 300, y: 0, w: 100, h: 40 };
  const there = edgeRoute(null, a, b, true, true);
  const back = edgeRoute(null, b, a, true, true);
  equal([there.points, back.points], [[[100, 26], [300, 26]], [[300, 14], [100, 14]]], 'each 6 to the right of its way');
  const plain = edgeRoute(null, a, b, true);
  equal([plain.points, plain.side], [[[100, 20], [300, 20]], undefined], 'alone: through the middle');
  equal(labelSpot(plain, 80), [160, 14], 'alone: its label above the middle');
  const [, below] = labelSpot(there, 80);
  const [, above] = labelSpot(back, 80);
  assert(below - 11 > 26 && above + 4 < 14, `the labels clear both lines: box ${below - 11}..${below + 4} and ${above - 11}..${above + 4}`);
  const down = edgeRoute(null, a, { x: 0, y: 200, w: 100, h: 40 }, true, true);
  const [left] = labelSpot(down, 80);
  assert(left + 80 + 3 < down.points[0][0], `beside a vertical line, the label ends left of it: ${left + 83} ${down.points[0][0]}`);
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

test('stateFragment and fragmentLock: a state in a flow mapping on the line of another key is locked, not shown', () => {
  const text = 'stategraph: 1\nstates: {x: {transitions: [{target: y}]}, y: {type: final}}\n';
  equal(stateFragment(text, 2, 'x'), '', 'the line of states: shows no state');
  assert(fragmentLock(text, 2, 'x').includes('flow style'), 'x in states: {...} is not locked');
  const own = 'states:\n  x: {transitions: [{target: y}]}\n  "y": {type: final}\n';
  equal([stateFragment(own, 2, 'x'), stateFragment(own, 3, 'y')], ['{transitions: [{target: y}]}', '{type: final}'],
    'a flow value on its own key line (plain or quoted) is shown');
  equal([fragmentLock(own, 2, 'x'), fragmentLock(own, 3, 'y')], ['', ''], 'and not locked');
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

// ------------------------------------------------------------------ names and paths from the server

test('problemIndex and runOverlay: a state named like an Object property is a state like any other', () => {
  const graph = { initial: 'constructor', states: [
    { name: 'constructor', parent: null, type: 'state', composite: false, path: 'states.constructor', line: 5 },
    { name: 'toString', parent: null, type: 'state', composite: false, path: 'states.toString', line: 9 }],
  transitions: [] };
  const index = problemIndex(graph, [{ level: 'error', code: 'SG004', message: 'x', path: 'states.constructor.do', file: 'm.yaml', line: 6 }], 'm.yaml');
  equal(index.states.constructor.errors, 1, 'the problem is pinned to the state');
  assert(!('toString' in index.states), 'a state without problems has no entry, inherited or not');
  const overlay = runOverlay({ view: { frames: [{ prefix: '', config: [], visits: { constructor: 2 } }] } });
  equal(overlay.visits.constructor, 2, 'visits of the state');
  assert(!('toString' in overlay.visits), 'no visits inherited for another state');
});

test('problemIndex: a file named with backslashes (a server on Windows) is the root file', () => {
  const index = problemIndex(GRAPH, [{ level: 'error', code: 'SG004', message: 'x', path: 'states.write.do',
    file: 'E:\\machines\\review.yaml', line: 9 }], 'review.yaml');
  equal(index.states.write?.errors, 1, 'pinned to its state, not to the machine');
  equal(posixPath('a\\b\\c.yaml'), 'a/b/c.yaml', 'backslashes become slashes');
});

test('elkInput and gridLayout: a composite holding a state of its own name is laid out once, not without end', () => {
  const graph = { initial: 'a', states: [
    { name: 'a', parent: null, type: 'state', composite: true, initial: 'a' },
    { name: 'a', parent: 'a', type: 'state', composite: true, initial: 'b' },
    { name: 'b', parent: 'a', type: 'state', composite: false }],
  transitions: [] };
  const input = elkInput(graph);
  equal(input.children.map((n) => n.id), ['i:', 's:a'], 'one a at the top');
  equal(input.children[1].children.map((n) => n.id), ['i:a', 's:b'], 'the second a is left out');
  equal(Object.keys(gridLayout(graph).nodes).sort(), ['s:a', 's:b'], 'the grid places each name once');
});

test('ELK gives a composite the width its title needs', async () => {
  const graph = { initial: 'a_composite_with_a_rather_long_name', states: [
    { name: 'a_composite_with_a_rather_long_name', parent: null, type: 'state', composite: true, initial: 'x', icon: 'layers' },
    { name: 'x', parent: 'a_composite_with_a_rather_long_name', type: 'state', composite: false }],
  transitions: [] };
  const box = layoutFrom(await new ELK().layout(elkInput(graph))).nodes['s:a_composite_with_a_rather_long_name'];
  const needed = compositeTitleWidth(graph.states[0]);
  assert(needed > nodeSize(graph.states[1])[0] + PAD.left + PAD.right, 'fixture: the title is wider than the child');
  assert(box.w >= needed, `the box holds its title: ${box.w} < ${needed}`);
});

test('fragmentLock: a state that uses an alias is applied in its place in the file: no lock', () => {
  const file = [
    'states:', '  a:', '    do: *work', '  b:', '    <<: *base', '    transitions: [{target: a}]', '  c:',
    '    do: {agent: w, task: "a *bold* word"}', '    description: say it *twice* please', '  d:', '    items: [*x, *y]',
  ].join('\n');
  equal(['a', 'b', 'c', 'd'].map((name) => Boolean(fragmentLock(file, file.split('\n').indexOf(`  ${name}:`) + 1))),
    [false, false, false, false], 'set_state reads the text where it stands: an alias there resolves');
});

test('toggled / selectionOf / sameSelection: Ctrl or Shift+click adds a state or a transition or takes it out', () => {
  const state = (id) => ({ kind: 'state', id });
  const edge = (id) => ({ kind: 'transition', id });
  const two = toggled(state('a'), state('b'));
  equal(two, { kind: 'many', states: ['a', 'b'], transitions: [] }, 'a second state makes a selection of several');
  equal(toggled(two, state('a')), state('b'), 'taking one out of two leaves one state');
  equal(toggled(state('a'), state('a')), null, 'taking out the only one leaves none');
  const mixed = toggled(edge('a#0'), state('b'));
  equal(mixed, { kind: 'many', states: ['b'], transitions: ['a#0'] }, 'a state joins a selected transition');
  equal(toggled(mixed, state('b')), edge('a#0'), 'the transition left is selected alone');
  equal(toggled(state('a'), edge('a#0')), { kind: 'many', states: ['a'], transitions: ['a#0'] }, 'a transition joins a state');
  equal(toggled(toggled(two, edge('a#1')), edge('a#1')), two, 'a transition is taken out as it came in');
  assert(!sameSelection(two, selectionOf(['a', 'c'])), 'two selections of several differ by their states');
  assert(!sameSelection(selectionOf(['a'], ['b']), selectionOf(['a', 'b'])), '... and by what is a state, what a transition');
  assert(sameSelection(two, selectionOf(['a', 'b', 'a'])), 'the same states are the same selection');
  assert(sameSelection(null, null) && !sameSelection(state('a'), edge('a')), 'none is none; a state is no transition');
  equal([selectedStates(mixed), selectedTransitions(mixed), selectedTransitions(edge('x#0'))], [['b'], ['a#0'], ['x#0']],
    'what a selection holds');
});

test('groupedSpots: grouped states stay where they are drawn, inside the new composite\'s box', () => {
  const p = { x: 100, y: 100, w: 400, h: 300, parent: null };
  const a = { x: 150, y: 180, w: 80, h: 30, parent: 's:p' };
  const b = { x: 300, y: 200, w: 80, h: 30, parent: 's:p' };
  const spots = groupedSpots({ 's:p': p, 's:a': a, 's:b': b }, ['a', 'b'], 'g');
  equal(spots, { g: { x: 50 - PAD.left, y: 80 - PAD.top }, a: { x: PAD.left, y: PAD.top }, b: { x: 150 + PAD.left, y: 20 + PAD.top } },
    'relative to their parent p, the composite around them');
  // laid out anew (ELK puts them elsewhere): the positions draw them where they were
  const auto = { 's:p': p, 's:g': { x: 0, y: 0, w: 10, h: 10, parent: 's:p' },
    's:a': { ...a, x: 0, y: 0, parent: 's:g' }, 's:b': { ...b, x: 0, y: 0, parent: 's:g' } };
  const { nodes: drawn } = applyPositions({ nodes: auto }, spots);
  equal([drawn['s:a'].x, drawn['s:a'].y, drawn['s:b'].x, drawn['s:b'].y], [150, 180, 300, 200], 'drawn where they were');
});

test('statesWithin: only the states wholly inside the band', () => {
  const nodes = { 's:a': { x: 10, y: 10, w: 50, h: 30 }, 's:b': { x: 100, y: 10, w: 50, h: 30 }, 'i:': { x: 0, y: 0, w: 14, h: 14 } };
  equal(statesWithin(nodes, { x: 0, y: 0, w: 120, h: 60 }), ['a'], 'b reaches out of the band');
  equal(statesWithin(nodes, { x: 0, y: 0, w: 200, h: 60 }), ['a', 'b'], 'both inside; the initial dot is no state');
});

test('outermost: a state inside another selected one goes with it', () => {
  const parents = { read: 'review', verdict: 'review', review: null, deep: 'read' };
  equal(outermost(['read', 'review', 'write', 'deep'], (name) => parents[name] ?? null), ['review', 'write'],
    'read and deep lie inside review');
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
