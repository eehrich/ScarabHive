// State Graph: machines on the left; the open machine as graph, YAML and runs in the middle; the inspector and the
// debugger on the right. Every graph edit is one POST .../edit against the file version the panel shows, and the
// panel redraws from what the server answers. Runs are polled while they are alive.
import {
  abandon, api, autoRefresh, confirm, copyText, dialog, emptyState, errorText, html, icon, isAborted, jsonView,
  localTime, navigate, notice, pluginBase, prompt, render, selectTab, setDirty, setQuery, setTitle, toast, update,
  withBusy,
} from '/static/kit/panel-kit.js';
import { Canvas, fragmentLock, problemIndex, runOverlay, shorten, stateFragment } from './graph.js';

const API = `${pluginBase(import.meta.url)}/api`;
const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;
const NAME = /^[a-z][a-z0-9_]*$/;
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);
const STATUS_KIND = { running: 'info', paused: 'warn', waiting: 'info', succeeded: 'ok', failed: 'danger', cancelled: '', interrupted: 'warn' };
const PSEUDO = [
  { type: 'state', title: 'State', icon: 'square', hint: 'A state without activity: completes at once, or waits for events' },
  { type: 'choice', title: 'Choice', icon: 'git-branch', hint: 'Choice: guards decide, the last one is else' },
  { type: 'junction', title: 'Junction', icon: 'circle-dot', hint: 'Junction: guards decide before anything runs' },
  { type: 'final', title: 'Final', icon: 'circle-check', hint: 'Final state: ends its region' },
];

const S = {
  machines: [],
  machine: null,        // get_machine: id, file, writable, root_file, files, versions, problems, graph, layout
  kinds: [],
  selection: null,      // {kind: 'state' | 'transition', id}
  problems: { states: {}, transitions: {}, machine: [] },
  drafts: {},           // YAML tab: path -> unsaved text
  yamlFile: null,
  runs: [],
  runId: null,
  run: null,            // get_run of the selected run
  evaluation: null,     // {expr, value} | {expr, error}
  nextBreakpoints: [],  // [{state, at, machine}] for the next run of this machine
  nextWatch: [],        // [expr] for the next run, watched in this machine's frames
};

const badge = (text, kind = '') => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${text}</span>`;
const statusBadge = (status) => badge(status || 'unknown', STATUS_KIND[status] ?? '');
const stateOf = (name) => S.machine?.graph?.states?.find((state) => state.name === name) || null;
const transitionOf = (id) => S.machine?.graph?.transitions?.find((t) => t.id === id) || null;
const hasDrafts = () => Object.keys(S.drafts).length > 0;
const liveRun = () => (S.run && !TERMINAL.has(S.run.status) && S.run.active ? S.run : null);
/** Breakpoints of the live run, else those the next run starts with. */
const shownBreakpoints = () => (liveRun() ? (liveRun().debug?.breakpoints || []) : S.nextBreakpoints);
/** A point of the open machine: its own, or one without a machine, which stops in every frame. */
const ofThisMachine = (point) => !point.machine || point.machine === S.machine?.id;
/** "machine: " before a point of another machine (a submachine's), in the lists. */
const otherMachine = (point) => (point.machine && point.machine !== S.machine?.id ? `${point.machine}: ` : '');
const preview = (value, max = 90) => {
  if (value === undefined || value === null) return '';
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return shorten(text.replace(/\s+/g, ' '), max);
};

function remember(key, value) {
  try { localStorage.setItem(`stategraph:${key}`, JSON.stringify(value)); } catch { /* a convenience only */ }
}
function recall(key, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(`stategraph:${key}`));
    return value ?? fallback;
  } catch { return fallback; }
}

// ------------------------------------------------------------------ canvas

const canvas = new Canvas($('canvas'), {
  onSelect: (selection) => choose(selection),
  onConnect: (source, target) => connect(source, target),
  onMove: (spots) => savePositions(spots),
  onOpen: (target) => (target.kind === 'state' ? renameState(target.id) : choose(target)),
});

function positions() {
  return S.machine?.layout?.positions || {};
}

function redrawOverlay() {
  canvas.setOverlay({
    problems: S.problems,
    run: S.run && S.run.machine_id === S.machine?.id ? runOverlay(S.run) : null,
    breakpoints: new Set(shownBreakpoints().filter((p) => p.enabled !== false && ofThisMachine(p)).map((p) => p.state)),
  });
}

let fitPending = true;
async function drawGraph({ fit = false } = {}) {
  const drawn = await canvas.setGraph(S.machine.graph, positions());
  if (!drawn) return;
  canvas.select(S.selection);
  redrawOverlay();
  if (fit || fitPending) {
    fitPending = !$('canvas').getBoundingClientRect().width;  // hidden now: fit once the graph tab shows
    canvas.fit();
  }
  $('canvasHint').textContent = S.machine.graph.states.length
    ? 'Drag a state to move it, from its handle to another state to connect; double-click to rename.'
    : 'The file does not parse: fix it in the YAML tab.';
}

async function savePositions(spots) {
  const layout = { version: 1, positions: { ...positions(), ...spots } };
  S.machine.layout = layout;
  canvas.setPositions(layout.positions);
  if (!S.machine.writable) return;  // kept for this view only: the sidecar sits next to a read-only file
  try {
    await api(`${API}/machines/${enc(S.machine.id)}/layout`, { method: 'PUT', json: { layout }, quiet: true });
  } catch (error) {
    if (!isAborted(error)) toast(`Positions not saved: ${errorText(error)}`, { kind: 'warn' });
  }
}

// ------------------------------------------------------------------ machines

async function loadMachines() {
  try {
    S.machines = await api(`${API}/machines`, { latest: 'machines', quiet: true });
  } catch (error) {
    if (isAborted(error)) return;
    update($('machineList'), emptyState('circle-alert', 'Machines could not be loaded', errorText(error)));
    return;
  }
  drawMachineList();
}

function drawMachineList() {
  const needle = $('search').value.trim().toLowerCase();
  const shown = S.machines.filter((m) => !needle || `${m.id} ${m.title} ${m.description}`.toLowerCase().includes(needle));
  update($('machineList'), shown.length ? shown.map((m) => html`
    <button type="button" class="pk-btn pk-btn--ghost sg-item" data-machine="${m.id}" aria-current="${String(m.id === S.machine?.id)}">
      <span class="sg-item-top"><span class="sg-item-name">${m.title || m.id}</span>
        ${m.errors ? badge(`${m.errors} err`, 'danger') : m.warnings ? badge(`${m.warnings} warn`, 'warn') : badge('valid', 'ok')}
        ${m.writable ? '' : badge('read-only')}</span>
      <span class="sg-item-sub pk-mono">${m.id}</span>
      ${m.description ? html`<span class="sg-item-sub">${shorten(m.description, 120)}</span>` : ''}
    </button>`)
    : emptyState('workflow', S.machines.length ? 'No machine matches' : 'No machines yet', S.machines.length ? '' : 'Create one with New.'));
}

/** Open (or reload) a machine. Unsaved YAML is discarded only after the author agreed, or when the caller has
 * dealt with it already (discard: a reload after a conflict or a save). */
async function openMachine(id, { keepRun = false, discard = false } = {}) {
  if (hasDrafts() && !discard && !await confirm(id === S.machine?.id
    ? 'The YAML tab has unsaved changes. Reload the machine and discard them?'
    : 'The YAML tab has unsaved changes. Open another machine and discard them?', { danger: true, confirmLabel: 'Discard' })) {
    return;
  }
  abandon('machine-refresh');  // a refresh in flight would answer for the machine this one replaces
  let machine;
  try {
    machine = await api(`${API}/machines/${enc(id)}`, { latest: 'machine' });
  } catch (error) {
    return;  // toasted
  }
  const switched = machine.id !== S.machine?.id;
  S.drafts = {};
  setDirty(false);
  if (switched) {
    S.selection = null;
    S.yamlFile = machine.root_file;
    const stored = recall(`breakpoints:${machine.id}`, []);
    // stored per machine, so one kept without its machine (an older panel) belongs to this one
    S.nextBreakpoints = (Array.isArray(stored) ? stored : []).filter((p) => p?.state)
      .map((p) => ({ ...p, machine: p.machine || machine.id }));
    S.nextWatch = recall(`watch:${machine.id}`, []);
    $('mocks').value = recall(`mocks:${machine.id}`, '');
    fitPending = true;
    if (!keepRun) selectRun(null);
  }
  showMachine(machine);
  setTitle(`State Graph · ${machine.id}`);
  setQuery(S.runId ? { machine: machine.id, run: S.runId } : { machine: machine.id });
  loadRuns();
}

/** A machine answer (get, edit): everything that shows it is drawn again. */
function showMachine(machine) {
  S.machine = machine;
  S.problems = problemIndex(machine.graph, machine.problems, machine.file || machine.root_file);
  if (S.selection?.kind === 'state' && !stateOf(S.selection.id)) S.selection = null;
  if (S.selection?.kind === 'transition' && !transitionOf(S.selection.id)) S.selection = null;
  $('placeholder').hidden = true;
  $('machineView').hidden = false;
  drawHead();
  drawPalette();
  drawGraph();
  drawYaml();
  drawStartForm();
  drawInspector();
  drawMachineList();
}

function drawHead() {
  const m = S.machine;
  const errors = m.problems.filter((p) => p.level === 'error').length;
  const warnings = m.problems.length - errors;
  update($('machineHead'), html`
    <div class="sg-head-title">
      <h2 class="sg-head-name">${m.graph.title || m.id}</h2>
      <span class="sg-head-sub"><span class="pk-mono">${m.id}</span> · <span class="pk-mono">${m.file}</span></span>
    </div>
    ${errors ? badge(`${errors} error${errors > 1 ? 's' : ''}`, 'danger') : badge('valid', 'ok')}
    ${warnings ? badge(`${warnings} warning${warnings > 1 ? 's' : ''}`, 'warn') : ''}
    ${m.writable ? '' : html`<span class="pk-badge" title="Not in a writable machine root: shown, run and debugged, not edited">read-only</span>`}
    <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-act="copy-id" title="Copy the machine id">${icon('copy', { size: 'sm' })}</button>`);
  $('yamlCount').textContent = hasDrafts() ? 'unsaved' : '';
}

function drawPalette() {
  const writable = S.machine?.writable;
  render($('palette'), [
    ...S.kinds.map((kind) => html`<button type="button" class="pk-btn pk-btn--sm" data-add-kind="${kind.key}"
      title="${`Add a ${kind.title} state: ${kind.summary}`}" ${writable ? '' : 'disabled'}>${icon(kind.icon || 'square', { size: 'sm' })}${kind.title}</button>`),
    ...PSEUDO.map((p) => html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-add-type="${p.type}"
      title="${p.hint}" ${writable ? '' : 'disabled'}>${icon(p.icon, { size: 'sm' })}${p.title}</button>`),
  ]);
}

// ------------------------------------------------------------------ edits

/** One graph edit on the saved file; the answer is the machine as it is now. */
async function edit(op) {
  const m = S.machine;
  if (!m?.writable) {
    toast('This machine is read-only: it is not in a writable machine root.', { kind: 'warn' });
    return null;
  }
  if (hasDrafts() && !await confirm('The YAML tab has unsaved changes, and graph edits change the saved file. Discard the unsaved changes?',
    { danger: true, confirmLabel: 'Discard' })) {
    return null;
  }
  try {
    const next = await api(`${API}/machines/${enc(m.id)}/edit`, {
      method: 'POST', json: { op, expected_version: m.versions[m.root_file] }, quiet: true,
    });
    S.drafts = {};
    setDirty(false);
    showMachine(next);
    return next;
  } catch (error) {
    if (isAborted(error)) return null;
    if (error.status === 409) {
      const choice = await dialog({
        title: 'The file changed',
        message: 'The machine file changed since this panel loaded it (another editor, or an agent). Reload it and make the edit again.',
        actions: [{ label: 'Cancel', value: null }, { label: 'Reload', value: 'reload', primary: true }],
      });
      if (choice === 'reload') await openMachine(m.id, { keepRun: true, discard: true });  // agreed to above
    } else {
      toast(errorText(error), { kind: 'error' });
    }
    return null;
  }
}

/** A state name the author types, checked here so the dialog can say what is wrong. */
async function askName(message, value = '') {
  const name = await prompt(message, { title: 'State name', value, placeholder: 'lowercase_with_underscores' });
  if (name === null) return null;
  const trimmed = name.trim();
  if (!NAME.test(trimmed)) {
    toast(`"${trimmed}" is no state name: lowercase letters, digits and _, starting with a letter.`, { kind: 'warn' });
    return null;
  }
  if (stateOf(trimmed)) {
    toast(`A state "${trimmed}" exists already.`, { kind: 'warn' });
    return null;
  }
  return trimmed;
}

function freeName(base) {
  const stem = (base || 'state').replace(/[^a-z0-9_]/g, '_').replace(/^[^a-z]+/, '') || 'state';
  for (let n = 1; ; n += 1) {
    const name = n === 1 ? stem : `${stem}_${n}`;
    if (!stateOf(name)) return name;
  }
}

/** A do mapping that names the kind and has a slot for each required key, so the validator says what is missing. */
function activitySkeleton(kind) {
  const schema = kind.schema || {};
  const properties = schema.properties || {};
  const slot = (key) => {
    const spec = properties[key] || {};
    const type = spec.type || (spec.anyOf || []).map((option) => option.type).find((t) => t && t !== 'null');
    return type === 'object' ? {} : type === 'array' ? [] : type === 'integer' || type === 'number' ? 0 : '';
  };
  const out = { [kind.key]: slot(kind.key) };
  for (const key of schema.required || []) if (!(key in out)) out[key] = slot(key);
  return out;
}

/** New states go into the selected composite, else to the top level. */
function targetParent() {
  const selected = S.selection?.kind === 'state' ? stateOf(S.selection.id) : null;
  return selected?.composite ? selected.name : null;
}

async function addState({ kind = null, type = 'state' }) {
  const name = await askName(`Name of the new ${kind ? kind.title : type} state${targetParent() ? ` in ${targetParent()}` : ''}:`,
    freeName(kind ? kind.key : type));
  if (!name) return;
  const op = { op: 'add_state', name, type, parent: targetParent() };
  if (kind) op.do = activitySkeleton(kind);
  if (await edit(op)) choose({ kind: 'state', id: name });
}

async function renameState(old) {
  const name = await askName(`New name for ${old} (every transition to it and every initial naming it follows):`, old);
  if (!name || name === old) return;
  if (!await edit({ op: 'rename_state', old, new: name })) return;
  if (positions()[old]) {
    const moved = { ...positions(), [name]: positions()[old] };
    delete moved[old];
    S.machine.layout = { version: 1, positions: {} };
    await savePositions(moved);
  }
  choose({ kind: 'state', id: name });
}

async function removeState(name) {
  const state = stateOf(name);
  const incoming = S.machine.graph.transitions.filter((t) => t.target === name).length;
  const inner = S.machine.graph.states.filter((s) => s.parent === name).length;
  const message = [`Remove the state ${name}${inner ? ` with the states inside it` : ''}?`,
    incoming ? `${incoming} transition${incoming > 1 ? 's' : ''} into it go${incoming > 1 ? '' : 'es'} too.` : ''].join(' ');
  if (!state || !await confirm(message, { title: 'Remove state', danger: true, confirmLabel: 'Remove' })) return;
  if (await edit({ op: 'remove_state', name })) choose(null);
}

async function connect(source, target) {
  const before = S.machine.graph.transitions.filter((t) => t.source === source);
  const index = before.length ? Math.max(...before.map((t) => t.index)) + 1 : 0;
  if (await edit({ op: 'add_transition', source, target })) choose({ kind: 'transition', id: `${source}#${index}` });
}

async function removeTransition(id) {
  const t = transitionOf(id);
  if (!t || !await confirm(`Remove the transition ${t.source} → ${t.target ?? '(internal)'}?`,
    { title: 'Remove transition', danger: true, confirmLabel: 'Remove' })) return;
  if (await edit({ op: 'remove_transition', source: t.source, index: t.index })) choose({ kind: 'state', id: t.source });
}

// ------------------------------------------------------------------ selection and inspector

function choose(selection) {
  S.selection = selection;
  canvas.select(selection);
  if (selection?.kind === 'state') canvas.reveal(selection.id);
  if (selection) selectTab($('sideTabs'), 'inspect');
  drawInspector();
}

function problemList(problems) {
  if (!problems?.length) return '';
  return html`<div class="sg-problems">${problems.map((p) => html`
    <div class="pk-callout pk-callout--${p.level === 'error' ? 'danger' : 'warn'}"><strong>${p.code}</strong> ${p.message}
      ${p.path ? html`<div class="sg-problem-where">${p.path}${p.line ? `, line ${p.line}` : ''}</div>` : ''}</div>`)}</div>`;
}

function breakpointsFor(name) {
  return new Set(shownBreakpoints().filter((p) => p.state === name && p.enabled !== false && ofThisMachine(p))
    .map((p) => p.at || 'enter'));
}

/** The hooks that can stop in a state (the interpreter's, §6), and a line on why the others cannot. */
function hooksOf(state) {
  if (state.type === 'choice' || state.type === 'junction') {
    return { hooks: [], why: `A ${state.type} is decided within a transition: no hook stops there.` };
  }
  if (state.type === 'final') {
    return state.parent ? { hooks: ['exit'], why: 'A nested final completes its composite at once: it stops on exit only.' }
      : { hooks: [], why: 'The run ends as it enters a top-level final: no hook runs there.' };
  }
  if (state.composite) {
    return state.max_visits ? { hooks: ['error'], why: 'A composite stops in the states inside it; error, when its max_visits is exceeded.' }
      : { hooks: [], why: 'A composite stops in the states inside it: set breakpoints there.' };
  }
  return { hooks: ['enter', 'exit', 'error'], why: '' };
}

function transitionEditor(t) {
  const states = S.machine.graph.states.map((s) => s.name);
  const events = Object.keys(S.machine.graph.events || {});
  const triggers = [...new Set(['done', 'error', ...events, t.trigger])];
  const writable = S.machine.writable;
  const siblings = S.machine.graph.transitions.filter((other) => other.source === t.source);
  const pinned = S.problems.transitions[t.id];
  const current = S.selection?.kind === 'transition' && S.selection.id === t.id;
  return html`<form class="sg-transition" data-transition="${t.id}" aria-current="${String(current)}">
    <div class="sg-transition-head">
      <span class="pk-mono">#${t.index}</span>
      ${t.trigger !== 'done' ? badge(t.trigger, t.trigger === 'error' ? 'danger' : 'info') : ''}
      <span class="pk-mono">${t.source} → ${t.target ?? '(internal)'}</span>
      ${pinned?.errors ? badge(`${pinned.errors} err`, 'danger') : ''}
      <span class="pk-grow"></span>
      <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-act="move-up" title="Try it earlier" aria-label="Move up" ${!writable || t.index === Math.min(...siblings.map((s) => s.index)) ? 'disabled' : ''}>${icon('arrow-up', { size: 'sm' })}</button>
      <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-act="move-down" title="Try it later" aria-label="Move down" ${!writable || t.index === Math.max(...siblings.map((s) => s.index)) ? 'disabled' : ''}>${icon('arrow-down', { size: 'sm' })}</button>
      <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-act="remove-transition" title="Remove" aria-label="Remove transition" ${writable ? '' : 'disabled'}>${icon('trash-2', { size: 'sm' })}</button>
    </div>
    <div class="sg-fields">
      <label for="tr-trigger-${t.id}">Trigger</label>
      <select class="pk-select pk-select--sm" id="tr-trigger-${t.id}" name="trigger">${triggers.map((name) => html`<option value="${name}" ${name === t.trigger ? 'selected' : ''}>${name === 'done' ? 'done (completion)' : name}</option>`)}</select>
      <label for="tr-target-${t.id}">Target</label>
      <select class="pk-select pk-select--sm" id="tr-target-${t.id}" name="target"><option value="">(internal: no target)</option>${states.map((name) => html`<option value="${name}" ${name === t.target ? 'selected' : ''}>${name}</option>`)}</select>
      <label for="tr-guard-${t.id}">Guard</label>
      <input class="pk-input pk-input--sm pk-input--mono" id="tr-guard-${t.id}" name="guard" value="${t.guard ?? ''}" placeholder="Python expression, or else">
      <label for="tr-effect-${t.id}">Effect</label>
      <textarea class="pk-textarea pk-input--mono" id="tr-effect-${t.id}" name="effect" rows="2" placeholder="Python statements">${t.effect ?? ''}</textarea>
    </div>
    ${problemList(pinned?.problems)}
    <div class="pk-form-actions"><button type="submit" class="pk-btn pk-btn--sm pk-btn--primary" ${writable ? '' : 'disabled'}>${icon('save', { size: 'sm' })} Apply</button></div>
  </form>`;
}

function drawInspector() {
  const pane = $('side-inspect');
  const m = S.machine;
  if (!m) {
    render(pane, emptyState('workflow', 'Nothing open'));
    return;
  }
  const sel = S.selection;
  if (sel?.kind === 'transition' && transitionOf(sel.id)) {
    const t = transitionOf(sel.id);
    render(pane, html`<div class="sg-section">
      <div class="sg-inspect-head"><h3 class="sg-inspect-name">Transition</h3>
        <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-select-state="${t.source}">${icon('arrow-left', { size: 'sm' })} ${t.source}</button></div>
      ${transitionEditor(t)}
    </div>`);
    return;
  }
  const state = sel?.kind === 'state' ? stateOf(sel.id) : null;
  if (!state) {
    render(pane, machineOverview());
    return;
  }
  const pinned = S.problems.states[state.name];
  const outgoing = m.graph.transitions.filter((t) => t.source === state.name);
  const points = breakpointsFor(state.name);
  const { hooks, why } = hooksOf(state);
  const live = liveRun();
  const parentInitial = state.parent ? stateOf(state.parent)?.initial : m.graph.initial;
  const fragment = stateFragment(m.files[m.root_file], state.line);
  const lock = fragmentLock(m.files[m.root_file], state.line);
  const applies = m.writable && !lock;
  render(pane, html`
    <div class="sg-section">
      <div class="sg-inspect-head">
        ${state.icon ? icon(state.icon) : ''}<h3 class="sg-inspect-name">${state.name}</h3>
        ${badge(state.composite ? 'composite' : state.wait ? 'wait state' : state.type)}
        ${state.kind ? badge(state.kind, 'accent') : ''}
        ${parentInitial === state.name ? badge('initial', 'info') : ''}
      </div>
      ${state.label ? html`<div class="sg-mono">${state.label}</div>` : ''}
      ${state.description ? html`<div class="pk-help">${state.description}</div>` : ''}
      <div class="pk-row sg-actions">
        <button type="button" class="pk-btn pk-btn--sm" data-act="rename" ${m.writable ? '' : 'disabled'}>${icon('pencil', { size: 'sm' })} Rename</button>
        <button type="button" class="pk-btn pk-btn--sm" data-act="initial" ${m.writable && parentInitial !== state.name ? '' : 'disabled'} title="Make it the initial state of its region">${icon('play', { size: 'sm' })} Initial</button>
        <button type="button" class="pk-btn pk-btn--sm pk-btn--danger" data-act="remove" ${m.writable ? '' : 'disabled'}>${icon('trash-2', { size: 'sm' })} Remove</button>
      </div>
      ${problemList(pinned?.problems)}
    </div>
    <div class="sg-section">
      <h4 class="sg-section-title">Breakpoints ${live ? html`<span class="pk-badge pk-badge--info">run ${shorten(live.id, 12)}</span>` : html`<span class="pk-muted">(next run)</span>`}</h4>
      ${hooks.length ? html`<div class="pk-row sg-checks">
        ${hooks.map((at) => html`<label class="pk-check"><input type="checkbox" data-breakpoint="${at}" ${points.has(at) ? 'checked' : ''}> ${at}</label>`)}
      </div>` : ''}
      ${why ? html`<p class="pk-help">${why}</p>` : ''}
    </div>
    <div class="sg-section">
      <h4 class="sg-section-title">Transitions (tried in this order)</h4>
      ${outgoing.length ? outgoing.map(transitionEditor) : html`<p class="pk-help">None. Drag from the state's handle to another state to add one.</p>`}
      <form class="pk-row sg-inline" data-form="add-transition">
        <select class="pk-select pk-select--sm pk-grow" name="target" aria-label="Target of the new transition">${m.graph.states.map((s) => html`<option value="${s.name}">${s.name}</option>`)}</select>
        <button type="submit" class="pk-btn pk-btn--sm" ${m.writable ? '' : 'disabled'}>${icon('plus', { size: 'sm' })} Transition</button>
      </form>
    </div>
    <div class="sg-section">
      <h4 class="sg-section-title">YAML</h4>
      <form data-form="set-state" class="pk-stack">
        <textarea class="pk-textarea pk-input--mono sg-fragment" name="yaml" spellcheck="false" aria-label="The state's YAML" ${applies ? '' : 'readonly'}>${fragment}</textarea>
        ${lock ? html`<p class="pk-help">${lock}: edit it in the YAML tab.</p>` : ''}
        <div class="pk-form-actions"><button type="submit" class="pk-btn pk-btn--sm pk-btn--primary" ${applies ? '' : 'disabled'}>${icon('save', { size: 'sm' })} Apply</button></div>
      </form>
    </div>`);
}

function machineOverview() {
  const m = S.machine;
  const g = m.graph;
  const table = (entries, head) => (entries.length ? html`<table class="pk-table"><thead><tr>${head.map((h) => html`<th>${h}</th>`)}</tr></thead>
    <tbody>${entries}</tbody></table>` : html`<p class="pk-help">None.</p>`);
  return html`
    <div class="sg-section">
      <h3 class="sg-inspect-name">${g.title || m.id}</h3>
      ${g.description ? html`<p class="pk-help">${g.description}</p>` : ''}
      <dl class="pk-kv"><dt>initial</dt><dd class="pk-mono">${g.initial ?? '—'}</dd>
        <dt>states</dt><dd>${g.states.length}</dd><dt>transitions</dt><dd>${g.transitions.length}</dd></dl>
      <p class="pk-help">Click a state or a transition to edit it. New states from the bar above the graph go into the selected composite.</p>
    </div>
    ${S.problems.machine.length ? html`<div class="sg-section"><h4 class="sg-section-title">Problems of the machine</h4>${problemList(S.problems.machine)}</div>` : ''}
    <div class="sg-section"><h4 class="sg-section-title">Params</h4>
      ${table(Object.entries(g.params || {}).map(([name, p]) => html`<tr><td class="pk-mono">${name}${p.required ? ' *' : ''}</td><td>${p.type || 'any'}</td><td class="sg-mono">${p.default === undefined ? '' : preview(p.default, 40)}</td></tr>`), ['Name', 'Type', 'Default'])}</div>
    <div class="sg-section"><h4 class="sg-section-title">Events</h4>
      ${table(Object.entries(g.events || {}).map(([name, e]) => html`<tr><td class="pk-mono">${name}</td><td>${e.description || ''}</td></tr>`), ['Name', 'Description'])}</div>
    <div class="sg-section"><h4 class="sg-section-title">Context</h4>${jsonView(g.context || {})}</div>
    <div class="sg-section"><h4 class="sg-section-title">Imports</h4>
      ${table(Object.entries(g.imports || {}).map(([alias, ref]) => html`<tr><td class="pk-mono">${alias}</td>
        <td><button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-open-machine="${importedId(ref)}">${ref}</button></td></tr>`), ['Alias', 'Machine'])}</div>`;
}

function importedId(ref) {
  const base = String(ref).split('/').pop();
  return base.endsWith('.yaml') ? base.slice(0, -5) : base;
}

$('side-inspect').addEventListener('click', async (event) => {
  const target = event.target.closest('button, [data-select-state]');
  if (!target) return;
  const name = S.selection?.kind === 'state' ? S.selection.id : null;
  const act = target.dataset.act;
  if (target.dataset.selectState) return choose({ kind: 'state', id: target.dataset.selectState });
  if (target.dataset.openMachine) return openMachine(target.dataset.openMachine);
  if (act === 'rename' && name) return renameState(name);
  if (act === 'initial' && name) {
    const state = stateOf(name);
    return edit({ op: 'set_initial', name, parent: state?.parent ?? null });
  }
  if (act === 'remove' && name) return removeState(name);
  const form = target.closest('[data-transition]');
  if (!form) return;
  const t = transitionOf(form.dataset.transition);
  if (!t) return;
  if (act === 'remove-transition') return removeTransition(t.id);
  if (act === 'move-up' || act === 'move-down') {
    const siblings = S.machine.graph.transitions.filter((other) => other.source === t.source).map((other) => other.index);
    const at = siblings.indexOf(t.index);
    const to = siblings[at + (act === 'move-up' ? -1 : 1)];
    if (to === undefined) return;
    if (await edit({ op: 'move_transition', source: t.source, index: t.index, to })) {
      if (S.selection?.kind === 'transition') choose({ kind: 'transition', id: `${t.source}#${to}` });
    }
  }
});

$('side-inspect').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('[type="submit"]');
  await withBusy(button, async () => {
    if (form.dataset.form === 'set-state' && S.selection?.kind === 'state') {
      await edit({ op: 'set_state', name: S.selection.id, yaml: form.elements.yaml.value });
    } else if (form.dataset.form === 'add-transition' && S.selection?.kind === 'state') {
      await connect(S.selection.id, form.elements.target.value);
    } else if (form.dataset.transition) {
      const t = transitionOf(form.dataset.transition);
      if (!t) return;
      const value = (name) => form.elements[name].value.trim() || null;
      const trigger = value('trigger');
      const fields = { trigger: trigger === 'done' ? null : trigger, target: value('target'), guard: value('guard'),
        effect: form.elements.effect.value.replace(/\s+$/, '') || null };
      await edit({ op: 'update_transition', source: t.source, index: t.index, fields });
    }
  });
});

$('side-inspect').addEventListener('change', async (event) => {
  const box = event.target.closest('[data-breakpoint]');
  if (!box || S.selection?.kind !== 'state') return;
  await toggleBreakpoint(S.selection.id, box.dataset.breakpoint, box.checked);
});

/** A breakpoint of the open machine: it stops in that machine's frames only, not in a submachine's same-named state. */
async function toggleBreakpoint(name, at, on) {
  const kept = shownBreakpoints().filter((p) => !(p.state === name && (p.at || 'enter') === at && ofThisMachine(p)));
  const next = on ? [...kept, { state: name, at, machine: S.machine.id }] : kept;
  if (liveRun()) {
    await control('set_breakpoints', { breakpoints: next });
  } else {
    S.nextBreakpoints = next.map(({ state, at: hook, machine, condition }) => ({
      state, at: hook || 'enter', ...(machine ? { machine } : {}), ...(condition ? { condition } : {}) }));
    remember(`breakpoints:${S.machine.id}`, S.nextBreakpoints);
    drawStartForm();
    redrawOverlay();
  }
}

// ------------------------------------------------------------------ YAML tab

function yamlText(path) {
  return path in S.drafts ? S.drafts[path] : S.machine.files[path] ?? '';
}

function drawYaml() {
  const m = S.machine;
  const files = Object.keys(m.files);
  if (!files.includes(S.yamlFile)) S.yamlFile = m.root_file;
  render($('yamlFile'), files.map((path) => html`<option value="${path}" ${path === S.yamlFile ? 'selected' : ''}>${path}${path in S.drafts ? ' (unsaved)' : ''}</option>`));
  const area = $('yamlText');
  if (area.value !== yamlText(S.yamlFile)) area.value = yamlText(S.yamlFile);
  area.readOnly = !m.writable;
  $('yamlSave').disabled = !m.writable || !hasDrafts();
  $('yamlRevert').disabled = !hasDrafts();
  $('yamlState').textContent = !m.writable ? 'read-only' : hasDrafts() ? `${Object.keys(S.drafts).length} file(s) unsaved` : 'saved';
  $('yamlCount').textContent = hasDrafts() ? 'unsaved' : '';
  drawProblems($('yamlProblems'), m.problems, 'Saved file');
}

function drawProblems(element, problems, label) {
  if (!problems.length) {
    render(element, html`<div class="pk-callout pk-callout--ok">${label}: no problems.</div>`);
    return;
  }
  render(element, html`<div class="pk-help">${label}: ${problems.length} problem${problems.length > 1 ? 's' : ''}. Click one to go there.</div>
    ${problems.map((p, i) => html`<button type="button" class="pk-btn pk-btn--ghost sg-problem" data-problem="${i}">
      ${badge(p.code, p.level === 'error' ? 'danger' : 'warn')}<span class="pk-grow">${p.message}
      <span class="sg-problem-where">${[p.file?.split('/').pop(), p.line && `line ${p.line}`, p.path].filter(Boolean).join(' · ')}</span></span></button>`)}`);
  element.problems = problems;
}

$('yamlFile').addEventListener('change', () => {
  S.yamlFile = $('yamlFile').value;
  $('yamlText').value = yamlText(S.yamlFile);
});

$('yamlText').addEventListener('input', () => {
  const text = $('yamlText').value;
  if (text === S.machine.files[S.yamlFile]) delete S.drafts[S.yamlFile];
  else S.drafts[S.yamlFile] = text;
  setDirty(hasDrafts());
  $('yamlSave').disabled = !S.machine.writable || !hasDrafts();
  $('yamlRevert').disabled = !hasDrafts();
  $('yamlState').textContent = hasDrafts() ? `${Object.keys(S.drafts).length} file(s) unsaved` : 'saved';
  $('yamlCount').textContent = hasDrafts() ? 'unsaved' : '';
});

$('yamlText').addEventListener('keydown', (event) => {
  if (event.key !== 'Tab' || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
  event.preventDefault();  // YAML is indented with spaces: Tab types two of them
  const area = event.target;
  area.setRangeText('  ', area.selectionStart, area.selectionEnd, 'end');
  area.dispatchEvent(new Event('input'));
});

$('yamlProblems').addEventListener('click', (event) => {
  const row = event.target.closest('[data-problem]');
  if (row) goToProblem($('yamlProblems').problems[Number(row.dataset.problem)]);
});

function goToProblem(problem) {
  const index = problemIndex(S.machine.graph, [problem], S.machine.file || S.machine.root_file);
  const state = Object.keys(index.states)[0];
  const transition = Object.keys(index.transitions)[0];
  if (state || transition) {
    selectTab($('mainTabs'), 'graph');
    onTab('graph');
    choose(transition ? { kind: 'transition', id: transition } : { kind: 'state', id: state });
    return;
  }
  const path = Object.keys(S.machine.files).find((p) => problem.file && (problem.file === p || problem.file.endsWith(`/${p}`)));
  if (path && problem.line) {
    S.yamlFile = path;
    drawYaml();
    const area = $('yamlText');
    const lines = area.value.split('\n');
    const at = lines.slice(0, problem.line - 1).reduce((sum, line) => sum + line.length + 1, 0);
    area.focus();
    area.setSelectionRange(at, at + (lines[problem.line - 1] || '').length);
  }
}

function allFiles() {
  return { ...S.machine.files, ...S.drafts };
}

$('yamlValidate').addEventListener('click', () => withBusy($('yamlValidate'), async () => {
  try {
    const result = await api(`${API}/validate`, { method: 'POST', json: { files: allFiles(), machine_id: S.machine.id } });
    drawProblems($('yamlProblems'), result.problems, hasDrafts() ? 'Unsaved text' : 'Saved file');
  } catch (error) { /* toasted */ }
}));

$('yamlRevert').addEventListener('click', async () => {
  if (!await confirm('Discard every unsaved change in the YAML tab?', { danger: true, confirmLabel: 'Discard' })) return;
  S.drafts = {};
  setDirty(false);
  drawYaml();
});

$('yamlSave').addEventListener('click', () => withBusy($('yamlSave'), () => saveYaml(false)));

async function saveYaml(force) {
  const m = S.machine;
  const files = { ...S.drafts };
  const expected = Object.fromEntries(Object.keys(files).filter((p) => p in m.versions).map((p) => [p, m.versions[p]]));
  try {
    await api(`${API}/machines/${enc(m.id)}`, { method: 'PUT', json: { files, expected_versions: expected, force }, quiet: true });
  } catch (error) {
    if (isAborted(error)) return;
    if (error.status === 409) {
      const choice = await dialog({
        title: 'The file changed',
        message: `${errorText(error)}. Reload shows the file as it is now, and your unsaved text is gone. Cancel keeps your text, so you can copy it first.`,
        actions: [{ label: 'Cancel', value: null }, { label: 'Reload', value: 'reload', danger: true }],
      });
      if (choice === 'reload') {
        S.drafts = {};
        setDirty(false);
        await openMachine(m.id, { keepRun: true, discard: true });
      }
    } else if (error.status === 422 && !force) {
      const choice = await dialog({
        title: 'Not saved',
        message: `${errorText(error)}. A machine with errors cannot run. Save it anyway, to fix it later?`,
        actions: [{ label: 'Cancel', value: null }, { label: 'Save anyway', value: 'force', danger: true }],
      });
      if (choice === 'force') await saveYaml(true);
    } else {
      toast(errorText(error), { kind: 'error' });
    }
    return;
  }
  S.drafts = {};
  setDirty(false);
  toast('Saved', { kind: 'ok' });
  await openMachine(m.id, { keepRun: true, discard: true });
  loadMachines();
}

// ------------------------------------------------------------------ runs: start

function paramField(name, p) {
  const id = `param-${name}`;
  const label = html`<span class="pk-label">${name}${p.required ? ' *' : ''} <span class="pk-muted">${p.type || 'any'}</span></span>`;
  const help = p.description ? html`<span class="pk-help">${p.description}</span>` : '';
  const value = p.default ?? '';
  if (Array.isArray(p.enum)) {
    return html`<label class="pk-field">${label}<select class="pk-select pk-select--sm" id="${id}" data-param="${name}" data-type="enum">
      ${p.required ? '' : html`<option value="">(default)</option>`}
      ${p.enum.map((option, i) => html`<option value="${i}" ${JSON.stringify(option) === JSON.stringify(p.default) ? 'selected' : ''}>${preview(option, 60)}</option>`)}</select>${help}</label>`;
  }
  if (p.type === 'boolean') {
    return html`<label class="pk-check"><input type="checkbox" id="${id}" data-param="${name}" data-type="boolean" ${p.default ? 'checked' : ''}> ${name} ${help}</label>`;
  }
  if (p.type === 'integer' || p.type === 'number') {
    return html`<label class="pk-field">${label}<input class="pk-input pk-input--sm" type="number" id="${id}" data-param="${name}" data-type="${p.type}" value="${value}" ${p.type === 'integer' ? 'step="1"' : 'step="any"'}>${help}</label>`;
  }
  if (p.type === 'string') {
    return html`<label class="pk-field">${label}<textarea class="pk-textarea" rows="2" id="${id}" data-param="${name}" data-type="string">${value}</textarea>${help}</label>`;
  }
  return html`<label class="pk-field">${label}<textarea class="pk-textarea pk-input--mono" rows="2" id="${id}" data-param="${name}" data-type="json" placeholder="JSON">${p.default === undefined || p.default === null ? '' : JSON.stringify(p.default)}</textarea>${help}</label>`;
}

function drawStartForm() {
  const g = S.machine.graph;
  const params = Object.entries(g.params || {});
  const fields = $('paramFields');
  const signature = JSON.stringify(g.params || {});
  if (fields.dataset.signature !== signature) {  // typed values stay while the machine's params stay the same
    fields.dataset.signature = signature;
    render(fields, params.length ? params.map(([name, p]) => paramField(name, p)) : html`<p class="pk-help">This machine takes no params.</p>`);
  }
  const points = S.nextBreakpoints.map((p) => `${p.state}@${p.at || 'enter'}`);
  $('nextPoints').textContent = [points.length ? `breakpoints: ${points.join(', ')}` : '',
    S.nextWatch.length ? `watching: ${S.nextWatch.join(', ')}` : ''].filter(Boolean).join(' · ');
}

function readParams() {
  const params = {};
  for (const input of $('paramFields').querySelectorAll('[data-param]')) {
    const name = input.dataset.param;
    const spec = S.machine.graph.params[name] || {};
    const type = input.dataset.type;
    if (type === 'boolean') {
      params[name] = input.checked;
      continue;
    }
    const raw = input.value;
    if (raw === '') {
      if (spec.required) throw new Error(`${name} is required`);
      continue;
    }
    if (type === 'enum') params[name] = spec.enum[Number(raw)];
    else if (type === 'integer' || type === 'number') {
      const number = Number(raw);
      if (!Number.isFinite(number) || (type === 'integer' && !Number.isInteger(number))) throw new Error(`${name}: ${type} expected`);
      params[name] = number;
    } else if (type === 'json') {
      try { params[name] = JSON.parse(raw); } catch { throw new Error(`${name}: not valid JSON`); }
    } else params[name] = raw;
  }
  return params;
}

$('startForm').addEventListener('submit', (event) => {
  event.preventDefault();
  withBusy($('startRun'), async () => {
    const error = $('startError');
    let params;
    let mocks;
    try {
      params = readParams();
      const text = $('mocks').value.trim();
      mocks = text ? JSON.parse(text) : null;
      if (mocks !== null && (typeof mocks !== 'object' || Array.isArray(mocks))) throw new Error('mocks: a JSON object {state path: out}');
    } catch (problem) {
      notice(error, problem instanceof SyntaxError ? `mocks: not valid JSON (${problem.message})` : problem.message);
      return;
    }
    notice(error, '');
    remember(`mocks:${S.machine.id}`, $('mocks').value);
    try {
      const started = await api(`${API}/runs`, {
        method: 'POST',
        json: {
          machine_id: S.machine.id, params, mocks, mock_only: $('mockOnly').checked, pause_at_start: $('pauseAtStart').checked,
          breakpoints: S.nextBreakpoints, watchpoints: S.nextWatch.map((expr) => ({ expr, machine: S.machine.id })),
        },
        quiet: true,
      });
      await loadRuns();
      selectRun(started.run_id);
      selectTab($('sideTabs'), 'debug');
    } catch (failure) {
      if (!isAborted(failure)) notice(error, errorText(failure));
    }
  });
});

// ------------------------------------------------------------------ runs: list, selection, polling

async function loadRuns() {
  if (!S.machine) return;
  try {
    S.runs = await api(`${API}/runs?machine_id=${enc(S.machine.id)}&limit=50`, { latest: 'runs', quiet: true });
  } catch (error) {
    if (!isAborted(error)) update($('runList'), emptyState('circle-alert', 'Runs could not be loaded', errorText(error)));
    return;
  }
  $('runCount').textContent = S.runs.length ? String(S.runs.length) : '';
  drawRunList();
}

function drawRunList() {
  update($('runList'), S.runs.length ? html`<div class="pk-table-wrap"><table class="pk-table" data-pk-sort="runs" data-pk-select>
    <thead><tr><th>Run</th><th>Status</th><th>State</th><th aria-sort="descending">Started</th><th>Ended</th><th>By</th></tr></thead>
    <tbody>${S.runs.map((r) => html`<tr data-id="${r.id}" tabindex="0" aria-selected="${String(r.id === S.runId)}">
      <td class="pk-mono">${shorten(r.id, 14)}${r.parent_run ? html` <span class="pk-muted" title="${`forked from ${r.parent_run} at step ${r.fork_step}`}">fork</span>` : ''}</td>
      <td data-sort-value="${r.status}">${statusBadge(r.status)}</td>
      <td class="pk-mono">${r.final_state || ''}</td>
      <td data-sort-value="${r.created_at || ''}">${localTime(r.created_at, { seconds: true })}</td>
      <td data-sort-value="${r.finished_at || ''}">${r.finished_at ? localTime(r.finished_at, { seconds: true }) : ''}</td>
      <td>${r.user_id || ''}</td></tr>`)}</tbody></table></div>`
    : emptyState('play', 'No runs yet', 'Start one above.'));
}

$('runList').addEventListener('rowselect', (event) => selectRun(event.detail.id));

const poller = autoRefresh(() => loadRun({ tick: true }), 1000);
let runRequests = 0;  // GETs of the selected run that are out

function selectRun(id) {
  S.runId = id || null;
  S.run = null;
  S.evaluation = null;
  poller.stop();
  if (S.machine) setQuery(S.runId ? { machine: S.machine.id, run: S.runId } : { machine: S.machine.id });
  drawRunList();
  if (!S.runId) {
    drawRun();
    return;
  }
  loadRun();
}

/**
 * The selected run from the server. A poll tick while a GET is out is skipped, so an answer slower than the tick is
 * never aborted by the tick after it; a load the author starts (a run picked, a command sent) replaces the one out.
 */
async function loadRun({ tick = false } = {}) {
  const id = S.runId;
  if (!id || (tick && runRequests)) return;
  let run;
  runRequests += 1;
  try {
    run = await api(`${API}/runs/${enc(id)}?steps=200`, { latest: 'run', quiet: true });
  } catch (error) {
    if (isAborted(error)) return;
    poller.stop();
    if (error.status === 404) selectRun(null);
    else toast(`Run not refreshed: ${errorText(error)}`, { kind: 'warn' });
    return;
  } finally {
    runRequests -= 1;
  }
  if (id !== S.runId) return;
  const before = S.run?.status;
  S.run = run;
  if (!TERMINAL.has(run.status) && run.status !== 'interrupted') poller.start();
  else poller.stop();
  if (before && before !== run.status) {
    const listed = S.runs.find((r) => r.id === run.id);
    if (listed) Object.assign(listed, { status: run.status, final_state: run.final_state, finished_at: run.finished_at });
    drawRunList();
  }
  drawRun();
}

function drawRun() {
  drawDebugBar();
  drawDebugPane();
  drawHistory();
  redrawOverlay();
  if (S.selection?.kind === 'state') {
    // the breakpoint boxes follow the run; the rest of the inspector stays as the author left it
    const points = breakpointsFor(S.selection.id);
    for (const box of $('side-inspect').querySelectorAll('[data-breakpoint]')) box.checked = points.has(box.dataset.breakpoint);
  }
}

// ------------------------------------------------------------------ runs: debug bar and controls

function drawDebugBar() {
  const bar = $('debugBar');
  const run = S.run;
  if (!run) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  bar.dataset.status = run.status;
  const paused = run.debug?.paused;
  const live = liveRun();
  // run_to stops on a state's enter hook: offer only states that have one
  const states = (S.machine?.graph?.states || []).filter((s) => hooksOf(s).hooks.includes('enter')).map((s) => s.name);
  const can = {
    pause: live && run.status === 'running',
    resume: live && run.status === 'paused',
    runTo: live && ['running', 'paused'].includes(run.status),
    terminate: live && !TERMINAL.has(run.status),
    restart: run.status === 'interrupted',
  };
  update(bar, html`
    <strong class="pk-mono" title="${run.id}">${shorten(run.id, 16)}</strong> ${statusBadge(run.status)}
    ${run.machine_id !== S.machine?.id ? badge(`machine ${run.machine_id}`, 'warn') : ''}
    ${paused ? html`<span title="${paused.reason}">paused at <span class="pk-mono">${paused.state ?? '—'}</span> (${paused.hook}${paused.frame ? `, frame ${paused.frame}` : ''})</span>` : ''}
    ${!paused && run.final_state ? html`<span>ended in <span class="pk-mono">${run.final_state}</span></span>` : ''}
    ${!live && !TERMINAL.has(run.status) && run.status !== 'interrupted' ? html`<span class="pk-muted" title="Runs of other processes are shown from their journal; they cannot be paused from here">not in this process</span>` : ''}
    <span class="pk-grow"></span>
    <button type="button" class="pk-btn pk-btn--sm" data-control="continue" ${can.resume ? '' : 'disabled'} title="Continue">${icon('play', { size: 'sm' })} Continue</button>
    <button type="button" class="pk-btn pk-btn--sm" data-control="step" ${can.resume ? '' : 'disabled'} title="Run to the next hook">${icon('step-forward', { size: 'sm' })} Step</button>
    <button type="button" class="pk-btn pk-btn--sm" data-control="pause" ${can.pause ? '' : 'disabled'} title="Pause at the next hook">${icon('pause', { size: 'sm' })} Pause</button>
    <select class="pk-select pk-select--sm" id="runToState" aria-label="State to run to" ${can.runTo ? '' : 'disabled'}>${states.map((name) => html`<option value="${name}">${name}</option>`)}</select>
    <button type="button" class="pk-btn pk-btn--sm" data-control="run_to" ${can.runTo ? '' : 'disabled'} title="Run until the chosen state is entered">${icon('crosshair', { size: 'sm' })} Run to</button>
    <button type="button" class="pk-btn pk-btn--sm pk-btn--danger" data-control="terminate" ${can.terminate ? '' : 'disabled'}>${icon('square', { size: 'sm' })} Terminate</button>
    ${can.restart ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--primary" data-control="resume" title="Resume the interrupted run: finished activities are replayed, not repeated">${icon('rotate-ccw', { size: 'sm' })} Resume</button>` : ''}
    <input class="pk-input pk-input--sm sg-step-input" type="number" min="0" id="forkStep" aria-label="Top-level step to fork from" placeholder="step">
    <button type="button" class="pk-btn pk-btn--sm" data-control="fork" title="A new run from this top-level step (current definition with Shift)">${icon('git-branch', { size: 'sm' })} Fork</button>
    <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-control="close" title="Stop showing this run" aria-label="Stop showing this run">${icon('x', { size: 'sm' })}</button>`);
}

$('debugBar').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-control]');
  if (!button || button.disabled) return;
  const action = button.dataset.control;
  if (action === 'close') return selectRun(null);
  if (action === 'terminate' && !await confirm('Terminate the run? A running agent call is cancelled.', { danger: true, confirmLabel: 'Terminate' })) return;
  const extra = {};
  if (action === 'run_to') Object.assign(extra, { state: $('runToState').value, machine: S.machine?.id });
  if (action === 'fork') {
    const step = Number($('forkStep').value);
    if (!$('forkStep').value || !Number.isInteger(step) || step < 0) {
      toast('Fork: name the top-level step to fork from.', { kind: 'warn' });
      return;
    }
    extra.at_step = step;
    extra.definition = event.shiftKey ? 'current' : 'snapshot';
  }
  await withBusy(button, () => control(action, extra));
});

async function control(action, extra = {}) {
  const id = S.runId;
  if (!id) return null;
  try {
    const answer = await api(`${API}/runs/${enc(id)}/control`, { method: 'POST', json: { action, ...extra } });
    if (action === 'fork' && answer.run_id) {
      toast(`Forked as ${answer.run_id}`, { kind: 'ok' });
      await loadRuns();
      selectRun(answer.run_id);
      return answer;
    }
    if (action === 'evaluate' || action === 'set') return answer;
    if (answer && answer.id === id) {
      S.run = answer;
      if (!TERMINAL.has(answer.status)) poller.start();
      drawRun();
    } else {
      await loadRun();
    }
    return answer;
  } catch (error) {
    return null;  // toasted
  }
}

// ------------------------------------------------------------------ runs: debug pane

/** The debug pane's forms are part of the page; a poll redraws only the lists and values beside them. */
function drawDebugPane() {
  const run = S.run;
  const live = liveRun();
  const debug = run?.debug || {};
  const paused = Boolean(live && debug.paused);
  const frames = run?.view?.frames || [];
  update($('dbgRun'), run ? html`<div class="sg-section">
      <div class="sg-inspect-head"><h3 class="sg-inspect-name">Run</h3>${statusBadge(run.status)}
        <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-act="copy-run" title="Copy the run id" aria-label="Copy the run id">${icon('copy', { size: 'sm' })}</button></div>
      <dl class="pk-kv"><dt>id</dt><dd class="pk-mono">${run.id}</dd>
        ${run.params && Object.keys(run.params).length ? html`<dt>params</dt><dd>${jsonView(run.params)}</dd>` : ''}
        ${debug.paused ? html`<dt>paused</dt><dd>${debug.paused.reason}</dd>` : ''}
        ${debug.paused?.out !== undefined && debug.paused?.out !== null ? html`<dt>out</dt><dd>${jsonView(debug.paused.out)}</dd>` : ''}
        ${debug.paused?.error ? html`<dt>error</dt><dd>${jsonView(debug.paused.error)}</dd>` : ''}
        ${run.output !== undefined && run.output !== null ? html`<dt>output</dt><dd>${jsonView(run.output)}</dd>` : ''}
        ${run.error ? html`<dt>error</dt><dd class="pk-text--danger">${jsonView(run.error)}</dd>` : ''}
        ${run.view?.inbox?.length ? html`<dt>inbox</dt><dd>${run.view.inbox.map((e) => e.name).join(', ')}</dd>` : ''}
      </dl></div>`
    : emptyState('bug', 'No run selected', 'Start a run in the Runs tab, or pick one there.'));

  // watch: the live run's watchpoints, else the list the next run starts with
  const watching = run ? (debug.watchpoints || []) : S.nextWatch.map((expr) => ({ expr }));
  const values = debug.watch || {};
  const editable = !run || Boolean(live);
  $('dbgWatchScope').textContent = run ? (live ? '' : '(this run cannot be changed from here)') : '(next run)';
  update($('dbgWatch'), watching.length ? html`<ul class="sg-plain-list">${watching.map((p, i) => {
    const seen = values[p.id];
    return html`<li class="sg-watch"><span class="sg-watch-expr">${otherMachine(p)}${p.expr}</span>
      <span class="pk-grow">${seen ? ('error' in seen ? html`<span class="pk-text--danger">${seen.error}</span>`
        : html`<span class="pk-mono">${preview(seen.value, 120)}</span>`) : ''}${seen ? html` <span class="pk-muted">(${seen.frame}, step ${seen.step})</span>` : ''}</span>
      ${editable ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-drop-watch="${i}" aria-label="Stop watching">${icon('x', { size: 'sm' })}</button>` : ''}</li>`;
  })}</ul>` : html`<p class="pk-help">A watch pauses the run when its value changes.</p>`);
  for (const control of $('watchForm').elements) control.disabled = !editable;

  const points = run ? (debug.breakpoints || []) : S.nextBreakpoints;
  $('dbgPointsScope').textContent = run ? '' : '(next run)';
  update($('dbgPoints'), points.length ? html`<ul class="sg-plain-list">${points.map((p, i) => html`<li class="sg-watch">
      <span class="sg-watch-expr">${otherMachine(p)}${p.state}@${p.at || 'enter'}</span>${p.condition ? html`<span class="pk-mono">if ${p.condition}</span>` : ''}
      <span class="pk-grow"></span>${editable ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-drop-breakpoint="${i}" aria-label="Remove breakpoint">${icon('x', { size: 'sm' })}</button>` : ''}</li>`)}</ul>`
    : html`<p class="pk-help">None. Set them on a state in the inspector.</p>`);

  $('dbgEvalHint').textContent = paused ? '' : '(while paused)';
  for (const form of [$('evalForm'), $('setForm')]) for (const control of form.elements) control.disabled = !paused;
  update($('dbgEval'), S.evaluation ? html`<div class="sg-frame"><span class="pk-mono">${S.evaluation.expr}</span>${'error' in S.evaluation
    ? html`<span class="pk-text--danger">${S.evaluation.error}</span>` : jsonView(S.evaluation.value)}</div>` : '');

  const events = Object.keys(S.machine?.graph?.events || {});
  $('eventSection').hidden = !events.length;
  const accepted = new Set((run?.accepts || []).flatMap((a) => a.events || []));
  update($('eventForm').elements.name, events.map((name) => html`<option value="${name}">${name}${accepted.has(name) ? ' (accepted now)' : ''}</option>`));
  update($('eventFrame'), html`<option value="">any frame</option>${frames.map((f) => html`<option value="${f.prefix}">${f.prefix || 'top'}</option>`)}`);
  for (const control of $('eventForm').elements) control.disabled = !run || TERMINAL.has(run.status);

  update($('dbgFrames'), frames.length ? frames.map((f, i) => html`<div class="sg-frame">
      <div class="sg-frame-head">${badge(f.prefix ? 'submachine' : 'top', f.prefix ? 'info' : '')}<span class="pk-mono">${f.machine}</span>
        ${f.path ? html`<span class="pk-muted">under ${f.path}</span>` : ''}<span class="pk-grow"></span>
        <span class="pk-mono">${f.state ?? '—'}</span><span class="pk-muted">step ${f.step}</span></div>
      <details class="pk-details" ${i === 0 ? 'open' : ''}><summary>ctx</summary>${jsonView(f.ctx ?? {})}</details>
      ${f.visits && Object.keys(f.visits).length ? html`<div class="pk-help">visits: ${Object.entries(f.visits).map(([n, v]) => `${n} ×${v}`).join(', ')}</div>` : ''}
    </div>`) : html`<p class="pk-help">${run ? 'No live frames: the run has not started or has ended.' : 'Frames show while a run is selected.'}</p>`);
}

$('side-debug').addEventListener('click', async (event) => {
  const target = event.target.closest('button');
  if (!target) return;
  if (target.dataset.act === 'copy-run' && S.run) return copyText(S.run.id);
  if (target.dataset.dropBreakpoint !== undefined) {
    const index = Number(target.dataset.dropBreakpoint);
    if (!S.run) {
      S.nextBreakpoints = S.nextBreakpoints.filter((_, i) => i !== index);
      remember(`breakpoints:${S.machine?.id}`, S.nextBreakpoints);
      drawStartForm();
      drawDebugPane();
      redrawOverlay();
      return;
    }
    return control('set_breakpoints', { breakpoints: (S.run.debug?.breakpoints || []).filter((_, i) => i !== index) });
  }
  if (target.dataset.dropWatch !== undefined) {
    const index = Number(target.dataset.dropWatch);
    if (!S.run) {
      S.nextWatch = S.nextWatch.filter((_, i) => i !== index);
      remember(`watch:${S.machine?.id}`, S.nextWatch);
      drawStartForm();
      drawDebugPane();
      return;
    }
    return control('set_watchpoints', { watchpoints: (S.run.debug?.watchpoints || []).filter((_, i) => i !== index) });
  }
});

function onSubmit(form, fn) {
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    withBusy(form.querySelector('[type="submit"]'), () => fn(form.elements));
  });
}

onSubmit($('watchForm'), async (fields) => {
  const expr = fields.expr.value.trim();
  if (!expr) return;
  if (!S.run) {
    S.nextWatch = [...S.nextWatch, expr];
    remember(`watch:${S.machine?.id}`, S.nextWatch);
    fields.expr.value = '';
    drawStartForm();
    drawDebugPane();
    return;
  }
  const watchpoints = [...(S.run.debug?.watchpoints || []), { expr, machine: S.machine?.id }];
  if (await control('set_watchpoints', { watchpoints })) fields.expr.value = '';
});

onSubmit($('evalForm'), async (fields) => {
  const expr = fields.expr.value.trim();
  if (!expr) return;
  const answer = await control('evaluate', { expr });
  S.evaluation = answer ? { expr, value: answer.value } : { expr, error: 'not evaluated (see the message)' };
  drawDebugPane();
});

onSubmit($('setForm'), async (fields) => {
  const path = fields.path.value.trim();
  const expr = fields.expr.value.trim();
  if (!path || !expr) {
    toast('Set: a path (ctx.round) and an expression', { kind: 'warn' });
    return;
  }
  const answer = await control('set', { path, expr });
  if (!answer) return;
  S.evaluation = { expr: `${path} = ${expr}`, value: answer.value };
  await loadRun();
});

onSubmit($('eventForm'), async (fields) => {
  let data = null;
  const text = fields.data.value.trim();
  if (text) {
    try { data = JSON.parse(text); } catch {
      toast('Event data: not valid JSON', { kind: 'warn' });
      return;
    }
  }
  const name = fields.name.value;
  try {
    const answer = await api(`${API}/runs/${enc(S.runId)}/events`, {
      method: 'POST', json: { name, data, frame: fields.frame.value || null },
    });
    toast(answer.accepted ? `${name} accepted${answer.frame ? ` by frame ${answer.frame}` : ''}`
      : `${name} not taken now: ${answer.reason || 'it waits in the inbox until a frame accepts it'}`, { kind: answer.accepted ? 'ok' : 'warn' });
    await loadRun();
  } catch (error) { /* toasted */ }
});

// ------------------------------------------------------------------ runs: history

const WHAT = { activity: 'activity', trace: 'trace', event: 'event', edit: 'edit', timer: 'timer' };

function historyRow(row) {
  const data = row.data || {};
  let what = row.status || '';
  let detail = '';
  let kind = '';
  if (row.kind === 'activity') {
    what = `${data.kind || 'activity'} ${row.status}`;
    kind = row.status === 'error' ? 'danger' : row.status === 'done' ? 'ok' : 'info';
    detail = row.status === 'error' ? `${data.error?.type || 'error'}: ${data.error?.message || ''}` : preview(data.out);
    if (data.meta?.mocked) what += ' (mocked)';
  } else if (row.kind === 'trace') {
    if (row.status === 'transition') detail = `${data.from} → ${data.to ?? '(internal)'} on ${data.event}`;
    else if (row.status === 'paused') detail = data.reason || '';
    else if (row.status === 'failed') detail = `${data.type || ''} ${data.message || ''}`;
    else if (row.status === 'final') detail = data.status || '';
    kind = row.status === 'failed' ? 'danger' : row.status === 'paused' ? 'warn' : '';
  } else if (row.kind === 'event') {
    what = `event ${row.status || ''}`;
    detail = `${data.name || ''} ${preview(data.data, 60)}`;
  } else if (row.kind === 'edit') {
    detail = `${data.path} = ${preview(data.value, 60)}`;
  }
  const duration = data.meta?.duration_s;
  return html`<tr>
    <td class="pk-num" data-sort-value="${row.seq}">${row.seq}</td>
    <td class="pk-mono">${row.key}</td>
    <td>${WHAT[row.kind] || row.kind}</td>
    <td>${kind ? html`<span class="pk-text--${kind}">${what}</span>` : what}</td>
    <td class="pk-mono">${row.state || ''}</td>
    <td class="pk-num" data-sort-value="${duration ?? ''}">${duration === undefined || duration === null ? '' : `${duration.toFixed(2)} s`}</td>
    <td class="sg-mono sg-preview" title="${detail}">${detail}</td>
  </tr>`;
}

function drawHistory() {
  const rows = S.run?.journal || [];
  update($('runHistory'), S.run ? html`<div class="pk-card">
    <div class="pk-card-head"><h3 class="pk-card-title">History of ${shorten(S.run.id, 16)}</h3><span class="pk-muted">the last ${rows.length} journal rows</span></div>
    ${rows.length ? html`<div class="pk-table-wrap"><table class="pk-table" data-pk-sort="history">
      <thead><tr><th class="pk-num" aria-sort="descending">#</th><th>Key</th><th>Row</th><th>What</th><th>State</th><th class="pk-num">Took</th><th>Detail</th></tr></thead>
      <tbody>${rows.map(historyRow)}</tbody></table></div>` : html`<p class="pk-help">Nothing journaled yet.</p>`}
  </div>` : '');
}

// ------------------------------------------------------------------ tabs, toolbar, keys

function onTab(tab) {
  if (tab === 'graph' && S.machine) {
    requestAnimationFrame(() => {
      if (fitPending) {
        fitPending = false;
        canvas.fit();
      }
    });
  }
  if (tab === 'runs') loadRuns();
}

$('mainTabs').addEventListener('tabchange', (event) => onTab(event.detail.tab));
$('sideTabs').addEventListener('tabchange', (event) => { if (event.detail.tab === 'debug') drawDebugPane(); });

$('machineList').addEventListener('click', (event) => {
  const item = event.target.closest('[data-machine]');
  if (item) openMachine(item.dataset.machine);
});
$('search').addEventListener('input', drawMachineList);

$('newMachine').addEventListener('click', async () => {
  const id = await prompt('Id of the new machine (it is saved as <id>.yaml in the writable machine root):',
    { title: 'New machine', placeholder: 'review_loop' });
  if (id === null) return;
  const trimmed = id.trim();
  if (!NAME.test(trimmed)) {
    toast(`"${trimmed}" is no machine id: lowercase letters, digits and _, starting with a letter.`, { kind: 'warn' });
    return;
  }
  try {
    const machine = await api(`${API}/machines`, { method: 'POST', json: { id: trimmed } });
    await loadMachines();
    await openMachine(machine.id);
  } catch (error) { /* toasted */ }
});

$('machineHead').addEventListener('click', (event) => {
  if (event.target.closest('[data-act="copy-id"]') && S.machine) copyText(S.machine.id);
});

$('palette').addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.addKind) addState({ kind: S.kinds.find((k) => k.key === button.dataset.addKind), type: 'state' });
  else if (button.dataset.addType) addState({ type: button.dataset.addType });
});

$('zoomIn').addEventListener('click', () => canvas.zoom(1.2));
$('zoomOut').addEventListener('click', () => canvas.zoom(1 / 1.2));
$('fit').addEventListener('click', () => canvas.fit());
$('autoLayout').addEventListener('click', async () => {
  if (!S.machine) return;
  S.machine.layout = { version: 1, positions: {} };
  await savePositions({});
  await drawGraph({ fit: true });
});

$('canvas').addEventListener('keydown', (event) => {
  if (event.target.closest('input, textarea, select')) return;
  if (event.key === 'Escape') choose(null);
  if ((event.key === 'Delete' || event.key === 'Backspace') && S.selection) {
    event.preventDefault();
    if (S.selection.kind === 'state') removeState(S.selection.id);
    else removeTransition(S.selection.id);
  }
});

document.addEventListener('refresh', async (event) => {
  await loadMachines();
  if (S.machine && !hasDrafts()) {
    try {
      // a name of its own: under openMachine's, this reload of the open machine would abort a click on another one
      const machine = await api(`${API}/machines/${enc(S.machine.id)}`, { latest: 'machine-refresh', quiet: true });
      const same = machine.id === S.machine?.id && !hasDrafts();  // no other machine opened, no text typed meanwhile
      if (same && JSON.stringify(machine.versions) !== JSON.stringify(S.machine.versions)) showMachine(machine);
    } catch (error) {
      if (!isAborted(error) && !event.detail?.auto) toast(`Machine not refreshed: ${errorText(error)}`, { kind: 'warn' });
    }
  }
  await loadRuns();
  if (S.runId) await loadRun();
});

// ------------------------------------------------------------------ start

async function start() {
  try {
    S.kinds = await api(`${API}/kinds`, { quiet: true });
  } catch (error) {
    S.kinds = [];  // the palette then offers the pseudostates only
  }
  await loadMachines();
  const query = new URLSearchParams(location.search);
  const wanted = query.get('machine');
  if (wanted && S.machines.some((m) => m.id === wanted)) {
    S.runId = query.get('run') || null;
    await openMachine(wanted, { keepRun: true });
    if (S.runId) selectRun(S.runId);
  }
  navigate();
}

start();
