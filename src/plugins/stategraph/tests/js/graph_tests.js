// The canvas's pure functions, in JavaScriptCore (no DOM):
//   jsc -m src/plugins/stategraph/tests/js/graph_tests.js
// Prints one PASS/FAIL line per test and "SUMMARY <passed>/<total>"; test_plugin_stategraph_js.py runs it.
// The layout test loads the vendored ELK the way the panel does (a classic script defining the global ELK).

import {
  applyPositions, clipToBox, compositeTitleWidth, dropInto, edgeRoute, edgeText, elkInput, gridLayout, layoutFrom, nodeSize, PAD,
  posixPath, problemIndex, runOverlay, fragmentLock, stateFragment, stateId, outermost, sameSelection, selectedStates,
  groupedSpots, labelSpot, lanes, lineKeys, orthogonalRoute, renamedLines, selectedTransitions, transitionRoute, selectionOf, statesWithin, toggled,
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

test('dropInto takes the innermost composite under the point, never the dragged state or one inside it', () => {
  const nodes = {
    's:outer': { x: 0, y: 0, w: 400, h: 400, parent: null },
    's:inner': { x: 50, y: 50, w: 100, h: 100, parent: 's:outer' },
    's:leaf': { x: 60, y: 60, w: 20, h: 20, parent: 's:inner' },
    's:a': { x: 500, y: 0, w: 40, h: 40, parent: null },
  };
  const composites = ['outer', 'inner'];
  equal(dropInto(nodes, composites, 'a', 60, 60), 'inner', 'the inner one, not the one around it');
  equal(dropInto(nodes, composites, 'a', 300, 300), 'outer', 'outside the inner one');
  equal(dropInto(nodes, composites, 'a', 520, 20), null, 'no composite there');
  equal(dropInto(nodes, composites, 'inner', 60, 60), null, 'not into itself, nor the one it sits in');
  equal(dropInto(nodes, composites, 'outer', 60, 60), null, 'not into one inside it');
  // its composite grew around it while it was dragged, over a bigger one: the bigger one takes it
  const grown = {
    's:p': { x: 0, y: 0, w: 260, h: 160, parent: null },
    's:kid': { x: 200, y: 100, w: 40, h: 40, parent: 's:p' },
    's:q': { x: 150, y: 50, w: 600, h: 600, parent: null },
  };
  equal(dropInto(grown, ['p', 'q'], 'kid', 220, 120), 'q', 'not the grown composite it sits in');
});

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
  const there = edgeRoute(null, a, b, true, { offset: 6 });
  const back = edgeRoute(null, b, a, true, { offset: 6 });
  equal([there.points, back.points], [[[100, 26], [300, 26]], [[300, 14], [100, 14]]], 'each 6 to the right of its way');
  const plain = edgeRoute(null, a, b, true);
  equal([plain.points, plain.side], [[[100, 20], [300, 20]], undefined], 'alone: through the middle');
  equal(labelSpot(plain, 80), [160, 14], 'alone: its label above the middle');
  const [, below] = labelSpot(there, 80);
  const [, above] = labelSpot(back, 80);
  assert(below - 11 > 26 && above + 4 < 14, `the labels clear both lines: box ${below - 11}..${below + 4} and ${above - 11}..${above + 4}`);
  const down = edgeRoute(null, a, { x: 0, y: 200, w: 100, h: 40 }, true, { offset: 6 });
  const [left] = labelSpot(down, 80);
  assert(left + 80 + 3 < down.points[0][0], `beside a vertical line, the label ends left of it: ${left + 83} ${down.points[0][0]}`);
  const other = edgeRoute(null, a, b, true, { offset: -6 });
  equal([other.points, other.side], [[[100, 14], [300, 14]], [-0, -1]], 'a negative offset: on its left, the label there too');
});

test('lanes: the transitions between two states, either way, side by side', () => {
  const t = (id, source, target) => ({ id, source, target });
  const offsets = (found) => Object.fromEntries(Object.entries(found).map(([id, lane]) => [id, lane.offset]));
  equal(offsets(lanes([t('a#0', 'a', 'b'), t('b#0', 'b', 'a')])), { 'a#0': 6, 'b#0': 6 }, 'there and back: each on its right');
  equal(offsets(lanes([t('x#0', 'x', 'y'), t('x#1', 'x', 'y')])), { 'x#0': 6, 'x#1': -6 }, 'twice the same way: one right, one left');
  const three = lanes([t('y#0', 'y', 'x'), t('y#1', 'y', 'x'), t('x#0', 'x', 'y')]);
  equal(three, { 'y#0': { offset: -20, at: 0.75, crowd: true, spread: 20 }, 'y#1': { offset: 0, at: 0.5, crowd: true, spread: 20 },
    'x#0': { offset: -20, at: 0.75, crowd: true, spread: 20 } },
    'three: 20 apart, each label at its own place along the way (y to x counts from x); the widest offset is their spread');
  equal(offsets(lanes([t('a#0', 'a', 'b'), t('a#1', 'a', 'a'), t('a#2', 'a', null), t('c#0', 'c', 'a')])), { 'a#0': 0, 'c#0': 0 },
    'alone: through the middle; a self-transition and an internal one are no pair');
});

test('orthogonalRoute: out of the facing side, one bend half way, in through the side facing back', () => {
  const a = { x: 0, y: 0, w: 100, h: 40 };
  equal(orthogonalRoute(a, { x: 300, y: 200, w: 100, h: 40 }).points, [[100, 20], [200, 20], [200, 220], [300, 220]], 'a Z sideways');
  equal(orthogonalRoute(a, { x: 300, y: 0, w: 100, h: 40 }).points, [[100, 20], [300, 20]], 'level: a straight line');
  equal(orthogonalRoute(a, { x: 60, y: 300, w: 100, h: 40 }).points, [[50, 40], [50, 170], [110, 170], [110, 300]], 'a Z downwards');
  // mostly sideways, but the sides overlap: down and in from above instead
  equal(orthogonalRoute({ x: 0, y: 0, w: 400, h: 40 }, { x: 300, y: 100, w: 300, h: 40 }).points,
    [[200, 40], [200, 70], [450, 70], [450, 100]], 'no room sideways: along the other axis');
  equal(orthogonalRoute(a, { x: 50, y: 10, w: 100, h: 40 }), null, 'overlapping boxes: no right angle fits');
  const near = { x: 140, y: 60, w: 100, h: 40 };  // 40 between the facing sides: two runs of 16 fit, lanes' do not
  equal(orthogonalRoute(a, near).points.length, 4, 'a Z: a run of 16 out of and into the boxes');
  const close = orthogonalRoute(a, { x: 120, y: 60, w: 100, h: 40 });  // 20 between the facing sides: no Z
  equal([close.points, close.span], [[[100, 20], [170, 20], [170, 60]], [[100, 20], [170, 20]]],
    'an L instead: out of the side, in from above, labelled on its level leg');
  equal(labelSpot(close, 20), [125, 14], 'above the middle of the level leg');
  const tall = orthogonalRoute(a, { x: 110, y: 50, w: 40, h: 200 });
  equal([tall.points, tall.span], [[[50, 40], [50, 150], [110, 150]], [[50, 150], [110, 150]]], 'mostly down: down first, then in from the side');
  const lane = orthogonalRoute(a, near, { offset: 6 });
  equal([lane.points, lane.side], [[[100, 26], [184, 26], [184, 60]], [0, 1]], 'a lane needs its offset besides for a Z: an L right of its way');
  equal(orthogonalRoute(a, { x: 102, y: 60, w: 20, h: 30 }).points, [[50, 40], [50, 75], [102, 75]], 'a first leg shorter than a run: the other L');
  equal(orthogonalRoute(a, { x: 110, y: 30, w: 40, h: 60 }).points, [[50, 40], [50, 60], [110, 60]], 'a second leg shorter than a run: the other L');
  const left = orthogonalRoute(a, { x: -50, y: 50, w: 40, h: 200 }, { offset: 6 });
  equal([left.points, left.side], [[[44, 40], [44, 144], [-10, 144]], [0, -1]], 'down, then left: right of a way west is up');
  const up = orthogonalRoute(a, { x: 120, y: -60, w: 100, h: 40 }, { offset: 6 });
  equal([up.points, up.side], [[[100, 26], [176, 26], [176, -20]], [0, 1]], 'right, then up: the label right of the way east');
  const facing = { x: 20, y: 70, w: 100, h: 40 };  // 30 below, offset: room for neither a bend nor an L's legs
  equal(orthogonalRoute(a, facing).points, [[60, 40], [60, 70]], 'straight down in the middle of where they face each other');
  const beside = orthogonalRoute(a, facing, { offset: 6 });
  equal([beside.points, beside.side], [[[54, 40], [54, 70]], [-1, 0]], 'a lane of it: right of its way');
  equal(orthogonalRoute(a, { x: 105, y: 45, w: 10, h: 10 }), null, 'all but meeting at a corner: no right angle fits');
});

test('a straight line between a composite and a state inside it goes to the nearest border, not through the state', () => {
  const box = { x: 0, y: 0, w: 300, h: 200 };
  const kid = { x: 150, y: 150, w: 96, h: 34 };  // nearest the bottom; the centre-to-centre line leaves at the right
  equal(transitionRoute('straight', null, kid, box, true, null).points, [[198, 184], [198, 200]], 'down to the bottom');
  equal(transitionRoute('straight', null, box, kid, true, null).points, [[198, 200], [198, 184]], 'and back up');
  const other = { x: 400, y: 0, w: 96, h: 34 };
  equal(transitionRoute('straight', null, kid, other, true, null).points, edgeRoute(null, kid, other, true).points,
    'between two states side by side: centre to centre as before');
  const filling = { x: 2, y: 2, w: 96, h: 36 };
  equal(transitionRoute('straight', null, filling, { x: 0, y: 0, w: 100, h: 40 }, true, null).points,
    edgeRoute(null, filling, { x: 0, y: 0, w: 100, h: 40 }, true).points, 'no room for a line to a border: as before');
});

test('orthogonalRoute: a group of lanes takes one way, squeezed where it must; a composite and a state inside it', () => {
  const t = (id, source, target) => ({ id, source, target });
  const a = { x: 0, y: 0, w: 96, h: 34 };
  const pair = lanes([t('a#0', 'a', 'b'), t('b#0', 'b', 'a')]);
  const close = { x: 136, y: -26, w: 96, h: 34 };  // 40 apart: a Z fits one line, not two 12 apart
  const [there, back] = [orthogonalRoute(a, close, pair['a#0']), orthogonalRoute(close, a, pair['b#0'])];
  equal([there.points, back.points], [[[96, 20.6], [119.6, 20.6], [119.6, -5.4], [136, -5.4]],
    [[136, -12.6], [112.4, -12.6], [112.4, 13.4], [96, 13.4]]], 'both a Z, 7.2 apart instead of 12: as little closer as it takes');
  const three = lanes([t('a#0', 'a', 'b'), t('a#1', 'a', 'b'), t('a#2', 'a', 'b')]);
  const near = { x: 146, y: 10, w: 96, h: 34 };  // 50 apart: the middle one alone would bend, the outer ones not
  equal(['a#0', 'a#1', 'a#2'].map((id) => orthogonalRoute(a, near, three[id]).points),
    [[[96, 25], [113, 25], [113, 35], [146, 35]], [[96, 17], [121, 17], [121, 27], [146, 27]], [[96, 9], [129, 9], [129, 19], [146, 19]]],
    'all three bend, 8 apart: each starts and ends on its box');
  equal(orthogonalRoute(a, { x: 101, y: 60, w: 40, h: 34 }, three['a#1']).points, [[48, 34], [48, 77], [101, 77]],
    'the middle one alone would go right first: the outer ones\' legs would be too short that way, so all go down first');
  const far = orthogonalRoute(a, { x: 400, y: 200, w: 96, h: 34 }, three['a#0']);
  equal([far.points, labelSpot(far, 40)], [[[28, 34], [28, 137], [428, 137], [428, 200]], [108, 131]],
    'three 20 apart fit the boxes\' width, not their height: down first, the label above the level middle, a quarter along');
  const beside = ['a#0', 'a#2'].map((id) => orthogonalRoute(a, { x: 400, y: 40, w: 96, h: 34 }, three[id]));
  equal(beside.map((drawn) => [drawn.points[0][1], labelSpot(drawn, 60)]), [[33, [100, 27]], [1, [192, -5]]],
    'sideways, 16 apart: each label above its own first leg, clear of the others');
  const onSide = ([x, y], b) => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h
    && (x === b.x || x === b.x + b.w || y === b.y || y === b.y + b.h);
  const tall = { x: 0, y: 0, w: 96, h: 80 };
  for (const [from, to] of [[tall, { x: 400, y: 100, w: 96, h: 34 }], [a, { x: 400, y: 100, w: 96, h: 80 }],  // Zs
    [tall, { x: 130, y: 120, w: 30, h: 34 }], [{ x: 0, y: 0, w: 96, h: 30 }, { x: 150, y: 60, w: 96, h: 80 }]]) {  // Ls
    for (const id of ['a#0', 'a#1', 'a#2']) {
      const drawn = orthogonalRoute(from, to, three[id]);
      assert(drawn && onSide(drawn.points[0], from) && onSide(drawn.points[drawn.points.length - 1], to),
        `${JSON.stringify([from, to])} ${id}: a lane starts or ends beside its box: ${JSON.stringify(drawn?.points)}`);
    }
  }
  const box = { x: 0, y: 0, w: 300, h: 200 };
  equal([orthogonalRoute({ x: 40, y: 60, w: 96, h: 34 }, box).points, orthogonalRoute(box, { x: 40, y: 60, w: 96, h: 34 }).points],
    [[[40, 77], [0, 77]], [[0, 77], [40, 77]]], 'to and from the composite\'s nearest border: left');
  equal(orthogonalRoute({ x: 150, y: 150, w: 96, h: 34 }, box).points, [[198, 184], [198, 200]], 'nearest the bottom: down');
  equal([orthogonalRoute({ x: 0, y: 60, w: 96, h: 34 }, box).points, orthogonalRoute({ x: 4, y: 60, w: 96, h: 34 }, box).points],
    [[[48, 94], [48, 200]], [[52, 94], [52, 200]]], 'against the left border, or 4 from it: no line there, to the next nearest');
  equal(orthogonalRoute({ x: 100, y: 34, w: 96, h: 34 }, box).points, [[100, 51], [0, 51]],
    'nearest the top: not through the composite\'s name, to the next nearest');
  equal(orthogonalRoute({ x: 14, y: 60, w: 96, h: 34 }, box).points, [[14, 77], [0, 77]], 'the padding ELK leaves (14) is room enough');
  equal(orthogonalRoute({ x: 2, y: 2, w: 96, h: 36 }, { x: 0, y: 0, w: 100, h: 40 }), null, 'filling its composite: no room for a line');
  const kid = { x: 40, y: 60, w: 96, h: 34 };
  const inside = lanes([t('kid#0', 'kid', 'box'), t('box#0', 'box', 'kid')]);
  equal([orthogonalRoute(kid, box, inside['kid#0']).points, orthogonalRoute(box, kid, inside['box#0']).points],
    [[[40, 83], [0, 83]], [[0, 71], [40, 71]]], 'there and back: side by side, each left of its way (offset -6)');
  const crowded = lanes([t('kid#0', 'kid', 'box'), t('kid#1', 'kid', 'box'), t('kid#2', 'kid', 'box')]);
  equal(['kid#0', 'kid#2'].map((id) => orthogonalRoute(kid, box, crowded[id]).points[0][1]), [93, 61],
    'three 20 apart would leave the state (34 high): 16 apart');
});

test('orthogonalRoute: transitions between the same two states on lanes neither cover nor cross each other', () => {
  const t = (id, source, target) => ({ id, source, target });
  const segments = (points) => points.slice(1).map((p, i) => [points[i], p]);
  const touch = ([[x1, y1], [x2, y2]], [[x3, y3], [x4, y4]]) => {  // axis-aligned segments: do they share a point?
    const [ax, bx, ay, by] = [Math.min(x1, x2), Math.max(x1, x2), Math.min(y1, y2), Math.max(y1, y2)];
    const [cx, dx, cy, dy] = [Math.min(x3, x4), Math.max(x3, x4), Math.min(y3, y4), Math.max(y3, y4)];
    return ax <= dx && cx <= bx && ay <= dy && cy <= by;
  };
  const a = { x: 0, y: 0, w: 100, h: 40 };
  for (const b of [{ x: 400, y: 200, w: 100, h: 40 }, { x: 400, y: -200, w: 100, h: 40 }, { x: 200, y: 400, w: 100, h: 40 },
    { x: -300, y: 400, w: 100, h: 40 }, { x: 400, y: 0, w: 100, h: 40 },
    { x: 130, y: 70, w: 100, h: 40 }, { x: -130, y: -70, w: 100, h: 40 }, { x: 110, y: 50, w: 40, h: 200 },  // Ls
    { x: 130, y: -70, w: 100, h: 40 }]) {  // an L up
    for (const group of [[t('a#0', 'a', 'b'), t('b#0', 'b', 'a')], [t('a#0', 'a', 'b'), t('a#1', 'a', 'b')],
      [t('a#0', 'a', 'b'), t('b#0', 'b', 'a'), t('a#1', 'a', 'b')]]) {
      const found = lanes(group);
      const drawn = group.map((one) => (one.source === 'a' ? orthogonalRoute(a, b, found[one.id]) : orthogonalRoute(b, a, found[one.id])));
      const where = `${JSON.stringify(b)} ${group.map((one) => one.id)}`;
      const boxes = drawn.map((route) => {  // the label's box, as drawEdge makes it (text of 120)
        const [x, y] = labelSpot(route, 120);
        return [[x - 3, y - 11], [x + 123, y + 4]];
      });
      drawn.forEach((p, i) => drawn.forEach((q, j) => {
        if (i >= j) return;
        const hits = segments(p.points).flatMap((s) => segments(q.points).filter((r) => touch(s, r)));
        assert(!hits.length, `${where}: ${JSON.stringify(p.points)} meets ${JSON.stringify(q.points)}`);
        assert(!touch(boxes[i], boxes[j]), `${where}: labels ${i} and ${j} meet`);
      }));
      // two: each label clear of the other's line (a crowd's may cross its neighbours', as straight). ponytail: not an
      // L's -- where no Z fits, a label of 120 is longer than its leg, and one beside the inner L lies on the outer
      if (group.length === 2 && !drawn.some((route) => route.points.length === 3)) {
        drawn.forEach((p, i) => segments(drawn[1 - i].points).forEach((s) => assert(!touch(boxes[i], s),
          `${where}: label ${i} covers the other line at ${JSON.stringify(s)}`)));
      }
    }
  }
});

test('transitionRoute: moving a state keeps the style -- right-angled is ELK\'s route, then ours; straight stays straight', () => {
  const a = { x: 0, y: 0, w: 100, h: 40 };
  const b = { x: 300, y: 200, w: 100, h: 40 };
  const elk = { points: [[100, 20], [150, 20], [150, 220], [300, 220]], label: null };
  const kinds = (style, moved, to = b, route = elk) => {
    const drawn = transitionRoute(style, route, a, to, moved, undefined);
    return drawn.points === elk.points ? 'elk' : drawn.points.length === 2 ? 'straight' : drawn.points.length === 4 && to === a ? 'loop' : 'square';
  };
  equal(['orthogonal', 'straight', undefined].map((style) => kinds(style, false)), ['elk', 'straight', 'elk'], 'where ELK put them');
  equal(['orthogonal', 'straight', undefined].map((style) => kinds(style, true)), ['square', 'straight', 'square'], 'moved');
  equal(kinds('straight', false, a), 'loop', 'a straight self-transition keeps its loop too');
  equal(kinds('orthogonal', false, b, null), 'square', 'no route of ELK (its layout failed): right-angled all the same');
  equal(kinds('orthogonal', true, { x: 50, y: 10, w: 100, h: 40 }), 'straight', 'overlapping: straight');
  equal(kinds('orthogonal', true, a), 'loop', 'a self-transition keeps its loop');
});

test('lineKeys / renamedLines: a line style is kept by the way it goes, and follows a renamed state', () => {
  const t = (id, source, target) => ({ id, source, target });
  equal(lineKeys([t('a#0', 'a', 'b'), t('a#1', 'a', 'c'), t('a#2', 'a', 'b'), t('a#3', 'a', null)]),
    { 'a#0': 'a→b', 'a#1': 'a→c', 'a#2': 'a→b' }, 'the same way, the same style; an internal transition has none');
  equal(renamedLines({ 'a→b': 'straight', 'b→a': 'orthogonal', 'ab→c': 'straight' }, 'a', 'x'),
    { 'ab→c': 'straight', 'x→b': 'straight', 'b→x': 'orthogonal' }, 'both ends; a longer name untouched');
  equal(renamedLines({ 'a→b': 'orthogonal', 'z→b': 'straight' }, 'a', 'z'), { 'z→b': 'orthogonal' },
    'the renamed state\'s own style wins over one left from a removed state of its new name');
});

test('lanes / edgeRoute / labelSpot: of three or four between two states, no label covers another or another\'s line', () => {
  const t = (id, source, target) => ({ id, source, target });
  const width = 180;
  const boxOf = (drawn) => {  // the label's box: 3 beyond the text, 11 above its baseline, 4 below
    const [x, y] = labelSpot(drawn, width);
    return { x: x - 3, y: y - 11, w: width + 6, h: 15 };
  };
  const meets = (p, q) => p.x < q.x + q.w && q.x < p.x + p.w && p.y < q.y + q.h && q.y < p.y + p.h;
  const crosses = (box, [[x1, y1], [x2, y2]]) => {  // a straight line through a box: sampled finely
    for (let s = 0; s <= 400; s += 1) {
      const [x, y] = [x1 + ((x2 - x1) * s) / 400, y1 + ((y2 - y1) * s) / 400];
      if (x > box.x && x < box.x + box.w && y > box.y && y < box.y + box.h) return true;
    }
    return false;
  };
  const a = { x: 0, y: 0, w: 100, h: 40 };
  for (const [where, b] of [['beside', { x: 700, y: 0, w: 100, h: 40 }], ['below', { x: 0, y: 500, w: 100, h: 40 }]]) {
    for (const group of [[t('a#0', 'a', 'b'), t('a#1', 'a', 'b'), t('b#0', 'b', 'a')],
      [t('a#0', 'a', 'b'), t('b#0', 'b', 'a'), t('a#1', 'a', 'b'), t('b#1', 'b', 'a')]]) {
      const found = lanes(group);
      const drawn = group.map((one) => (one.source === 'a' ? edgeRoute(null, a, b, true, found[one.id]) : edgeRoute(null, b, a, true, found[one.id])));
      const boxes = drawn.map(boxOf);
      boxes.forEach((box, i) => boxes.forEach((other, j) => {
        assert(i >= j || !meets(box, other), `${where}, ${group.length}: labels ${i} and ${j} meet`);
        if (where === 'beside') assert(i === j || !crosses(box, drawn[j].points), `${where}, ${group.length}: label ${i} covers line ${j}`);
      }));
    }
  }
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
