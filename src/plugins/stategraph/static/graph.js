// State Graph canvas: a machine as SVG, laid out by ELK (static/vendor/elkjs, loaded before this module as the
// global ELK), with pan, zoom, selection, drag-to-connect, manual positions and the run overlay.
//
// The pure functions (ELK input, positions, labels, problem pinning, run overlay, fragments) are exported for the
// tests in tests/js, which run in JavaScriptCore without a DOM. The Canvas class is the only part that touches the
// document; it builds every element with createElementNS and textContent, never markup from data.

const SVG_NS = 'http://www.w3.org/2000/svg';
const SPRITE = '/static/kit/icons.svg';

/** A composite's inner padding: its title sits in the top band. */
export const PAD = { top: 34, left: 14, bottom: 14, right: 14 };
/** Pseudostates are small shapes of a fixed size. */
const SHAPES = { choice: [30, 30], junction: [14, 14], final: [26, 26] };
const INITIAL_SIZE = 14;
/** Between an initial dot and the state placed by hand it points to. */
const INITIAL_GAP = 40;
/** How far a straight transition is drawn beside the centre line when another goes back between the same two. */
const PAIR_GAP = 6;

export const ROOT_OPTIONS = {
  'elk.algorithm': 'layered',
  'elk.direction': 'RIGHT',
  'elk.hierarchyHandling': 'INCLUDE_CHILDREN',
  'elk.json.shapeCoords': 'ROOT',
  'elk.json.edgeCoords': 'ROOT',
  'elk.edgeRouting': 'ORTHOGONAL',
  'elk.layered.spacing.nodeNodeBetweenLayers': '56',
  'elk.spacing.nodeNode': '28',
  'elk.spacing.edgeLabel': '4',
  'elk.padding': '[top=24,left=24,bottom=24,right=24]',
};

export const stateId = (name) => `s:${name}`;
export const initialId = (region) => `i:${region || ''}`;
export const edgeId = (transitionIdValue) => `t:${transitionIdValue}`;
const initialEdgeId = (region) => `ie:${region || ''}`;

const clamp = (value, low, high) => Math.min(Math.max(value, low), high);

/** Rough rendered width of a text: good enough to size boxes before the browser has drawn anything. */
export function textWidth(text, px, mono = false) {
  return Math.ceil(String(text ?? '').length * px * (mono ? 0.62 : 0.56));
}

export function shorten(text, max) {
  const value = String(text ?? '');
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

/** UML transition label: `trigger [guard] / effect`; the completion trigger is left out. */
export function edgeText(transition) {
  const parts = [];
  if (transition.trigger && transition.trigger !== 'done') parts.push(transition.trigger);
  if (transition.guard) parts.push(`[${shorten(transition.guard, 36)}]`);
  if (transition.effect) {
    const first = transition.effect.split('\n')[0];
    parts.push(`/ ${shorten(first, 24)}${transition.effect.includes('\n') && first.length <= 24 ? ' …' : ''}`);
  }
  return parts.join(' ');
}

/** The width a composite's title band needs: icon, name (as drawState draws it, at most 30 characters), handle. */
export function compositeTitleWidth(state) {
  return textWidth(shorten(state.name, 30), 13) + (state.icon ? 44 : 24) + 8;
}

/** [width, height] of a state that is drawn as one box or shape. */
export function nodeSize(state) {
  if (SHAPES[state.type]) return SHAPES[state.type];
  const name = textWidth(state.name, 13) + (state.icon ? 40 : 24);
  const label = state.label ? textWidth(shorten(state.label, 34), 11, true) + 24 : 0;
  return [clamp(Math.max(name, label, 96), 96, 280), state.label ? 50 : 34];
}

/** The states by parent name, in document order. A name seen before is left out: the validator reports it, and a
 * composite holding a state of its own name would otherwise be laid out inside itself without end. */
function childrenOf(graph) {
  const byParent = new Map();
  const seen = new Set();
  for (const state of graph.states || []) {
    if (seen.has(state.name)) continue;
    seen.add(state.name);
    const key = state.parent || '';
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(state);
  }
  return byParent;
}

/** The ELK graph: composites as compound nodes, an initial dot per region, transitions with their labels. */
export function elkInput(graph) {
  const byParent = childrenOf(graph);
  const known = new Set((graph.states || []).map((state) => state.name));
  const edges = [];
  const region = (parent, initial) => {
    const nodes = (byParent.get(parent) || []).map(node);
    if (initial && known.has(initial)) {
      nodes.unshift({ id: initialId(parent), width: INITIAL_SIZE, height: INITIAL_SIZE });
      edges.push({ id: initialEdgeId(parent), sources: [initialId(parent)], targets: [stateId(initial)] });
    }
    return nodes;
  };
  const node = (state) => {
    if (state.composite && byParent.has(state.name)) {
      return {
        id: stateId(state.name),
        layoutOptions: {
          'elk.padding': `[top=${PAD.top},left=${PAD.left},bottom=${PAD.bottom},right=${PAD.right}]`,
          // ELK sizes a composite from its children: the title band needs room of its own
          'elk.nodeSize.constraints': 'MINIMUM_SIZE',
          'elk.nodeSize.minimum': `(${compositeTitleWidth(state)}, ${PAD.top + PAD.bottom})`,
        },
        children: region(state.name, state.initial),
      };
    }
    const [width, height] = nodeSize(state);
    return { id: stateId(state.name), width, height };
  };
  const children = region('', graph.initial);
  for (const transition of graph.transitions || []) {
    if (!transition.target || !known.has(transition.target) || !known.has(transition.source)) continue;
    const text = edgeText(transition);
    edges.push({
      id: edgeId(transition.id),
      sources: [stateId(transition.source)],
      targets: [stateId(transition.target)],
      labels: text ? [{ text, width: textWidth(text, 11, true) + 8, height: 16 }] : [],
    });
  }
  return { id: 'root', layoutOptions: { ...ROOT_OPTIONS }, children, edges };
}

/** ELK's answer as flat boxes (absolute, with their parent), edge routes, and the state each initial dot points to. */
export function layoutFrom(out) {
  const nodes = {};
  const edges = {};
  const initials = {};
  const walk = (parent, owner) => {
    for (const child of parent.children || []) {
      nodes[child.id] = { x: child.x, y: child.y, w: child.width, h: child.height, parent: owner };
      walk(child, child.id);
    }
  };
  walk(out, null);
  for (const edge of out.edges || []) {
    const section = (edge.sections || [])[0];
    const points = section
      ? [section.startPoint, ...(section.bendPoints || []), section.endPoint].map((p) => [p.x, p.y]) : [];
    const label = (edge.labels || [])[0];
    edges[edge.id] = {
      points,
      label: label && Number.isFinite(label.x) ? { x: label.x, y: label.y, w: label.width, h: label.height } : null,
    };
    if (edge.id.startsWith('ie:') && edge.sources?.[0] && edge.targets?.[0]) initials[edge.sources[0]] = edge.targets[0];
  }
  return { nodes, edges, initials };
}

/**
 * Stored positions over the automatic layout. positions: {state name: {x, y}}, relative to the parent's box (top
 * level: absolute). A composite grows to hold its children; a child stays inside its parent's content area; a
 * region's initial dot sits left of its initial state once that one is placed. Returns the boxes and the ids of
 * those that differ from ELK's.
 */
export function applyPositions(layout, positions) {
  const auto = layout.nodes;
  const ids = Object.keys(auto);  // pre-order: a parent before its children
  const relative = {};
  for (const id of ids) {
    const parent = auto[id].parent ? auto[auto[id].parent] : null;
    relative[id] = { x: auto[id].x - (parent ? parent.x : 0), y: auto[id].y - (parent ? parent.y : 0) };
  }
  const placed = new Set();
  for (const [name, spot] of Object.entries(positions || {})) {
    const id = stateId(name);
    if (!relative[id] || !Number.isFinite(spot?.x) || !Number.isFinite(spot?.y)) continue;
    const nested = Boolean(auto[id].parent);
    relative[id] = { x: nested ? Math.max(spot.x, PAD.left) : spot.x, y: nested ? Math.max(spot.y, PAD.top) : spot.y };
    placed.add(id);
  }
  const nodes = {};
  for (const id of ids) {
    const parent = auto[id].parent ? nodes[auto[id].parent] : null;
    nodes[id] = { ...auto[id], x: (parent ? parent.x : 0) + relative[id].x, y: (parent ? parent.y : 0) + relative[id].y };
  }
  for (const id of [...ids].reverse()) {  // children before their parent
    const kids = ids.filter((other) => nodes[other].parent === id);
    if (!kids.length) continue;
    const box = nodes[id];
    box.w = Math.max(box.w, Math.max(...kids.map((k) => nodes[k].x + nodes[k].w)) + PAD.right - box.x);
    box.h = Math.max(box.h, Math.max(...kids.map((k) => nodes[k].y + nodes[k].h)) + PAD.bottom - box.y);
  }
  // an initial state placed by hand takes its dot along: where ELK put it would be anywhere in the region now. Set
  // after the growth, the dot is centred on the box as drawn and lies inside its parent without growing it (left of
  // a state the parent holds, clear of its padding). An initial outside its composite (SG002) moves no dot.
  for (const [dot, target] of Object.entries(layout.initials || {})) {
    if (!nodes[dot] || !placed.has(target) || auto[dot].parent !== auto[target].parent) continue;
    const parent = auto[dot].parent ? nodes[auto[dot].parent] : null;
    const box = nodes[target];
    nodes[dot].x = Math.max(parent ? parent.x + PAD.left : -Infinity, box.x - INITIAL_GAP - nodes[dot].w);
    nodes[dot].y = box.y + (box.h - nodes[dot].h) / 2;
  }
  const moved = new Set(ids.filter((id) => ['x', 'y', 'w', 'h'].some((k) => Math.abs(nodes[id][k] - auto[id][k]) > 0.5)));
  return { nodes, moved };
}

/** The selection of these state names and transition ids: null, one state {kind: 'state', id}, one transition
 * {kind: 'transition', id}, or several of either {kind: 'many', states, transitions}. */
export function selectionOf(states = [], transitions = []) {
  const names = [...new Set(states)];
  const ids = [...new Set(transitions)];
  if (names.length + ids.length > 1) return { kind: 'many', states: names, transitions: ids };
  if (names.length) return { kind: 'state', id: names[0] };
  return ids.length ? { kind: 'transition', id: ids[0] } : null;
}

export function selectedStates(selection) {
  if (selection?.kind === 'state') return [selection.id];
  return selection?.kind === 'many' ? [...selection.states] : [];
}

export function selectedTransitions(selection) {
  if (selection?.kind === 'transition') return [selection.id];
  return selection?.kind === 'many' ? [...selection.transitions] : [];
}

export function sameSelection(a, b) {
  const key = (s) => (s ? [s.kind, ...selectedStates(s), '', ...selectedTransitions(s)].join('\n') : '');
  return key(a) === key(b);
}

/** Ctrl/Shift+click: the selection with `item` ({kind: 'state' | 'transition', id}) added, or taken out when it was
 * in it. */
export function toggled(selection, item) {
  const flip = (ids) => (ids.includes(item.id) ? ids.filter((id) => id !== item.id) : [...ids, item.id]);
  const states = selectedStates(selection);
  const transitions = selectedTransitions(selection);
  return item.kind === 'state' ? selectionOf(flip(states), transitions) : selectionOf(states, flip(transitions));
}

/** The states whose box lies wholly inside `band` (a rubber band, canvas units). */
export function statesWithin(nodes, band) {
  return Object.entries(nodes).filter(([id, box]) => id.startsWith('s:') && box.x >= band.x && box.y >= band.y
    && box.x + box.w <= band.x + band.w && box.y + box.h <= band.y + band.h).map(([id]) => id.slice(2));
}

/** The names without those inside another of them (`parentOf(name)`: its composite's name): moving or removing a
 * composite takes the states inside it along. */
export function outermost(names, parentOf) {
  const chosen = new Set(names);
  return names.filter((name) => {
    for (let parent = parentOf(name); parent; parent = parentOf(parent)) if (chosen.has(parent)) return false;
    return true;
  });
}

/** The point where the line from the centre of box towards (tx, ty) leaves the box. */
export function clipToBox(box, tx, ty) {
  const cx = box.x + box.w / 2;
  const cy = box.y + box.h / 2;
  const dx = tx - cx;
  const dy = ty - cy;
  if (!dx && !dy) return [cx, cy];
  const scale = Math.min(dx ? (box.w / 2) / Math.abs(dx) : Infinity, dy ? (box.h / 2) / Math.abs(dy) : Infinity);
  return [cx + dx * scale, cy + dy * scale];
}

/** A drawn edge: ELK's route, or a straight line when an end was moved by hand. `paired`: a transition goes back
 * between the two as well -- each straight line is drawn beside the centre line, on its right (`side`), so the two
 * do not cover each other. */
export function edgeRoute(route, source, target, moved, paired = false) {
  if (route && route.points.length && !moved) return { points: route.points, label: route.label, straight: false };
  if (source === target) {  // a self-transition: a loop over the top right corner
    const x = source.x + source.w * 0.75;
    const y = source.y;
    return { points: [[x - 12, y], [x - 12, y - 18], [x + 12, y - 18], [x + 12, y]], label: null, straight: true };
  }
  const from = clipToBox(source, target.x + target.w / 2, target.y + target.h / 2);
  const to = clipToBox(target, source.x + source.w / 2, source.y + source.h / 2);
  if (!paired) return { points: [from, to], label: null, straight: true };
  const length = Math.hypot(to[0] - from[0], to[1] - from[1]) || 1;
  const side = [-(to[1] - from[1]) / length, (to[0] - from[0]) / length];
  const shift = ([x, y]) => [x + side[0] * PAIR_GAP, y + side[1] * PAIR_GAP];
  return { points: [shift(from), shift(to)], label: null, straight: true, side };
}

/** Where an edge's label text starts ([x, baseline]; its box reaches 3 beyond, 11 above and 4 below): ELK's spot,
 * above the middle of a straight line, or beside it on its `side` -- clear of the line going back. */
export function labelSpot(drawn, width) {
  if (drawn.label) return [drawn.label.x + 4, drawn.label.y + 12];
  const [a, b] = [drawn.points[0], drawn.points[drawn.points.length - 1]];
  const [mx, my] = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  if (!drawn.side) return [mx - width / 2, my - 6];
  const [sx, sy] = drawn.side;
  const reach = Math.abs(sx) * (width / 2 + 3) + Math.abs(sy) * 7.5 + 4;  // half the box across the line, and a gap
  return [mx + sx * reach - width / 2, my + sy * reach + 3.5];
}

export function pathData(points) {
  return points.map(([x, y], i) => `${i ? 'L' : 'M'}${Math.round(x * 10) / 10} ${Math.round(y * 10) / 10}`).join(' ');
}

/**
 * Run `draw`, a redraw of `element`, keeping what the viewer chose or typed meanwhile: redrawn options reset a select
 * to its first one, and a redrawn input is empty -- a poll that changes one label is enough. The controls are
 * `element` itself (a select whose options are redrawn) or the selects and inputs with an id inside it; a value the
 * new options no longer offer is not forced back. Returns what `draw` returns.
 */
export function keepingChoices(element, draw) {
  const controls = () => (element.tagName === 'SELECT' ? [element] : [...element.querySelectorAll('select[id], input[id]')]);
  const kept = controls().map((c) => ({ id: c.id, value: c.value, checked: c.checked, focused: document.activeElement === c }));
  const drawn = draw();
  if (!drawn) return drawn;
  const now = new Map(controls().map((c) => [c.id, c]));
  for (const { id, value, checked, focused } of kept) {
    const control = now.get(id);
    if (!control) continue;
    if (control.type === 'checkbox') control.checked = checked;
    else if (control.tagName !== 'SELECT' || [...control.options].some((o) => o.value === value)) control.value = value;
    if (focused) control.focus();
  }
  return drawn;
}

/** What a form shows: {name: data-orig} of its controls that have one. */
const shownBy = (form) => Object.fromEntries([...form.elements].filter((c) => c.name && c.dataset.orig !== undefined)
  .map((c) => [c.name, c.dataset.orig]));

/**
 * What was typed into the forms under `root` that `keep` names (`keyOf(form)`: its key) and is not applied: every
 * control with a data-orig (what it showed) whose value differs from it, and what the whole form showed --
 * {key: {controls: [{name, value}], shown}}. `putTyped` gives it back after a redraw.
 */
export function typedIn(root, keep, keyOf) {
  const typed = new Map();
  for (const form of root.querySelectorAll('form')) {
    const key = keyOf(form);
    if (!key || !keep.has(key)) continue;
    const controls = [...form.elements].filter((c) => c.name && c.dataset.orig !== undefined && c.value !== c.dataset.orig)
      .map((c) => ({ name: c.name, value: c.value }));
    if (controls.length) typed.set(key, { controls, shown: shownBy(form) });
  }
  return typed;
}

/**
 * Give what `typedIn` kept back to the redrawn forms: a form gets its typed values when it shows all it showed
 * then -- one the edit changed underneath (a field of it, or another thing now under its key: a transition moved
 * or removed) or that is gone keeps what is drawn. Returns the keys of the forms that got their text back, and
 * "key: name" of each typed control that did not.
 */
export function putTyped(root, typed, keyOf) {
  const restored = new Set();
  const dropped = [];
  const forms = new Map([...root.querySelectorAll('form')].map((form) => [keyOf(form), form]));
  for (const [key, { controls, shown }] of typed) {
    const form = forms.get(key);
    const now = form ? shownBy(form) : null;
    const same = now && Object.keys({ ...shown, ...now }).every((name) => shown[name] === now[name]);
    for (const { name, value } of controls) {
      if (same) form.elements[name].value = value;
      else dropped.push(`${key}: ${name}`);
    }
    if (same) restored.add(key);
  }
  return { restored, dropped };
}

/** A file path with forward slashes: a server on Windows names files with backslashes. */
export function posixPath(path) {
  return path ? String(path).replace(/\\/g, '/') : path;
}

function prefixOf(path, prefix) {
  return Boolean(prefix) && (path === prefix || path.startsWith(`${prefix}.`) || path.startsWith(`${prefix}[`));
}

/**
 * Problems pinned to what they are about: the state or transition whose path is the longest prefix of the problem's
 * path, else (a problem of the root file with a line only) the state whose lines hold it. Problems of other files,
 * and of the machine as a whole, stay in `machine`.
 */
export function problemIndex(graph, problems, rootFile) {
  // keyed by state names: without a prototype, so a state named constructor is a state like any other
  const index = { states: Object.create(null), transitions: Object.create(null), machine: [] };
  const states = [...(graph.states || [])].sort((a, b) => (b.path || '').length - (a.path || '').length);
  const transitions = [...(graph.transitions || [])].sort((a, b) => (b.path || '').length - (a.path || '').length);
  const byLine = [...(graph.states || [])].filter((s) => s.line).sort((a, b) => b.line - a.line);
  const add = (bucket, key, problem) => {
    bucket[key] = bucket[key] || { errors: 0, warnings: 0, problems: [] };
    bucket[key][problem.level === 'error' ? 'errors' : 'warnings'] += 1;
    bucket[key].problems.push(problem);
  };
  for (const problem of problems || []) {
    const file = posixPath(problem.file);
    const inRoot = !file || !rootFile || file === posixPath(rootFile) || file.endsWith(`/${posixPath(rootFile)}`);
    const path = problem.path || '';
    if (!inRoot) {
      index.machine.push(problem);
      continue;
    }
    const transition = path ? transitions.find((t) => prefixOf(path, t.path)) : null;
    const state = path ? states.find((s) => prefixOf(path, s.path))
      : byLine.find((s) => problem.line && s.line <= problem.line);
    if (transition) add(index.transitions, transition.id, problem);
    if (state) add(index.states, state.name, problem);
    if (!state && !transition) index.machine.push(problem);
  }
  return index;
}

/**
 * What a run shows on the canvas: the root frame's active states and visit counts, where it is paused, the root
 * states that submachine frames run under, and the last transition the root frame fired.
 */
export function runOverlay(run) {
  const frames = run?.view?.frames || [];
  const root = frames.find((frame) => !frame.prefix) || null;
  const paused = run?.debug?.paused || null;
  const lastTransition = [...(run?.journal || [])].reverse()
    .find((row) => row.kind === 'trace' && row.status === 'transition' && !(row.data?.frame));
  return {
    active: new Set(root?.config || []),
    current: root?.state || null,
    visits: Object.assign(Object.create(null), root?.visits || {}),  // by state name: see problemIndex
    paused: paused && !paused.frame ? paused.state : null,
    submachines: new Set(frames.filter((frame) => frame.prefix && frame.path).map((frame) => frame.path.split('/')[0])),
    lastEdge: lastTransition && Number.isInteger(lastTransition.data?.index)
      ? `${lastTransition.data.from}#${lastTransition.data.index}` : null,
  };
}

/** `a: &base` or `a: !tag` (maybe with a comment): the value itself is on the lines below the key. */
const PROPERTIES_ONLY = /^(?:[&!]\S*\s*)+(?:#.*)?$/;

/**
 * A key line and the lines below it (the rule yamledit's replace_body uses): `rest` is what follows the colon (''
 * for nothing or a comment), `body` the deeper lines, dedented, and `continued` whether any of them is more than a
 * comment, i.e. a value on the key line goes on below it.
 */
function valueAt(text, line) {
  const lines = String(text ?? '').split('\n');
  const at = line - 1;
  if (!Number.isInteger(at) || at < 0 || at >= lines.length) return null;
  const head = lines[at];
  const column = head.search(/\S/);
  let end = at + 1;
  for (let i = at + 1; i < lines.length; i += 1) {
    if (!lines[i].trim()) continue;
    if (lines[i].search(/\S/) <= column) break;
    end = i + 1;
  }
  const body = lines.slice(at + 1, end);
  const depths = body.filter((l) => l.trim()).map((l) => l.search(/\S/));
  const depth = depths.length ? Math.min(...depths) : 0;
  const rest = head.slice(head.indexOf(':') + 1).trim();
  return {
    rest: rest.startsWith('#') ? '' : rest,
    body: body.map((l) => (l.trim() ? l.slice(depth) : '')).join('\n'),
    continued: body.some((l) => l.trim() && !l.trim().startsWith('#')),
  };
}

/**
 * Whether the key at `line` is the state `name` (plain or quoted); true without a name. In flow style
 * (`states: {a: ..., b: ...}`) a state's line starts with another key, and what follows that colon is not its value.
 */
function keyedBy(text, line, name) {
  if (name === undefined) return true;
  const head = String(text ?? '').split('\n')[line - 1] ?? '';
  const key = String(name).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`^(?:${key}|"${key}"|'${key}')\\s*:(?:\\s|$)`).test(head.trim());
}

/**
 * A state's value as YAML text, dedented, from the file text and the 1-based line of its key ('' if it is empty).
 * A value on the key line keeps its comment, so an Apply writes it back.
 */
export function stateFragment(text, line, name) {
  const value = valueAt(text, line);
  if (!value || !keyedBy(text, line, name)) return '';
  if (!value.rest || PROPERTIES_ONLY.test(value.rest)) return value.body;
  return value.continued ? `${value.rest}\n${value.body}` : value.rest;
}

/** Why the inspector cannot apply a state's YAML ('' when it can): set_state refuses these key lines. */
export function fragmentLock(text, line, name) {
  const value = valueAt(text, line);
  if (!value) return '';
  if (!keyedBy(text, line, name)) return 'It is written in flow style, on the line of another key';
  if (value.rest && /^[&!*]/.test(value.rest)) return 'It has an anchor, a tag or an alias on its key line';
  return value.rest && value.continued ? 'It starts on its key line and goes on below it' : '';
}

/** Relative position of a box inside its parent (what the layout sidecar stores). */
export function relativeSpot(nodes, id) {
  const box = nodes[id];
  const parent = box.parent ? nodes[box.parent] : null;
  return { x: Math.round(box.x - (parent ? parent.x : 0)), y: Math.round(box.y - (parent ? parent.y : 0)) };
}

/** Positions that keep states side by side where they are drawn (`nodes`) once they are grouped into a new
 * composite `name`: the composite's box around theirs, each of them relative to it. (Close to the top of a composite
 * they sit in, the new one's title band pushes them down: applyPositions keeps it inside that one's padding.) */
export function groupedSpots(nodes, names, name) {
  const spots = names.map((one) => relativeSpot(nodes, stateId(one)));
  const x = Math.min(...spots.map((spot) => spot.x)) - PAD.left;
  const y = Math.min(...spots.map((spot) => spot.y)) - PAD.top;
  return Object.fromEntries([[name, { x, y }], ...names.map((one, i) => [one, { x: spots[i].x - x, y: spots[i].y - y }])]);
}

// ---------------------------------------------------------------------------------------------------- the canvas

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined && value !== false) node.setAttribute(key, String(value));
  }
  if (parent) parent.appendChild(node);
  return node;
}

function text(parent, value, attrs) {
  const node = el('text', attrs, parent);
  node.textContent = value;
  return node;
}

function spriteIcon(parent, name, x, y, size, cls = 'sg-icon') {
  const holder = el('svg', { x, y, width: size, height: size, viewBox: '0 0 24 24', class: cls }, parent);
  el('use', { href: `${SPRITE}#${name}` }, holder);
  return holder;
}

/**
 * The canvas. Callbacks: onSelect(a selection, see selectionOf), onConnect(source, target),
 * onMove({name: {x, y}}) with every position the drag changed, onOpen({kind, id}) on a double click.
 */
export class Canvas {
  constructor(svg, { onSelect, onConnect, onMove, onOpen } = {}) {
    this.svg = svg;
    this.handlers = { onSelect, onConnect, onMove, onOpen };
    this.graph = { states: [], transitions: [] };
    this.positions = {};
    this.auto = { nodes: {}, edges: {} };
    this.view = { x: 0, y: 0, k: 1 };
    this.selected = null;
    this.overlay = { problems: { states: {}, transitions: {} }, run: null, breakpoints: new Set() };
    this.elk = typeof ELK === 'function' ? new ELK() : null;  // eslint-disable-line no-undef
    this.layoutRun = 0;
    svg.replaceChildren();
    const defs = el('defs', {}, svg);
    for (const kind of ['plain', 'error', 'active', 'selected']) {
      const marker = el('marker', {
        id: `sg-arrow-${kind}`, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7,
        orient: 'auto-start-reverse', class: `sg-arrow sg-arrow--${kind}`,
      }, defs);
      el('path', { d: 'M0 0 L10 5 L0 10 z' }, marker);
    }
    this.viewport = el('g', { class: 'sg-viewport' }, svg);
    // composites under the transitions: their filled box would hide the ones inside them
    this.compositeLayer = el('g', { class: 'sg-composites' }, this.viewport);
    this.edgeLayer = el('g', { class: 'sg-edges' }, this.viewport);
    this.nodeLayer = el('g', { class: 'sg-nodes' }, this.viewport);
    this.dragLayer = el('g', { class: 'sg-drag' }, this.viewport);
    this.bindPointer();
  }

  /** Lay the graph out (ELK) and draw it; positions: the sidecar's {name: {x, y}}. */
  async setGraph(graph, positions = {}) {
    this.graph = graph || { states: [], transitions: [] };
    this.positions = { ...(positions || {}) };
    const run = ++this.layoutRun;
    let auto = { nodes: {}, edges: {} };
    if (this.graph.states.length && this.elk) {
      try {
        auto = layoutFrom(await this.elk.layout(elkInput(this.graph)));
      } catch (error) {
        console.error('stategraph: layout failed', error);  // eslint-disable-line no-console
        auto = gridLayout(this.graph);
      }
    } else if (this.graph.states.length) {
      auto = gridLayout(this.graph);
    }
    if (run !== this.layoutRun) return false;  // a newer graph arrived while this one was laid out
    this.auto = auto;
    this.draw();
    return true;
  }

  setPositions(positions) {
    this.positions = { ...(positions || {}) };
    this.draw();
  }

  setOverlay(overlay) {
    this.overlay = { ...this.overlay, ...overlay };
    this.decorate();
  }

  select(selection, { quiet = true } = {}) {
    this.selected = selection;
    this.decorate();
    if (!quiet) this.handlers.onSelect?.(selection);
  }

  // ------------------------------------------------------------------ drawing
  draw() {
    const { nodes, moved } = applyPositions(this.auto, this.positions);
    this.nodes = nodes;
    this.compositeLayer.replaceChildren();
    this.edgeLayer.replaceChildren();
    this.nodeLayer.replaceChildren();
    const byName = new Map(this.graph.states.map((state) => [state.name, state]));
    // outer composites first, so the ones nested in them are drawn on top
    const order = Object.keys(nodes).sort((a, b) => depth(nodes, a) - depth(nodes, b));
    for (const id of order) {
      const box = nodes[id];
      if (id.startsWith('i:')) {
        el('circle', { cx: box.x + box.w / 2, cy: box.y + box.h / 2, r: box.w / 2, class: 'sg-initial' }, this.nodeLayer);
        continue;
      }
      const state = byName.get(id.slice(2));
      if (state) this.drawState(state, box);
    }
    for (const [id, route] of Object.entries(this.auto.edges)) {
      if (id.startsWith('ie:')) {
        const region = id.slice(3);
        const initial = region ? byName.get(region)?.initial : this.graph.initial;
        const source = nodes[initialId(region)];
        const target = nodes[stateId(initial)];
        if (!source || !target) continue;
        const drawn = edgeRoute(route, source, target, moved.has(initialId(region)) || moved.has(stateId(initial)));
        el('path', { d: pathData(drawn.points), class: 'sg-edge sg-edge--initial', 'marker-end': 'url(#sg-arrow-plain)' },
          this.edgeLayer);
      }
    }
    const linked = new Set(this.graph.transitions.map((t) => `${t.source}\n${t.target}`));
    for (const transition of this.graph.transitions) {
      const source = nodes[stateId(transition.source)];
      const target = transition.target ? nodes[stateId(transition.target)] : null;
      if (!source || !target) continue;
      const route = this.auto.edges[edgeId(transition.id)];
      const drawn = edgeRoute(route, source, target,
        moved.has(stateId(transition.source)) || moved.has(stateId(transition.target)),
        linked.has(`${transition.target}\n${transition.source}`));
      this.drawEdge(transition, drawn);
    }
    this.decorate();
  }

  drawState(state, box) {
    const kind = state.composite ? 'composite' : state.type;
    const group = el('g', {
      class: `sg-node sg-node--${kind}${state.wait ? ' sg-node--wait' : ''}`,
      'data-state': state.name, tabindex: 0, role: 'button', 'aria-label': `State ${state.name}`,
    }, state.composite ? this.compositeLayer : this.nodeLayer);
    const title = el('title', {}, group);
    title.textContent = [state.name, state.kind && `${state.kind}: ${state.label}`, state.description]
      .filter(Boolean).join('\n');
    const cx = box.x + box.w / 2;
    const cy = box.y + box.h / 2;
    if (state.type === 'choice') {
      el('polygon', { class: 'sg-shape', points: `${cx},${box.y} ${box.x + box.w},${cy} ${cx},${box.y + box.h} ${box.x},${cy}` }, group);
      text(group, state.name, { x: cx, y: box.y - 6, class: 'sg-caption', 'text-anchor': 'middle' });
    } else if (state.type === 'junction') {
      el('circle', { class: 'sg-shape sg-shape--filled', cx, cy, r: box.w / 2 }, group);
      text(group, state.name, { x: cx, y: box.y - 6, class: 'sg-caption', 'text-anchor': 'middle' });
    } else if (state.type === 'final') {
      el('circle', { class: `sg-shape${state.status === 'failed' ? ' sg-shape--failed' : ''}`, cx, cy, r: box.w / 2 }, group);
      el('circle', { class: `sg-shape--filled${state.status === 'failed' ? ' sg-shape--failed' : ''}`, cx, cy, r: box.w / 2 - 5 }, group);
      text(group, state.name, { x: cx, y: box.y + box.h + 13, class: 'sg-caption', 'text-anchor': 'middle' });
    } else {
      el('rect', { class: 'sg-box', x: box.x, y: box.y, width: box.w, height: box.h, rx: 10 }, group);
      const nameY = state.composite ? box.y + 21 : box.y + (state.label ? 21 : box.h / 2 + 4.5);
      let nameX = box.x + 12;
      if (state.icon) {
        spriteIcon(group, state.icon, box.x + 10, nameY - 12, 15);
        nameX += 20;
      }
      text(group, shorten(state.name, 30), { x: nameX, y: nameY, class: 'sg-name' });
      if (state.label && !state.composite) {
        text(group, shorten(state.label, 34), { x: box.x + 12, y: box.y + 39, class: 'sg-label' });
      }
      if (state.composite) el('line', { x1: box.x, x2: box.x + box.w, y1: box.y + 30, y2: box.y + 30, class: 'sg-rule' }, group);
    }
    if (state.type !== 'final') {
      el('circle', { class: 'sg-handle', cx: box.x + box.w, cy: state.composite ? box.y + 15 : cy, r: 5,
        'data-handle': state.name }, group);
    }
    // overlay slots, filled by decorate()
    el('g', { class: 'sg-badges', 'data-x': box.x + box.w, 'data-y': box.y, 'data-left': box.x }, group);
  }

  drawEdge(transition, drawn) {
    const kind = transition.trigger === 'error' ? 'error' : transition.trigger !== 'done' ? 'event' : 'done';
    const group = el('g', {
      class: `sg-link sg-link--${kind}${transition.guard === 'else' ? ' sg-link--else' : ''}`,
      'data-transition': transition.id,
    }, this.edgeLayer);
    const title = el('title', {}, group);
    title.textContent = [`${transition.source} → ${transition.target}`, transition.trigger !== 'done' && `on ${transition.trigger}`,
      transition.guard && `[${transition.guard}]`, transition.effect && `/ ${transition.effect}`].filter(Boolean).join('\n');
    const d = pathData(drawn.points);
    el('path', { d, class: 'sg-hit' }, group);
    el('path', { d, class: 'sg-edge', 'marker-end': `url(#sg-arrow-${kind === 'error' ? 'error' : 'plain'})` }, group);
    const label = edgeText(transition);
    if (!label) return;
    const [lx, ly] = labelSpot(drawn, textWidth(label, 11, true));
    el('rect', { class: 'sg-edge-label-bg', x: lx - 3, y: ly - 11, width: textWidth(label, 11, true) + 6, height: 15, rx: 3 }, group);
    text(group, label, { x: lx, y: ly, class: 'sg-edge-label' });
  }

  /** Classes and badges from the selection, the problems, the breakpoints and the run: no re-layout. */
  decorate() {
    if (!this.nodes) return;
    const run = this.overlay.run;
    const problems = this.overlay.problems || { states: {}, transitions: {} };
    const breakpoints = this.overlay.breakpoints || new Set();
    const chosen = new Set(selectedStates(this.selected));
    const chosenEdges = new Set(selectedTransitions(this.selected));
    for (const group of this.viewport.querySelectorAll('.sg-node')) {
      const name = group.dataset.state;
      const pinned = problems.states[name];
      group.classList.toggle('is-selected', chosen.has(name));
      group.classList.toggle('is-active', Boolean(run?.active.has(name)));
      group.classList.toggle('is-current', run?.current === name);
      group.classList.toggle('is-paused', run?.paused === name);
      group.classList.toggle('is-sub', Boolean(run?.submachines.has(name)));
      group.classList.toggle('has-error', Boolean(pinned?.errors));
      group.classList.toggle('has-warning', Boolean(pinned && !pinned.errors && pinned.warnings));
      const badges = group.querySelector('.sg-badges');
      badges.replaceChildren();
      const right = Number(badges.dataset.x);
      const top = Number(badges.dataset.y);
      let x = right - 4;
      const badge = (value, cls) => {
        const width = textWidth(value, 10) + 10;
        x -= width;
        el('rect', { x, y: top - 8, width, height: 16, rx: 8, class: `sg-badge ${cls}` }, badges);
        text(badges, value, { x: x + width / 2, y: top + 3.5, class: 'sg-badge-text', 'text-anchor': 'middle' });
        x -= 3;
      };
      if (pinned?.errors) badge(`${pinned.errors} err`, 'sg-badge--danger');
      else if (pinned?.warnings) badge(`${pinned.warnings} warn`, 'sg-badge--warn');
      const visits = run && Object.hasOwn(run.visits, name) ? run.visits[name] : 0;
      if (visits) badge(`×${visits}`, 'sg-badge--info');
      if (breakpoints.has(name)) el('circle', { cx: Number(badges.dataset.left), cy: top, r: 5, class: 'sg-breakpoint' }, badges);
    }
    for (const group of this.edgeLayer.querySelectorAll('.sg-link')) {
      const id = group.dataset.transition;
      const pinned = problems.transitions[id];
      group.classList.toggle('is-selected', chosenEdges.has(id));
      group.classList.toggle('is-last', run?.lastEdge === id);
      group.classList.toggle('has-error', Boolean(pinned?.errors));
      const edge = group.querySelector('.sg-edge');
      const kind = group.classList.contains('is-selected') ? 'selected' : group.classList.contains('is-last') ? 'active'
        : group.classList.contains('sg-link--error') ? 'error' : 'plain';
      edge.setAttribute('marker-end', `url(#sg-arrow-${kind})`);
    }
  }

  // ------------------------------------------------------------------ view
  applyView() {
    const { x, y, k } = this.view;
    this.viewport.setAttribute('transform', `translate(${x} ${y}) scale(${k})`);
  }

  bounds() {
    const boxes = Object.values(this.nodes || {});
    if (!boxes.length) return { x: 0, y: 0, w: 1, h: 1 };
    const x = Math.min(...boxes.map((b) => b.x));
    const y = Math.min(...boxes.map((b) => b.y)) - 24;
    return { x, y, w: Math.max(...boxes.map((b) => b.x + b.w)) - x, h: Math.max(...boxes.map((b) => b.y + b.h)) + 20 - y };
  }

  fit() {
    const rect = this.svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const box = this.bounds();
    const k = clamp(Math.min((rect.width - 48) / box.w, (rect.height - 48) / box.h), 0.2, 1.5);
    this.view = { k, x: (rect.width - box.w * k) / 2 - box.x * k, y: (rect.height - box.h * k) / 2 - box.y * k };
    this.applyView();
  }

  zoom(factor, cx = null, cy = null) {
    const rect = this.svg.getBoundingClientRect();
    const px = cx ?? rect.width / 2;
    const py = cy ?? rect.height / 2;
    const k = clamp(this.view.k * factor, 0.15, 3);
    this.view = { k, x: px - ((px - this.view.x) / this.view.k) * k, y: py - ((py - this.view.y) / this.view.k) * k };
    this.applyView();
  }

  /** Scroll the view so a state is in sight. */
  reveal(name) {
    const box = this.nodes?.[stateId(name)];
    if (!box) return;
    const rect = this.svg.getBoundingClientRect();
    const { k } = this.view;
    const sx = box.x * k + this.view.x;
    const sy = box.y * k + this.view.y;
    if (sx < 0 || sy < 0 || sx + box.w * k > rect.width || sy + box.h * k > rect.height) {
      this.view = { k, x: rect.width / 2 - (box.x + box.w / 2) * k, y: rect.height / 2 - (box.y + box.h / 2) * k };
      this.applyView();
    }
  }

  toCanvas(event) {
    const rect = this.svg.getBoundingClientRect();
    return [(event.clientX - rect.left - this.view.x) / this.view.k, (event.clientY - rect.top - this.view.y) / this.view.k];
  }

  // ------------------------------------------------------------------ pointer
  bindPointer() {
    const svg = this.svg;
    let gesture = null;
    // the wheel scrolls the view (Shift: sideways), Ctrl+wheel zooms -- a touchpad's pinch comes as Ctrl+wheel too
    svg.addEventListener('wheel', (event) => {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        const rect = svg.getBoundingClientRect();
        this.zoom(event.deltaY < 0 ? 1.1 : 1 / 1.1, event.clientX - rect.left, event.clientY - rect.top);
        return;
      }
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? svg.getBoundingClientRect().height : 1;
      const [dx, dy] = event.shiftKey && !event.deltaX ? [event.deltaY, 0] : [event.deltaX, event.deltaY];
      this.view = { ...this.view, x: this.view.x - dx * unit, y: this.view.y - dy * unit };
      this.applyView();
    }, { passive: false });
    svg.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      const handle = event.target.closest?.('[data-handle]');
      const node = event.target.closest?.('.sg-node');
      const link = event.target.closest?.('.sg-link');
      const [x, y] = this.toCanvas(event);
      const adding = event.shiftKey || event.ctrlKey || event.metaKey;
      if (handle) {
        gesture = { type: 'connect', source: handle.dataset.handle, x, y };
      } else if (adding) {
        // with Ctrl or Shift: a click toggles the state or transition under it, a drag -- from anywhere -- draws a band
        const item = node ? { kind: 'state', id: node.dataset.state }
          : link ? { kind: 'transition', id: link.dataset.transition } : null;
        gesture = { type: 'band', item, x, y, moved: false };
      } else if (node) {
        // a state of a selection of several moves them all (a composite takes the states inside it along); the
        // positions count from where they were, not step by step: a rounded step would drift, or stick when zoomed
        const name = node.dataset.state;
        const chosen = selectedStates(this.selected);
        const names = this.selected?.kind === 'many' && chosen.includes(name)
          ? outermost(chosen, (child) => this.nodes?.[stateId(child)]?.parent?.slice(2) || null) : [name];
        const { nodes } = applyPositions(this.auto, this.positions);
        const from = Object.fromEntries(names.map((one) => [one, relativeSpot(nodes, stateId(one))]));
        gesture = { type: 'move', name, names, from, x, y, moved: false };
      } else if (link) {
        this.select({ kind: 'transition', id: link.dataset.transition }, { quiet: false });
        return;
      } else {
        gesture = { type: 'pan', x: event.clientX, y: event.clientY, view: { ...this.view }, moved: false };
      }
      svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener('pointermove', (event) => {
      if (!gesture) return;
      if (gesture.type === 'pan') {
        const dx = event.clientX - gesture.x;
        const dy = event.clientY - gesture.y;
        gesture.moved ||= Math.abs(dx) + Math.abs(dy) > 3;
        this.view = { ...gesture.view, x: gesture.view.x + dx, y: gesture.view.y + dy };
        this.applyView();
        return;
      }
      const [x, y] = this.toCanvas(event);
      if (gesture.type === 'band') {
        gesture.moved ||= (Math.abs(x - gesture.x) + Math.abs(y - gesture.y)) * this.view.k > 4;
        if (!gesture.moved) return;
        gesture.band ={ x: Math.min(x, gesture.x), y: Math.min(y, gesture.y), w: Math.abs(x - gesture.x), h: Math.abs(y - gesture.y) };
        this.dragLayer.replaceChildren();
        const { band } = gesture;
        el('rect', { x: band.x, y: band.y, width: band.w, height: band.h, class: 'sg-band' }, this.dragLayer);
        return;
      }
      if (gesture.type === 'connect') {
        gesture.moved ||= (Math.abs(x - gesture.x) + Math.abs(y - gesture.y)) * this.view.k > 4;
        const box = this.nodes[stateId(gesture.source)];
        this.dragLayer.replaceChildren();
        const from = box ? clipToBox(box, x, y) : [gesture.x, gesture.y];
        el('path', { d: pathData([from, [x, y]]), class: 'sg-edge sg-edge--draft', 'marker-end': 'url(#sg-arrow-selected)' }, this.dragLayer);
        return;
      }
      const dx = x - gesture.x;
      const dy = y - gesture.y;
      // screen pixels, not canvas units: zoomed out, 4 units are less than a pixel and a click became a drag
      if (!gesture.moved && (Math.abs(dx) + Math.abs(dy)) * this.view.k < 4) return;
      gesture.moved = true;
      const moved = Object.fromEntries(gesture.names.map((name) => [name,
        { x: gesture.from[name].x + dx, y: gesture.from[name].y + dy }]));
      this.positions = { ...this.positions, ...moved };
      this.draw();
    });
    const finish = (event) => {
      if (!gesture) return;
      const done = gesture;
      gesture = null;
      this.dragLayer.replaceChildren();
      if (done.type === 'pan') {
        if (!done.moved) this.select(null, { quiet: false });
      } else if (done.type === 'connect') {
        // a click on the handle is no connection: a self-transition takes a drag out and back
        const target = document.elementFromPoint(event.clientX, event.clientY)?.closest?.('.sg-node');
        if (target && done.moved && event.type === 'pointerup') this.handlers.onConnect?.(done.source, target.dataset.state);
      } else if (done.type === 'band') {
        // a band adds the states wholly inside it; a click toggles what it is on -- on the empty canvas it keeps all
        if (done.moved && done.band) {
          this.select(selectionOf([...selectedStates(this.selected), ...statesWithin(this.nodes, done.band)],
            selectedTransitions(this.selected)), { quiet: false });
        } else if (!done.moved && done.item) {
          this.select(toggled(this.selected, done.item), { quiet: false });
        }
      } else if (done.type === 'move') {
        if (done.moved) {
          const { nodes } = applyPositions(this.auto, this.positions);
          this.handlers.onMove?.(Object.fromEntries(done.names.map((name) => [name, relativeSpot(nodes, stateId(name))])));
        } else {
          this.select({ kind: 'state', id: done.name }, { quiet: false });
        }
      }
    };
    svg.addEventListener('pointerup', finish);
    svg.addEventListener('pointercancel', finish);
    svg.addEventListener('dblclick', (event) => {
      if (event.shiftKey || event.ctrlKey || event.metaKey) return;  // two quick toggles are no rename
      // pointerdown captured the pointer for the svg, and a click goes where the pointer is captured: what was
      // clicked is what lies under the pointer
      const hit = document.elementFromPoint(event.clientX, event.clientY) || event.target;
      const node = hit.closest?.('.sg-node');
      const link = hit.closest?.('.sg-link');
      if (node) this.handlers.onOpen?.({ kind: 'state', id: node.dataset.state });
      else if (link) this.handlers.onOpen?.({ kind: 'transition', id: link.dataset.transition });
    });
    svg.addEventListener('keydown', (event) => {
      const node = event.target.closest?.('.sg-node');
      if (node && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        const name = node.dataset.state;
        const adding = event.shiftKey || event.ctrlKey || event.metaKey;
        this.select(adding ? toggled(this.selected, { kind: 'state', id: name }) : { kind: 'state', id: name }, { quiet: false });
      }
    });
  }
}

function depth(nodes, id) {
  let count = 0;
  for (let parent = nodes[id]?.parent; parent; parent = nodes[parent]?.parent) count += 1;
  return count;
}

/** Without ELK (it failed to load): a plain grid in document order, nested states inside their composite. */
export function gridLayout(graph) {
  const nodes = {};
  const byParent = childrenOf(graph);
  const place = (parent, owner, ox, oy) => {
    let x = ox;
    let y = oy;
    let rowHeight = 0;
    let right = ox;
    let bottom = oy;
    for (const state of byParent.get(parent) || []) {
      const id = stateId(state.name);
      let [w, h] = nodeSize(state);
      nodes[id] = { x, y, w, h, parent: owner };
      if (state.composite && byParent.has(state.name)) {
        const inner = place(state.name, id, x + PAD.left, y + PAD.top);
        w = Math.max(w, inner.right - x + PAD.right);
        h = Math.max(h, inner.bottom - y + PAD.bottom);
        Object.assign(nodes[id], { w, h });
      }
      right = Math.max(right, x + w);
      bottom = Math.max(bottom, y + h);
      rowHeight = Math.max(rowHeight, h);
      x += w + 48;
      if (x - ox > 900) {
        x = ox;
        y += rowHeight + 48;
        rowHeight = 0;
      }
    }
    return { right, bottom };
  };
  place('', null, 24, 24);
  return { nodes, edges: {} };
}
