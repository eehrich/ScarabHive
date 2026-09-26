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

/** [width, height] of a state that is drawn as one box or shape. */
export function nodeSize(state) {
  if (SHAPES[state.type]) return SHAPES[state.type];
  const name = textWidth(state.name, 13) + (state.icon ? 40 : 24);
  const label = state.label ? textWidth(shorten(state.label, 34), 11, true) + 24 : 0;
  return [clamp(Math.max(name, label, 96), 96, 280), state.label ? 50 : 34];
}

function childrenOf(graph) {
  const byParent = new Map();
  for (const state of graph.states || []) {
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
        layoutOptions: { 'elk.padding': `[top=${PAD.top},left=${PAD.left},bottom=${PAD.bottom},right=${PAD.right}]` },
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

/** ELK's answer as flat boxes (absolute, with their parent) and edge routes. */
export function layoutFrom(out) {
  const nodes = {};
  const edges = {};
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
  }
  return { nodes, edges };
}

/**
 * Stored positions over the automatic layout. positions: {state name: {x, y}}, relative to the parent's box (top
 * level: absolute). A composite grows to hold its children; a child stays inside its parent's content area.
 * Returns the boxes and the ids of those that differ from ELK's.
 */
export function applyPositions(layout, positions) {
  const auto = layout.nodes;
  const ids = Object.keys(auto);  // pre-order: a parent before its children
  const relative = {};
  for (const id of ids) {
    const parent = auto[id].parent ? auto[auto[id].parent] : null;
    relative[id] = { x: auto[id].x - (parent ? parent.x : 0), y: auto[id].y - (parent ? parent.y : 0) };
  }
  for (const [name, spot] of Object.entries(positions || {})) {
    const id = stateId(name);
    if (!relative[id] || !Number.isFinite(spot?.x) || !Number.isFinite(spot?.y)) continue;
    const nested = Boolean(auto[id].parent);
    relative[id] = { x: nested ? Math.max(spot.x, PAD.left) : spot.x, y: nested ? Math.max(spot.y, PAD.top) : spot.y };
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
  const moved = new Set(ids.filter((id) => ['x', 'y', 'w', 'h'].some((k) => Math.abs(nodes[id][k] - auto[id][k]) > 0.5)));
  return { nodes, moved };
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

/** A drawn edge: ELK's route, or a straight line when an end was moved by hand. */
export function edgeRoute(route, source, target, moved) {
  if (route && route.points.length && !moved) return { points: route.points, label: route.label, straight: false };
  if (source === target) {  // a self-transition: a loop over the top right corner
    const x = source.x + source.w * 0.75;
    const y = source.y;
    return { points: [[x - 12, y], [x - 12, y - 18], [x + 12, y - 18], [x + 12, y]], label: null, straight: true };
  }
  const from = clipToBox(source, target.x + target.w / 2, target.y + target.h / 2);
  const to = clipToBox(target, source.x + source.w / 2, source.y + source.h / 2);
  return { points: [from, to], label: null, straight: true };
}

export function pathData(points) {
  return points.map(([x, y], i) => `${i ? 'L' : 'M'}${Math.round(x * 10) / 10} ${Math.round(y * 10) / 10}`).join(' ');
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
  const index = { states: {}, transitions: {}, machine: [] };
  const states = [...(graph.states || [])].sort((a, b) => (b.path || '').length - (a.path || '').length);
  const transitions = [...(graph.transitions || [])].sort((a, b) => (b.path || '').length - (a.path || '').length);
  const byLine = [...(graph.states || [])].filter((s) => s.line).sort((a, b) => b.line - a.line);
  const add = (bucket, key, problem) => {
    bucket[key] = bucket[key] || { errors: 0, warnings: 0, problems: [] };
    bucket[key][problem.level === 'error' ? 'errors' : 'warnings'] += 1;
    bucket[key].problems.push(problem);
  };
  for (const problem of problems || []) {
    const inRoot = !problem.file || !rootFile || problem.file === rootFile || problem.file.endsWith(`/${rootFile}`);
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
    visits: { ...(root?.visits || {}) },
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
 * A state's value as YAML text, dedented, from the file text and the 1-based line of its key ('' if it is empty).
 * A value on the key line keeps its comment, so an Apply writes it back.
 */
export function stateFragment(text, line) {
  const value = valueAt(text, line);
  if (!value) return '';
  if (!value.rest || PROPERTIES_ONLY.test(value.rest)) return value.body;
  return value.continued ? `${value.rest}\n${value.body}` : value.rest;
}

/** Why the inspector cannot apply a state's YAML ('' when it can): set_state refuses these key lines. */
export function fragmentLock(text, line) {
  const value = valueAt(text, line);
  if (!value?.rest) return '';
  if (/^[&!*]/.test(value.rest)) return 'It has an anchor, a tag or an alias on its key line';
  return value.continued ? 'It starts on its key line and goes on below it' : '';
}

/** Relative position of a box inside its parent (what the layout sidecar stores). */
export function relativeSpot(nodes, id) {
  const box = nodes[id];
  const parent = box.parent ? nodes[box.parent] : null;
  return { x: Math.round(box.x - (parent ? parent.x : 0)), y: Math.round(box.y - (parent ? parent.y : 0)) };
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
 * The canvas. Callbacks: onSelect({kind: 'state'|'transition', id} | null), onConnect(source, target),
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
    this.edgeLayer.replaceChildren();
    this.nodeLayer.replaceChildren();
    const byName = new Map(this.graph.states.map((state) => [state.name, state]));
    // composites first, so their children are drawn on top
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
    for (const transition of this.graph.transitions) {
      const source = nodes[stateId(transition.source)];
      const target = transition.target ? nodes[stateId(transition.target)] : null;
      if (!source || !target) continue;
      const route = this.auto.edges[edgeId(transition.id)];
      const drawn = edgeRoute(route, source, target,
        moved.has(stateId(transition.source)) || moved.has(stateId(transition.target)));
      this.drawEdge(transition, drawn);
    }
    this.decorate();
  }

  drawState(state, box) {
    const kind = state.composite ? 'composite' : state.type;
    const group = el('g', {
      class: `sg-node sg-node--${kind}${state.wait ? ' sg-node--wait' : ''}`,
      'data-state': state.name, tabindex: 0, role: 'button', 'aria-label': `State ${state.name}`,
    }, this.nodeLayer);
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
    let lx;
    let ly;
    if (drawn.label) {
      lx = drawn.label.x + 4;
      ly = drawn.label.y + 12;
    } else {
      const [a, b] = [drawn.points[0], drawn.points[drawn.points.length - 1]];
      lx = (a[0] + b[0]) / 2 - textWidth(label, 11, true) / 2;
      ly = (a[1] + b[1]) / 2 - 6;
    }
    el('rect', { class: 'sg-edge-label-bg', x: lx - 3, y: ly - 11, width: textWidth(label, 11, true) + 6, height: 15, rx: 3 }, group);
    text(group, label, { x: lx, y: ly, class: 'sg-edge-label' });
  }

  /** Classes and badges from the selection, the problems, the breakpoints and the run: no re-layout. */
  decorate() {
    if (!this.nodes) return;
    const run = this.overlay.run;
    const problems = this.overlay.problems || { states: {}, transitions: {} };
    const breakpoints = this.overlay.breakpoints || new Set();
    for (const group of this.nodeLayer.querySelectorAll('.sg-node')) {
      const name = group.dataset.state;
      const pinned = problems.states[name];
      group.classList.toggle('is-selected', this.selected?.kind === 'state' && this.selected.id === name);
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
      const visits = run?.visits?.[name];
      if (visits) badge(`×${visits}`, 'sg-badge--info');
      if (breakpoints.has(name)) el('circle', { cx: Number(badges.dataset.left), cy: top, r: 5, class: 'sg-breakpoint' }, badges);
    }
    for (const group of this.edgeLayer.querySelectorAll('.sg-link')) {
      const id = group.dataset.transition;
      const pinned = problems.transitions[id];
      group.classList.toggle('is-selected', this.selected?.kind === 'transition' && this.selected.id === id);
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
    svg.addEventListener('wheel', (event) => {
      event.preventDefault();
      const rect = svg.getBoundingClientRect();
      this.zoom(event.deltaY < 0 ? 1.1 : 1 / 1.1, event.clientX - rect.left, event.clientY - rect.top);
    }, { passive: false });
    svg.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      const handle = event.target.closest?.('[data-handle]');
      const node = event.target.closest?.('.sg-node');
      const link = event.target.closest?.('.sg-link');
      const [x, y] = this.toCanvas(event);
      if (handle) {
        gesture = { type: 'connect', source: handle.dataset.handle, x, y };
      } else if (node) {
        gesture = { type: 'move', name: node.dataset.state, x, y, moved: false };
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
      if (gesture.type === 'connect') {
        const box = this.nodes[stateId(gesture.source)];
        this.dragLayer.replaceChildren();
        const from = box ? clipToBox(box, x, y) : [gesture.x, gesture.y];
        el('path', { d: pathData([from, [x, y]]), class: 'sg-edge sg-edge--draft', 'marker-end': 'url(#sg-arrow-selected)' }, this.dragLayer);
        return;
      }
      const dx = x - gesture.x;
      const dy = y - gesture.y;
      if (!gesture.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
      gesture.moved = true;
      const id = stateId(gesture.name);
      const spot = relativeSpot(applyPositions(this.auto, this.positions).nodes, id);
      gesture.x = x;
      gesture.y = y;
      this.positions = { ...this.positions, [gesture.name]: { x: spot.x + dx, y: spot.y + dy } };
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
        const target = document.elementFromPoint(event.clientX, event.clientY)?.closest?.('.sg-node');
        if (target && event.type === 'pointerup') this.handlers.onConnect?.(done.source, target.dataset.state);
      } else if (done.type === 'move') {
        if (done.moved) {
          const { nodes } = applyPositions(this.auto, this.positions);
          this.handlers.onMove?.({ [done.name]: relativeSpot(nodes, stateId(done.name)) });
        } else {
          this.select({ kind: 'state', id: done.name }, { quiet: false });
        }
      }
    };
    svg.addEventListener('pointerup', finish);
    svg.addEventListener('pointercancel', finish);
    svg.addEventListener('dblclick', (event) => {
      const node = event.target.closest?.('.sg-node');
      const link = event.target.closest?.('.sg-link');
      if (node) this.handlers.onOpen?.({ kind: 'state', id: node.dataset.state });
      else if (link) this.handlers.onOpen?.({ kind: 'transition', id: link.dataset.transition });
    });
    svg.addEventListener('keydown', (event) => {
      const node = event.target.closest?.('.sg-node');
      if (node && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        this.select({ kind: 'state', id: node.dataset.state }, { quiet: false });
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
