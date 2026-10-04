// State Graph: machines on the left; the open machine as graph, YAML and runs in the middle; the inspector and the
// debugger on the right. Every graph edit is one POST .../edit, and the panel redraws from what the server answers:
// onto the unsaved drafts, written by Save -- or, with auto-save on, onto the file version the panel shows, written at
// once. Runs are polled while they are alive.
import {
  abandon, api, autoRefresh, confirm, copyText, dialog, emptyState, errorText, html, icon, isAborted, jsonView,
  localTime, navigate, notice, openSession, pluginBase, prompt, render, selectTab, setDirty, setQuery, setTitle, toast,
  trusted, update, withBusy, yamlCode,
} from '/static/kit/panel-kit.js';
import {
  Canvas, fragmentLock, groupedSpots, keepingChoices, LINE_STYLES, lineKeys, NOTE, noteKey, outermost, posixPath,
  problemIndex, putTyped, renamedLines, runOverlay, sameSelection, selectionOf, shorten, stateFragment, typedIn,
  foldTrace,
} from './graph.js';

const API = `${pluginBase(import.meta.url)}/api`;
const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;
const NAME = /^[a-z][a-z0-9_]*$/;
/** Who looks ('' without auth): a run's sessions are its user's, and the chat opens only the viewer's own. */
const VIEWER = document.querySelector?.('.sg-layout')?.dataset.viewer || '';
const ownSessions = (run) => !VIEWER || run?.user_id === VIEWER;
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);
const STATUS_KIND = { running: 'info', paused: 'warn', waiting: 'info', succeeded: 'ok', failed: 'danger', cancelled: '', interrupted: 'warn' };
const PSEUDO = [
  { type: 'state', title: 'State', icon: 'square', hint: 'A state without activity: completes at once, or waits for events' },
  { type: 'choice', title: 'Choice', icon: 'git-branch', hint: 'Choice: guards decide, the last one is else' },
  { type: 'junction', title: 'Junction', icon: 'circle-dot', hint: 'Junction: guards decide before anything runs' },
  { type: 'final', title: 'Final', icon: 'circle-check', hint: 'Final state: ends its region' },
  { type: 'composite', title: 'Composite', icon: 'network',
    hint: 'A composite state: a region of its own, with a first state in it; new states go into it while it is selected' },
];

const S = {
  machines: [],
  machine: null,        // get_machine: id, file, writable, root_file, files, versions, problems, graph, layout
  kinds: [],
  selection: null,      // {kind: 'state' | 'transition' | 'note', id} or {kind: 'many', states, transitions} (graph.js selectionOf)
  problems: { states: {}, transitions: {}, machine: [] },
  drafts: {},           // path -> unsaved text: the YAML tab's, and the graph edits' without auto-save
  autosave: recall('autosave', false),  // graph edits and the layout written at once, not by Save
  layoutDirty: false,   // positions or line styles changed and not saved (without auto-save)
  editing: 0,           // graph edits on their way: auto-save is not switched under them
  inspectorDrafts: new Set(),  // the inspector's forms with text typed and not applied: 'state', 'transition:<id>'
  yamlFile: null,
  runs: [],
  historyKind: '',      // the history shows the rows of this kind only ('': every row)
  runId: null,
  run: null,            // get_run of the selected run
  evaluation: null,     // {expr, value} | {expr, error}
  result: null,         // loadResult: {runId, rows, after, complete, frames}
  framePrefix: null,    // the frame of the selected run the author picked to see on another machine's canvas
  stateFramesOpen: false,  // the inspector's folded list of a state's submachine runs is open ...
  stateFramesFor: null,    // ... for this state
  resultOpen: new Set(),  // indexes of the result's activities the author opened
  nextBreakpoints: [],  // [{state, at, machine}] for the next run of this machine
  nextWatch: [],        // [expr] for the next run, watched in this machine's frames
  catalog: null,        // /catalog: agents, tools and decision profiles the fields offer
  catalogFor: undefined,  // the machine whose runner's tools the catalog holds (null: a new machine's)
  runStatus: '',        // the runs list shows runs of this status only ('': every run)
  runsMore: false,      // the list's last page was full: older runs may follow
  acceptedSeen: '',     // the events the run waited for when the event form last chose one for the viewer
  undo: [],             // [{machine, text, version}]: the root text before each edit (the draft, or with auto-save
                        // the file) and, with auto-save, the file's version after it
  redo: [],             // the same for each undo: the text it replaced, and the version it left
};
const UNDO_DEPTH = 20;

const badge = (text, kind = '') => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${text}</span>`;
const statusBadge = (status) => badge(status || 'unknown', STATUS_KIND[status] ?? '');
const stateOf = (name) => S.machine?.graph?.states?.find((state) => state.name === name) || null;
const transitionOf = (id) => S.machine?.graph?.transitions?.find((t) => t.id === id) || null;
const noteOf = (name) => S.machine?.graph?.notes?.find((note) => note.name === name) || null;
const hasDrafts = () => Object.keys(S.drafts).length > 0;
const unsaved = () => hasDrafts() || S.layoutDirty || S.inspectorDrafts.size > 0;
const savable = () => hasDrafts() || S.layoutDirty;
/** The root file as the panel holds it: its draft, else as saved. */
const rootText = () => yamlText(S.machine.root_file);
/** The root text the graph is drawn from: a draft edit's, else the saved file's. */
const drawnText = (m) => m.draft ?? m.files[m.root_file];
/** The inspector form an input belongs to, as S.inspectorDrafts names it. */
const FORM_DRAFTS = { 'set-state': 'state', activity: 'activity', 'state-fields': 'fields', 'machine-fields': 'machine',
  'state-description': 'description', note: 'note' };
const draftKey = (form) => (FORM_DRAFTS[form?.dataset.form]
  || (form?.dataset.transition ? `transition:${form.dataset.transition}` : null));
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
function forget(key) {
  try { localStorage.removeItem(`stategraph:${key}`); } catch { /* nothing kept */ }
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
  onOpen: (target) => (target.kind === 'state' ? renameState(target.id) : target.kind === 'note' ? openNote(target.id) : choose(target)),
  onReparent: (name, into, spot, here) => moveState(name, into, { spot, here }),
});

function positions() {
  return S.machine?.layout?.positions || {};
}

/** The stored positions of these states, null for one laid out by ELK. */
const spotsOf = (names) => Object.fromEntries(names.map((name) => [name, positions()[name] ?? null]));
/** The stored line styles of these keys, null for the machine's. */
const stylesOf = (keys) => Object.fromEntries(keys.map((key) => [key, lineStyles()[key] ?? null]));
/** `map` with `changes` over it: null removes a key. */
const patched = (map, changes) => {
  const next = { ...map };
  for (const [key, value] of Object.entries(changes || {})) {
    if (value === null) delete next[key];
    else next[key] = value;
  }
  return next;
};

/** The submachine frames of the run by prefix, in the order they started: those the run recorded (frames_started,
 * the first 500), those its journal names (a run from before the record, or past those 500; their path unknown), and
 * the live ones of its view. */
function runFrames(run) {
  const found = new Map((run?.frames_started || []).map((f) => [f.prefix, { ...f, live: false }]));
  for (const [prefix, traced] of S.result?.runId === run?.id ? S.result.frames : []) {
    if (prefix && !found.has(prefix)) found.set(prefix, { prefix, machine: traced.machine, path: startedUnder(prefix), live: false });
  }
  // the view of a run that ended or was interrupted keeps its last frames: they run no more
  const alive = run && !TERMINAL.has(run.status) && run.status !== 'interrupted';
  for (const f of run?.view?.frames || []) {
    if (f.prefix) found.set(f.prefix, { prefix: f.prefix, machine: f.machine, path: f.path, live: alive });
  }
  return found;
}

/** The path a frame only the journal names ran under: its starting activity's (key: the prefix without `/m/`, and
 * without a retry's `/a<n>`), from the result's rows -- kept by key on the result once read. */
function startedUnder(prefix) {
  const result = S.result;
  if (!result) return null;
  result.paths ||= new Map(result.rows.filter((row) => row.kind === 'activity').map((row) => [row.key, row.data?.path]));
  return result.paths.get(prefix.replace(/(\/a\d+)?\/m\/$/, '')) ?? null;
}

/** The frames `machine` ran as a submachine in the run. */
const framesOf = (run, machine) => [...runFrames(run).values()].filter((f) => f.machine === machine);

/** The submachine frames the shown frame started, each with the `state` of it they ran under: its children (one
 * `/m/` past its prefix) whose path goes on from its own with that state's name. */
function shownChildren() {
  const shown = shownFrame();
  if (!shown) return [];
  const frames = runFrames(S.run);
  const base = shown.prefix ? frames.get(shown.prefix)?.path : '';
  if (base == null) return [];  // the shown frame's own path is not known: neither is what runs under its states
  const found = [];
  for (const f of frames.values()) {
    const rest = f.prefix.slice(shown.prefix.length);
    if (!f.prefix.startsWith(shown.prefix) || rest.indexOf('/m/') !== rest.length - 3 || f.path == null) continue;
    const inner = !base ? f.path : f.path.startsWith(`${base}/`) ? f.path.slice(base.length + 1) : null;
    if (inner) found.push({ ...f, state: inner.split('/')[0] });
  }
  return found;
}

/** The submachine frames the shown frame started from its state `name`. */
const framesUnder = (name) => shownChildren().filter((f) => f.state === name);

/** How a frame of the selected run stands: running, or the state it ended in (from its trace). */
const frameStatus = (f) => {
  const traced = tracedFrame(f.prefix);
  if (f.live) return 'running';
  // failed, cancelled; one an interrupted run left behind has not ended: a resume goes on with it
  const how = traced?.ended ? (traced.ended !== 'finished' ? traced.ended : 'ended')
    : S.run?.status === 'interrupted' ? 'interrupted' : 'ended';
  return traced?.state ? `${how} in ${traced.state}` : how;
};

/** The inspector's list of the submachine runs the selected state started in the selected run. */
function drawStateFrames() {
  const box = $('stateFrames');
  if (!box) return;
  const name = S.selection?.kind === 'state' ? S.selection.id : null;
  const frames = S.run && name ? framesUnder(name) : [];
  box.hidden = !frames.length;
  if (S.stateFramesFor !== name) {  // another state: its list starts folded
    S.stateFramesFor = name;
    S.stateFramesOpen = false;
  }
  const count = Object.create(null);  // by machine id
  for (const f of frames) count[f.machine] = (count[f.machine] || 0) + 1;
  const seen = Object.create(null);
  const shown = shownFrame()?.prefix;
  // last in the inspector and folded (a map runs one per item); open stays open through the polls
  update(box, frames.length ? html`<details class="pk-details" data-state-frames ${S.stateFramesOpen ? 'open' : ''}>
    <summary data-key="state-frames">Submachine runs (${frames.length}) <span class="pk-badge pk-badge--info">run ${shorten(S.run.id, 12)}</span></summary>
    <ul class="sg-plain-list">${frames.map((f) => {
      seen[f.machine] = (seen[f.machine] || 0) + 1;
      return html`<li class="sg-watch"><span class="pk-mono">${f.machine}${count[f.machine] > 1 ? ` #${seen[f.machine]}` : ''}</span>
        <span class="pk-muted">${frameStatus(f)}</span><span class="pk-grow"></span>${showFrameButton(f.machine, f.prefix, shown,
          `${f.machine}${count[f.machine] > 1 ? ` #${seen[f.machine]}` : ''}`)}</li>`;
    })}</ul></details>` : '');
}

$('side-inspect').addEventListener('toggle', (event) => {
  if (event.target.dataset?.stateFrames !== undefined) S.stateFramesOpen = event.target.open;
}, true);

/** The frame of the selected run the canvas shows: the root on the run's own machine; on another, the frame of the
 * open machine picked, else a live one, else the last it ran in. null: the open machine did not run in it. */
function shownFrame() {
  if (!S.run || !S.machine) return null;
  if (S.run.machine_id === S.machine.id) return { prefix: '' };
  const mine = framesOf(S.run, S.machine.id);
  return mine.find((f) => f.prefix === S.framePrefix) || mine.find((f) => f.live) || mine[mine.length - 1] || null;
}

/** What the journal says of a frame of the selected run (foldTrace): stands in for a frame that has ended. */
const tracedFrame = (prefix) => (S.result?.runId === S.run?.id ? S.result.frames.get(prefix) : null) || null;

/** A frame of the selected run on the canvas: its machine opened, the run kept. */
async function openFrame(machine, prefix) {
  const before = S.framePrefix;
  S.framePrefix = prefix || null;
  if (machine === S.machine?.id) {
    drawRun();
    return;
  }
  await openMachine(machine, { keepRun: true });
  // not opened (the author kept the drafts, or it failed) -- unless a later pick or another run changed it meanwhile
  if (S.machine?.id !== machine && S.framePrefix === (prefix || null)) S.framePrefix = before;
}

document.addEventListener('click', (event) => {
  const open = event.target.closest?.('[data-open-frame]');
  if (open) withBusy(open, () => openFrame(open.dataset.machine, open.dataset.openFrame));
});

function redrawOverlay() {
  const frame = shownFrame();
  const run = frame ? runOverlay(S.run, frame.prefix, tracedFrame(frame.prefix)) : null;
  if (run) {  // the states that started submachine runs: a badge says how many (the inspector lists them)
    run.subruns = Object.create(null);
    for (const f of shownChildren()) run.subruns[f.state] = (run.subruns[f.state] || 0) + 1;
  }
  canvas.setOverlay({
    problems: S.problems,
    run,
    breakpoints: new Set(shownBreakpoints().filter((p) => p.enabled !== false && ofThisMachine(p)).map((p) => p.state)),
  });
}

let fitPending = true;
async function drawGraph({ fit = false } = {}) {
  const drawn = await canvas.setGraph(S.machine.graph, S.machine.layout);
  if (!drawn) return;
  canvas.select(S.selection);
  redrawOverlay();
  if (fit || fitPending) {
    fitPending = !$('canvas').getBoundingClientRect().width;  // hidden now: fit once the graph tab shows
    canvas.fit();
  }
  const parses = !S.machine.problems.some((p) => p.level === 'error' && !p.path);
  render($('sgStates'), S.machine.graph.states.map((s) => html`<option value="${s.name}">${s.label || ''}</option>`));
  $('canvasHint').textContent = S.machine.graph.states.length
    ? 'Drag a state to move it, from its handle to another state to connect; double-click to rename. Wheel scrolls, Ctrl+wheel zooms.'
    : parses ? 'No states yet: add one from the bar above.' : 'The file does not parse: fix it in the YAML tab.';
}

function savePositions(spots) {
  return saveLayout({ positions: { ...positions(), ...spots } });
}

/** The layout with `changes` (positions, line, lines) over it: drawn at once and stored in the sidecar. */
async function saveLayout(changes) {
  const layout = { ...S.machine.layout, version: 1, ...changes };
  S.machine.layout = layout;
  canvas.setLayout(layout);
  if (!S.machine.writable) return;  // kept for this view only: the sidecar sits next to a read-only file
  if (!S.autosave) {  // written by Save
    S.layoutDirty = true;
    setDirty(true);
    drawSaveControls();
    return;
  }
  try {
    await api(`${API}/machines/${enc(S.machine.id)}/layout`, { method: 'PUT', json: { layout }, quiet: true });
  } catch (error) {
    if (!isAborted(error)) toast(`Layout not saved: ${errorText(error)}`, { kind: 'warn' });
  }
}

/** A stored line style as the canvas draws it: one no longer offered is right-angled, like anything but 'straight'. */
function lineStyle(style) {
  return style && !LINE_STYLES.includes(style) ? 'orthogonal' : style;
}

/** The line styles the layout gives single transitions: {key (lineKeys): style}. */
function lineStyles() {
  return Object.fromEntries(Object.entries(S.machine?.layout?.lines || {}).map(([key, style]) => [key, lineStyle(style)]));
}

/** The one line style these transitions share ('' for the machine's), undefined when they differ. */
function styleOfAll(transitions) {
  const keys = lineKeys(S.machine.graph.transitions);
  const styles = new Set(transitions.map((t) => lineStyles()[keys[t.id]] || ''));
  return styles.size === 1 ? [...styles][0] : undefined;
}

/** Set the line style of these transitions ('' for the machine's). */
function setLines(ids, style) {
  const keys = lineKeys(S.machine.graph.transitions);
  const lines = { ...lineStyles() };
  for (const id of ids) {
    if (!keys[id]) continue;
    if (style) lines[keys[id]] = style;
    else delete lines[keys[id]];
  }
  return saveLayout({ lines });
}

const LINE_NAMES = { orthogonal: 'Right-angled', straight: 'Straight' };

/** A select of line styles: `inherit` offers the machine's as the first choice (''), `mixed` a first line that
 * says the selection has several. */
function lineChoices(chosen, { inherit = false, mixed = false } = {}) {
  const machine = lineStyle(S.machine?.layout?.line) || 'orthogonal';
  return [
    mixed ? html`<option value="" selected disabled>Several styles</option>` : '',
    inherit ? html`<option value="" ${!mixed && !chosen ? 'selected' : ''}>As the machine: ${LINE_NAMES[machine]}</option>` : '',
    LINE_STYLES.map((style) => html`<option value="${style}" ${!mixed && chosen === style ? 'selected' : ''}>${LINE_NAMES[style]}</option>`),
  ];
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

/** Folders the author closed, by path ("Reviews/nightly"): kept for the next visit. */
const closedFolders = new Set(recall('closed-folders', []));

/** The machines as a folder tree: a machine's group ("Reviews/nightly", set in its file, else where it comes from) is its
 * folder path. Folders keep the order the server lists their first machine in. */
function folderTree(machines) {
  const root = { path: '', children: new Map(), machines: [] };
  for (const m of machines) {
    let folder = root;
    for (const name of String(m.group || 'Machines').split('/').map((part) => part.trim()).filter(Boolean)) {
      const path = folder.path ? `${folder.path}/${name}` : name;
      if (!folder.children.has(name)) folder.children.set(name, { name, path, children: new Map(), machines: [] });
      folder = folder.children.get(name);
    }
    folder.machines.push(m);
  }
  return root;
}

const folderSize = (folder) => folder.machines.length + [...folder.children.values()].reduce((sum, f) => sum + folderSize(f), 0);

function machineItem(m) {
  return html`<button type="button" class="pk-btn pk-btn--ghost sg-item" data-machine="${m.id}" aria-current="${String(m.id === S.machine?.id)}">
      <span class="sg-item-top"><span class="sg-item-name">${m.title || m.id}</span>
        ${m.errors ? badge(`${m.errors} err`, 'danger') : m.warnings ? badge(`${m.warnings} warn`, 'warn') : badge('valid', 'ok')}
        ${m.writable ? '' : badge('read-only')}</span>
      <span class="sg-item-sub pk-mono">${m.id}</span>
      ${m.description ? html`<span class="sg-item-sub">${shorten(m.description, 120)}</span>` : ''}
    </button>`;
}

/** A folder, open unless the author closed it -- a search opens every folder it finds something in. */
function folderView(folder, searching) {
  const open = searching || !closedFolders.has(folder.path);
  return html`<details class="sg-folder" data-folder="${folder.path}" ${open ? 'open' : ''}>
    <summary class="sg-folder-name">${icon('folder', { size: 'sm' })}<span class="pk-grow">${folder.name}</span><span class="pk-muted">${folderSize(folder)}</span></summary>
    <div class="sg-folder-body">${[...folder.children.values()].map((child) => folderView(child, searching))}${folder.machines.map(machineItem)}</div>
  </details>`;
}

function drawMachineList() {
  drawMachineChoices();
  const needle = $('search').value.trim().toLowerCase();
  const shown = S.machines.filter((m) => !needle || `${m.id} ${m.title} ${m.description} ${m.group || ''}`.toLowerCase().includes(needle));
  const tree = folderTree(shown);
  update($('machineList'), shown.length
    ? [...[...tree.children.values()].map((folder) => folderView(folder, Boolean(needle))), ...tree.machines.map(machineItem)]
    : emptyState('workflow', S.machines.length ? 'No machine matches' : 'No machines yet', S.machines.length ? '' : 'Create one with New.'));
}

// details fire toggle on themselves only: caught on the way down. A search's open folders are not the author's choice.
$('machineList').addEventListener('toggle', (event) => {
  const folder = event.target.closest?.('[data-folder]');
  if (!folder || $('search').value.trim()) return;
  if (folder.open) closedFolders.delete(folder.dataset.folder);
  else closedFolders.add(folder.dataset.folder);
  remember('closed-folders', [...closedFolders]);
}, true);

/** Open (or reload) a machine. Unsaved YAML is discarded only after the author agreed, or when the caller has
 * dealt with it already (discard: a reload after a conflict or a save). */
async function openMachine(id, { keepRun = false, discard = false } = {}) {
  const where = savable() ? 'The machine has unsaved changes' : 'The inspector has changes that are not applied';
  if (unsaved() && !discard && !await confirm(id === S.machine?.id
    ? `${where}. Reload the machine and discard them?`
    : `${where}. Open another machine and discard them?`, { danger: true, confirmLabel: 'Discard' })) {
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
  S.layoutDirty = false;
  if (!S.autosave) S.undo = S.redo = [];  // steps of the drafts just dropped
  S.inspectorDrafts.clear();
  setDirty(false);
  if (switched) {
    foldListWhenNarrow();
    S.runs = [];  // another machine's: older pages of this list must not be read after them
    S.runsMore = false;
    $('olderRuns').hidden = true;
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
  if (switched && S.run) drawRun();  // the run kept: its bar, frames and breakpoints as this machine sees them
  setTitle(`State Graph · ${machine.id}`);
  setQuery(S.runId ? { machine: machine.id, run: S.runId } : { machine: machine.id });
  loadRuns();
}

/** Narrow, the machine list folds away once a machine is open -- its toggle brings it back; not kept as the
 * viewer's choice. */
function foldListWhenNarrow() {
  if (!globalThis.matchMedia?.('(max-width: 900px)').matches) return;
  $('machinesPane').hidden = true;
  document.querySelector?.('[data-pk-sidebar-toggle][aria-controls="machinesPane"]')?.setAttribute('aria-expanded', 'false');
}

/** A machine answer (get, edit): everything that shows it is drawn again. */
function showMachine(machine) {
  const shown = S.machine;
  S.machine = machine;
  if ([...S.undo, ...S.redo].some((step) => step.machine !== machine.id)) S.undo = S.redo = [];  // another machine's
  // without auto-save a step is the text of a draft of the file as read: another version of the file outdates it
  if (!S.autosave && shown?.id === machine.id && shown.versions[shown.root_file] !== machine.versions[machine.root_file]) {
    S.undo = S.redo = [];
  }
  drawUndo();
  S.problems = problemIndex(machine.graph, machine.problems, machine.file || machine.root_file);
  if (S.selection?.kind === 'state' && !stateOf(S.selection.id)) S.selection = null;
  if (S.selection?.kind === 'transition' && !transitionOf(S.selection.id)) S.selection = null;
  if (S.selection?.kind === 'note' && !noteOf(S.selection.id)) S.selection = null;
  // states and transitions gone meanwhile (an undo, another editor) leave the selection: one left is selected alone
  if (S.selection?.kind === 'many') {
    S.selection = selectionOf(S.selection.states.filter((name) => stateOf(name)),
      S.selection.transitions.filter((id) => transitionOf(id)));
  }
  $('placeholder').hidden = true;
  $('machineView').hidden = false;
  if (S.catalogFor !== machine.id) loadCatalog(machine.id);  // not awaited: the fields offer its lists once they come
  drawHead();
  drawPalette();
  drawGraph();
  drawYaml();
  drawStartForm();
  drawInspector();
  drawMachineList();
}

/** A new event, typed into the trigger select's "New event…": declared in the machine, then the transition's. */
async function newEventFor(t, select) {
  const g = S.machine.graph;
  const name = await askUntil('Name of the new event (declared in the machine, then this transition\'s trigger):', {
    title: 'New event', placeholder: 'approve',
    problem: (id) => (!NAME.test(id) ? `"${id}" is no event name: lowercase letters, digits and _, starting with a letter.`
      : id in (g.events || {}) || id === 'done' || id === 'error' ? `"${id}" is an event already.` : '') });
  select.value = select.dataset.orig;
  if (!name) return;
  // appended to the block as it is written (its comments stay); a flow mapping, or none, is written anew
  const text = (g.yaml?.events ?? '').replace(/\s+$/, '');
  const block = Object.keys(g.events || {}).length > 0 && text && !text.startsWith('{');
  const events = block ? { $yaml: `${text}\n${name}: {description: ""}` } : { ...(g.events || {}), [name]: { description: '' } };
  if (!await edit({ op: 'update_machine', fields: { events } })) return;
  // the transition's form, drawn anew with the event among its triggers: chosen there, applied with its Apply
  const form = $('side-inspect').querySelector?.(`[data-transition="${t.id}"]`);
  if (form?.elements.trigger) {
    form.elements.trigger.value = name;
    S.inspectorDrafts.add(draftKey(form));
    setDirty(true);
  }
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
    ${errors || warnings ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost sg-head-problems" data-act="show-problems" title="Show every problem in the inspector">
      ${errors ? badge(`${errors} error${errors > 1 ? 's' : ''}`, 'danger') : ''}${warnings ? badge(`${warnings} warning${warnings > 1 ? 's' : ''}`, 'warn') : ''}</button>`
    : badge('valid', 'ok')}
    ${m.writable ? '' : html`<span class="pk-badge" title="Not in a writable machine root: shown, run and debugged, not edited">read-only</span>`}
    ${m.writable ? html`<button type="button" class="pk-btn pk-btn--sm${savable() ? ' pk-btn--primary' : ''}" data-act="save" title="Save the changes (Ctrl+S)" ${savable() ? '' : 'disabled'}>${icon('save', { size: 'sm' })} Save</button>
    <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-act="autosave" aria-pressed="${String(S.autosave)}" title="Write every graph edit and move at once, not by Save">Auto-save</button>` : ''}
    <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-act="copy-id" title="Copy the machine id">${icon('copy', { size: 'sm' })}</button>
    <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-act="duplicate-machine" title="A copy under a new id among your own machines">${icon('layers', { size: 'sm' })} Duplicate</button>
    ${m.writable ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-act="delete-machine" title="Delete the machine" aria-label="Delete the machine">${icon('trash-2', { size: 'sm' })}</button>` : ''}`);
  $('yamlCount').textContent = savable() ? 'unsaved' : '';
}

/** The buttons that add a state, in the bar -- and, for a narrow panel, in the menu that stands in for it. */
function drawPalette() {
  const writable = S.machine?.writable;
  const items = (look, ghost) => [
    ...S.kinds.map((kind) => html`<button type="button" class="${look}" data-add-kind="${kind.key}"
      title="${`Add a ${kind.title} state: ${kind.summary}`}" ${writable ? '' : 'disabled'}>${icon(kind.icon || 'square', { size: 'sm' })}${kind.title}</button>`),
    ...PSEUDO.map((p) => html`<button type="button" class="${look}${ghost}" data-add-type="${p.type}"
      title="${p.hint}" ${writable ? '' : 'disabled'}>${icon(p.icon, { size: 'sm' })}${p.title}</button>`),
    html`<button type="button" class="${look}${ghost}" data-add-note title="A note of free text on the canvas: kept in the file under notes:, never run"
      ${writable ? '' : 'disabled'}>${icon('notebook-pen', { size: 'sm' })}Note</button>`,
  ];
  render($('palette'), items('pk-btn pk-btn--sm', ' pk-btn--ghost'));
  render($('paletteMenu'), items('pk-menu-item', ''));
}

// ------------------------------------------------------------------ edits

/** A read-only machine takes no edit: said once, before anything is asked. */
function readOnly() {
  if (S.machine?.writable) return false;
  toast('This machine is read-only: it is not in a writable machine root.', { kind: 'warn' });
  return true;
}

/** One graph edit: onto the drafts, or with auto-save onto the saved file; the machine is drawn as the server
 * answers. `places`: the states whose positions
 * the caller changes after it (a rename, a group), `keys`: the transitions' line styles it changes (a rename) -- an
 * undo puts theirs back with the text, and only theirs. */
async function edit(op, { from = null, places = null, keys = null } = {}) {
  const m = S.machine;
  const auto = S.autosave;  // the answer is read the way the request was made: toggleAutosave waits for it
  if (readOnly()) return null;
  // the root text the edit starts from, and what an undo writes back (auto-save) or puts back as the draft
  let before = auto ? m.files[m.root_file] : rootText();
  // without auto-save, text typed in the YAML tab the graph does not show yet: the edit works from the text drawn,
  // and the typed text goes once the edit is made
  if (!auto && before !== drawnText(m)) {
    if (!await confirm('The YAML tab has text the graph does not show yet (Validate there draws it). Discard that text and make the edit?',
      { danger: true, confirmLabel: 'Discard' })) return null;
    before = drawnText(m);
  }
  // with auto-save the edit changes the saved file; without, it goes onto the drafts
  if (auto && hasDrafts() && !await confirm('The YAML tab has unsaved changes, and graph edits change the saved file. Discard the unsaved changes?',
    { danger: true, confirmLabel: 'Discard' })) {
    return null;
  }
  // the edit redraws the inspector: what the other forms hold comes back into them (keepTyped) -- but the state's
  // YAML box is the whole state, which the edit changes: its text would undo the edit when applied. A note's edit
  // changes no state: the box keeps its text
  const touchesStates = op.op !== 'set_note';
  if (touchesStates && from !== 'state' && S.inspectorDrafts.has('state') && !await confirm('The state\'s YAML box has text that is not applied, and this edit changes the state. Discard the text?',
    { danger: true, confirmLabel: 'Discard' })) {
    return null;
  }
  const spots = places && spotsOf(places);
  const styles = keys && stylesOf(keys);
  try {
    let next;
    const held = rootText();  // the YAML tab as the request left it
    S.editing += 1;
    try {
      next = await api(`${API}/machines/${enc(m.id)}/edit`, {
        method: 'POST', quiet: true, json: auto ? { op, expected_version: m.versions[m.root_file] }
          : { op, drafts: { ...S.drafts, [m.root_file]: before } },  // the text as read: not the file as it is now
      });
    } finally {
      S.editing -= 1;
    }
    // another machine opened meanwhile -- or, without auto-save, this one reloaded or redrawn: not the answer's
    if (auto ? S.machine?.id !== m.id : S.machine !== m) {
      if (S.machine?.id === m.id) toast('The edit was not applied: the machine was reloaded meanwhile.', { kind: 'warn' });
      return null;
    }
    if (!auto && rootText() !== held) {  // typed into meanwhile: the answer would overwrite it
      toast('The edit was not applied: the YAML tab changed meanwhile. Make it again.', { kind: 'warn' });
      return null;
    }
    if (auto) S.drafts = {};
    else setRoot(next.draft);
    setDirty(unsaved());
    S.undo = [...S.undo.filter((step) => step.machine === m.id), { machine: m.id, text: before,
      version: next.versions?.[m.root_file], ...(spots && { spots }), ...(styles && { styles }) }].slice(-UNDO_DEPTH);
    S.redo = [];  // a new edit: what was undone before it is not redone over it
    // read now, not before the request: what was typed meanwhile counts, what was discarded meanwhile does not
    const others = new Set([...S.inspectorDrafts].filter((key) => key !== from && (key !== 'state' || !touchesStates)));
    let typed = typedIn($('side-inspect'), others, draftKey);
    if (op.op === 'rename_state' && S.selection?.kind === 'state' && S.selection.id === op.old) {
      S.selection = { kind: 'state', id: op.new };  // the renamed state stays the one shown, its forms under its name
      typed = new Map([...typed].map(([key, held]) => [key.replace(`transition:${op.old}#`, `transition:${op.new}#`), held]));
    }
    // a draft keeps the files, versions and layout the panel holds: Save checks against the version they came from
    const shown = auto ? next : { ...m, graph: next.graph, problems: next.problems, draft: next.draft };
    showMachine(shown);
    keepTyped(typed);
    return shown;
  } catch (error) {
    if (isAborted(error)) return null;
    if (error.status === 409) {
      // the form the edit came from keeps its text only without a reload: say so, so it can be copied first
      const typed = from && S.inspectorDrafts.has(from);
      const choice = await dialog({
        title: 'The file changed',
        message: 'The machine file changed since this panel loaded it (another editor, or an agent). Reload it and make the edit again.'
          + (typed ? ' Reload drops what you typed in the inspector; Cancel keeps it, so you can copy it first.' : ''),
        actions: [{ label: 'Cancel', value: null }, { label: 'Reload', value: 'reload', primary: !typed, danger: typed }],
      });
      if (choice === 'reload') await openMachine(m.id, { keepRun: true, discard: true });  // agreed to above
    } else {
      toast(errorText(error), { kind: 'error' });
    }
    return null;
  }
}

/** A name the author types, asked again with what was typed and why until it is one or the author cancels;
 * `current` given back unchanged is nothing to do (null), not an error. */
async function askUntil(message, { title, value = '', placeholder, current = null, problem }) {
  let why = '';
  for (;;) {
    const name = await prompt(why ? `${why} ${message}` : message, { title, value, placeholder });
    if (name === null) return null;
    const trimmed = name.trim();
    if (current !== null && trimmed === current) return null;
    why = problem(trimmed);
    if (!why) return trimmed;
    value = trimmed;
  }
}

/** What the other inspector forms held before a redraw, back in them: the activity form's kind first (it draws
 * the fields the rest goes into); a field the edit changed underneath keeps what is drawn, and the author hears. */
function keepTyped(typed) {
  if (!typed.size) return;
  const kind = typed.get('activity')?.controls.find((control) => control.name === 'kind');
  const select = $('side-inspect').querySelector('[data-form="activity"]')?.elements.kind;
  const state = S.selection?.kind === 'state' ? stateOf(S.selection.id) : null;
  if (kind && select && state && select.dataset.orig === typed.get('activity').shown.kind) {
    select.value = kind.value;
    render($('activityFields'), activityFields(kind.value, state));
  }
  const { restored, dropped } = putTyped($('side-inspect'), typed, draftKey);
  for (const key of restored) S.inspectorDrafts.add(key);
  if (restored.size) setDirty(true);
  if (dropped.length) toast(`Not kept -- the edit changed or removed the form you typed into: ${dropped.join(', ')}`, { kind: 'warn' });
}

/** Undo the last edit (`back`) or redo the last undo: the root file written back as the step holds it, if nothing
 * changed it since; the text it replaces goes onto the other stack. Versions are hashes of the text, so a step
 * fits the file again once an undo or redo gave it back that text. */
async function travel(back) {
  const m = S.machine;
  const [from, to, word] = back ? ['undo', 'redo', 'Undo'] : ['redo', 'undo', 'Redo'];
  const step = S[from][S[from].length - 1];
  if (!m?.writable || !step || step.machine !== m.id) return;
  if (!S.autosave) {
    await travelDrafts(back, step);
    return;
  }
  if (unsaved() && !await confirm(`${word} reloads the machine: unsaved text in the YAML tab and the inspector is lost. ${word} anyway?`,
    { danger: true, confirmLabel: word })) return;
  const replaced = m.files[m.root_file];
  let saved;
  try {
    // force: the text was the file once; a machine saved with errors had them then too
    saved = await api(`${API}/machines/${enc(m.id)}`, { method: 'PUT', quiet: true, json: {
      files: { [m.root_file]: step.text }, expected_versions: { [m.root_file]: step.version }, force: true } });
  } catch (error) {
    if (isAborted(error)) return;
    S.undo = S.redo = [];  // the file changed since (a save, another editor): nothing here undoes or redoes that
    drawUndo();
    toast(error.status === 409 ? `Not done: the file changed since.` : `Not done: ${errorText(error)}`, { kind: 'warn' });
    return;
  }
  S[from] = S[from].slice(0, -1);
  S[to] = [...S[to], { machine: m.id, text: replaced, version: saved.versions[m.root_file],
    ...(step.spots && { spots: spotsOf(Object.keys(step.spots)) }),
    ...(step.styles && { styles: stylesOf(Object.keys(step.styles)) }) }].slice(-UNDO_DEPTH);
  if (step.spots || step.styles) {  // the edit changed the layout as well: that part goes back with its text, the rest stays
    const layout = { ...m.layout, version: 1, positions: patched(positions(), step.spots), lines: patched(lineStyles(), step.styles) };
    try {
      await api(`${API}/machines/${enc(m.id)}/layout`, { method: 'PUT', json: { layout }, quiet: true });
    } catch (error) {
      if (!isAborted(error)) toast(`Layout not put back: ${errorText(error)}`, { kind: 'warn' });
    }
  }
  S.drafts = {};
  await openMachine(m.id, { keepRun: true, discard: true });
}

/** travel() without auto-save: the root draft goes back to the step's text, the layout part with it; nothing is
 * written, the graph is drawn from the drafts. */
async function travelDrafts(back, step) {
  const m = S.machine;
  const [from, to] = back ? ['undo', 'redo'] : ['redo', 'undo'];
  if (!await dropTyped()) return;
  S[from] = S[from].slice(0, -1);
  S[to] = [...S[to], { machine: m.id, text: rootText(),
    ...(step.spots && { spots: spotsOf(Object.keys(step.spots)) }),
    ...(step.styles && { styles: stylesOf(Object.keys(step.styles)) }) }].slice(-UNDO_DEPTH);
  setRoot(step.text);
  if (step.spots || step.styles) saveLayout({ positions: patched(positions(), step.spots), lines: patched(lineStyles(), step.styles) });
  S.inspectorDrafts.clear();
  setDirty(unsaved());
  await drawDrafts();
}

/** The inspector's unapplied text is dropped by a redraw: asked first. */
const dropTyped = async () => !S.inspectorDrafts.size
  || confirm('The inspector has changes that are not applied, and this redraws it. Go on and drop them?', { danger: true, confirmLabel: 'Drop' });

/** Without auto-save: the graph drawn from the drafts, the root file's text as the YAML tab holds it. */
async function drawDrafts() {
  const m = S.machine;
  try {
    const shown = await api(`${API}/validate`, { method: 'POST', json: { files: allFiles(), machine_id: m.id } });
    if (S.machine === m) showMachine({ ...m, graph: shown.graph, problems: shown.problems, draft: rootText() });
  } catch (error) { /* toasted */ }
}

/** The root file's draft: none when it is the saved text. */
function setRoot(text) {
  const root = S.machine.root_file;
  if (text === S.machine.files[root]) delete S.drafts[root];
  else S.drafts[root] = text;
}

let saving = null;  // the save in flight: a second Ctrl+S (a held key) joins it

/** Save: the drafts with the layout after them (the YAML tab's save, which reloads the machine), else the layout. */
function saveAll() {
  saving ??= saveNow().finally(() => { saving = null; });
  return saving;
}

async function saveNow() {
  const m = S.machine;
  if (!m?.writable || !savable()) return;
  const layout = S.layoutDirty ? m.layout : null;
  if (hasDrafts()) {
    await saveYaml(false, layout);  // the layout after the text: it names the states the saved text has
    return;
  }
  if (!await putLayout(m, layout)) return;
  S.layoutDirty = false;
  setDirty(unsaved());
  drawSaveControls();
  toast('Saved', { kind: 'ok' });
}

async function putLayout(m, layout) {
  try {
    await api(`${API}/machines/${enc(m.id)}/layout`, { method: 'PUT', json: { layout }, quiet: true });
    return true;
  } catch (error) {
    if (!isAborted(error)) toast(`Layout not saved: ${errorText(error)}`, { kind: 'warn' });
    return false;
  }
}

/** The save controls: the YAML tab's and the head's. */
function drawSaveControls() {
  const m = S.machine;
  $('yamlSave').disabled = !m.writable || !savable();
  $('yamlRevert').disabled = !savable();
  $('yamlState').textContent = !m.writable ? 'read-only' : hasDrafts() ? `${Object.keys(S.drafts).length} file(s) unsaved`
    : S.layoutDirty ? 'layout unsaved' : 'saved';
  drawHead();
}

/** Auto-save on saves what is unsaved first -- if that fails, it stays off. The undo steps of the other way go. */
async function toggleAutosave() {
  if (S.editing) {
    toast('An edit is on its way: switch auto-save once it is done.', { kind: 'warn' });
    return;
  }
  if (!S.autosave && savable()) {
    await saveAll();
    if (savable() || S.editing) return;
  }
  S.autosave = !S.autosave;
  remember('autosave', S.autosave);
  S.undo = S.redo = [];
  drawUndo();
  drawHead();
}

function drawUndo() {
  const usable = (stack) => S.machine?.writable && stack.length > 0 && stack[stack.length - 1].machine === S.machine.id;
  $('undo').disabled = !usable(S.undo);
  $('redo').disabled = !usable(S.redo);
}

/** A state name the author types, checked here so the dialog can say what is wrong. */
function askName(message, value = '', current = null) {
  return askUntil(message, { title: 'State name', value, current, placeholder: 'lowercase_with_underscores',
    problem: (name) => (!NAME.test(name) ? `"${name}" is no state name: lowercase letters, digits and _, starting with a letter.`
      : stateOf(name) ? `A state "${name}" exists already.` : '') });
}

function freeName(base, taken = []) {
  const stem = (base || 'state').replace(/[^a-z0-9_]/g, '_').replace(/^[^a-z]+/, '') || 'state';
  for (let n = 1; ; n += 1) {
    const name = n === 1 ? stem : `${stem}_${n}`;
    if (!stateOf(name) && !taken.includes(name)) return name;
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
  if (type === 'composite') {  // its first state makes it one: both in one edit, one undo step
    const first = freeName('start', [name]);
    const ops = [{ op: 'add_state', name, type: 'state', parent: targetParent() },
      { op: 'add_state', name: first, type: 'state', parent: name }];
    if (await edit({ op: 'batch', ops })) choose({ kind: 'state', id: name });
    return;
  }
  const op = { op: 'add_state', name, type, parent: targetParent() };
  if (kind) op.do = activitySkeleton(kind);
  if (await edit(op)) choose({ kind: 'state', id: name });
}

async function renameState(old) {
  const name = await askName(`New name for ${old} (every transition to it and every initial naming it follows):`, old, old);
  if (!name) return;
  const placed = Object.hasOwn(positions(), old);
  // the line styles of its transitions: keyed by its name as well
  const styled = Object.keys(lineStyles()).filter((key) => key.split('→').includes(old));
  const keys = [...styled, ...Object.keys(renamedLines(Object.fromEntries(styled.map((key) => [key, ''])), old, name))];
  if (!await edit({ op: 'rename_state', old, new: name }, { places: placed ? [old, name] : null, keys: styled.length ? keys : null })) return;
  keepNextPoints((p) => (p.state === old ? { ...p, state: name } : p));  // the next run's breakpoints follow it
  const changes = {};
  if (placed) {
    changes.positions = { ...positions(), [name]: positions()[old] };
    delete changes.positions[old];
  }
  if (styled.length) changes.lines = renamedLines(lineStyles(), old, name);
  if (Object.keys(changes).length) await saveLayout(changes);
  choose({ kind: 'state', id: name });
}

/** The next run's breakpoints of this machine through ``change`` (a point, or null to drop it); stored and shown. */
function keepNextPoints(change) {
  const kept = S.nextBreakpoints.flatMap((p) => {
    const changed = ofThisMachine(p) ? change(p) : p;
    return changed ? [changed] : [];
  });
  if (kept.length === S.nextBreakpoints.length && kept.every((p, i) => p === S.nextBreakpoints[i])) return [];
  const dropped = S.nextBreakpoints.filter((p) => !kept.includes(p) && ofThisMachine(p) && !change(p));
  S.nextBreakpoints = kept;
  remember(`breakpoints:${S.machine.id}`, kept);
  if (S.machine) drawStartForm();
  return dropped;
}

async function removeState(name) {
  if (readOnly()) return;
  const state = stateOf(name);
  const emptied = emptiedBy([name], (n) => { for (let s = n; s; s = stateOf(s)?.parent) if (s === name) return true; return false; });
  if (emptied) {
    toast(`${emptied} keeps at least one state: add or move another one into it first, or remove ${emptied} instead.`, { kind: 'warn' });
    return;
  }
  const incoming = S.machine.graph.transitions.filter((t) => t.target === name).length;
  const inner = S.machine.graph.states.filter((s) => s.parent === name).length;
  const message = [`Remove the state ${name}${inner ? ` with the states inside it` : ''}?`,
    incoming ? `${incoming} transition${incoming > 1 ? 's' : ''} into it go${incoming > 1 ? '' : 'es'} too.` : ''].join(' ');
  if (!state || !await confirm(message, { title: 'Remove state', danger: true, confirmLabel: 'Remove' })) return;
  if (await edit({ op: 'remove_state', name })) choose(null);
}

/** The states a removal of these takes: those not inside another of them (that one takes them along). */
const removedWith = (names) => outermost(names.filter((name) => stateOf(name)), (name) => stateOf(name)?.parent || null);

/** What removing these states and transitions takes: the outermost states, and the transitions that do not go with
 * them anyway (out of one, or into one). `within(name)`: the state goes. */
function removal(names, ids) {
  const states = removedWith(names);
  const within = (name) => { for (let n = name; n; n = stateOf(n)?.parent) if (states.includes(n)) return true; return false; };
  const transitions = ids.map((id) => transitionOf(id)).filter((t) => t && !within(t.source) && !within(t.target));
  return { states, transitions, within };
}

const counted = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
const arrow = (t) => `${t.source} → ${t.target ?? '(internal)'}`;

/** The composite that removing `states` would leave empty -- one that stays itself (`within`: the state goes). */
function emptiedBy(states, within) {
  return S.machine.graph.states.find((c) => c.composite && !within(c.name)
    && S.machine.graph.states.every((s) => s.parent !== c.name || states.includes(s.name)))?.name || null;
}

/** Remove states and transitions in one edit (one undo step); a state inside another of them goes with it. */
async function removeSelection(names, ids) {
  const { states, transitions, within } = removal(names, ids);
  if ((!states.length && !transitions.length) || readOnly()) return;
  if (states.length && S.machine.graph.states.every((s) => s.parent || states.includes(s.name))) {
    toast('A machine needs at least one state: keep one of the top level.', { kind: 'warn' });
    return;
  }
  const emptied = emptiedBy(states, within);
  if (emptied) {
    toast(`${emptied} keeps at least one state: add or move another one into it first, or remove ${emptied} too.`, { kind: 'warn' });
    return;
  }
  const incoming = S.machine.graph.transitions.filter((t) => within(t.target) && !within(t.source)).length;
  const nested = S.machine.graph.states.some((s) => s.parent && states.includes(s.parent));
  const parts = [
    states.length && `${states.length === 1 ? 'the state' : `${states.length} states`} ${states.join(', ')}${nested ? ' with the states inside' : ''}`,
    transitions.length && `${transitions.length === 1 ? 'the transition' : `${transitions.length} transitions`} ${transitions.map(arrow).join(', ')}`,
  ];
  const message = [`Remove ${parts.filter(Boolean).join(' and ')}?`,
    incoming ? `${counted(incoming, 'transition')} into ${states.length === 1 ? 'it' : 'them'} go${incoming > 1 ? '' : 'es'} too.` : '']
    .filter(Boolean).join(' ');
  if (!await confirm(message, { title: 'Remove', danger: true, confirmLabel: 'Remove' })) return;
  // the later transitions of a state first: the earlier ones keep their index
  const ops = [...transitions].sort((a, b) => a.source.localeCompare(b.source) || b.index - a.index)
    .map((t) => ({ op: 'remove_transition', source: t.source, index: t.index }))
    .concat(states.map((name) => ({ op: 'remove_state', name })));
  if (await edit({ op: 'batch', ops })) choose(null);
}

/** Group states side by side into a new composite (one edit); placed by hand, they keep their place in it. */
async function groupStates(names) {
  const outer = removedWith(names);
  if (!outer.length || readOnly()) return;
  if (new Set(outer.map((name) => stateOf(name).parent || '')).size > 1) {
    toast('Only states side by side are grouped: all at the top level, or all in one composite.', { kind: 'warn' });
    return;
  }
  const name = await askName(`Name of the composite around ${outer.join(', ')}:`, freeName('group'));
  if (!name) return;
  const placed = outer.some((one) => Object.hasOwn(positions(), one)) && canvas.nodes;
  const spots = placed ? groupedSpots(canvas.nodes, outer, name) : null;
  if (!await edit({ op: 'group_states', names: outer, name }, { places: spots ? Object.keys(spots) : null })) return;
  if (spots) await savePositions(spots);
  choose({ kind: 'state', id: name });
}

/** A state into another composite (`into`; null: the top level). Dropped there: at `spot` in it -- refused, it
 * stays where it was dropped (`here`, in its own). Chosen in the inspector: the layout places it. */
async function moveState(name, into, { spot = null, here = null } = {}) {
  if (!S.machine.writable) {  // a read-only machine is only moved around: the drop is a plain move
    if (here) await savePositions({ [name]: here });
    return;
  }
  const placed = spot || Object.hasOwn(positions(), name) ? [name] : null;
  const machine = S.machine.id;
  if (!await edit({ op: 'move_state', name, into }, { places: placed })) {
    if (here && S.machine?.id === machine) await savePositions({ [name]: here });  // not in another machine opened meanwhile
    return;
  }
  const next = { ...positions() };
  if (spot) next[name] = spot;
  else delete next[name];  // a position counts from its parent: the old one would place it anywhere
  if (placed) await saveLayout({ positions: next });
  choose({ kind: 'state', id: name });
}

/** A new note in the middle of the view, its text chosen to be typed over. */
/** The notes: block is shared with another place (an anchor, a merge): its notes are edited in the YAML tab. */
function notesShared() {
  if (!S.machine?.graph?.locked?.includes('notes')) return false;
  toast(`Notes: ${SHARED_HINT}`, { kind: 'warn' });
  return true;
}

let addingNote = false;  // a second click while the first note is being added would name the same note again
async function addNote() {
  if (readOnly() || notesShared() || addingNote) return;
  addingNote = true;
  try {
    const taken = new Set((S.machine.graph.notes || []).map((note) => note.name));
    let n = 1;
    while (taken.has(`note_${n}`)) n += 1;
    const name = `note_${n}`;
    const key = noteKey(name);
    const middle = canvas.viewCenter();
    if (!await edit({ op: 'set_note', name, text: 'New note' }, { places: [key] })) return;
    if (middle) await savePositions({ [key]: { x: middle.x - NOTE.w / 2, y: middle.y - 30 } });
    await choose({ kind: 'note', id: name });
    focusNote(name, true);
  } finally {
    addingNote = false;
  }
}

async function openNote(name) {
  await choose({ kind: 'note', id: name });
  focusNote(name, false);
}

/** The note's text box, focused (`all`: its text chosen, to be typed over) -- if the note is the one shown: a choice
 * turned down (unapplied text elsewhere) leaves another form there. */
function focusNote(name, all) {
  if (S.selection?.kind !== 'note' || S.selection.id !== name) return;
  const area = $('side-inspect').querySelector('[data-form="note"] textarea');
  if (!area || area.readOnly) return;
  area.focus();
  if (all) area.select();
}

async function removeNote(name) {
  if (readOnly() || !noteOf(name) || notesShared()) return;
  if (!await confirm(`Remove the note ${name}?`, { title: 'Remove note', danger: true, confirmLabel: 'Remove' })) return;
  const key = noteKey(name);
  const placed = Object.hasOwn(positions(), key);
  if (!await edit({ op: 'set_note', name, text: null }, { from: 'note', places: placed ? [key] : null })) return;
  if (placed) await saveLayout({ positions: patched(positions(), { [key]: null }) });
  choose(null);
}

async function connect(source, target) {
  const before = S.machine.graph.transitions.filter((t) => t.source === source);
  const index = before.length ? Math.max(...before.map((t) => t.index)) + 1 : 0;
  if (await edit({ op: 'add_transition', source, target })) choose({ kind: 'transition', id: `${source}#${index}` });
}

async function removeTransition(id) {
  if (readOnly()) return;
  const t = transitionOf(id);
  if (!t || !await confirm(`Remove the transition ${t.source} → ${t.target ?? '(internal)'}?`,
    { title: 'Remove transition', danger: true, confirmLabel: 'Remove' })) return;
  if (await edit({ op: 'remove_transition', source: t.source, index: t.index })) choose({ kind: 'state', id: t.source });
}

// ------------------------------------------------------------------ selection and inspector

async function choose(selection) {
  const same = sameSelection(selection, S.selection);
  if (same && S.inspectorDrafts.size) {  // chosen again (a click, Enter, a problem link): keep what was typed
    canvas.select(selection);
    return;
  }
  if (!same && S.inspectorDrafts.size && !await confirm('The inspector has changes that are not applied. Discard them?',
    { danger: true, confirmLabel: 'Discard' })) {
    canvas.select(S.selection);  // the canvas marked the click already
    return;
  }
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
  const newEvent = writable && !S.machine.graph.locked?.includes('events');
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
      <select class="pk-select pk-select--sm" id="tr-trigger-${t.id}" name="trigger" data-shape="enum" data-orig="${t.trigger}">${triggers.map((name) => html`<option value="${name}" ${name === t.trigger ? 'selected' : ''}>${name === 'done' ? 'done (completion)' : name}</option>`)}
        ${newEvent ? html`<option value="${NEW_EVENT}">New event…</option>` : ''}</select>
      <label for="tr-target-${t.id}">Target</label>
      <select class="pk-select pk-select--sm" id="tr-target-${t.id}" name="target" data-shape="enum" data-orig="${t.target ?? ''}"><option value="">(internal: no target)</option>${states.map((name) => html`<option value="${name}" ${name === t.target ? 'selected' : ''}>${name}</option>`)}</select>
      <label for="tr-guard-${t.id}">Guard</label>
      ${(t.guard ?? '').includes('\n')  // an input drops line breaks: a guard over lines needs a text area
        ? html`<textarea class="pk-textarea pk-input--mono" id="tr-guard-${t.id}" name="guard" rows="3" data-shape="code" data-orig="${t.guard}">
${t.guard}</textarea>`
        : html`<input class="pk-input pk-input--sm pk-input--mono" id="tr-guard-${t.id}" name="guard" value="${t.guard ?? ''}" data-shape="line" data-orig="${t.guard ?? ''}" placeholder="Python expression, or else">`}
      <label for="tr-effect-${t.id}">Effect</label>
      <textarea class="pk-textarea pk-input--mono" id="tr-effect-${t.id}" name="effect" rows="2" data-shape="code" data-orig="${t.effect ?? ''}" placeholder="Python statements">
${t.effect ?? ''}</textarea>
      ${t.target ? html`<label for="tr-line-${t.id}">Line</label>
      <select class="pk-select pk-select--sm" id="tr-line-${t.id}" data-line="${t.id}" title="How the canvas draws every transition from ${t.source} to ${t.target}: kept in the layout, not in the YAML, and set at once">${lineChoices(lineStyles()[lineKeys(S.machine.graph.transitions)[t.id]], { inherit: true })}</select>` : ''}
    </div>
    ${problemList(pinned?.problems)}
    <div class="pk-form-actions"><button type="submit" class="pk-btn pk-btn--sm pk-btn--primary" ${writable ? '' : 'disabled'}>${icon('save', { size: 'sm' })} Apply</button></div>
  </form>`;
}

function drawInspector() {
  const pane = $('side-inspect');
  const m = S.machine;
  S.inspectorDrafts.clear();  // the forms are drawn anew: what was typed into them is gone (callers asked first)
  setDirty(savable());
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
  const note = sel?.kind === 'note' ? noteOf(sel.id) : null;
  if (note) {
    const shared = m.graph.locked?.includes('notes');
    const editable = m.writable && !shared;
    render(pane, html`<div class="sg-section">
      <div class="sg-inspect-head">${icon('notebook-pen')}<h3 class="sg-inspect-name">Note</h3><span class="pk-mono pk-muted">${note.name}</span></div>
      <form data-form="note" class="pk-stack">
        <textarea class="pk-textarea sg-note-input" name="text" rows="10" aria-label="The note's text" data-orig="${note.text}"
          placeholder="Free text: what the machine is for, what to watch, what is left to do" ${editable ? '' : 'readonly'}>
${note.text}</textarea>
        ${shared ? html`<p class="pk-help">${SHARED_HINT}</p>` : ''}
        <p class="pk-help">Kept in the file under notes: and drawn on the canvas; the engine never reads it. Drag the note to move it.</p>
        ${editable ? html`<div class="pk-form-actions">
          <button type="button" class="pk-btn pk-btn--sm pk-btn--danger" data-act="remove-note">${icon('trash-2', { size: 'sm' })} Remove</button>
          <button type="submit" class="pk-btn pk-btn--sm pk-btn--primary">${icon('save', { size: 'sm' })} Apply</button></div>` : ''}
      </form></div>`);
    return;
  }
  if (sel?.kind === 'many') {
    const names = sel.states.filter((name) => stateOf(name));
    const edges = sel.transitions.map((id) => transitionOf(id)).filter(Boolean);
    const ways = edges.filter((t) => t.target);  // an internal transition has no line
    const goes = removal(names, sel.transitions);
    const gone = [goes.states.length && counted(goes.states.length, 'state'),
      goes.transitions.length && counted(goes.transitions.length, 'transition')].filter(Boolean);
    render(pane, html`<div class="sg-section">
      <div class="sg-inspect-head"><h3 class="sg-inspect-name">${[names.length && counted(names.length, 'state'),
        edges.length && counted(edges.length, 'transition')].filter(Boolean).join(', ')}</h3></div>
      <div class="pk-row">${names.map((name) => html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost"
        data-select-state="${name}">${name}</button>`)}${edges.map((t) => html`<button type="button"
        class="pk-btn pk-btn--sm pk-btn--ghost pk-mono" data-select-transition="${t.id}">${arrow(t)}</button>`)}</div>
      ${ways.length ? html`<div class="sg-fields"><label for="many-line">Line${ways.length > 1 ? 's' : ''}</label>
        <select class="pk-select pk-select--sm" id="many-line" data-line="*" title="How the canvas draws the selected transitions (each the way it goes): kept in the layout, set at once">${lineChoices(
          styleOfAll(ways), { inherit: true, mixed: styleOfAll(ways) === undefined })}</select></div>` : ''}
      <p class="pk-help">Drag one of the states to move them all; Delete removes what is selected, Group puts the states
        into a new composite. Ctrl or Shift+click adds or takes out a state or a transition, Ctrl or Shift+drag draws a
        box and adds the states wholly inside it.</p>
      ${m.writable ? html`<div class="pk-row">
        ${goes.states.length ? html`<button type="button" class="pk-btn pk-btn--sm" data-act="group">
          ${icon('network', { size: 'sm' })} Group</button>` : ''}
        <button type="button" class="pk-btn pk-btn--sm pk-btn--danger" data-act="remove-selection">
          ${icon('trash-2', { size: 'sm' })} Remove ${goes.states.length + goes.transitions.length === 1 ? 'it' : gone.join(' and ')}</button>
      </div>` : ''}
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
  const fragment = stateFragment(drawnText(m), state.line, state.name);
  const lock = fragmentLock(drawnText(m), state.line, state.name);
  const applies = m.writable && !lock;
  render(pane, html`
    <div class="sg-section">
      <div class="sg-inspect-head">
        ${state.icon ? icon(state.icon) : ''}<h3 class="sg-inspect-name">${state.name}</h3>
        ${badge(state.composite ? 'composite' : state.after != null ? `timer ${state.after}` : state.wait ? 'wait state' : state.type)}
        ${state.kind ? badge(state.kind, 'accent') : ''}
        ${parentInitial === state.name ? badge('initial', 'info') : ''}
      </div>
      ${state.label ? html`<div class="sg-mono">${state.label}</div>` : ''}
      <form data-form="state-description" class="pk-stack sg-description">
        <div class="sg-fields">${field('description', STATE_FIELD_SCHEMA.description, state.description,
          { locked: state.locked?.includes('description'), prefix: 'sd' })}</div>
        ${m.writable && !state.locked?.includes('description') ? html`<div class="pk-form-actions">
          <button type="submit" class="pk-btn pk-btn--sm">${icon('save', { size: 'sm' })} Apply</button></div>` : ''}
      </form>
      <div class="pk-row sg-actions">
        <button type="button" class="pk-btn pk-btn--sm" data-act="rename" ${m.writable ? '' : 'disabled'}>${icon('pencil', { size: 'sm' })} Rename</button>
        <button type="button" class="pk-btn pk-btn--sm" data-act="initial" ${m.writable && parentInitial !== state.name ? '' : 'disabled'} title="Make it the initial state of its region">${icon('play', { size: 'sm' })} Initial</button>
        <button type="button" class="pk-btn pk-btn--sm pk-btn--danger" data-act="remove" ${m.writable ? '' : 'disabled'}>${icon('trash-2', { size: 'sm' })} Remove</button>
      </div>
      <label class="pk-field sg-parent">Inside
        <select class="pk-select pk-select--sm" data-act="parent" ${m.writable ? '' : 'disabled'} title="The composite it sits in: another one moves it there">
          ${parentChoices(state).map((one) => html`<option value="${one}" ${one === (state.parent || '') ? 'selected' : ''}>${one || '(top level)'}</option>`)}
        </select>
      </label>
      ${problemList(pinned?.problems)}
    </div>
    <div class="sg-section">
      <h4 class="sg-section-title">Breakpoints ${live ? html`<span class="pk-badge pk-badge--info">run ${shorten(live.id, 12)}</span>` : html`<span class="pk-muted">(next run)</span>`}</h4>
      ${hooks.length ? html`<div class="pk-row sg-checks">
        ${hooks.map((at) => html`<label class="pk-check"><input type="checkbox" data-breakpoint="${at}" ${points.has(at) ? 'checked' : ''}> ${at}</label>`)}
      </div>` : ''}
      ${why ? html`<p class="pk-help">${why}</p>` : ''}
    </div>
    ${state.type === 'state' && !state.composite ? html`<div class="sg-section">
      <h4 class="sg-section-title">Activity</h4>
      <form data-form="activity" class="pk-stack">
        ${state.locked?.includes('do') ? html`<p class="pk-help">${SHARED_HINT}</p>` : ''}
        <div class="sg-fields"><label for="af-kind">kind</label>
          <select class="pk-select pk-select--sm" id="af-kind" name="kind" data-orig="${state.kind || ''}" ${m.writable && !state.locked?.includes('do') ? '' : 'disabled'}>
            <option value="">(none: waits, or passes on)</option>
            ${S.kinds.map((k) => html`<option value="${k.key}" ${k.key === state.kind ? 'selected' : ''}>${k.title} (${k.key})</option>`)}
          </select></div>
        <div id="activityFields">${activityFields(state.kind, state)}</div>
        <div class="pk-form-actions"><button type="submit" class="pk-btn pk-btn--sm pk-btn--primary" ${m.writable ? '' : 'disabled'}>${icon('save', { size: 'sm' })} Apply</button></div>
      </form>
    </div>` : ''}
    <div class="sg-section">
      <h4 class="sg-section-title">Settings</h4>
      <form data-form="state-fields" class="pk-stack">
        ${stateFieldNames(state).some((name) => state.locked?.includes(name)) ? html`<p class="pk-help">${SHARED_HINT}</p>` : ''}
        <div class="sg-fields">${stateFieldNames(state).map((name) => field(name, STATE_FIELD_SCHEMA[name], stateValue(state, name), {
          text: state.yaml?.[name], locked: state.locked?.includes(name), prefix: 'sf' }))}</div>
        <div class="pk-form-actions"><button type="submit" class="pk-btn pk-btn--sm pk-btn--primary" ${m.writable ? '' : 'disabled'}>${icon('save', { size: 'sm' })} Apply</button></div>
      </form>
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
        ${codeBox(fragment, html`<textarea class="pk-textarea pk-input--mono sg-fragment" name="yaml" spellcheck="false" wrap="off"
          aria-label="The state's YAML" ${applies ? '' : 'readonly'}>${fragment}</textarea>`)}
        ${lock ? html`<p class="pk-help">${lock}: edit it in the YAML tab.</p>` : ''}
        <div class="pk-form-actions"><button type="submit" class="pk-btn pk-btn--sm pk-btn--primary" ${applies ? '' : 'disabled'}>${icon('save', { size: 'sm' })} Apply</button></div>
      </form>
    </div>
    <div class="sg-section" id="stateFrames" hidden></div>`);
  drawStateFrames();
}

/** The machine agents that run this machine (a SAM, the chat, agent-cli --agent reach it through them), and the
 * agent: block that offers it as one -- no config entry: every process declares it as it starts. */
function agentSection(m) {
  const agents = m.agents || [];
  const offer = m.offer;
  const listed = agents.length ? html`<ul class="sg-plain-list">${agents.map((a) => html`<li class="sg-watch">
      <span class="pk-mono">${a.name}</span> ${badge(a.visibility, a.visibility === 'private' ? '' : 'info')}
      <span class="pk-muted">input ${a.input}, on a wait: ${a.on_wait}</span>
      ${a.problems.length ? html`<div class="pk-text--danger">${a.problems.join('; ')}</div>` : ''}</li>`)}</ul>
    ${agents.some((a) => a.visibility === 'private') ? html`<p class="pk-help">A private agent is reached only by its name
      (agent-cli --agent, writer_jobs): a SAM does not offer it, the chat does not list it.</p>` : ''}` : '';
  if (offer) {
    return html`${listed}<p class="pk-help">Its <span class="pk-mono">agent:</span> block offers it as
      <span class="pk-mono">${offer.name}</span>${offer.declared ? '.' : ' after a restart: every process reads the agent: blocks as it starts.'}</p>`;
  }
  return html`${listed}<p class="pk-help">${agents.length ? 'An agent: block in its settings offers it under a name of its own'
    : 'No agent runs this machine. An agent: block like this one in its settings offers it as one after a restart'}
    -- no config entry.</p>
    <details class="pk-details" ${agents.length ? '' : 'open'}><summary>agent: block</summary>
    <pre class="sg-result-text" id="agentEntry">${agentEntry(m)}</pre>
    <button type="button" class="pk-btn pk-btn--sm" data-act="copy-agent-entry">${icon('copy', { size: 'sm' })} Copy</button></details>`;
}

/** The agent: block that offers the machine as an agent: one text param is the message, else a JSON object. */
function agentEntry(m) {
  const g = m.graph;
  const params = Object.keys(g.params || {});
  const text = params.length === 1 && [undefined, 'string', 'any'].includes(g.params[params[0]].type);
  return ['agent:', `  name: ${m.id}_agent`,
    text ? `  input: text        # the message is the param ${params[0]}` : '  input: json        # the message is a JSON object of the params',
    ...(text ? [`  task_param: ${params[0]}`] : []),
    '  on_wait: ask       # a wait state asks in the conversation (called as a tool it waits); block: the request waits until the run ends',
    '  visibility: tool   # a SAM may start it; both: the chat lists it too; private (default): only by its name'].join('\n');
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
        <dt>states</dt><dd>${g.states.length}</dd><dt>transitions</dt><dd>${g.transitions.length}</dd>
        ${m.runner ? html`<dt>runner</dt><dd class="pk-mono" title="Hosts its runs: its tool allowlist is what the machine's tool activities may call. Chosen by the machine's folder (runs_machines_in).">${m.runner}</dd>` : ''}</dl>
      ${m.runner_problem ? html`<div class="pk-callout pk-callout--warn">${m.runner_problem}</div>` : ''}
      <div class="sg-fields"><label for="machine-line">Lines</label>
        <select class="pk-select pk-select--sm" id="machine-line" data-line-default title="How the canvas draws the transitions that have no style of their own: kept in the layout, set at once">${lineChoices(lineStyle(m.layout?.line))}</select></div>
      <p class="pk-help">Click a state or a transition to edit it. New states from the bar above the graph go into the selected composite.
        Ctrl or Shift+click selects several states and transitions (on a state also +Enter), a Ctrl or Shift+drag box the states in it: drag
        one to move them all, Delete removes them, Group puts the states into a new composite. Note in the bar adds a
        note of free text: drag it anywhere, click it to edit it.</p>
    </div>
    ${m.problems.length ? html`<div class="sg-section"><h4 class="sg-section-title">Problems</h4>${problemButtons(m.problems)}</div>` : ''}
    <div class="sg-section"><h4 class="sg-section-title">Settings</h4>
      <form data-form="machine-fields" class="pk-stack">
        <div class="sg-fields">${Object.keys(MACHINE_FIELD_SCHEMA).map((name) => field(name, MACHINE_FIELD_SCHEMA[name],
          g[name] ?? undefined, { text: g.yaml?.[name], locked: g.locked?.includes(name), prefix: 'mf' }))}</div>
        ${g.python ? html`<p class="pk-help">Companion module: <span class="pk-mono">${g.python}</span> (YAML tab)</p>` : ''}
        <div class="pk-form-actions"><button type="submit" class="pk-btn pk-btn--sm pk-btn--primary" ${m.writable ? '' : 'disabled'}>${icon('save', { size: 'sm' })} Apply</button></div>
      </form></div>
    ${Object.keys(g.imports || {}).length ? html`<div class="sg-section"><h4 class="sg-section-title">Imported machines</h4>
      ${table(Object.entries(g.imports).map(([alias, ref]) => html`<tr><td class="pk-mono">${alias}</td>
        <td><button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-open-machine="${importedId(ref)}">${ref}</button></td></tr>`), ['Alias', 'Machine'])}</div>` : ''}
    <div class="sg-section"><h4 class="sg-section-title">As an agent</h4>${agentSection(m)}</div>`;
}

// ------------------------------------------------------------------ inspector forms: fields from the schemas

/** A state's own keys (StateSpec, model/spec.py), in the shape a kind's JSON schema gives its fields. */
const STATE_FIELD_SCHEMA = {
  type: { enum: ['state', 'choice', 'junction', 'final'], description: 'state: may run an activity; choice / junction: decided within a transition; final: ends its region' },
  description: { type: 'string', description: 'free text for whoever reads the machine: shown with the state, never run' },
  max_visits: { type: 'integer', description: 'entries of this state per frame; one more raises loop_limit' },
  timeout: { anyOf: [{ type: 'number' }, { type: 'string' }], description: 'wait state: raise wait_timeout after this long (30s, 5m)' },
  after: { anyOf: [{ type: 'number' }, { type: 'string' }], description: 'timer state: complete this long after entry (10m); an event it takes may come first' },
  entry: { type: 'string', 'x-code': true, description: 'Python statements, run on entry' },
  exit: { type: 'string', 'x-code': true, description: 'Python statements, run on exit' },
  status: { enum: ['succeeded', 'failed'], description: 'final state of the root region: how the run ends' },
  output: { 'x-yaml': true, description: 'final state: what the run or the calling state gets (templates)' },
  finally: { type: 'object', description: 'an activity that runs once on every exit of this state' },
};
/** The machine's own keys (MachineSpec) the inspector sets; id is the file, states and initial have their own edits. */
const MACHINE_FIELD_SCHEMA = {
  title: { type: 'string' },
  description: { type: 'string' },
  group: { type: 'string', description: 'its folder in the machine list, nested by / (Reviews/nightly)' },
  vars_from: { type: 'string', description: 'agent whose configured template_vars lie under vars' },
  params: { type: 'object', description: 'name: {type: string | integer | number | boolean | object | array, required, default, enum, description}' },
  events: { type: 'object', description: 'name: {description, data (a JSON schema of what it carries)}' },
  context: { type: 'object', description: 'the run context and its start values' },
  vars: { type: 'object', description: 'agent template vars: a map of templates' },
  imports: { type: 'object', description: 'alias: ./file.yaml or machine id' },
  machines: { type: 'object', description: 'machines inside this file -- name: {params, context, initial, states, ...}; machine: <name> runs one; they share the companion module and imports' },
  resources: { type: 'object', description: 'name: {open, fork, close} activities' },
  agent: { type: 'object', description: 'offer this machine as an agent, no config entry -- {name, description, input, task_param, on_wait, params, promote, visibility (default private)}; every process declares it as it starts' },
  limits: { type: 'object', description: 'max_steps, timeout, concurrency' },
  finally: { type: 'object', description: 'an activity that runs once when the machine ends' },
};
const COMMON_FIELDS = ['timeout', 'retry', 'idempotent', 'description'];  // every kind has them: listed last
const SHARED_HINT = 'Some of this is shared with another place through a YAML anchor, alias or merge: those fields are edited in the YAML tab.';

/** The Settings form's fields: the description has a form of its own, at the top. */
function stateFieldNames(state) {
  if (state.composite) return ['max_visits', 'entry', 'exit', 'finally'];
  if (state.type === 'final') return ['type', 'status', 'output'];
  if (state.type !== 'state') return ['type'];
  return ['type', 'max_visits', ...(state.wait || state.timeout != null ? ['timeout'] : []),
    ...(state.kind ? [] : ['after']), 'entry', 'exit', 'finally'];
}

const stateValue = (state, name) => (name === 'type' ? state.type : state[name] ?? undefined);

/** How a field is edited: enum, bool, number, duration (a number or 30s), line, text (a template), code, yaml -- and
 * a description, prose (free text over lines; see field). */
function shapeOf(p = {}, value) {
  if (value !== null && typeof value === 'object') return 'yaml';
  if (p['x-yaml']) return 'yaml';
  if (p['x-code']) return 'code';
  const options = [p, ...(p.anyOf || [])];
  if (options.some((o) => o.enum)) return 'enum';
  const types = new Set(options.flatMap((o) => [].concat(o.type || (o.$ref || o.allOf ? 'object' : []))));
  types.delete('null');
  if (!types.size) return 'text';
  if (types.has('object') || types.has('array')) return 'yaml';
  if (types.size === 1 && types.has('boolean')) return 'bool';
  if ([...types].every((t) => t === 'integer' || t === 'number')) return 'number';
  return types.has('number') || types.has('integer') ? 'duration' : 'line';
}

/** One labelled control; data-orig holds what it showed, so a submit sends only what changed. */
function field(name, p = {}, value, { text, locked = false, required = false, prefix = 'af' } = {}) {
  let shape = shapeOf(p, value);
  if (name === 'description' && shape === 'line') shape = 'prose';  // free text, lines of its own
  const id = `${prefix}-${name}`;
  const orig = shape === 'yaml' ? (text ?? (value === undefined || value === null ? '' : JSON.stringify(value)))
    : value === undefined || value === null ? '' : String(value);
  if ((shape === 'line' || shape === 'duration') && orig.includes('\n')) shape = 'text';  // an input drops line breaks
  const off = !S.machine.writable || locked;
  const common = { id, name, shape, orig, off };
  const label = html`<label for="${id}" title="${p.description || ''}">${name}${required ? ' *' : ''}</label>`;
  if (shape === 'enum' || shape === 'bool') {
    const options = shape === 'bool' ? ['true', 'false'] : [...new Set([p, ...(p.anyOf || [])].flatMap((o) => o.enum || []))];
    return html`${label}<select class="pk-select pk-select--sm" ${attrs(common)}>
      <option value="">${required ? '(choose)' : '(default)'}</option>
      ${options.map((o) => html`<option value="${o}" ${String(o) === orig ? 'selected' : ''}>${o}</option>`)}</select>${fieldHelp(p)}`;
  }
  if (shape === 'yaml' || shape === 'text' || shape === 'code' || shape === 'prose') {
    const prose = shape === 'prose';
    const rows = Math.min(8, Math.max(prose ? 3 : 2, orig.split('\n').length));
    return html`${label}<div class="pk-stack"><textarea class="pk-textarea${prose ? '' : ' pk-input--mono'} sg-field-text" rows="${rows}" spellcheck="${String(prose)}"
      placeholder="${{ yaml: 'YAML', code: 'Python', prose: 'free text' }[shape] ?? 'text, {{ templates }}'}" ${attrs(common)}>
${orig}</textarea>
      ${locked ? html`<span class="pk-help">Uses a YAML anchor, alias or merge: edit it in the YAML tab.</span>` : ''}</div>${fieldHelp(p)}`;
  }
  // (the line break after <textarea> above is dropped by the parser: a value that starts with one keeps it)
  return html`${label}<input class="pk-input pk-input--sm${shape === 'line' ? '' : ' pk-input--mono'}" ${shape === 'number' ? html`type="number"` : ''}
    ${CHOICES_OF[name] ? html`list="${CHOICES_OF[name]}" autocomplete="off"` : ''} value="${orig}" ${attrs(common)}>${fieldHelp(p)}`;
}

const attrs = ({ id, name, shape, orig, off }) => html`id="${id}" name="${name}" data-shape="${shape}" data-orig="${orig}" ${off ? 'disabled' : ''}`;

/** Fields whose values the panel can offer: the datalist (panel.html) with the catalog's agents, tools, profiles
 * and the machines. */
const CHOICES_OF = { agent: 'sgAgents', by: 'sgAgents', vars_from: 'sgAgents', tool: 'sgTools', profile: 'sgProfiles', machine: 'sgMachines' };
const NEW_EVENT = '+new-event';  // the trigger select's "New event…": no event name has a +

const fieldHelp = (p) => (p.description ? html`<span class="pk-help sg-field-help">${p.description}</span>` : '');

/** The catalog for a machine: its runner (by its folder) decides the tools a tool activity may call. */
async function loadCatalog(machineId = null) {
  S.catalogFor = machineId;
  let catalog;
  try {
    catalog = await api(`${API}/catalog${machineId ? `?machine_id=${enc(machineId)}` : ''}`, { quiet: true });
  } catch (error) {
    if (S.catalogFor === machineId) {  // the fields stay plain inputs: not with the last machine's names
      S.catalogFor = undefined;  // the next showMachine asks again
      S.catalog = null;
      for (const list of ['sgAgents', 'sgTools', 'sgProfiles']) render($(list), []);
    }
    return;
  }
  if (S.catalogFor !== machineId) return;  // another machine opened meanwhile: its catalog is on its way
  S.catalog = catalog;
  const options = (items) => items.map((item) => html`<option value="${item.name}">${shorten(item.description || '', 80)}</option>`);
  render($('sgAgents'), options(S.catalog.agents || []));
  render($('sgTools'), options(S.catalog.tools || []));
  render($('sgProfiles'), (S.catalog.profiles || []).map((name) => html`<option value="${name}"></option>`));
}

/** The machines a machine activity may name: this machine's import aliases, then every machine id. */
function drawMachineChoices() {
  const aliases = Object.keys(S.machine?.graph?.imports || {});
  render($('sgMachines'), [...(S.machine?.graph?.machines || []).map((name) => html`<option value="${name}">in this file</option>`),
    ...aliases.map((alias) => html`<option value="${alias}">import of ${S.machine.graph.imports[alias]}</option>`),
    ...S.machines.map((m) => html`<option value="${m.id}">${m.title || ''}</option>`)]);
}

/** The fields of an activity kind (its JSON schema from /kinds): the kind's key first, then what it needs. */
function activityFields(kind, state) {
  if (!kind) return html`<p class="pk-help">No activity: the state waits for an event, or its transitions go on at once.</p>`;
  const spec = S.kinds.find((k) => k.key === kind);
  if (!spec) return html`<p class="pk-help">The kind ${kind} is not known here: edit it in the YAML.</p>`;
  const props = spec.schema.properties || {};
  const required = new Set(spec.schema.required || []);
  const names = [kind, ...Object.keys(props).filter((n) => n !== kind && required.has(n)),
    ...Object.keys(props).filter((n) => n !== kind && !required.has(n) && !COMMON_FIELDS.includes(n)),
    ...COMMON_FIELDS.filter((n) => n in props)];
  // another kind chosen: what the file keeps through the change (timeout, retry, ...) shows, the rest is empty
  return html`${spec.summary ? html`<p class="pk-help">${spec.summary}</p>` : ''}<div class="sg-fields">${names.map((name) => field(name, props[name],
    state?.do?.[name], { text: state?.yaml?.[`do.${name}`],
      locked: state?.locked?.includes('do') || state?.locked?.includes(`do.${name}`),
      required: required.has(name) }))}</div>`;
}

/** A control's value as the edit sends it; `changed` compares it with what it showed. */
function fieldValue(control) {
  const raw = control.value;
  switch (control.dataset.shape) {
    case 'yaml': return raw.trim() ? { $yaml: raw } : null;
    case 'number': {
      // a browser gives '' for what does not parse as a number: without this, a typo would remove the value
      if (control.validity?.badInput) throw new FormError(`${control.name}: that is not a number.`);
      if (raw.trim() === '') return null;
      if (Number.isNaN(Number(raw))) throw new FormError(`${control.name}: ${raw.trim()} is not a number.`);
      return Number(raw);
    }
    case 'duration': return raw.trim() === '' ? null : /^\d+(\.\d+)?$/.test(raw.trim()) ? Number(raw) : raw.trim();
    case 'bool': return raw === '' ? null : raw === 'true';
    case 'text': case 'code': case 'prose': return raw.replace(/\s+$/, '') || null;
    default: return raw.trim() || null;
  }
}
const changed = (control) => control.dataset.shape !== undefined && !control.disabled
  && (control.validity?.badInput || control.value.replace(/\s+$/, '') !== (control.dataset.orig ?? '').replace(/\s+$/, ''));

/** The changed fields of a form: {name: value}. */
function changedFields(form) {
  const out = {};
  for (const control of form.elements) if (control.name && control.name !== 'kind' && changed(control)) out[control.name] = fieldValue(control);
  return out;
}

/** The activity form as an update_state: the changed keys; another kind drops the old one's keys it has not. */
function activityEdit(form, state) {
  const kind = form.elements.kind.value;
  if (!kind) return state.kind ? { fields: { do: null } } : null;
  const edits = changedFields(form);
  if (kind !== state.kind) {
    // without its key, dropping the old kind's would leave no activity at all
    if (edits[kind] == null) throw new FormError(`Set ${kind} first: it says what the activity does.`);
    const props = S.kinds.find((k) => k.key === kind)?.schema.properties || {};
    for (const key of Object.keys(state.do || {})) if (!(key in props)) edits[key] = null;
  }
  return Object.keys(edits).length ? { do: edits } : null;
}

/** A form value the edit cannot take; the panel says why instead of sending it. */
class FormError extends Error {}

const nonEmpty = (fields) => (Object.keys(fields).length ? fields : null);
/** The field forms as edits: the request, or null when nothing changed. */
const FIELD_FORMS = {
  activity: (form, state) => {
    const op = state && activityEdit(form, state);
    return op ? { op: 'update_state', name: state.name, ...op } : null;
  },
  'state-fields': (form, state) => {
    const fields = state && nonEmpty(changedFields(form));
    return fields ? { op: 'update_state', name: state.name, fields } : null;
  },
  'state-description': (form, state) => FIELD_FORMS['state-fields'](form, state),
  'machine-fields': (form) => {
    const fields = nonEmpty(changedFields(form));
    return fields ? { op: 'update_machine', fields } : null;
  },
};

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
  if (target.dataset.selectTransition) return choose({ kind: 'transition', id: target.dataset.selectTransition });
  if (target.dataset.problem !== undefined && !S.selection) return goToProblem(S.machine.problems[Number(target.dataset.problem)]);
  if (target.dataset.openMachine) return openMachine(target.dataset.openMachine, { keepRun: true });  // a run of this one shows its frame
  if (act === 'remove-selection' && S.selection?.kind === 'many') return removeSelection(S.selection.states, S.selection.transitions);
  if (act === 'group' && S.selection?.kind === 'many') return groupStates(S.selection.states);
  if (act === 'rename' && name) return renameState(name);
  if (act === 'initial' && name) {
    const state = stateOf(name);
    return edit({ op: 'set_initial', name, parent: state?.parent ?? null });
  }
  if (act === 'remove' && name) return removeState(name);
  if (act === 'remove-note' && S.selection?.kind === 'note') return removeNote(S.selection.id);
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
    if (form.dataset.form in FIELD_FORMS) {
      let request;
      try {
        request = FIELD_FORMS[form.dataset.form](form, S.selection?.kind === 'state' ? stateOf(S.selection.id) : null);
      } catch (error) {
        if (!(error instanceof FormError)) throw error;
        return toast(error.message, { kind: 'warn' });
      }
      if (!request) return toast('Nothing changed', { kind: 'info' });
      await edit(request, { from: FORM_DRAFTS[form.dataset.form] });
    } else if (form.dataset.form === 'set-state' && S.selection?.kind === 'state') {
      await edit({ op: 'set_state', name: S.selection.id, yaml: form.elements.yaml.value }, { from: 'state' });
    } else if (form.dataset.form === 'note' && S.selection?.kind === 'note') {
      const name = S.selection.id;
      const text = form.elements.text.value.replace(/\s+$/, '');
      // what the file holds as the textarea shows it: a | block ends in a line break the typed text loses here
      if (text === (noteOf(name)?.text ?? '').replace(/\s+$/, '')) return toast('Nothing changed', { kind: 'info' });
      if (!text.trim()) return removeNote(name);  // emptied: the note goes, after asking
      await edit({ op: 'set_note', name, text }, { from: 'note' });
    } else if (form.dataset.form === 'add-transition' && S.selection?.kind === 'state') {
      await connect(S.selection.id, form.elements.target.value);
    } else if (form.dataset.transition) {
      const t = transitionOf(form.dataset.transition);
      if (!t) return;
      // only what changed: a key sent unchanged would be written anew (a guard's layout, an explicit trigger: done)
      const fields = changedFields(form);
      if ('trigger' in fields && fields.trigger === 'done') fields.trigger = null;  // completion: no trigger key
      if (!Object.keys(fields).length) return toast('Nothing changed', { kind: 'info' });
      await edit({ op: 'update_transition', source: t.source, index: t.index, fields }, { from: draftKey(form) });
    }
  });
});

/** The composites a state can go into, and '' for the top level: not itself, nor one inside it. */
function parentChoices(state) {
  const inside = (name) => { for (let n = name; n; n = stateOf(n)?.parent) if (n === state.name) return true; return false; };
  return ['', ...S.machine.graph.states.filter((s) => s.composite && !inside(s.name)).map((s) => s.name)];
}

$('side-inspect').addEventListener('change', async (event) => {
  // line styles: layout, set at once (no Apply, no undo -- like a drag)
  if (event.target.dataset.lineDefault !== undefined) return saveLayout({ line: event.target.value });
  if (event.target.dataset.act === 'parent' && S.selection?.kind === 'state') {
    const name = S.selection.id;
    const into = event.target.value || null;
    if (into === (stateOf(name)?.parent ?? null)) return;
    await moveState(name, into);
    event.target.value = stateOf(name)?.parent || '';  // refused or cancelled: where it still is (moved: redrawn)
    return;
  }
  if (event.target.dataset.line !== undefined) {
    const ids = event.target.dataset.line === '*' ? (S.selection?.kind === 'many' ? S.selection.transitions : []) : [event.target.dataset.line];
    return setLines(ids, event.target.value);
  }
  if (event.target.name === 'trigger' && event.target.value === NEW_EVENT) {
    const t = transitionOf(event.target.closest('[data-transition]')?.dataset.transition);
    if (t) await newEventFor(t, event.target);
    return;
  }
  if (event.target.name === 'kind' && event.target.closest('[data-form="activity"]')) {
    const state = S.selection?.kind === 'state' ? stateOf(S.selection.id) : null;
    if (state) render($('activityFields'), activityFields(event.target.value, state));
    S.inspectorDrafts.add('activity');
    setDirty(true);
    return;
  }
  const box = event.target.closest('[data-breakpoint]');
  if (!box || S.selection?.kind !== 'state') return;
  await toggleBreakpoint(S.selection.id, box.dataset.breakpoint, box.checked);
});

// paint() and followScroll() no-op for anything but the coloured YAML box: harmless on every other field here
$('side-inspect').addEventListener('input', (event) => {
  paint(event.target);
  if (event.target.value === NEW_EVENT) return;  // a dialog asks for it; the select goes back to what it showed
  if (event.target.dataset.line !== undefined || event.target.dataset.lineDefault !== undefined) return;  // no draft: set at once
  const key = draftKey(event.target.closest('form'));
  if (key) {
    S.inspectorDrafts.add(key);
    setDirty(true);
  }
});
$('side-inspect').addEventListener('scroll', (event) => followScroll(event.target), true);

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

// ------------------------------------------------------------------ coloured YAML boxes

// a pre drops a last empty line: pad so the coloured copy never ends one line short of the textarea's text
const padded = (text) => `${text}${text.endsWith('\n') || !text ? ' ' : ''}`;

/** A YAML textarea over a coloured copy of `text` (the YAML tab's file, the inspector's state fragment): the copy
 * starts coloured already, so a fresh render never shows plain text first; `paint` repaints it when the textarea's
 * own value changes without a fresh render (typing, a revert, a reload). */
const codeBox = (text, textarea) => html`<div class="sg-code"><pre class="sg-code-view" aria-hidden="true">${yamlCode(padded(text))}</pre>${textarea}</div>`;

/** Python coloured by the vendored Prism the page loads (it escapes the text); plain where it is missing. */
const pythonCode = (text) => (globalThis.Prism?.languages?.python
  ? trusted(globalThis.Prism.highlight(text, globalThis.Prism.languages.python, 'python')) : html`${text}`);

function paint(area) {
  const view = area.previousElementSibling;
  if (!view?.classList.contains('sg-code-view')) return;
  const text = padded(area.value);
  render(view, area.dataset.lang === 'python' ? pythonCode(text) : yamlCode(text));
  followScroll(area);
}

function followScroll(area) {
  const view = area.previousElementSibling;
  if (!view?.classList.contains('sg-code-view')) return;
  view.scrollTop = area.scrollTop;
  view.scrollLeft = area.scrollLeft;
}

function setCode(area, text) {
  area.value = text;
  paint(area);
}

// ------------------------------------------------------------------ YAML tab

function yamlText(path) {
  return path in S.drafts ? S.drafts[path] : S.machine.files[path] ?? '';
}

const isPython = (path) => /\.py$/i.test(path || '');
const PYTHON_KEY = /^python[ \t]*:/m;

/** The open file into the text box, coloured as what it is. */
function showFile() {
  const area = $('yamlText');
  const lang = isPython(S.yamlFile) ? 'python' : 'yaml';
  if (area.value === yamlText(S.yamlFile) && area.dataset.lang === lang) return;
  area.dataset.lang = lang;
  setCode(area, yamlText(S.yamlFile));
}

function drawYaml() {
  const m = S.machine;
  const files = [...new Set([...Object.keys(m.files), ...Object.keys(S.drafts)])];  // a new module is a draft first
  if (!files.includes(S.yamlFile)) S.yamlFile = m.root_file;
  render($('yamlFile'), files.map((path) => html`<option value="${path}" ${path === S.yamlFile ? 'selected' : ''}>${path}${path in S.drafts ? ' (unsaved)' : ''}</option>`));
  showFile();
  const area = $('yamlText');
  area.readOnly = !m.writable;
  $('yamlAddModule').hidden = !m.writable || PYTHON_KEY.test(yamlText(m.root_file));
  drawSaveControls();
  drawProblems($('yamlProblems'), m.problems, m.draft !== undefined && m.draft !== m.files[m.root_file] ? 'Unsaved text' : 'Saved file');
}

/** Problems as buttons that go where each one is (data-problem: its index in `problems`). */
const problemButtons = (problems) => problems.map((p, i) => html`<button type="button" class="pk-btn pk-btn--ghost sg-problem" data-problem="${i}">
      ${badge(p.code, p.level === 'error' ? 'danger' : 'warn')}<span class="pk-grow">${p.message}
      <span class="sg-problem-where">${[posixPath(p.file)?.split('/').pop(), p.line && `line ${p.line}`, p.path].filter(Boolean).join(' · ')}</span></span></button>`);

function drawProblems(element, problems, label) {
  if (!problems.length) {
    render(element, html`<div class="pk-callout pk-callout--ok">${label}: no problems.</div>`);
    return;
  }
  render(element, html`<div class="pk-help">${label}: ${problems.length} problem${problems.length > 1 ? 's' : ''}. Click one to go there.</div>
    ${problemButtons(problems)}`);
  element.problems = problems;
}

$('yamlFile').addEventListener('change', () => {
  S.yamlFile = $('yamlFile').value;
  showFile();
});

const MODULE_TEXT = (id) => `"""Companion module of ${id}.yaml.

Its public names (not starting with _) are in scope in this machine's code fields and templates, and its
functions can be \`call\` and \`parse\` targets. A function that code fields or templates use must be pure:
a run replays them. A \`call\` function runs as an activity; a first parameter named \`sg\` gets
\`sg.tool()\` and \`sg.Error\`.
"""
`;

/** A companion module as two drafts, saved like any other change: `python: <id>.py` after the id line, and the file. */
$('yamlAddModule').addEventListener('click', () => {
  const m = S.machine;
  const root = yamlText(m.root_file);
  const idLine = root.match(/^id[ \t]*:[ \t]*[^\s#].*$/m);  // with its value: `id:` over two lines takes no line after it
  const module = `${m.id}.py`;
  if (!idLine || PYTHON_KEY.test(root)) {
    toast(`Add "python: ${module}" to the YAML yourself: there is no top-level id line to put it after.`, { kind: 'warn' });
    return;
  }
  const at = idLine.index + idLine[0].length;
  S.drafts[m.root_file] = `${root.slice(0, at)}\npython: ${module}${root.slice(at)}`;
  if (!(module in m.files) && !(module in S.drafts)) S.drafts[module] = MODULE_TEXT(m.id);
  setDirty(unsaved());
  S.yamlFile = module;
  drawYaml();
});

$('yamlText').addEventListener('input', () => {
  paint($('yamlText'));
  const text = $('yamlText').value;
  if (text === S.machine.files[S.yamlFile]) delete S.drafts[S.yamlFile];
  else S.drafts[S.yamlFile] = text;
  setDirty(unsaved());
  drawSaveControls();
});

$('yamlText').addEventListener('scroll', () => followScroll($('yamlText')));

$('yamlText').addEventListener('keydown', (event) => {
  if (event.key !== 'Tab' || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.readOnly) return;  // setRangeText ignores readonly; Tab then moves on, as on any field
  event.preventDefault();  // indented with spaces: Tab types two of them in YAML, four in Python
  const area = event.target;
  area.setRangeText(isPython(S.yamlFile) ? '    ' : '  ', area.selectionStart, area.selectionEnd, 'end');
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
  const file = posixPath(problem.file);
  const path = Object.keys(S.machine.files).find((p) => file && (file === p || file.endsWith(`/${p}`)));
  if (path && problem.line) {
    selectTab($('mainTabs'), 'yaml');  // from the inspector's overview it is not the tab shown
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
  if (!S.autosave) {  // the graph drawn from the text: graph edits go on from it
    if (await dropTyped()) await drawDrafts();
    return;
  }
  try {
    const result = await api(`${API}/validate`, { method: 'POST', json: { files: allFiles(), machine_id: S.machine.id } });
    drawProblems($('yamlProblems'), result.problems, hasDrafts() ? 'Unsaved text' : 'Saved file');
  } catch (error) { /* toasted */ }
}));

$('yamlRevert').addEventListener('click', async () => {
  if (!await confirm('Discard every unsaved change?', { danger: true, confirmLabel: 'Discard' })) return;
  if (S.autosave) {  // the graph shows the saved file: only the text goes
    S.drafts = {};
    setDirty(unsaved());
    drawYaml();
    return;
  }
  await openMachine(S.machine.id, { keepRun: true, discard: true });  // graph and layout as saved
});

$('yamlSave').addEventListener('click', () => withBusy($('yamlSave'), saveAll));

async function saveYaml(force, layout = null) {
  const m = S.machine;
  if (!force && S.inspectorDrafts.size && !await confirm('The inspector has changes that are not applied: saving '
    + 'reloads the machine and drops them. Save anyway?', { danger: true, confirmLabel: 'Save' })) return;
  const files = { ...S.drafts };
  const expected = Object.fromEntries(Object.keys(files).filter((p) => p in m.versions).map((p) => [p, m.versions[p]]));
  try {
    await api(`${API}/machines/${enc(m.id)}`, { method: 'PUT', json: { files, expected_versions: expected, force }, quiet: true });
  } catch (error) {
    if (isAborted(error)) return;
    // a new file (the module button's) whose name another file has already: not a change, nothing to reload
    const inTheWay = error.status === 409
      && Object.keys(files).find((p) => !(p in m.versions) && String(error.detail ?? '').startsWith(`${p} exists already`));
    if (inTheWay) {
      const choice = await dialog({
        title: 'The file exists already',
        message: `${inTheWay} exists already next to the machine but is not one of its files: an earlier module, or one another machine uses. Use it as it is (your new text for it is dropped), or cancel and give the new file another name in the YAML.`,
        actions: [{ label: 'Cancel', value: null }, { label: 'Use the existing file', value: 'use', primary: true }],
      });
      if (choice === 'use') {
        delete S.drafts[inTheWay];
        await saveYaml(force, layout);
      }
    } else if (error.status === 409) {
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
      if (choice === 'force') await saveYaml(true, layout);
    } else {
      toast(errorText(error), { kind: 'error' });
    }
    return;
  }
  if (layout) await putLayout(m, layout);  // said when it fails: the text is saved, the reload shows the old layout
  S.drafts = {};
  setDirty(false);
  S.undo = S.redo = [];  // the edits before the save are not undone or redone over it
  toast('Saved', { kind: 'ok' });
  await openMachine(m.id, { keepRun: true, discard: true });
  loadMachines();
}

// ------------------------------------------------------------------ runs: start

/** A param's field, showing `given` (what the machine was last started with) or else its default. */
function paramField(name, p, given) {
  const id = `param-${name}`;
  const label = html`<span class="pk-label">${name}${p.required ? ' *' : ''} <span class="pk-muted">${p.type || 'any'}</span></span>`;
  const help = p.description ? html`<span class="pk-help">${p.description}</span>` : '';
  const shown = given !== undefined ? given : p.default;
  const value = shown ?? '';
  if (Array.isArray(p.enum)) {
    return html`<label class="pk-field">${label}<select class="pk-select pk-select--sm" id="${id}" data-param="${name}" data-type="enum">
      ${p.required ? '' : html`<option value="">(default)</option>`}
      ${p.enum.map((option, i) => html`<option value="${i}" ${JSON.stringify(option) === JSON.stringify(shown) ? 'selected' : ''}>${preview(option, 60)}</option>`)}</select>${help}</label>`;
  }
  if (p.type === 'boolean') {
    return html`<label class="pk-check"><input type="checkbox" id="${id}" data-param="${name}" data-type="boolean" ${shown ? 'checked' : ''}> ${name} ${help}</label>`;
  }
  if (p.type === 'integer' || p.type === 'number') {
    return html`<label class="pk-field">${label}<input class="pk-input pk-input--sm" type="number" id="${id}" data-param="${name}" data-type="${p.type}" value="${value}" ${p.type === 'integer' ? 'step="1"' : 'step="any"'}>${help}</label>`;
  }
  if (p.type === 'string') {
    return html`<label class="pk-field">${label}<textarea class="pk-textarea" rows="2" id="${id}" data-param="${name}" data-type="string">${value}</textarea>${help}</label>`;
  }
  return html`<label class="pk-field">${label}<textarea class="pk-textarea pk-input--mono" rows="2" id="${id}" data-param="${name}" data-type="json" placeholder="JSON">${shown === undefined || shown === null ? '' : JSON.stringify(shown)}</textarea>${help}</label>`;
}

function drawStartForm() {
  const g = S.machine.graph;
  const params = Object.entries(g.params || {});
  const fields = $('paramFields');
  const signature = `${S.machine.id} ${JSON.stringify(g.params || {})}`;
  if (fields.dataset.signature !== signature) {  // typed values stay while the machine and its params stay the same
    fields.dataset.signature = signature;
    const kept = recall(`params:${S.machine.id}`, {});
    render(fields, params.length ? params.map(([name, p]) => paramField(name, p, kept?.[name]))
      : html`<p class="pk-help">This machine takes no params.</p>`);
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
    let params;
    let mocks;
    try {
      params = readParams();
      const text = $('mocks').value.trim();
      mocks = text ? JSON.parse(text) : null;
      if (mocks !== null && (typeof mocks !== 'object' || Array.isArray(mocks))) throw new Error('mocks: a JSON object {state path: out}');
    } catch (problem) {
      notice($('startError'), problem instanceof SyntaxError ? `mocks: not valid JSON (${problem.message})` : problem.message);
      return;
    }
    await startRun(params, mocks, $('mockOnly').checked);
  });
});

/** Start a run of the open machine with the next run's breakpoints and watches; its params are kept for the
 * machine's form, and the run is shown. */
async function startRun(params, mocks, mockOnly) {
  const error = $('startError');
  notice(error, '');
  if (hasDrafts() && !await confirm('The machine has unsaved changes: the run starts from the saved file. Start anyway?',
    { confirmLabel: 'Start' })) return;
  remember(`mocks:${S.machine.id}`, $('mocks').value);
  remember(`params:${S.machine.id}`, params);
  // a point on a state the file no longer has (removed, renamed in the YAML tab) would be refused by the server
  const stale = keepNextPoints((p) => (hooksOf(stateOf(p.state) || { type: 'choice' }).hooks.includes(p.at || 'enter') ? p : null));
  if (stale.length) {
    toast(`Dropped ${stale.length} breakpoint${stale.length > 1 ? 's' : ''} the machine cannot stop at any more: `
      + stale.map((p) => `${p.state}@${p.at || 'enter'}`).join(', '), { kind: 'warn' });
  }
  try {
    const started = await api(`${API}/runs`, {
      method: 'POST',
      json: {
        machine_id: S.machine.id, params, mocks, mock_only: mockOnly, pause_at_start: $('pauseAtStart').checked,
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
}

/** The run's inputs -- params, mocks, mock only -- into the start form, and a new run with them. */
async function rerun(run) {
  const mocks = run.mocks?.mocks && Object.keys(run.mocks.mocks).length ? run.mocks.mocks : null;
  $('mocks').value = mocks ? JSON.stringify(mocks) : '';
  $('mockOnly').checked = Boolean(run.mocks?.mock_only);
  await startRun(run.params || {}, mocks, Boolean(run.mocks?.mock_only));
  $('paramFields').dataset.signature = '';  // drawn anew with the params startRun kept
  drawStartForm();
}

// ------------------------------------------------------------------ runs: list, selection, polling

const RUN_PAGE = 50;

/** The machine's newest runs of the status chosen, and those of other machines it ran in as a submachine -- as many
 * as are shown already (older pages stay through a refresh); `older`: the page after the last one shown. */
async function loadRuns({ older = false, tick = false } = {}) {
  if (!S.machine) {
    runsPoller.stop();
    return;
  }
  if (tick && runsRequests) return;  // a tick never aborts a load the author started
  const last = S.runs[S.runs.length - 1];
  const limit = older ? RUN_PAGE : Math.min(500, Math.max(RUN_PAGE, S.runs.length));
  const query = `machine_id=${enc(S.machine.id)}&nested=true&limit=${limit}${S.runStatus ? `&status=${enc(S.runStatus)}` : ''}`
    + (older && last ? `&before=${enc(last.id)}` : '');
  let page;
  runsRequests += 1;
  try {
    page = await api(`${API}/runs?${query}`, { latest: 'runs', quiet: true });
  } catch (error) {
    if (!isAborted(error) && !tick) update($('runList'), emptyState('circle-alert', 'Runs could not be loaded', errorText(error)));
    return;
  } finally {
    runsRequests -= 1;
  }
  S.runs = older ? [...S.runs, ...page] : page;
  S.runsMore = page.length === limit;
  $('runCount').textContent = S.runs.length ? `${S.runs.length}${S.runsMore ? '+' : ''}` : '';
  $('olderRuns').hidden = !S.runsMore;
  drawRunList();
  if (S.run) drawDebugBar();
  followRuns();
}

const isLive = (r) => !TERMINAL.has(r.status) && r.status !== 'interrupted';

/** The list is asked again while a run is shown and another one is live: the bar offers it, with its status. */
function followRuns() {
  if (S.runId && S.runs.some((r) => r.id !== S.runId && isLive(r))) runsPoller.start();
  else runsPoller.stop();
}

$('olderRuns').addEventListener('click', () => withBusy($('olderRuns'), () => loadRuns({ older: true })));
$('runStatus').addEventListener('change', () => {
  S.runStatus = $('runStatus').value;
  S.runs = [];  // another list: its first page
  loadRuns();
});

function drawRunList() {
  update($('runList'), S.runs.length ? html`<div class="pk-table-wrap"><table class="pk-table" data-pk-sort="runs" data-pk-select>
    <thead><tr><th>Run</th><th>Status</th><th>State</th><th aria-sort="descending">Started</th><th>Ended</th><th>By</th></tr></thead>
    <tbody>${S.runs.map((r) => html`<tr data-id="${r.id}" tabindex="0" aria-selected="${String(r.id === S.runId)}">
      <td class="pk-mono">${shorten(r.id, 14)}${r.parent_run ? html` <span class="pk-muted" title="${`forked from ${r.parent_run} at step ${r.fork_step}`}">fork</span>` : ''}${r.machine_id !== S.machine?.id ? html` <span class="pk-muted" title="${`a run of ${r.machine_id} this machine ran in as a submachine`}">in ${r.machine_id}</span>` : ''}</td>
      <td data-sort-value="${r.status}">${statusBadge(r.status)}</td>
      <td class="pk-mono">${r.final_state || ''}</td>
      <td data-sort-value="${r.created_at || ''}">${localTime(r.created_at, { seconds: true })}</td>
      <td data-sort-value="${r.finished_at || ''}">${r.finished_at ? localTime(r.finished_at, { seconds: true }) : ''}</td>
      <td>${r.user_id || ''}</td></tr>`)}</tbody></table></div>`
    : S.runStatus ? emptyState('play', `No ${S.runStatus} run`, 'Another status shows others.')
      : emptyState('play', 'No runs yet', 'Start one above.'));
}

$('runList').addEventListener('rowselect', (event) => selectRun(event.detail.id));

const poller = autoRefresh(() => loadRun({ tick: true }), 1000);
let runRequests = 0;  // GETs of the selected run that are out
const runsPoller = autoRefresh(() => loadRuns({ tick: true }), 3000);
let runsRequests = 0;  // GETs of the run list that are out
/** Journal rows a run answer carries: the poll's and every control's alike, so the history does not jump. */
const HISTORY_STEPS = 200;

function selectRun(id) {
  S.runId = id || null;
  S.run = null;
  S.framePrefix = null;
  S.evaluation = null;
  S.pollError = null;
  S.result = null;
  S.resultOpen = new Set();
  poller.stop();
  followRuns();
  if (S.machine) setQuery(S.runId ? { machine: S.machine.id, run: S.runId } : { machine: S.machine.id });
  drawRunList();
  drawResult();  // the run left takes its result along at once: a read of the next one may fail
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
    run = await api(`${API}/runs/${enc(id)}?steps=${HISTORY_STEPS}`, { latest: 'run', quiet: true });
  } catch (error) {
    if (isAborted(error) || id !== S.runId) return;
    if (error.status === 404) {
      poller.stop();
      selectRun(null);
      return;
    }
    // a blip (a restart, a 5xx) must not freeze the view on a state that is long gone: the poll goes on, and the
    // bar says the run is not refreshed until a poll gets through
    if (!S.pollError) toast(`Run not refreshed: ${errorText(error)}`, { kind: 'warn' });
    S.pollError = errorText(error);
    drawDebugBar();
    return;
  } finally {
    runRequests -= 1;
  }
  if (id !== S.runId) return;
  S.pollError = null;
  showRun(run);
}

/** A run answer (a poll, a control) for the selected run: shown, polled while alive, and its row in the list kept. */
function showRun(run) {
  // a control's answer does not list the frames a run started: the last poll's stand until the next
  if (!run.frames_started && S.run?.id === run.id) run.frames_started = S.run.frames_started;
  S.run = run;
  if (!TERMINAL.has(run.status) && run.status !== 'interrupted') poller.start();
  else poller.stop();
  keepListed(run);
  drawRun();
  followResult(run);
}

/** The run's row in the list says what the answer says. Compared with the row, not with the run shown before: a
 * control answer that ended the run was shown already, and the row still said running. */
function keepListed(run) {
  const listed = S.runs.find((r) => r.id === run.id);
  if (listed && (listed.status !== run.status || listed.final_state !== run.final_state)) {
    Object.assign(listed, { status: run.status, final_state: run.final_state, finished_at: run.finished_at });
    drawRunList();
    if (S.run) drawDebugBar();  // the runs it offers
    followRuns();
  }
}

function drawRun() {
  drawDebugBar();
  drawDebugPane();
  drawHistory();
  redrawOverlay();
  drawStateFrames();
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
  // held by another process: the server hands pause, continue, step and terminate over to it (a second at most)
  const elsewhere = !run.active && ['running', 'waiting', 'paused'].includes(run.status);
  // run_to stops on a state's enter hook: offer only states that have one
  const states = (S.machine?.graph?.states || []).filter((s) => hooksOf(s).hooks.includes('enter')).map((s) => s.name);
  const can = {
    pause: (live || elsewhere) && run.status === 'running',
    resume: (live || elsewhere) && run.status === 'paused',
    runTo: live && ['running', 'paused'].includes(run.status),
    // an interrupted run is live nowhere: terminating it runs its finally here and ends it (service: terminate)
    terminate: (live && !TERMINAL.has(run.status)) || elsewhere || run.status === 'interrupted',
    restart: run.status === 'interrupted',
  };
  const answers = run.status === 'waiting' ? acceptedEvents(run) : [];
  const choices = runChoices(run);
  keepingChoices(bar, () => update(bar, html`
    ${choices.length > 1 ? html`<span class="sg-runpick" role="group" aria-label="Live runs: the one to show">${choices.map((r) => html`<button
      type="button" class="pk-btn pk-btn--sm${r.id === run.id ? ' pk-btn--primary' : ''}" data-pick-run="${r.id}" data-key="pick:${r.id}"
      aria-pressed="${String(r.id === run.id)}" title="${r.id}${r.machine_id && r.machine_id !== S.machine?.id ? ` (a run of ${r.machine_id})` : ''}"><span
      class="pk-mono">${runTail(r.id)}</span>
      ${statusBadge(r.id === run.id ? run.status : r.status)}</button>`)}</span>`
    : html`<strong class="pk-mono" title="${run.id}">${shorten(run.id, 16)}</strong> ${statusBadge(run.status)}`}
    ${answers.length ? html`<span class="sg-answers" role="group" aria-label="Answer the wait">${answers.map(({ name, frames }) => html`<button type="button"
      class="pk-btn pk-btn--sm pk-btn--primary" data-send-event="${name}" title="${eventHelp(name) || `Send ${name}`}${frames.length > 1 ? ` (${frames.length} frames wait for it: pick one)` : ''}">${icon('send-horizontal', { size: 'sm' })} ${name}</button>`)}</span>` : ''}
    ${run.machine_id !== S.machine?.id ? frameChoice(run) : ''}
    ${paused ? html`<span title="${paused.reason}">paused at <span class="pk-mono">${paused.state ?? '—'}</span> (${paused.hook}${paused.frame ? `, frame ${paused.frame}` : ''})</span>` : ''}
    ${!paused && run.final_state ? html`<span>ended in <span class="pk-mono">${run.final_state}</span></span>` : ''}
    ${elsewhere ? html`<span class="pk-muted" title="Shown from its journal; pause, continue, step and terminate reach it within a second -- run to, breakpoints and edits only in its own process">in another process</span>` : ''}
    ${S.pollError ? html`<span class="pk-text--warn" title="${S.pollError}">${icon('circle-alert', { size: 'sm' })} not refreshed</span>` : ''}
    <span class="pk-grow"></span>
    <button type="button" class="pk-btn pk-btn--sm" data-control="continue" ${can.resume ? '' : 'disabled'} title="Continue">${icon('play', { size: 'sm' })} Continue</button>
    <button type="button" class="pk-btn pk-btn--sm" data-control="step" ${can.resume ? '' : 'disabled'} title="Run to the next hook">${icon('step-forward', { size: 'sm' })} Step</button>
    <button type="button" class="pk-btn pk-btn--sm" data-control="pause" ${can.pause ? '' : 'disabled'} title="Pause at the next hook">${icon('pause', { size: 'sm' })} Pause</button>
    <select class="pk-select pk-select--sm" id="runToState" aria-label="State to run to" ${can.runTo ? '' : 'disabled'}>${states.map((name) => html`<option value="${name}">${name}</option>`)}</select>
    <button type="button" class="pk-btn pk-btn--sm" data-control="run_to" ${can.runTo ? '' : 'disabled'} title="Run until the chosen state is entered">${icon('crosshair', { size: 'sm' })} Run to</button>
    <button type="button" class="pk-btn pk-btn--sm pk-btn--danger" data-control="terminate" ${can.terminate ? '' : 'disabled'}>${icon('square', { size: 'sm' })} Terminate</button>
    ${can.restart ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--primary" data-control="resume" title="Resume the interrupted run: finished activities are replayed, not repeated">${icon('rotate-ccw', { size: 'sm' })} Resume</button>` : ''}
    <input class="pk-input pk-input--sm sg-step-input" type="number" min="0" id="forkStep" aria-label="Top-level step to fork from" placeholder="step">
    <label class="pk-check" title="Hold the fork at the fork point, to look at or set ctx before it goes on"><input type="checkbox" id="forkPause"> paused</label>
    <button type="button" class="pk-btn pk-btn--sm" data-control="fork" title="A new run from this top-level step (current definition with Shift)">${icon('git-branch', { size: 'sm' })} Fork</button>
    <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-control="close" title="Stop showing this run" aria-label="Stop showing this run">${icon('x', { size: 'sm' })}</button>`));
}

/** A run id by its end: the runs a machine tool starts share their caller's prefix (`<request id>_sg<6>`). */
const runTail = (id) => (id.length > 10 ? `…${id.slice(-9)}` : id);

/** The runs the bar offers: the one shown and every live one listed, in the order they started (the list is newest
 * first). */
function runChoices(run) {
  const listed = [...S.runs].reverse().filter((r) => r.id === run.id || isLive(r));
  return listed.some((r) => r.id === run.id) ? listed : [run, ...listed];
}

/** The run's own machine is not the open one: the way back to it, and which frame of the open machine the canvas
 * shows. */
function frameChoice(run) {
  const mine = framesOf(run, S.machine?.id);
  const shown = shownFrame();
  // a state run again (a loop, a retry) has several frames under it: they get their number
  const where = (f) => f.path || f.prefix;
  const total = Object.create(null);  // by state name: a state may be called constructor
  const seen = Object.create(null);
  for (const f of mine) total[where(f)] = (total[where(f)] || 0) + 1;
  const label = (f) => {
    seen[where(f)] = (seen[where(f)] || 0) + 1;
    const nth = total[where(f)] > 1 ? ` #${seen[where(f)]}` : '';
    return `under ${where(f)}${nth} · ${frameStatus(f)}`;
  };
  return html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-open-frame="" data-machine="${run.machine_id}"
      title="Open the run's own machine">${icon('arrow-up', { size: 'sm' })} ${run.machine_id}</button>
    ${mine.length ? html`<select class="pk-select pk-select--sm" data-frame-choice aria-label="The frame of this machine the canvas shows">${mine.map((f) => html`<option
      value="${f.prefix}" ${f.prefix === shown?.prefix ? 'selected' : ''}>${label(f)}</option>`)}</select>`
    : badge(`${S.machine?.id} has not run in it`, 'warn')}`;
}

/** A frame of the selected run: a button that shows it on the canvas, unless the canvas shows it already, or its
 * machine has no graph of its own (one of a file's `machines:`, `<id>.<name>`). */
function showFrameButton(machine, prefix, shown = shownFrame()?.prefix, name = machine) {
  if (machine === S.machine?.id && shown === prefix) return '';
  if (!S.machines.some((m) => m.id === machine)) return '';
  return html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-open-frame="${prefix}" data-key="${`frame:${prefix}`}"
    data-machine="${machine}" title="${`Show this run on ${machine}'s graph`}" aria-label="${`Show ${name} (${prefix || 'top'})`}">${icon('layers', { size: 'sm' })} Show</button>`;
}

// the picker has no id: keepingChoices must not keep a frame picked by default -- it shows the one the canvas shows
$('debugBar').addEventListener('change', (event) => {
  if (event.target.dataset?.frameChoice === undefined) return;
  S.framePrefix = event.target.value;
  drawRun();
});

/** The events a run takes now, each with the frames that take it. */
function acceptedEvents(run) {
  const found = new Map();
  for (const { frame, events } of run?.accepts || []) {
    for (const name of events || []) found.set(name, [...(found.get(name) || []), frame ?? '']);
  }
  return [...found].map(([name, frames]) => ({ name, frames }));
}

/** What an event of the open machine is for, and the data it carries. */
function eventHelp(name) {
  const spec = S.machine?.graph?.events?.[name];
  if (!spec) return '';
  return [spec.description, spec.data ? `data: ${preview(spec.data, 120)}` : ''].filter(Boolean).join(' · ');
}

$('debugBar').addEventListener('click', async (event) => {
  const pick = event.target.closest('[data-pick-run]');
  if (pick) {
    if (pick.dataset.pickRun !== S.runId) selectRun(pick.dataset.pickRun);
    return;
  }
  const answer = event.target.closest('[data-send-event]');
  if (answer && S.run) {
    const name = answer.dataset.sendEvent;
    const frames = acceptedEvents(S.run).find((e) => e.name === name)?.frames || [];
    if (S.machine?.graph?.events?.[name]?.data || frames.length > 1) {  // data to give, or a frame to pick: the form
      selectTab($('sideTabs'), 'debug');
      drawDebugPane();
      $('eventForm').elements.name.value = name;
      drawEventHelp();
      $('eventForm').elements.data.focus();
      return;
    }
    await withBusy(answer, () => sendEvent(name, null, frames[0] || null));
    return;
  }
  const button = event.target.closest('[data-control]');
  if (!button || button.disabled) return;
  const action = button.dataset.control;
  if (action === 'close') return selectRun(null);
  if (action === 'terminate' && !await confirm(S.run?.status === 'interrupted'
    ? 'Terminate the interrupted run? It is not resumed: its finally activities run, and it ends as cancelled.'
    : 'Terminate the run? A running agent call is cancelled.', { danger: true, confirmLabel: 'Terminate' })) return;
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
    if ($('forkPause').checked) extra.pause = true;
  }
  await withBusy(button, () => control(action, extra));
});

async function control(action, extra = {}) {
  const id = S.runId;
  if (!id) return null;
  try {
    const answer = await api(`${API}/runs/${enc(id)}/control`, { method: 'POST', json: { action, steps: HISTORY_STEPS, ...extra } });
    if (action === 'fork' && answer.run_id) {
      toast(`Forked as ${answer.run_id}`, { kind: 'ok' });
      await loadRuns();
      selectRun(answer.run_id);
      return answer;
    }
    if (action === 'evaluate' || action === 'set') return answer;
    if (id !== S.runId) {
      // another run was picked while this answer was out (a terminate waits for the run to stop): the view is
      // that run's now; only the list learns what became of this one
      if (answer?.id === id) keepListed(answer);
      return answer;
    }
    if (answer && answer.id === id) showRun(answer);
    else await loadRun();
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
  const shown = shownFrame()?.prefix;  // once: each frame's button asks it
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

  // watch and breakpoints: the live run's, else the lists the next run starts with -- the same the inspector shows
  // and edits (shownBreakpoints); a run that ended keeps its lists in its journal, not here
  const watching = live ? (debug.watchpoints || []) : S.nextWatch.map((expr) => ({ expr }));
  const values = live ? (debug.watch || {}) : {};
  $('dbgWatchScope').textContent = live ? `(run ${shorten(live.id, 12)})` : '(next run)';
  update($('dbgWatch'), watching.length ? html`<ul class="sg-plain-list">${watching.map((p, i) => {
    const seen = values[p.id];
    return html`<li class="sg-watch"><span class="sg-watch-expr">${otherMachine(p)}${p.expr}</span>
      <span class="pk-grow">${seen ? ('error' in seen ? html`<span class="pk-text--danger">${seen.error}</span>`
        : html`<span class="pk-mono">${preview(seen.value, 120)}</span>`) : ''}${seen ? html` <span class="pk-muted">(${seen.frame}, step ${seen.step})</span>` : ''}</span>
      <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-drop-watch="${i}" aria-label="Stop watching">${icon('x', { size: 'sm' })}</button></li>`;
  })}</ul>` : html`<p class="pk-help">A watch pauses the run when its value changes.</p>`);

  const points = shownBreakpoints();
  $('dbgPointsScope').textContent = live ? `(run ${shorten(live.id, 12)})` : '(next run)';
  update($('dbgPoints'), points.length ? html`<ul class="sg-plain-list">${points.map((p, i) => html`<li class="sg-watch">
      <span class="sg-watch-expr">${otherMachine(p)}${p.state}@${p.at || 'enter'}</span>${p.condition ? html`<span class="pk-mono">if ${p.condition}</span>` : ''}
      <span class="pk-grow"></span><button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-drop-breakpoint="${i}" aria-label="Remove breakpoint">${icon('x', { size: 'sm' })}</button></li>`)}</ul>`
    : html`<p class="pk-help">None. Set them on a state in the inspector.</p>`);

  $('dbgEvalHint').textContent = paused ? '' : '(while paused)';
  for (const form of [$('evalForm'), $('setForm')]) for (const control of form.elements) control.disabled = !paused;
  update($('dbgEval'), S.evaluation ? html`<div class="sg-frame"><span class="pk-mono">${S.evaluation.expr}</span>${'error' in S.evaluation
    ? html`<span class="pk-text--danger">${S.evaluation.error}</span>` : jsonView(S.evaluation.value)}</div>` : '');

  const events = Object.keys(S.machine?.graph?.events || {});
  $('eventSection').hidden = !events.length;
  const accepted = new Set((run?.accepts || []).flatMap((a) => a.events || []));
  // a wait state marks its events "(accepted now)": the redraw must not put another event in the viewer's choice
  const eventName = $('eventForm').elements.name;
  keepingChoices(eventName, () => update(eventName, events.map((name) => html`<option value="${name}">${name}${accepted.has(name) ? ' (accepted now)' : ''}</option>`)));
  const waitsFor = [...accepted].sort().join(' ');
  if (waitsFor && waitsFor !== S.acceptedSeen) {  // a new wait: the event it takes is the one to send
    const first = events.find((name) => accepted.has(name));
    if (first) eventName.value = first;
  }
  S.acceptedSeen = waitsFor;
  drawEventHelp();
  keepingChoices($('eventFrame'), () => update($('eventFrame'), html`<option value="">any frame</option>${frames.map((f) => html`<option value="${f.prefix}">${f.prefix || 'top'}</option>`)}`));
  for (const control of $('eventForm').elements) control.disabled = !run || TERMINAL.has(run.status);

  update($('dbgFrames'), frames.length ? frames.map((f, i) => html`<div class="sg-frame">
      <div class="sg-frame-head">${badge(f.prefix ? 'submachine' : 'top', f.prefix ? 'info' : '')}<span class="pk-mono">${f.machine}</span>
        ${f.path ? html`<span class="pk-muted">under ${f.path}</span>` : ''}<span class="pk-grow"></span>
        <span class="pk-mono">${f.state ?? '—'}</span><span class="pk-muted">step ${f.step}</span>
        ${showFrameButton(f.machine, f.prefix || '', shown)}</div>
      ${f.waiting_since ? html`<div class="pk-help">waits for ${(f.accepts || []).join(', ') || 'an event'} since ${localTime(f.waiting_since, { seconds: true })}${f.deadline ? `, until ${localTime(f.deadline, { seconds: true })}` : ''}</div>` : ''}
      <details class="pk-details" ${i === 0 ? 'open' : ''}><summary>ctx</summary>${jsonView(f.ctx ?? {})}</details>
      ${f.visits && Object.keys(f.visits).length ? html`<div class="pk-help">visits: ${Object.entries(f.visits).map(([n, v]) => `${n} ×${v}`).join(', ')}</div>` : ''}
    </div>`) : html`<p class="pk-help">${run ? 'No live frames: the run has not started or has ended.' : 'Frames show while a run is selected.'}</p>`);
}

$('side-inspect').addEventListener('click', (event) => {
  if (!event.target.closest?.('[data-act="copy-agent-entry"]') || !S.machine) return;
  copyText(agentEntry(S.machine));  // it says itself that it copied
});

$('side-debug').addEventListener('click', async (event) => {
  const target = event.target.closest('button');
  if (!target) return;
  if (target.dataset.act === 'copy-run' && S.run) return copyText(S.run.id);
  if (target.dataset.dropBreakpoint !== undefined) {
    const index = Number(target.dataset.dropBreakpoint);
    if (!liveRun()) {
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
    if (!liveRun()) {
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
  if (!liveRun()) {
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

function drawEventHelp() {
  $('eventHelp').textContent = eventHelp($('eventForm').elements.name.value);
}

$('eventForm').addEventListener('change', (event) => { if (event.target.name === 'name') drawEventHelp(); });

async function sendEvent(name, data, frame) {
  try {
    const answer = await api(`${API}/runs/${enc(S.runId)}/events`, { method: 'POST', json: { name, data, frame } });
    toast(answer.accepted ? `${name} accepted${answer.frame ? ` by frame ${answer.frame}` : ''}`
      : `${name} not taken now: ${answer.reason || 'it waits in the inbox until a frame accepts it'}`, { kind: answer.accepted ? 'ok' : 'warn' });
    await loadRun();
  } catch (error) { /* toasted */ }
}

onSubmit($('eventForm'), async (fields) => {
  let data = null;
  const text = fields.data.value.trim();
  if (text) {
    try { data = JSON.parse(text); } catch {
      toast('Event data: not valid JSON', { kind: 'warn' });
      return;
    }
  }
  await sendEvent(fields.name.value, data, fields.frame.value || null);
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
    if (row.status === 'transition') {
      detail = `${data.from} → ${data.to ?? '(internal)'} on ${data.event}`
        + (data.guards ? ` [${data.guards.map((g) => `${g.guard} = ${'error' in g ? 'error' : g.result}`).join('; ')}]` : '');
    }
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
    <td data-sort-value="${row.ts || ''}">${localTime(row.ts, { seconds: true })}</td>
    <td class="pk-mono">${row.key}</td>
    <td>${WHAT[row.kind] || row.kind}</td>
    <td>${kind ? html`<span class="pk-text--${kind}">${what}</span>` : what}</td>
    <td class="pk-mono">${row.state || ''}</td>
    <td class="pk-num" data-sort-value="${duration ?? ''}">${duration === undefined || duration === null ? '' : `${duration.toFixed(2)} s`}</td>
    <td class="sg-mono sg-preview" title="${detail}">${detail}</td>
  </tr>`;
}

function drawHistory() {
  const all = S.run?.journal || [];
  const rows = S.historyKind ? all.filter((row) => row.kind === S.historyKind) : all;
  keepingChoices($('runHistory'), () => update($('runHistory'), S.run ? html`<div class="pk-card">
    <div class="pk-card-head"><h3 class="pk-card-title">History of ${shorten(S.run.id, 16)}</h3><span class="pk-muted">the last ${all.length} journal rows</span>
      <span class="pk-grow"></span>
      <select class="pk-select pk-select--sm" id="historyKind" aria-label="Rows to show">
        <option value="">every row</option>${Object.keys(WHAT).map((kind) => html`<option value="${kind}" ${kind === S.historyKind ? 'selected' : ''}>${kind}</option>`)}</select></div>
    ${rows.length ? html`<div class="pk-table-wrap"><table class="pk-table" data-pk-sort="history">
      <thead><tr><th class="pk-num" aria-sort="descending">#</th><th>Time</th><th>Key</th><th>Row</th><th>What</th><th>State</th><th class="pk-num">Took</th><th>Detail</th></tr></thead>
      <tbody>${rows.map(historyRow)}</tbody></table></div>` : html`<p class="pk-help">${all.length ? 'No row of that kind among them.' : 'Nothing journaled yet.'}</p>`}
  </div>` : ''));
}

$('runHistory').addEventListener('change', (event) => {
  if (event.target.id !== 'historyKind') return;
  S.historyKind = event.target.value;
  drawHistory();
});

// ------------------------------------------------------------------ runs: result

/** Journal rows the result reads per request, and at most how many requests: a longer run shows its first rows. */
const RESULT_PAGE = 500;
const RESULT_PAGES = 20;
/** What the result shows of the journal: finished activities and the finals frames ended in. */
const resultRow = (row) => (row.kind === 'activity' && (row.status === 'done' || row.status === 'error'))
  || (row.kind === 'trace' && row.status === 'final');

let resultLoad = null;   // the run whose result is being read: a poll meanwhile leaves the read alone
let resultAgain = null;  // ... and asks for another look once it is done

/**
 * The selected run's result: every activity's answer (an agent's text, a tool's result, a decision) and the final
 * state each frame ended in. `S.result` holds the rows of S.runId read up to `after`. An activity's row is written
 * as started and rewritten in place when it ends -- the same seq -- so the activities read while they ran are kept
 * in `running`, and `ended` (seqs of those that ended since) are read again one by one: a composite that runs for
 * hours while its children end must not make every poll read everything after it.
 */
async function loadResult({ ended = [], more = true } = {}) {
  const id = S.runId;
  if (!id) return;
  const kept = S.result?.runId === id ? S.result
    : { runId: id, rows: [], after: 0, running: new Set(), complete: true, frames: new Map() };
  let { after } = kept;
  const { frames } = kept;  // folded in seq order: a page read again after an aborted read folds to the same
  let complete = true;
  const rows = new Map(kept.rows.map((row) => [row.seq, row]));
  const running = new Set(kept.running);
  const take = (row) => {
    foldTrace(frames, row);
    if (resultRow(row)) {
      rows.set(row.seq, row);
      running.delete(row.seq);
    } else if (row.kind === 'activity') {
      running.add(row.seq);
    }
  };
  resultLoad = id;
  try {
    for (const seq of ended) {
      const [row] = await api(`${API}/runs/${enc(id)}/journal?kinds=activity&after=${seq - 1}&limit=1`,
        { latest: 'result', quiet: true });
      if (row?.seq === seq) take(row);
    }
    for (let page = 0; more; page += 1) {  // more: rows past `after` to read
      if (page === RESULT_PAGES) {
        complete = false;
        break;
      }
      const got = await api(`${API}/runs/${enc(id)}/journal?kinds=activity,trace&after=${after}&limit=${RESULT_PAGE}`,
        { latest: 'result', quiet: true });
      got.forEach(take);
      if (got.length) after = got[got.length - 1].seq;
      if (got.length < RESULT_PAGE) break;
    }
  } catch (error) {
    if (!isAborted(error) && id === S.runId) toast(`Result not loaded: ${errorText(error)}`, { kind: 'warn' });
    return;
  } finally {
    if (resultLoad === id) resultLoad = null;
  }
  if (id !== S.runId) return;
  S.result = { runId: id, rows: [...rows.values()].sort((a, b) => a.seq - b.seq), after, running, complete, frames };
  drawResult();
  drawStateFrames();  // the frames a state started: also those only the journal names, and how they ended
  if (shownFrame()) {  // an ended submachine frame is drawn from its trace; the badges count what only it names
    redrawOverlay();
    drawDebugBar();
  }
  if (resultAgain === id) {  // a poll came meanwhile: what it brought (the run's end, say) is read now
    resultAgain = null;
    if (S.run?.id === id) followResult(S.run);
  }
}

/** A poll or control answer: the result reads what is new, and again the activities that ended since they were
 * read running -- all of them once the run has ended, also those past the poll's rows. A read of this run that is
 * out is left alone (aborting it for the next would start it over every second) and looked at again after it. */
function followResult(run) {
  if (resultLoad === run.id) {
    resultAgain = run.id;
    return;
  }
  const result = S.result?.runId === run.id ? S.result : null;
  if (!result) {
    loadResult();
    return;
  }
  const journal = run.journal || [];
  const ended = TERMINAL.has(run.status) ? [...result.running]
    : journal.filter((row) => resultRow(row) && result.running.has(row.seq)).map((row) => row.seq);
  const newest = Math.max(0, ...journal.filter(resultRow).map((row) => row.seq));
  if (ended.length || newest > result.after) loadResult({ ended, more: newest > result.after });
  else drawResult();
}

function drawResult() {
  const run = S.run;
  if (!run) {
    update($('runResult'), '');
    return;
  }
  const rows = S.result?.runId === run.id ? S.result.rows : [];
  const activities = rows.filter((row) => row.kind === 'activity');
  const finals = rows.filter((row) => row.kind === 'trace');
  const shown = shownFrame()?.prefix;  // once: each row's button asks it
  const reason = run.error?.type || (TERMINAL.has(run.status) ? run.status : '');
  update($('runResult'), html`<div class="pk-card sg-result">
    <div class="pk-card-head"><h3 class="pk-card-title">Result of ${shorten(run.id, 16)}</h3>${statusBadge(run.status)}
      ${run.final_state ? html`<span>ended in <span class="pk-mono">${run.final_state}</span>${reason && reason !== run.status ? html` (${reason})` : ''}</span>` : ''}
      <span class="pk-grow"></span>
      ${run.machine_id === S.machine?.id ? html`<button type="button" class="pk-btn pk-btn--sm" data-act="rerun"
        title="A new run with this run's params and mocks (they go into the start form too)">${icon('rotate-ccw', { size: 'sm' })} Run again</button>` : ''}
      ${run.mocks?.mock_only || !ownSessions(run) ? '' : html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-open-session="${run.session_id || `sg_${run.id}`}"
        title="The run's session in the chat: what it was asked, how it ended, its agents' conversations below it">${icon('message-square', { size: 'sm' })} Session</button>`}</div>
    ${run.error ? errorView(run.error) : ''}
    ${run.output !== undefined && run.output !== null
    ? html`<details class="pk-details" open><summary>Output</summary>${resultValue(run.output)}</details>`
    : html`<p class="pk-help">${TERMINAL.has(run.status) ? 'No output.' : 'The output comes when the run ends.'}</p>`}
    ${finals.length ? html`<h4 class="sg-section-title">End states</h4><ul class="sg-plain-list">${finals.map((row) => html`<li class="sg-watch">
      ${badge(row.data?.frame ? 'submachine' : 'top', row.data?.frame ? 'info' : '')}
      <span class="pk-mono">${row.data?.frame ? `${row.data.machine || row.data.frame} · ` : ''}${row.state}</span>
      ${statusBadge(row.data?.status || 'succeeded')}
      ${row.data?.machine ? showFrameButton(row.data.machine, row.data.frame || '', shown) : ''}</li>`)}</ul>` : ''}
    <h4 class="sg-section-title">Activities <span class="pk-muted">${activities.length}${S.result?.complete === false ? ', the first ones' : ''}</span></h4>
    ${activities.length ? html`<div class="sg-results">${activities.map(resultActivity)}</div>`
    : html`<p class="pk-help">${TERMINAL.has(run.status) ? 'No activity finished.' : 'None finished yet.'}</p>`}
  </div>`);
}

/** One finished activity, folded: what it was and how it went; its answer is drawn when it is opened. */
function resultActivity(row) {
  const data = row.data || {};
  const meta = data.meta || {};
  const failed = row.status === 'error';
  const who = meta.agent || stateOf(row.state)?.label || '';
  const open = S.resultOpen.has(row.seq);  // by seq: an activity that ends later can land before others
  return html`<details class="pk-details sg-result-item" data-result="${row.seq}" ${open ? 'open' : ''}>
    <summary><span class="pk-mono">${data.path || row.state || row.key}</span> ${badge(data.kind || 'activity', 'accent')}
      ${who ? html`<span class="pk-mono">${who}</span>` : ''}
      ${failed ? badge(data.error?.type || 'error', 'danger') : ''}
      ${meta.mocked ? badge('mocked') : ''}
      <span class="pk-muted">${meta.duration_s === undefined || meta.duration_s === null ? '' : `${Number(meta.duration_s).toFixed(1)} s`}</span>
      ${failed ? '' : html`<span class="sg-mono sg-preview">${preview(data.out, 120)}</span>`}</summary>
    <div class="sg-result-body" data-drawn="${open ? 'yes' : ''}">${open ? resultBody(row) : ''}</div>
  </details>`;
}

/** An error as the journal keeps it: type and message, what caused it, and its data (a traceback, the guards that
 * were evaluated, a tool's result, an answer that did not parse). */
function errorView(error) {
  const e = error || {};
  const causes = [];
  for (let cause = e.cause; cause && causes.length < 5; cause = cause.cause) causes.push(`${cause.type || 'error'}: ${cause.message || ''}`);
  const data = e.data;
  return html`<div class="pk-callout pk-callout--danger"><strong>${e.type || 'error'}</strong> ${e.message || ''}
      ${e.state ? html`<div class="sg-problem-where">in ${e.state}</div>` : ''}
      ${causes.map((text) => html`<div class="sg-problem-where">caused by ${text}</div>`)}</div>
    ${data?.traceback ? html`<pre class="sg-result-text">${data.traceback}</pre>` : ''}
    ${data?.guards ? html`<details class="pk-details" open><summary>Guards evaluated</summary><ul class="sg-plain-list">
      ${data.guards.map((g) => html`<li><span class="pk-mono">${g.at}</span> <span class="pk-mono">${g.guard}</span> →
        ${'error' in g ? html`<span class="pk-text--danger">${g.error}</span>` : String(g.result)}</li>`)}</ul></details>` : ''}
    ${data !== undefined && data !== null && !data.traceback && !data.guards
    ? html`<details class="pk-details"><summary>Error data</summary>${jsonView(data)}</details>` : ''}`;
}

function resultValue(value) {
  return typeof value === 'string' ? html`<pre class="sg-result-text">${value}</pre>` : jsonView(value);
}

/** The body of an opened activity: its answer or its error, and where it ran (the agent's session). */
function resultBody(row) {
  const data = row.data || {};
  const meta = data.meta || {};
  return html`${row.status === 'error' ? errorView(data.error) : resultValue(data.out)}
    ${data.inputs ? html`<details class="pk-details"><summary>Input</summary>${jsonView(data.inputs)}</details>` : ''}
    ${meta.failures?.length ? html`<details class="pk-details"><summary>Failed attempts (${meta.failures.length})</summary>
      <ul class="sg-plain-list">${meta.failures.map((f) => html`<li><span class="pk-mono">#${f.attempt}</span> ${f.type}: ${f.message}</li>`)}</ul></details>` : ''}
    <dl class="pk-kv">
      ${meta.instance_id ? html`<dt>session</dt><dd><span class="pk-mono">${meta.instance_id}</span>
        ${ownSessions(S.run) ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-open-session="${meta.instance_id}" title="Open the agent's conversation in the chat">${icon('message-square', { size: 'sm' })} Open</button>`
    : html`<span class="pk-muted">${S.run?.user_id || 'anonymous'}'s</span>`}</dd>` : ''}
      ${meta.request_id ? html`<dt>request</dt><dd><span class="pk-mono">${meta.request_id}</span>
        <button type="button" class="pk-btn pk-btn--sm pk-btn--ghost pk-btn--icon" data-copy="${meta.request_id}"
          title="Copy the request id: the message debugger finds its LLM calls by it" aria-label="Copy the request id">${icon('copy', { size: 'sm' })}</button></dd>` : ''}
      ${meta.model ? html`<dt>model</dt><dd class="pk-mono">${meta.model}</dd>` : ''}
      ${meta.attempts > 1 ? html`<dt>attempts</dt><dd>${meta.attempts}</dd>` : ''}
      <dt>step</dt><dd class="pk-mono">${row.key}</dd>
    </dl>`;
}

// details fire toggle on themselves only: caught on the way down
$('runResult').addEventListener('toggle', (event) => {
  const item = event.target.closest?.('[data-result]');
  if (!item) return;
  const seq = Number(item.dataset.result);
  if (!item.open) {
    S.resultOpen.delete(seq);
    return;
  }
  S.resultOpen.add(seq);  // drawn open again when new rows redraw the list
  const row = S.result?.rows.find((r) => r.kind === 'activity' && r.seq === seq);
  const body = item.querySelector('.sg-result-body');
  if (row && body && body.dataset.drawn !== 'yes') {
    body.dataset.drawn = 'yes';
    render(body, resultBody(row));
  }
}, true);

$('runResult').addEventListener('click', (event) => {
  const again = event.target.closest('[data-act="rerun"]');
  if (again && S.run) {
    withBusy(again, () => rerun(S.run));
    return;
  }
  const copy = event.target.closest('[data-copy]');
  if (copy) {
    copyText(copy.dataset.copy);  // it says itself that it copied
    return;
  }
  const button = event.target.closest('[data-open-session]');
  if (!button) return;
  const id = button.dataset.openSession;
  if (!openSession(id)) {  // a tab of its own: no chat beside it
    copyText(id).then((copied) => { if (copied) toast('No chat in this tab: pick the copied session in the chat.'); });
  }
});

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

/** A machine id the author types: a name no listed machine has. */
function askMachineId(message, { title, value = '' }) {
  return askUntil(message, { title, value, placeholder: 'review_loop',
    problem: (id) => (!NAME.test(id) ? `"${id}" is no machine id: lowercase letters, digits and _, starting with a letter.`
      : S.machines.some((m) => m.id === id) ? `A machine "${id}" exists already.` : '') });
}

$('newMachine').addEventListener('click', async () => {
  const trimmed = await askMachineId('Id of the new machine (it is saved as <id>.yaml among your own machines):',
    { title: 'New machine' });
  if (!trimmed) return;
  try {
    const machine = await api(`${API}/machines`, { method: 'POST', json: { id: trimmed } });
    await loadMachines();
    await openMachine(machine.id);
  } catch (error) { /* toasted */ }
});

$('machineHead').addEventListener('click', (event) => {
  if (event.target.closest('[data-act="copy-id"]') && S.machine) copyText(S.machine.id);
  if (event.target.closest('[data-act="duplicate-machine"]') && S.machine) duplicateMachine(S.machine);
  if (event.target.closest('[data-act="show-problems"]') && S.machine) {
    selectTab($('sideTabs'), 'inspect');
    choose(null);  // the overview lists them all
  }
  if (event.target.closest('[data-act="delete-machine"]') && S.machine) deleteMachine(S.machine);
  if (event.target.closest('[data-act="save"]') && S.machine) withBusy(event.target.closest('[data-act="save"]'), saveAll);
  if (event.target.closest('[data-act="autosave"]') && S.machine) toggleAutosave();
});

document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && S.machine) {
    event.preventDefault();  // the browser's "save page"
    saveAll();
  }
});

const PYTHON_LINE = /^python[ \t]*:.*$/m;
const escapeRe = (text) => String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** A copy of a machine under a new id in the writable root: its file with the new id, and its companion module as
 * <id>.py -- the copy's own, to change without the original. What it imports from a file it names by machine id
 * instead: a copy of that file in the writable root would stand in for the machine of that id everywhere. The
 * layout comes along. */
async function duplicateMachine(m) {
  if (savable()) {
    toast('Save or revert the unsaved changes first: a copy is made from the saved machine.', { kind: 'warn' });
    return;
  }
  let free = `${m.id}_copy`;
  for (let n = 2; S.machines.some((other) => other.id === free); n += 1) free = `${m.id}_copy_${n}`;
  const id = await askMachineId(`Id of the copy of ${m.id}:`, { title: 'Duplicate machine', value: free });
  if (!id) return;
  let text = m.files[m.root_file].replace(/^id[ \t]*:.*$/m, `id: ${id}`);
  const module = m.graph.python;
  const files = {};
  if (module && module in m.files) {
    files[`${id}.py`] = m.files[module];
    text = text.replace(PYTHON_LINE, `python: ${id}.py`);
  }
  for (const [alias, ref] of Object.entries(m.graph.imports || {})) {
    if (!/\.ya?ml$/.test(ref)) continue;  // a machine id already
    const at = new RegExp(`(^|[\\s{,])(${escapeRe(alias)}[ \\t]*:[ \\t]*)(['"]?)${escapeRe(ref)}\\3`, 'm');
    text = text.replace(at, `$1$2$3${importedId(ref)}$3`);
  }
  files[`${id}.yaml`] = text;
  try {
    await api(`${API}/machines/${enc(id)}`, { method: 'PUT', json: { files, expected_versions: {} } });
  } catch (error) {
    return;  // toasted: a file in the way, a machine that does not validate
  }
  if (Object.keys(positions()).length || m.layout?.line || Object.keys(lineStyles()).length) {  // positions, line styles
    try {
      await api(`${API}/machines/${enc(id)}/layout`, { method: 'PUT', json: { layout: m.layout }, quiet: true });
    } catch (error) { /* the copy lays itself out */ }
  }
  toast(`${id} is a copy of ${m.id}`, { kind: 'ok' });
  await loadMachines();
  await openMachine(id);
}

/** Delete the open machine (the version shown): its runs stay, the panel shows no machine afterwards. */
async function deleteMachine(m) {
  if (!await confirm(`Delete the machine ${m.id}? Its file and layout go, and its Python module unless another machine `
    + 'uses it. Its runs stay readable. Unsaved changes are lost.', { title: 'Delete machine', danger: true, confirmLabel: 'Delete' })) return;
  try {
    await api(`${API}/machines/${enc(m.id)}`, { method: 'DELETE', json: { expected_version: m.versions[m.root_file] } });
  } catch (error) {
    return;  // toasted: a machine that imports it, a change since it was read
  }
  toast(`${m.id} deleted`, { kind: 'ok' });
  S.drafts = {};
  S.inspectorDrafts.clear();
  setDirty(false);
  S.machine = null;
  S.selection = null;
  // what this panel kept for the machine would come back with a new one of the same id
  for (const key of ['breakpoints', 'watch', 'mocks', 'params']) forget(`${key}:${m.id}`);
  S.undo = S.redo = [];  // a new machine of that id would take them for its own
  drawUndo();
  selectRun(null);
  drawInspector();
  $('machineView').hidden = true;
  $('placeholder').hidden = false;
  setTitle('State Graph');
  setQuery({});
  await loadMachines();
}

function addFrom(event) {
  const button = event.target.closest('button');
  if (!button) return;
  $('paletteMenu').hidePopover?.();
  if (button.dataset.addKind) addState({ kind: S.kinds.find((k) => k.key === button.dataset.addKind), type: 'state' });
  else if (button.dataset.addType) addState({ type: button.dataset.addType });
  else if (button.dataset.addNote !== undefined) addNote();
}
$('palette').addEventListener('click', addFrom);
$('paletteMenu').addEventListener('click', addFrom);

/** The state the search names: that name, else the one state whose name has the text in it. */
function findState() {
  const needle = $('stateSearch').value.trim().toLowerCase();
  if (!needle || !S.machine) return;
  const states = S.machine.graph.states.map((s) => s.name);
  const hits = states.includes(needle) ? [needle] : states.filter((name) => name.includes(needle));
  if (hits.length === 1) {
    choose({ kind: 'state', id: hits[0] });
    return;
  }
  toast(hits.length ? `${hits.length} states match: ${hits.slice(0, 8).join(', ')}` : `No state matches "${needle}"`, { kind: 'info' });
}
$('stateSearch').addEventListener('change', findState);
$('stateSearch').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    findState();
  }
});

/** Undo or redo from a button or a key: one at a time (a held key repeats), then the buttons say what is left --
 * withBusy frees them when it ends. */
const go = (back) => withBusy([$('undo'), $('redo')], () => travel(back)).then(drawUndo);
$('undo').addEventListener('click', () => go(true));
$('redo').addEventListener('click', () => go(false));
$('zoomIn').addEventListener('click', () => canvas.zoom(1.2));
$('zoomOut').addEventListener('click', () => canvas.zoom(1 / 1.2));
$('fit').addEventListener('click', () => canvas.fit());
$('autoLayout').addEventListener('click', async () => {
  if (!S.machine) return;
  if (Object.keys(positions()).length && !await confirm('Forget the positions dragged by hand and lay the machine out anew?',
    { title: 'Auto layout', confirmLabel: 'Lay out' })) return;
  await saveLayout({ positions: {} });  // the line styles stay: they are no positions
  await drawGraph({ fit: true });
});

$('canvas').addEventListener('keydown', (event) => {
  if (event.target.closest('input, textarea, select')) return;
  if (event.key === 'Escape') choose(null);
  const key = event.key.toLowerCase();
  if ((event.ctrlKey || event.metaKey) && (key === 'z' || key === 'y')) {
    event.preventDefault();
    go(key === 'z' && !event.shiftKey);  // Ctrl+Z undoes; Ctrl+Shift+Z and Ctrl+Y redo
  }
  if ((event.key === 'Delete' || event.key === 'Backspace') && S.selection) {
    event.preventDefault();
    if (S.selection.kind === 'many') removeSelection(S.selection.states, S.selection.transitions);
    else if (S.selection.kind === 'state') removeState(S.selection.id);
    else if (S.selection.kind === 'note') removeNote(S.selection.id);
    else removeTransition(S.selection.id);
  }
});

document.addEventListener('refresh', async (event) => {
  await loadMachines();
  if (S.machine && !unsaved()) {
    try {
      // a name of its own: under openMachine's, this reload of the open machine would abort a click on another one
      const machine = await api(`${API}/machines/${enc(S.machine.id)}`, { latest: 'machine-refresh', quiet: true });
      const same = machine.id === S.machine?.id && !unsaved();  // no other machine opened, no text typed meanwhile
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
  await loadMachines();  // the catalog comes with the machine shown: its runner names the tools
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
