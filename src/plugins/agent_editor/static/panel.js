// Agent Editor: agent definitions edited in a form and written back into the YAML files they came from.
import { api, html, render, icon, confirm, dialog, toast, initTabs, selectTab, setDirty, yamlCode } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const RELOAD_CONFIG = new URL('../../../admin/reload-config', import.meta.url).pathname;  // the core endpoint
const $ = (id) => document.getElementById(id);

const TABS = ['general', 'model', 'tools', 'subagents', 'prompt', 'hooks', 'yaml'];
const VISIBILITY = {
  ui: 'Shown in the UI agent list',
  tool: 'Offered to other agents as a tool',
  both: 'Shown in the UI and offered to other agents as a tool',
  private: 'Neither shown in the UI nor offered as a tool',
};
/** How the entry on disk relates to what the app runs: dot colour and words. */
const STATES = {
  in_sync: ['ok', 'Running'],
  changed: ['warn', 'Running with older settings'],
  new: ['info', 'Not running yet'],
  off: ['', 'Disabled'],
  removed: ['danger', 'Still running, gone from disk'],
};
const FILTERS = {
  all: () => true,
  enabled: (row) => row.enabled,
  restart: (row) => row.restart,
  readonly: (row) => !row.editable,
};
const LISTS = { allowed: 'Allowed', blocked: 'Blocked' };
const TREE_FILTERS = [['all', 'All servers'], ['in', 'In this list'], ['out', 'Not in this list']];
const SKILL_FILTERS = [['all', 'All skills'], ['on', 'Selected'], ['off', 'Not selected']];
const MANAGER_FILTERS = [['all', 'All agents'], ['in', 'Can start'], ['out', 'Cannot start']];
/** The sub-agent manager's settings the form offers: key, label, kind, and for a switch its two words. */
const MANAGER_SETTINGS = [
  ['max_sub_agents_per_session', 'Sub-agents per session', 'number'],
  ['max_sub_agents_per_type', 'Sub-agents per agent type', 'number'],
  ['max_nesting_depth', 'Nesting depth', 'number'],
  ['default_wait_timeout', 'Wait timeout (seconds)', 'number'],
  ['auto_archive_on_limit', 'At the limit', 'bool', 'Archive the oldest sub-agent', 'Refuse a new one'],
  ['allow_advanced_model', 'Advanced model', 'bool', 'Callers may ask for it', 'Never'],
];
const HOOK_FILTERS = [['all', 'All hooks'], ['set', 'Set for this agent'], ['on', 'On for this agent'], ['off', 'Off for this agent']];
const MODES = [['inherited', 'Inherited'], ['extend', 'Extend inherited'], ['own', 'Own list']];
const MODE_HELP = {
  inherited: 'The list comes from the parent unchanged.',
  extend: 'The inherited entries stay; this agent adds (+) or removes (!) entries.',
  own: 'This list replaces the inherited one.',
};
const PATTERN_HELP = 'server or server/* is the whole server, server/tool one tool; only a pattern with * is a wildcard, '
  + 'matched against the full path and the tool name. An allowed tool also needs its server let through by the list: '
  + 'a wildcard alone must match the server name as well.';
const SYSTEM_PROMPT = ['agent_config', 'system_prompt'];
const SYSTEM_TEMPLATE = ['agent_config', 'system_template'];
const SKILLS = ['agent_config', 'skills'];
const HOOKS = ['agent_config', 'hooks'];

/** The agent rows of the list, null until loaded. */
let rows = null;
let listError = '';
let listErrors = [];
/** Picker data: classes, profiles, skills, hooks, prompt files. */
let meta = null;
/** The /tools catalogue: built servers and their tools. */
let catalog = [];
let catalogError = '';
/** The agent shown: its Detail as loaded, or a stand-in while a new one is unsaved. */
let detail = null;
/** The entry being edited, null when nothing (or a removed agent) is shown. */
let own = null;
/** The entry and the inherited values as they were loaded: what Revert goes back to. */
let pristine = null;
let loaded = '';
/** {name, source} while a new agent is not saved yet. */
let draft = null;
let selected = null;
/** A write on its way: the form is locked. */
let busy = false;
const collapsed = new Set();
const expanded = new Set();
/** Per tools list: "extend" chosen while it has no entry yet. */
let listModes = {};
let toolList = 'allowed';
let treeQuery = '';
let treeFilter = 'all';
/**
 * The servers the filter keeps, per tools list: worked out when the filter, the search, the catalogue or the agent
 * changes, not on a click -- a checked box must not take its own row away. `settled`: worked out with an answer of
 * /tools/effective (before it, inherited grants are unknown). Null: work it out at the next draw.
 */
let treeKept = null;
const patternDrafts = {};
let skillQuery = '';
let skillFilter = 'all';
/** The skills the filter keeps, per list; like `treeKept`, worked out when the filter, the search or the agent
 * changes, not on a click. */
let skillKept = null;
const openHooks = new Set();
let hookQuery = '';
let hookType = '';
let hookFilter = 'all';
/** The enabled sub-agent managers (/managers), null until loaded, and the agents they could start. */
let managers = null;
let managerAgents = [];
/** The manager open in the editor: {name, own, loaded, version, file}; written with its own Save. */
let managerEdit = null;
let managerQuery = '';
let managerFilter = 'all';
/** Like `treeKept`, for the manager's agent list. */
let managerKept = null;
let newManagerName = '';
/** A reload of the config on its way. */
let reloading = false;
/** Counts what the editor showed one after another (an agent, a draft): each starts with its tabs at the top; a
 * reload of the same one (a refresh, a save, a new agent saved) keeps their places. */
let shownView = 0;
/** "template" or "inline", fixed when the prompt tab is first drawn for an entry, then set by the radios. */
let promptChoice = null;
/** The inline prompt typed before switching to a template file: back when switching to inline again. */
let inlineStash = null;
/** The last /tools/effective answer, with `key` (the lists it was asked for) and `sent` (the own entries then). */
let effective = null;
let effectiveError = '';
let effectiveTimer = null;
/** The lists a /tools/effective question is scheduled or on its way for. */
let effectivePending = null;
let previewTimer = null;
/** The YAML tab: the text last rendered from the entry, the entry it was made from, and typed text not applied. */
let yamlRendered = null;
let yamlFor = null;
let yamlDraft = null;
/** Why the typed text did not apply: shown again when the tab is drawn anew. */
let yamlProblem = '';
let leavingYaml = false;
/** Typed text of the small YAML fields, by field key, until it parses: {text, path, hook, error, job}. */
const yamlFieldDrafts = new Map();
const yamlCache = new Map();
/** The newest call per kind of load: an answer overtaken by a later one is dropped. */
const calls = {};
let uid = 0;

// --------------------------------------------------------------------- helpers

const nextId = () => `ae${++uid}`;
const aborted = (error) => error?.name === 'AbortError';
const badge = (kind, content) => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${content}</span>`;
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
const clone = (value) => (value === undefined ? undefined : structuredClone(value));
const isMapping = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const display = (value) => (value === undefined || value === null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value));
const prefixed = (item) => typeof item === 'string' && /^[+!]/.test(item);
const disabledIf = (off) => (off ? 'disabled' : '');

/** A call of one kind; `current` is false when a later call of the same kind was made meanwhile. */
async function newest(kind, path, options = {}) {
  const mine = (calls[kind] = (calls[kind] ?? 0) + 1);
  try {
    const value = await api(path, { quiet: true, ...options });
    return { current: mine === calls[kind], value };
  } catch (error) {
    return { current: mine === calls[kind] && !aborted(error), error };
  }
}

/** JSON with sorted keys: two entries that differ only in key order compare equal. */
function canonical(value) {
  return JSON.stringify(value ?? null, (key, item) => (isMapping(item)
    ? Object.fromEntries(Object.keys(item).sort().map((name) => [name, item[name]])) : item));
}

function getPath(object, path) {
  return path.reduce((node, key) => (node === null || node === undefined ? undefined : node[key]), object);
}

function hasPath(object, path) {
  const parent = getPath(object, path.slice(0, -1));
  return isMapping(parent) && Object.hasOwn(parent, path.at(-1));
}

function setPath(object, path, value) {
  let node = object;
  for (const key of path.slice(0, -1)) {
    if (!isMapping(node[key])) node[key] = {};
    node = node[key];
  }
  node[path.at(-1)] = value;
}

/** Removes the key, and every mapping above it that it leaves empty. */
function unsetPath(object, path) {
  const parents = [object];
  for (const key of path.slice(0, -1)) {
    const next = parents.at(-1)[key];
    if (!isMapping(next)) return;
    parents.push(next);
  }
  delete parents.at(-1)[path.at(-1)];
  for (let i = parents.length - 1; i > 0 && !Object.keys(parents[i]).length; i -= 1) delete parents[i - 1][path[i - 1]];
}

const ownValue = (path) => (hasPath(own, path) ? getPath(own, path) : undefined);
const inherited = (path) => getPath(detail?.inherited, path);
const shown = (path) => (hasPath(own, path) ? getPath(own, path) : inherited(path));
const currentName = () => (draft ? draft.name : detail?.name);
const readOnly = () => !draft && (!detail?.editable || Boolean(detail?.form_reason));
const changedFromStart = () => own !== null && canonical(own) !== loaded;
const dirty = () => own !== null && (Boolean(draft) || changedFromStart() || yamlDraft !== null
  || yamlFieldDrafts.size > 0);
const fieldElement = (path) => document.querySelector(`#editor [data-field="${CSS.escape(JSON.stringify(path))}"]`);

/** Redraws, and puts the focus back on the control that had it (by data-key, else data-path). */
function keepFocus(draw) {
  const active = document.activeElement;
  const key = active?.dataset?.key;
  const path = active?.dataset?.path;
  const caret = typeof active?.selectionStart === 'number' ? active.selectionStart : null;
  draw();
  const next = (key && document.querySelector(`#editor [data-key="${CSS.escape(key)}"]`))
    || (path && document.querySelector(`#editor [data-path="${CSS.escape(path)}"]`));
  if (!next || next === active) return;
  next.focus();
  if (caret !== null) {
    try { next.setSelectionRange(caret, caret); } catch { /* a control without a caret */ }
  }
}

/** Focus for a control that went away: the first of the candidates that is there and enabled. */
function focusFirst(...candidates) {
  candidates.find((element) => element && !element.disabled)?.focus();
}

function setBusy(on) {
  busy = on;
  $('formLock').disabled = on;
  updateHead();
}

async function failed(error, { reload = true } = {}) {
  if (aborted(error)) return;
  if (error.status === 409 && reload && !draft) {
    const again = await dialog({
      title: 'Changed on disk',
      message: error.message,
      actions: [{ label: 'Keep my changes', value: false }, { label: 'Reload from disk', value: true, primary: true }],
    });
    if (again) await loadDetail({ force: true });
    return;
  }
  toast(error.status ? `${error.status}: ${error.message}` : error.message, { kind: 'error' });
}

// ------------------------------------------------------------------------ data

async function loadList() {
  const { current, value, error } = await newest('list', `${BASE}agents`);
  if (!current) return;
  if (error) {
    rows = null;
    listError = error.message;
  } else {
    rows = value.agents ?? [];
    listErrors = value.errors ?? [];
    listError = '';
  }
  drawList();
}

async function loadMeta() {
  const { current, value, error } = await newest('meta', `${BASE}meta`);
  if (!current) return;
  if (error) {
    toast(`Pickers are incomplete: ${error.message}`, { kind: 'warn' });
    return;
  }
  // the manager plugin came or went (a restart): an edited form is not drawn anew, its tab is
  const flipped = Boolean(meta?.sub_agents) !== Boolean(value.sub_agents);
  meta = value;
  if (flipped && own !== null) keepFocus(drawSubagents);
}

async function loadCatalog() {
  const { current, value, error } = await newest('tools', `${BASE}tools`);
  if (!current) return;
  catalogError = error ? error.message : '';
  if (error) return;
  const servers = (value.servers ?? []).sort((a, b) => a.server.localeCompare(b.server));
  if (canonical(servers) !== canonical(catalog)) {
    catalog = servers;
    treeKept = null;
    // the tools answer (and one on its way) went by the old catalogue
    effective = null;
    effectivePending = null;
    scheduleEffective(true);
    if ($('toolSummary')) render($('toolSummary'), summary());
    drawToolCount();
    if ($('tools-allowed')) {
      keepFocus(() => {
        drawToolLists();
        drawManagerRows();
      });
    }
  }
}

const managerDirty = () => managerEdit !== null && canonical(managerEdit.own) !== managerEdit.loaded;
const managerRow = (name) => managers?.find((one) => one.name === name);
/** No unsaved manager edits, or the user lets them go. */
const managerMayGo = async () => !managerDirty() || confirm(`Discard the unsaved changes to the manager “${managerEdit.name}”?`,
  { title: 'Unsaved changes', confirmLabel: 'Discard', danger: true });

async function loadManagers() {
  const { current, value, error } = await newest('managers', `${BASE}managers`);
  if (!current) return;
  if (error) {
    toast(`The sub-agent managers could not be loaded: ${error.message}`, { kind: 'warn' });
    managers ??= [];
  } else {
    managers = value.managers ?? [];
    managerAgents = value.agents ?? [];
  }
  // an open manager without edits follows the file; one with edits keeps them, and takes the file's new version when
  // its entry there is still the one the edits started from (another write to the same file, say the agent's)
  const row = managerEdit && managerRow(managerEdit.name);
  if (managerEdit && !row) {
    if (managerDirty()) toast(`${managerEdit.name} is no longer an enabled manager: its unsaved changes are dropped`, { kind: 'warn' });
    managerEdit = null;
    updateHead();
  } else if (managerEdit && !managerDirty()) {
    Object.assign(managerEdit, { own: clone(row.own), loaded: canonical(row.own), version: row.version, file: row.file });
  } else if (managerEdit && canonical(row.own) === managerEdit.loaded) {
    Object.assign(managerEdit, { version: row.version, file: row.file });
  }
  if ($('managerRows')) keepFocus(drawManagerLists);
}

const ready = Promise.all([loadMeta(), loadCatalog(), loadManagers()]);

/** The selected agent from the server; over edits made while it was on its way only when forced. */
async function loadDetail({ force = false } = {}) {
  const name = selected;
  if (!name) return;
  await ready;
  const { current, value, error } = await newest('detail', `${BASE}agents/${encodeURIComponent(name)}`);
  if (!current || selected !== name || draft) return;
  if (!force && dirty()) {
    if (error) toast(`${name} could not be reloaded: ${error.message}`, { kind: 'warn' });
    return;
  }
  if (error) {
    detail = null;
    own = null;
    showEditor(false);
    render($('placeholder'), empty('circle-alert', `${name} could not be loaded`, error.message));
    return;
  }
  setLoaded(value, clone(value.own));
}

function resetForm() {
  listModes = {};
  promptChoice = null;
  inlineStash = null;
  yamlDraft = null;
  yamlProblem = '';
  yamlRendered = null;
  yamlFor = null;
  yamlFieldDrafts.clear();
  Object.keys(patternDrafts).forEach((list) => delete patternDrafts[list]);
  effective = null;
  effectivePending = null;  // a timer that found no form left its lists marked: the new form asks again
  effectiveError = '';
  treeKept = null;
  skillKept = null;
}

function setLoaded(answer, entry) {
  detail = answer;
  own = entry ?? null;
  pristine = { own: clone(entry), inherited: clone(answer.inherited) };
  loaded = own === null ? '' : canonical(own);
  resetForm();
  drawEditor();
}

async function refreshAll(event) {
  if (event?.detail?.auto && busy) return;
  await Promise.all([loadList(), loadMeta(), loadCatalog(), loadManagers()]);
  if (selected && !draft && !busy && !dirty()) await loadDetail();
}

/** What an entry of this `type` inherits: the parent's values, or the defaults for a class. */
async function inheritedFor(type) {
  const query = typeof type === 'string' && type ? `?type=${encodeURIComponent(type)}` : '';
  const { current, value, error } = await newest('inherited', `${BASE}inherited${query}`);
  if (error) throw error;
  return current ? (value.inherited ?? {}) : null;
}

/** "Based on" changed (a select, a reset, applied YAML): the inherited values follow it. */
async function refreshInherited() {
  const entry = own;
  const type = own?.type;
  let values;
  try {
    values = await inheritedFor(type);
  } catch (error) {
    if (!aborted(error)) toast(`The inherited values could not be loaded: ${error.message}`, { kind: 'warn' });
    return;
  }
  if (values === null || own !== entry || own.type !== type) return;
  detail.inherited = values;
  promptChoice = null;
  keepFocus(() => TABS.forEach(drawTab));
  scheduleEffective();
}

// ------------------------------------------------------------------------ list

function drawList() {
  if (listError) {
    render($('list'), empty('circle-alert', 'Agents could not be loaded', listError));
    return;
  }
  if (!rows) return;
  const query = $('search').value.trim().toLowerCase();
  const keep = FILTERS[$('filter').value] ?? FILTERS.all;
  const matching = rows.filter((row) => keep(row) && (!query
    || [row.name, row.description || '', ...(row.tags || [])].some((text) => text.toLowerCase().includes(query))));
  const groups = new Map();
  for (const row of [...matching].sort((a, b) => a.name.localeCompare(b.name))) {
    if (!groups.has(row.group)) groups.set(row.group, []);
    groups.get(row.group).push(row);
  }
  const order = [...groups.keys()].sort((a, b) => (a === 'config' ? -1 : b === 'config' ? 1 : a.localeCompare(b)));
  const focused = document.activeElement?.closest('#list [data-name]')?.dataset.name;
  render($('list'), html`
    ${listErrors.length ? html`<div class="pk-card ae-banner ae-banner--warn" role="status">${icon('triangle-alert')}
      <div class="pk-grow">${listErrors.map((text) => html`<div>${text}</div>`)}</div></div>` : ''}
    ${order.length ? order.map((group) => html`
      <details class="ae-group" data-group="${group}" ${collapsed.has(group) ? '' : 'open'}>
        <summary class="ae-group-head">${icon('chevron-right', { size: 'sm' })}<span class="pk-grow">${group}</span>
          <span class="pk-tab-count" data-count="${groups.get(group).length}">${groups.get(group).length}</span></summary>
        <ul class="ae-items">${groups.get(group).map(listItem)}</ul>
      </details>`)
    : empty('search', rows.length ? 'No agent matches' : 'No agents defined')}`);
  if (focused) $('list').querySelector(`[data-name="${CSS.escape(focused)}"]`)?.focus();
}

function listItem(row) {
  const current = !draft && row.name === selected;
  return html`<li><button type="button" class="pk-btn pk-btn--ghost ae-item" data-name="${row.name}" aria-current="${String(current)}">
    <span class="ae-item-top">
      <span class="ae-item-name pk-truncate">${row.name}</span>
      ${row.enabled ? '' : badge('', 'off')}
      ${row.state === 'new' ? badge('info', 'new') : ''}
      ${row.restart ? html`<span class="pk-badge pk-badge--warn" title="The running app uses older settings">restart</span>` : ''}
      ${row.editable ? '' : html`<span class="pk-badge" title="${row.readonly_reason || 'Cannot be edited here'}">read-only</span>`}
    </span>
    <span class="ae-item-desc pk-muted pk-truncate">${row.description || ''}</span>
  </button></li>`;
}

async function leave() {
  if (!dirty() && !managerDirty()) return true;
  const what = [dirty() && `“${currentName()}”`, managerDirty() && `the manager “${managerEdit.name}”`].filter(Boolean).join(' and ');
  const discard = await confirm(`Discard the unsaved changes to ${what}?`,
    { title: 'Unsaved changes', confirmLabel: 'Discard', danger: true });
  if (discard) managerEdit = null;
  return discard;
}

async function choose(name) {
  if (!draft && name === selected && detail) return;
  if (busy || !(await leave())) return;
  selected = name;
  shownView += 1;
  managerEdit = null;
  draft = null;
  detail = null;
  own = null;
  resetForm();
  setDirty(false);
  showEditor(false);
  render($('placeholder'), html`<div class="pk-stack"><span class="pk-skeleton"></span><span class="pk-skeleton"></span></div>`);
  drawList();
  await loadDetail({ force: true });
}

// ---------------------------------------------------------------------- editor

function showEditor(on) {
  $('editor').hidden = !on;
  $('placeholder').hidden = on;
}

function drawEditor() {
  if (!detail) {
    showEditor(false);
    render($('placeholder'), empty('cpu', 'No agent selected', 'Pick an agent on the left, or create a new one.'));
    return;
  }
  showEditor(true);
  drawHead();
  drawBanners();
  $('tabArea').hidden = own === null;
  if (own !== null) TABS.forEach(drawTab);
  topOfTab(document.querySelector('#tabArea > [role="tabpanel"]:not([hidden])'));
  drawToolCount();
  updateHead();
  scheduleEffective(true);
}

/** A tab scrolled for another agent starts at the top (a hidden element keeps its offset, so on showing, too). */
function topOfTab(panel) {
  if (!panel || panel.dataset.shownView === String(shownView)) return;
  panel.scrollTop = 0;
  panel.dataset.shownView = String(shownView);
}

function drawTab(name) {
  ({
    general: drawGeneral, model: drawModel, tools: drawTools, subagents: drawSubagents, prompt: drawPrompt, hooks: drawHooks,
    yaml: drawYaml,
  })[name]();
}

function drawHead() {
  const name = currentName();
  const parent = rows?.find((row) => row.name === own?.type);
  // without an own type the entry takes the inherited one (after a reset: the defaults'), not the one on disk
  const base = own ? (own.type ?? detail.inherited?.type ?? '') : (detail.effective?.type ?? '');
  const file = draft ? '' : detail.file;
  const noEntry = Boolean(draft) || own === null;
  const children = detail.children ?? [];
  const deleteOff = noEntry || !detail.editable || children.length > 0;
  const dot = html`<span aria-hidden="true">·</span>`;
  render($('head'), html`
    <div class="pk-grow pk-stack ae-title">
      <div class="pk-row">
        <h2 class="ae-name">${name}</h2>
        ${draft ? html`<span class="pk-badge pk-badge--info" data-draft>new, not saved yet</span>` : ''}
      </div>
      <div class="pk-row pk-muted ae-meta">
        <span>based on</span>
        ${parent ? html`<button type="button" class="pk-btn pk-btn--sm" data-goto="${parent.name}" title="Open ${parent.name}">${icon('git-branch', { size: 'sm' })}${parent.name}</button>`
    : badge('', base || 'unknown')}
        ${dot}
        ${file ? html`<span class="ae-file-box"><code class="pk-mono ae-file">${file}</code>
          <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-copy="${file}" title="Copy the file path" aria-label="Copy the file path">${icon('copy', { size: 'sm' })}</button></span>`
    : html`<span>${draft ? 'saved as a new file' : 'not defined on disk'}</span>`}
        ${!draft && detail.state ? html`${dot}${stateLabel(detail.state)}` : ''}
      </div>
    </div>
    <div class="pk-row">
      <span class="pk-badge pk-badge--warn" id="dirtyMark" hidden>unsaved changes</span>
      <button type="button" class="pk-btn pk-btn--sm" id="revert" disabled>${icon('rotate-ccw', { size: 'sm' })} Revert</button>
      <button type="button" class="pk-btn pk-btn--primary pk-btn--sm" id="save" disabled>${icon('save', { size: 'sm' })} Save</button>
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" id="more" popovertarget="moreMenu" aria-label="More actions" title="More actions">${icon('ellipsis-vertical')}</button>
      <div id="moreMenu" class="pk-menu" popover>
        <button type="button" class="pk-menu-item" data-menu="duplicate" ${disabledIf(noEntry || Boolean(detail.form_reason))}
          title="${detail.form_reason || ''}">${icon('copy', { size: 'sm' })} Duplicate</button>
        <button type="button" class="pk-menu-item" data-menu="child" ${disabledIf(noEntry)}>${icon('git-branch', { size: 'sm' })} New child agent</button>
        <hr class="pk-menu-separator">
        <button type="button" class="pk-menu-item pk-menu-item--danger" data-menu="delete" ${disabledIf(deleteOff)}
          title="${children.length ? `Parent of ${children.join(', ')}` : ''}">${icon('trash-2', { size: 'sm' })} Delete</button>
      </div>
    </div>`);
}

function stateLabel(state) {
  const [kind, words] = STATES[state] ?? ['', state];
  return html`<span class="ae-state" data-state="${state}"><span class="pk-dot${kind ? ` pk-dot--${kind}` : ''}"></span>${words}</span>`;
}

function updateHead() {
  setDirty(dirty() || managerDirty());
  if (!$('save')) return;
  const changedNow = changedFromStart() || yamlDraft !== null || yamlFieldDrafts.size > 0;
  $('save').disabled = busy || own === null || readOnly() || !dirty();
  $('revert').disabled = busy || !changedNow;
  $('more').disabled = busy;
  $('dirtyMark').hidden = !changedNow;
}

function banner(kind, name, title, lines, action = '') {
  return html`<div class="pk-card ae-banner ae-banner--${kind}" role="status">${icon(name)}
    <div class="pk-grow"><strong>${title}</strong>${lines.filter(Boolean).map((line) => html`<div>${line}</div>`)}</div>${action}</div>`;
}

function drawBanners() {
  const parts = [];
  if (readOnly()) {
    parts.push(banner('warn', 'eye', 'Read-only', [detail.readonly_reason || detail.form_reason || 'This agent cannot be edited here.']));
  }
  // "off" needs no words beyond the state in the head
  if (!draft && detail.state && !['in_sync', 'off'].includes(detail.state)) parts.push(stateBanner());
  render($('banners'), parts);
}

function stateBanner() {
  const operator = 'Restarting is up to the operator.';
  if (detail.state === 'new') return banner('info', 'info', 'Not running yet', ['It starts with the next restart.', operator]);
  if (detail.state === 'removed') {
    return banner('warn', 'triangle-alert', 'Still running', ['It is no longer defined (or disabled) on disk and goes away with the next restart.', operator]);
  }
  const changed = detail.changed ?? [];
  const reload = detail.reload_fields ?? [];
  const restart = changed.filter((key) => !reload.includes(key));
  const action = reload.length ? html`<button type="button" class="pk-btn pk-btn--sm" data-reload data-key="banner-reload"
    ${disabledIf(reloading)}>${icon('rotate-ccw', { size: 'sm' })} Reload config</button>` : '';
  return banner('warn', 'triangle-alert', 'The running agent uses older settings', [
    reload.length ? `Reload config applies: ${reload.join(', ')}.` : '',
    restart.length || detail.restart ? `Needs a restart: ${restart.join(', ') || 'the definition'}. ${operator}` : '',
  ], action);
}

// ------------------------------------------------------------ field controls

/**
 * One form field bound to a path of the entry: the own value when the path is set, else the inherited one
 * (a placeholder, or muted) with an "inherited" mark; the reset button removes the path.
 */
function field(path, label, control, { help = '', group = false, helpId = '', set = hasPath(own, path) } = {}) {
  const key = JSON.stringify(path);
  return html`<div class="pk-field ae-field" data-field="${key}" data-set="${String(set)}">
    <div class="ae-field-head">
      ${group ? html`<span class="pk-label">${label}</span>` : html`<label class="pk-label" for="${control.id}">${label}</label>`}
      <span class="pk-badge ae-inherited-mark">inherited</span>
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm ae-reset" data-reset
        title="Reset to inherited" aria-label="Reset ${label} to inherited">${icon('rotate-ccw', { size: 'sm' })}</button>
    </div>
    ${control.markup}
    ${help || helpId ? html`<span class="pk-help" ${helpId ? html`id="${helpId}"` : ''}>${help}</span>` : ''}
  </div>`;
}

function syncField(wrapper) {
  if (wrapper?.dataset.field) wrapper.dataset.set = String(hasPath(own, JSON.parse(wrapper.dataset.field)));
}

function input(path, { type = 'text', mono = false, list = '' } = {}) {
  const id = nextId();
  return { id, markup: html`<input id="${id}" class="pk-input${mono ? ' pk-input--mono' : ''}" type="${type}"
    data-bind="${type === 'number' ? 'number' : 'text'}" data-path="${JSON.stringify(path)}"
    value="${display(ownValue(path))}" placeholder="${display(inherited(path))}" ${list ? html`list="${list}"` : ''} autocomplete="off">` };
}

function textarea(path, { rows: lines = 3, mono = false, keepEmpty = false } = {}) {
  const id = nextId();
  return { id, markup: html`<textarea id="${id}" class="pk-textarea${mono ? ' pk-input--mono' : ''}" rows="${lines}"
    data-bind="text" data-path="${JSON.stringify(path)}" ${keepEmpty ? 'data-keep-empty' : ''}
    placeholder="${display(inherited(path))}" spellcheck="${String(!mono)}">${display(ownValue(path))}</textarea>` };
}

function choice(path, options) {
  const id = nextId();
  return { id, markup: html`<select id="${id}" class="pk-select" data-bind="select" data-path="${JSON.stringify(path)}">${options}</select>` };
}

function option(value, current, label = value, title = '') {
  return html`<option value="${value}" ${value === current ? 'selected' : ''} title="${title}">${label}</option>`;
}

function toggle(path, text, fallback) {
  const id = nextId();
  const on = shown(path) ?? fallback;
  return { id, markup: html`<label class="pk-switch"><input id="${id}" type="checkbox" role="switch" data-bind="bool"
    data-path="${JSON.stringify(path)}" ${on ? 'checked' : ''}> ${text}</label>` };
}

/** A YAML textarea over a coloured copy of its text; `paint` keeps the copy current. */
const codeBox = (textarea) => html`<div class="ae-code"><pre class="ae-code-view" aria-hidden="true"></pre>${textarea}</div>`;

function paint(area) {
  const view = area.previousElementSibling;
  if (!view?.classList.contains('ae-code-view')) return;
  // a pre drops a last empty line: the copy would end one line short of the text above it
  const text = `${area.value}${area.value.endsWith('\n') || !area.value ? ' ' : ''}`;
  render(view, yamlCode(text));
  followScroll(area);
}

function followScroll(area) {
  const view = area.previousElementSibling;
  if (!view?.classList.contains('ae-code-view')) return;
  view.scrollTop = area.scrollTop;
  view.scrollLeft = area.scrollLeft;
}

function setCode(area, text) {
  area.value = text;
  paint(area);
}

/** A textarea holding a mapping as YAML; parsed when it loses the focus. */
function yamlField(path, label, help, { hook = null } = {}) {
  const id = nextId();
  const markup = html`<div class="pk-stack ae-yaml-box" data-yaml-box>
    ${codeBox(html`<textarea id="${id}" class="pk-textarea pk-input--mono" rows="4" spellcheck="false" wrap="off" data-yaml="${JSON.stringify(path)}"
      data-key="yaml:${JSON.stringify(path)}" ${hook === null ? '' : html`data-hook="${hook}"`} aria-label="${label}"></textarea>`)}
    <span class="pk-error" data-yaml-error hidden></span></div>`;
  return hook === null ? field(path, label, { id, markup }, { help }) : markup;
}

/** A hook override without its `enabled`: the hook's own settings. */
const customKeys = (override) => Object.fromEntries(Object.entries(override ?? {}).filter(([key]) => key !== 'enabled'));

function yamlOf(value) {
  if (value === undefined || value === null || (isMapping(value) && !Object.keys(value).length)) return Promise.resolve('');
  const key = canonical(value);
  if (!yamlCache.has(key)) {
    yamlCache.set(key, api(`${BASE}yaml`, { method: 'POST', json: { entry: value }, quiet: true })
      .then((answer) => answer.yaml)
      .catch(() => {
        yamlCache.delete(key);
        return JSON.stringify(value, null, 2);  // JSON is YAML as well
      }));
  }
  return yamlCache.get(key);
}

const liveYaml = (key) => document.querySelector(`#editor [data-yaml][data-key="${CSS.escape(key)}"]`);

/** The values as YAML; a field with typed text keeps that text, and its error, through every redraw. */
async function fillYaml(root) {
  await Promise.all([...root.querySelectorAll('[data-yaml]')].map(async (area) => {
    const path = JSON.parse(area.dataset.yaml);
    const hook = area.dataset.hook !== undefined;
    const typed = yamlFieldDrafts.get(area.dataset.key);
    if (typed) {
      setCode(area, typed.text);
      markYaml(area, typed.error);
    }
    const current = () => (hook ? customKeys(getPath(own, path)) : ownValue(path));
    let asked;
    let value;
    let placeholder;
    do {  // a parse that lands meanwhile changes `own`: show what it holds now
      asked = canonical(current());
      [value, placeholder] = await Promise.all([
        yamlOf(current()), yamlOf(hook ? customKeys(inherited(path)) : inherited(path))]);
    } while (area.isConnected && canonical(current()) !== asked);
    if (!area.isConnected) return;
    area.placeholder = placeholder;
    if (!yamlFieldDrafts.has(area.dataset.key)) setCode(area, value);
  }));
}

function markYaml(area, problem) {
  const error = area.closest('[data-yaml-box]').querySelector('[data-yaml-error]');
  error.textContent = problem;
  error.hidden = !problem;
  if (problem) area.setAttribute('aria-invalid', 'true');
  else area.removeAttribute('aria-invalid');
}

function typedYamlField(area) {
  const { key, yaml, hook } = area.dataset;
  yamlFieldDrafts.set(key, { text: area.value, path: JSON.parse(yaml), hook: hook ?? null, error: '', job: null });
  markYaml(area, '');  // the error was about the text before
  updateHead();
}

/** A small YAML field's typed text into the entry. False while it does not parse; the error shows at the field. */
function parseYamlDraft(key) {
  const typed = yamlFieldDrafts.get(key);
  if (!typed) return Promise.resolve(true);
  if (typed.job) return typed.job;
  const entry = own;
  typed.job = (async () => {
    let value;
    let problem = '';
    if (typed.text.trim()) {
      const answer = await newest(`parse:${key}`, `${BASE}yaml/parse`, { method: 'POST', json: { yaml: typed.text } });
      if (!answer.current) {  // a later text of this field is on its way (or the page goes)
        typed.job = null;
        return true;
      }
      if (answer.error) problem = answer.error.message;
      else if (!isMapping(answer.value.entry)) problem = 'Expected a mapping: one "key: value" per line';
      else value = answer.value.entry;
    }
    // another agent, a reverted form, or typed on meanwhile: this text is not the one to apply
    if (own !== entry || yamlFieldDrafts.get(key) !== typed) return true;
    const live = liveYaml(key);
    if (problem) {
      typed.error = problem;
      typed.job = null;  // parsed again when asked again
      if (live) markYaml(live, problem);
      return false;
    }
    if (typed.hook !== null) setHookCustom(typed.hook, value ?? {});
    else if (value === undefined) unsetPath(own, typed.path);
    else setPath(own, typed.path, value);
    yamlFieldDrafts.delete(key);
    if (live) {
      markYaml(live, '');
      syncField(live.closest('[data-field]'));
    }
    changed();
    return true;
  })();
  return typed.job;
}

/** Before a save: the YAML tab's text applied, every typed YAML field parsed. The data-key of the first field that
 * does not parse, or null; its error shows at the field. */
async function settleYaml() {
  if (yamlDraft !== null && !(await applyYaml())) {
    toast('The YAML tab has text that does not parse: fix it or show the form’s entry again', { kind: 'error' });
    return 'yaml-text';
  }
  const keys = [...yamlFieldDrafts.keys()];
  const results = await Promise.all(keys.map(parseYamlDraft));
  if (results.every(Boolean) && !yamlFieldDrafts.size) return null;
  // a hook's settings may sit in a closed row: open it; a row the filter hides: clear the filter
  const hooks = [...yamlFieldDrafts.values()].filter((typed) => typed.hook !== null);
  if (hooks.some((typed) => !openHooks.has(typed.hook))) {
    hooks.forEach((typed) => openHooks.add(typed.hook));
    drawHookTable();
  }
  if ([...yamlFieldDrafts.keys()].some((key) => !liveYaml(key))) {
    hookQuery = '';
    hookType = '';
    hookFilter = 'all';
    drawHooks();
  }
  toast('Fix the YAML marked in red before saving', { kind: 'error' });
  return yamlFieldDrafts.keys().next().value ?? null;
}

/** A field with an error in sight: its tab selected, the focus in it. Only once the form is unlocked (a locked
 * fieldset disables the tab buttons, too). */
function reveal(key) {
  const area = key === 'yaml-text' ? $('yamlText') : liveYaml(key);
  const panel = area?.closest('#tabArea > [role="tabpanel"]');
  if (!panel) return;
  if (panel.hidden) $(`tabButton-${panel.id.replace('tab-', '')}`).click();
  area.focus();
}

// --------------------------------------------------------------------- general

function drawGeneral() {
  const type = shown(['type']) ?? '';
  const classes = meta?.classes ?? [];
  const agents = (rows ?? []).filter((row) => row.name !== currentName() && row.file);  // on disk: a possible parent
  const known = classes.some((one) => one.name === type) || agents.some((one) => one.name === type);
  const visibility = shown(['metadata', 'visibility']) ?? 'private';
  const visibilities = meta?.visibility ?? Object.keys(VISIBILITY);
  render($('tab-general'), html`
    <fieldset class="ae-form" ${disabledIf(readOnly())}>
      <section class="ae-section" aria-labelledby="basicsTitle">
        <h3 class="ae-section-title" id="basicsTitle">Basics</h3>
        <div class="ae-grid">
          ${field(['enabled'], 'Enabled', toggle(['enabled'], 'Starts with the app', false),
            { help: 'Not inherited: without a value of its own the agent stays off.' })}
          ${field(['type'], 'Based on', choice(['type'], html`
            ${known || !type ? '' : option(type, type)}
            <optgroup label="Agent classes">${classes.map((one) => option(one.name, type, one.name, one.description || ''))}</optgroup>
            <optgroup label="Inherit from agent">${agents.map((one) => option(one.name, type))}</optgroup>`),
            { help: 'A class starts from its defaults; an agent passes on all of its settings.' })}
        </div>
        ${field(['description'], 'Description', textarea(['description'], { rows: 2 }))}
      </section>
      <section class="ae-section" aria-labelledby="listingTitle">
        <h3 class="ae-section-title" id="listingTitle">Listing</h3>
        <div class="ae-grid">
          ${field(['metadata', 'visibility'], 'Visibility', choice(['metadata', 'visibility'],
            visibilities.map((value) => option(value, visibility))), { help: VISIBILITY[visibility] ?? '', helpId: 'visibilityHelp' })}
          ${field(['metadata', 'category'], 'Category', input(['metadata', 'category']))}
        </div>
        ${tagsField()}
      </section>
    </fieldset>`);
}

function tagsField() {
  const path = ['metadata', 'tags'];
  const tags = shown(path) ?? [];
  const id = nextId();
  return field(path, 'Tags', { id, markup: html`<div class="ae-chips">
    ${tags.map((tag, index) => html`<span class="pk-badge ae-chip">${tag}<button type="button"
      class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-tag-remove="${tag}" data-index="${index}"
      data-key="tag:${tag}" aria-label="Remove the tag ${tag}" title="Remove">${icon('x', { size: 'sm' })}</button></span>`)}
    <input id="${id}" class="pk-input pk-input--sm ae-chip-input" data-tag-add data-key="tag-add" placeholder="Add a tag, Enter" autocomplete="off">
  </div>` });
}

function editTags(next) {
  const path = ['metadata', 'tags'];
  setPath(own, path, next([...(shown(path) ?? [])]));
  keepFocus(drawGeneral);
  changed();
}

function removeTag(button) {
  const index = Number(button.dataset.index);
  editTags((tags) => tags.filter((tag) => tag !== button.dataset.tagRemove));
  const buttons = $('tab-general').querySelectorAll('[data-tag-remove]');
  focusFirst(buttons[index], buttons[index - 1], $('tab-general').querySelector('[data-tag-add]'));
}

function spawnRows() {
  if (draft) return html`<p class="pk-muted">Save the agent first.</p>`;
  const managers = detail.spawnable ?? [];
  if (!managers.length) return html`<p class="pk-muted">No enabled sub-agent manager.</p>`;
  return html`<div class="pk-stack">${managers.map((one) => {
    const key = `spawn:${one.sam}`;
    return html`<div class="ae-spawn">
      <label class="pk-switch"><input type="checkbox" role="switch" data-spawn="${one.sam}" data-key="${key}"
        ${one.allowed ? 'checked' : ''} ${disabledIf(!one.editable || own === null)}> <span class="pk-mono">${one.sam}</span></label>
      <span class="pk-muted" data-rule>${one.rule}</span>
      ${one.editable ? '' : html`<span class="pk-help">${one.file} cannot be edited here</span>`}
    </div>`;
  })}</div>`;
}

async function toggleSpawn(box) {
  const key = box.dataset.key;
  const manager = detail?.spawnable?.find((one) => one.sam === box.dataset.spawn);
  if (busy || !manager) {
    box.checked = !box.checked;  // nothing was asked: the switch shows what is
    return;
  }
  const allowed = box.checked;
  const name = detail.name;
  const url = `${BASE}agents/${encodeURIComponent(name)}/spawnable`;
  const body = (dryRun) => ({ sam: manager.sam, allowed, version: manager.version, dry_run: dryRun });
  // the same lock as Save and Delete: they share the diff dialog, and a write here may move the agent file's version
  setBusy(true);
  try {
    const preview = await api(url, { method: 'PUT', json: body(true), quiet: true });
    const title = `${allowed ? 'Allow' : 'Stop'} ${name} in ${manager.sam}`;
    if (preview.diff && await showDiff({ title, diff: preview.diff, confirmLabel: 'Write file', notes: [`Changes ${manager.file}.`] })) {
      const answer = await api(url, { method: 'PUT', json: body(false), quiet: true });
      toast(`${manager.sam}: ${name} is ${allowed ? 'allowed' : 'no longer allowed'}`, { kind: 'ok' });
      await Promise.all([refreshSpawnable(name, manager, answer.version), loadManagers()]);
    }
  } catch (error) {
    await failed(error, { reload: false });
    await refreshSpawnable(name, manager, null);
  } finally {
    setBusy(false);
    if (detail?.name === name && !draft && own !== null) {
      drawSubagents();
      $('tab-subagents').querySelector(`[data-key="${CSS.escape(key)}"]`)?.focus();
    }
  }
}

/** The managers' state for `name` after a write; an answer for another agent (or none shown) is dropped. */
async function refreshSpawnable(name, manager, version) {
  const { current, value: fresh, error } = await newest('spawnable', `${BASE}agents/${encodeURIComponent(name)}`);
  if (!current || error || !detail || detail.name !== name || draft) return;
  if (!dirty()) {
    setLoaded(fresh, clone(fresh.own));
    return;
  }
  detail.spawnable = fresh.spawnable;
  // the manager lives in the agent's own file: the write moved the version the save checks against
  if (version && manager.file === detail.file && fresh.version === version) detail.version = version;
}

// ------------------------------------------------------------------ sub-agents

function drawSubagents() {
  const shown = Boolean(meta?.sub_agents);
  const button = $('tabButton-subagents');
  if (!shown && button.getAttribute('aria-selected') === 'true') {
    selectTab($('tabs'), 'general');  // no tabchange: its work done here
    topOfTab($('tab-general'));
  }
  button.hidden = !shown;
  // the kit's arrow keys step through every [role="tab"]: a hidden button is none
  if (shown) button.setAttribute('role', 'tab');
  else button.removeAttribute('role');
  if (!shown) {
    render($('tab-subagents'), '');
    return;
  }
  render($('tab-subagents'), html`
    <section class="ae-section" aria-labelledby="usesTitle">
      <h3 class="ae-section-title" id="usesTitle">Managers this agent uses</h3>
      <p class="pk-help">The agent starts sub-agents through the managers it may use: a tool grant, saved with the agent.
        Which agents a manager can start is set in the manager.</p>
      <div id="managerRows"></div>
      <fieldset class="ae-plain" ${disabledIf(readOnly())}>
        <div class="pk-row ae-filters">
          <input class="pk-input pk-input--sm pk-input--mono ae-chip-input" data-new-manager data-key="new-manager" autocomplete="off"
            value="${newManagerName}" placeholder="${currentName()}_sam" aria-label="Name of a new manager">
          <button type="button" class="pk-btn pk-btn--sm" data-create-manager>${icon('plus', { size: 'sm' })} New manager</button>
        </div>
        <p class="pk-error" id="newManagerError" hidden></p>
      </fieldset>
    </section>
    <section class="ae-section" id="managerEditor" aria-labelledby="managerTitle" hidden></section>
    <section class="ae-section" aria-labelledby="spawnTitle">
      <h3 class="ae-section-title" id="spawnTitle">Managers that may start this agent</h3>
      <p class="pk-help">A switch changes that manager’s own lists; it is written right away, after its diff.</p>
      ${spawnRows()}
    </section>`);
  drawManagerLists();
}

function drawManagerLists() {
  drawManagerRows();
  drawManagerEditor();
}

/** An entry the manager switch writes or takes back: the manager, its tools, or all of them. */
const managerEntry = (sam) => (item) => item === sam || item.startsWith(`${sam}/`);

/**
 * Whether the agent's tools grant the manager: as the tree says for a running one (and locked when a pattern the
 * switch does not write grants or blocks it), from the lists for one that does not run yet.
 */
function usesManager(sam) {
  const model = listModel('allowed');
  const mine = managerEntry(sam);
  const inherited = model.mode !== 'own' && model.base.some(mine) && !model.removes.some(mine);
  const entry = catalog.find((one) => one.server === sam);
  if (!entry) return { on: model.adds.some(mine) || inherited, locked: false, title: inherited ? 'inherited' : '' };
  const on = serverView(model, entry).state !== 'off';
  if (!effective) return { on, locked: true, title: effectiveError || 'Waiting for the tools the agent gets' };
  const answers = entry.tools.map((tool) => effective?.per_tool?.[`${sam}/${tool.name}`] ?? {});
  const blockedBy = [...new Set(answers.flatMap((one) => one.blocked_by ?? []))];
  const foreign = [...new Set(answers.flatMap((one) => one.allowed_by ?? []).filter((pattern) => !mine(pattern)))];
  if (blockedBy.length) return { on, locked: true, title: `Blocked by ${blockedBy.join(', ')}` };
  if (on && foreign.length) return { on, locked: true, title: `Granted by ${foreign.join(', ')}` };
  return { on, locked: false, title: inherited ? 'inherited' : '' };
}

/** Grants or takes back the manager in the agent's allowed tools, in the list's own form. */
function useManager(sam, on) {
  const model = listModel('allowed');
  if (model.mode === 'mixed') {
    toast('The allowed tools mix + and ! entries with plain ones: fix that in the Tools tab first', { kind: 'error' });
    drawManagerRows();
    return;
  }
  const mine = managerEntry(sam);
  const inherited = model.mode !== 'own' && model.base.some(mine);
  let adds = model.adds.filter((item) => !mine(item));
  let removes = model.removes.filter((item) => !mine(item));
  if (on && !inherited) adds = [...adds, `${sam}/*`];
  if (!on && inherited) removes = [...removes, `${sam}/*`];
  writeList(model, adds, removes);
  toolsChanged();
}

function drawManagerRows() {
  const box = $('managerRows');
  if (!box || own === null) return;
  if (managers === null) {
    render(box, html`<span class="pk-skeleton"></span>`);
    return;
  }
  if (!managers.length) {
    render(box, html`<p class="pk-muted">No sub-agent manager is set up yet: create one below.</p>`);
    return;
  }
  render(box, html`<div class="pk-table-wrap"><table class="pk-table ae-managers">
    <thead><tr><th>Uses</th><th>Manager</th><th>Can start</th><th><span class="pk-sr-only">Configure</span></th></tr></thead>
    <tbody>${managers.map(managerLine)}</tbody>
  </table></div>`);
}

function managerLine(row) {
  const use = usesManager(row.name);
  const running = catalog.some((one) => one.server === row.name);
  const every = row.allowed.includes('*') && !row.blocked.length;
  const names = row.spawns;
  const open = managerEdit?.name === row.name;
  return html`<tr data-manager="${row.name}">
    <td><label class="pk-switch" title="${use.title}"><input type="checkbox" role="switch" data-use-manager="${row.name}"
      data-key="use:${row.name}" aria-label="${currentName()} may use ${row.name}" ${use.on ? 'checked' : ''}
      ${disabledIf(readOnly() || use.locked)}></label></td>
    <td><div class="pk-mono">${row.name}</div>
      <div class="pk-row pk-muted ae-meta">${row.file ? html`<span class="pk-mono">${row.file}</span>` : ''}
        ${running ? '' : badge('info', 'not running yet')}
        ${row.editable ? '' : html`<span class="pk-badge" title="${row.readonly_reason || ''}">read-only</span>`}</div></td>
    <td><span data-spawn-count="${row.name}">${every ? 'every agent' : plural(names.length, 'agent')}</span>
      ${every ? '' : html`<span class="pk-muted pk-truncate ae-spawn-names" title="${names.join(', ')}">${names.join(', ')}</span>`}</td>
    <td><button type="button" class="pk-btn pk-btn--sm" data-configure="${row.name}" data-key="configure:${row.name}"
      aria-expanded="${String(open)}">${icon('settings', { size: 'sm' })} ${open ? 'Close' : 'Configure'}</button></td>
  </tr>`;
}

const plainAgentList = (list) => Array.isArray(list)
  && list.every((item) => typeof item === 'string' && !/[*?[\]]/.test(item) && !prefixed(item));

function drawManagerEditor() {
  const box = $('managerEditor');
  if (!box) return;
  const row = managerEdit && managerRow(managerEdit.name);
  box.hidden = !row;
  if (!row) {
    render(box, '');
    return;
  }
  const own = managerEdit.own;
  render(box, html`
    <div class="pk-row">
      <h3 class="ae-section-title pk-grow" id="managerTitle">Manager <span class="pk-mono">${row.name}</span></h3>
      <span class="pk-badge pk-badge--warn" data-manager-dirty ${managerDirty() ? '' : 'hidden'}>unsaved changes</span>
    </div>
    <p class="pk-help">${row.editable ? html`Written to <span class="pk-mono">${row.file}</span> with its own Save, apart from this agent.`
    : `Read-only: ${row.readonly_reason}`}</p>
    <fieldset class="ae-form" ${disabledIf(!row.editable)}>
      <div class="pk-field">
        <span class="pk-label">Agents it can start</span>
        ${managerAgentsField(row, own)}
      </div>
      <div class="ae-grid">${MANAGER_SETTINGS.map((setting) => managerSetting(own, setting))}</div>
      <p class="pk-help">Other keys of the entry (hook_config, phase_filtering, …) stay as they are.</p>
      <div class="pk-row">
        <button type="button" class="pk-btn pk-btn--sm" data-revert-manager ${disabledIf(!managerDirty())}>${icon('rotate-ccw', { size: 'sm' })} Revert</button>
        <button type="button" class="pk-btn pk-btn--primary pk-btn--sm" data-save-manager ${disabledIf(!managerDirty())}>${icon('save', { size: 'sm' })} Save manager</button>
      </div>
    </fieldset>`);
}

function managerAgentsField(row, own) {
  const list = own.allowed_agents;
  if (!plainAgentList(list)) {
    const why = list === undefined
      ? `It has no list of its own and takes ${display(row.allowed)}: it starts ${plural(row.spawns.length, 'agent')} now`
      : `Its list uses patterns or +/! entries (${display(list)}): it starts ${plural(row.spawns.length, 'agent')} now`;
    return html`<p class="pk-help" data-manager-patterns>${why}.
      <button type="button" class="pk-btn pk-btn--sm" data-pick-agents>${icon('pencil', { size: 'sm' })} Pick agents instead</button></p>`;
  }
  const query = managerQuery.trim().toLowerCase();
  managerKept ??= new Set(managerAgents.filter((name) => list.includes(name) === (managerFilter === 'in')));
  const shown = managerAgents.filter((name) => (!query || name.includes(query)) && (managerFilter === 'all' || managerKept.has(name)));
  const count = shown.length === managerAgents.length ? plural(shown.length, 'agent') : `${shown.length} of ${plural(managerAgents.length, 'agent')}`;
  return html`<div class="pk-row ae-filters">
      <label class="pk-search pk-grow">${icon('search')}<input type="search" class="pk-input pk-input--sm" data-manager-search
        data-key="manager-search" value="${managerQuery}" placeholder="Search agents" aria-label="Search the agents"></label>
      <select class="pk-select pk-select--sm" data-manager-filter data-key="manager-filter" aria-label="Show agents">
        ${MANAGER_FILTERS.map(([value, label]) => option(value, managerFilter, label))}</select>
    </div>
    <div class="ae-checklist" role="group" aria-label="Agents ${row.name} can start">
      <span class="pk-help" data-managers-shown="${shown.length}">${count}</span>
      ${shown.length ? shown.map((name) => html`<label class="pk-check ae-check-row"><input type="checkbox" data-manager-agent="${name}"
        data-key="manager-agent:${name}" ${list.includes(name) ? 'checked' : ''}> <span class="pk-mono">${name}</span>
        ${row.blocked.includes(name) ? badge('danger', 'blocked') : ''}</label>`)
    : html`<span class="pk-muted">No agent matches.</span>`}
      ${list.filter((name) => !managerAgents.includes(name)).map((name) => badge('warn', `${name}: no such agent`))}
    </div>`;
}

function managerSetting(own, [key, label, kind, onText, offText]) {
  const value = own[key];
  if (kind === 'number') {
    return html`<label class="pk-field"><span class="pk-label">${label}</span><input class="pk-input" type="number" min="0" step="1"
      data-manager-setting="${key}" data-key="manager:${key}" value="${display(value)}" placeholder="plugin default"></label>`;
  }
  const state = typeof value === 'boolean' ? String(value) : '';
  return html`<label class="pk-field"><span class="pk-label">${label}</span><select class="pk-select" data-manager-setting="${key}"
    data-key="manager:${key}">${option('', state, 'Plugin default')}${option('true', state, onText)}${option('false', state, offText)}</select></label>`;
}

/** A manager edit: the dirty mark and the two buttons follow without drawing the editor anew. */
function managerChanged() {
  const box = $('managerEditor');
  const edited = managerDirty();
  box.querySelector('[data-manager-dirty]').hidden = !edited;
  box.querySelectorAll('[data-revert-manager], [data-save-manager]').forEach((button) => { button.disabled = !edited; });
  updateHead();
}

function setManagerValue(control) {
  const key = control.dataset.managerSetting;
  const own = managerEdit.own;
  if (control.tagName === 'SELECT') {
    if (control.value === '') delete own[key];
    else own[key] = control.value === 'true';
  } else {
    // min 0, step 1: "-1", "1.5" and a lone "-" (whose value reads empty) are no limit
    const valid = control.validity.valid;
    control.setAttribute('aria-invalid', String(!valid));
    if (!valid) return;
    if (control.value === '') delete own[key];
    else own[key] = control.valueAsNumber;
  }
  managerChanged();
}

function toggleManagerAgent(box) {
  const own = managerEdit.own;
  const name = box.dataset.managerAgent;
  own.allowed_agents = own.allowed_agents.filter((item) => item !== name);
  if (box.checked) own.allowed_agents.push(name);
  managerChanged();
}

async function openManager(name) {
  const closing = managerEdit?.name === name;
  if (!(await managerMayGo())) return;
  const row = managerRow(name);
  managerEdit = closing || !row ? null
    : { name, own: clone(row.own), loaded: canonical(row.own), version: row.version, file: row.file };
  managerQuery = '';
  managerFilter = 'all';
  managerKept = null;
  keepFocus(drawManagerLists);
  updateHead();
  if (managerEdit) $('managerEditor').scrollIntoView({ block: 'nearest' });
}

function revertManager() {
  const row = managerRow(managerEdit.name);
  Object.assign(managerEdit, { own: clone(row.own), loaded: canonical(row.own), version: row.version, file: row.file });
  managerKept = null;
  keepFocus(drawManagerEditor);
  updateHead();
}

async function saveManager() {
  const edit = managerEdit;
  if (busy || !edit || !managerDirty()) return;
  const url = `${BASE}managers/${encodeURIComponent(edit.name)}`;
  const body = (dryRun) => ({ entry: edit.own, version: edit.version, dry_run: dryRun });
  setBusy(true);
  try {
    const preview = await api(url, { method: 'PUT', json: body(true), quiet: true });
    if (!preview.diff) {
      toast('Nothing to write: the file says this already', { kind: 'info' });
      return;
    }
    if (!(await showDiff({ title: `Save ${edit.name}`, diff: preview.diff, confirmLabel: 'Write file', notes: [`Changes ${edit.file}.`] }))) return;
    const answer = await api(url, { method: 'PUT', json: body(false), quiet: true });
    toast(`${edit.name} saved to ${edit.file}`, { kind: 'ok' });
    edit.loaded = canonical(edit.own);
    edit.version = answer.version;
    const reloads = [loadManagers()];
    if (detail && !draft) reloads.push(refreshSpawnable(detail.name, { file: edit.file }, answer.version));
    await Promise.all(reloads);
  } catch (error) {
    await failed(error, { reload: false });
    await loadManagers();
  } finally {
    setBusy(false);
    if (own !== null) keepFocus(drawSubagents);
  }
}

function newManagerError(text) {
  $('newManagerError').textContent = text;
  $('newManagerError').hidden = !text;
}

/** A manager of its own for this agent: its file first (after the diff), then the grant in the agent's tools. */
async function createManager() {
  const name = newManagerName.trim();
  if (busy || !name) return;
  if (!(await managerMayGo())) return;
  const body = (dryRun) => ({ name, allowed_agents: [], dry_run: dryRun });
  let created = false;
  setBusy(true);
  try {
    const preview = await api(`${BASE}managers`, { method: 'POST', json: body(true), quiet: true });
    const notes = [`Creates ${preview.file}. It starts no agent until you pick some, and runs after the next restart.`,
      `${currentName()} gets it in its allowed tools; that is saved with the agent.`];
    if (!(await showDiff({ title: `Create ${name}`, diff: preview.diff, confirmLabel: 'Create file', notes }))) return;
    await api(`${BASE}managers`, { method: 'POST', json: body(false), quiet: true });
    toast(`${name} created in ${preview.file}`, { kind: 'ok' });
    newManagerName = '';
    const field = document.querySelector('#editor [data-new-manager]');
    if (field) field.value = '';
    newManagerError('');
    created = true;
    await loadManagers();
  } catch (error) {
    if (!aborted(error)) newManagerError(error.message);
  } finally {
    setBusy(false);
  }
  if (!created) return;
  managerEdit = null;
  useManager(name, true);
  await openManager(name);
}

// ----------------------------------------------------------------------- model

const chainOf = (value) => (value === undefined || value === null ? [] : Array.isArray(value) ? value : [value]);
const profileOf = (name) => meta?.profiles?.find((one) => one.name === name);

function drawModel() {
  render($('tab-model'), html`<fieldset class="ae-form" ${disabledIf(readOnly())}>
    <section class="ae-section" aria-labelledby="modelsTitle">
      <h3 class="ae-section-title" id="modelsTitle">Models</h3>
      ${chainField(['agent_config', 'llm_profile'], 'Model chain', 'The first profile answers; the others take over, in order, when it fails.')}
      ${chainField(['agent_config', 'llm_profile_advanced'], 'Advanced chain', 'Used while the agent escalates.')}
    </section>
    <section class="ae-section" aria-labelledby="runTitle">
      <h3 class="ae-section-title" id="runTitle">Run settings</h3>
      <div class="ae-grid">
        ${field(['agent_config', 'max_steps'], 'Max steps', input(['agent_config', 'max_steps'], { type: 'number' }))}
        ${field(['agent_config', 'fallback_recovery_seconds'], 'Fallback recovery (seconds)',
          input(['agent_config', 'fallback_recovery_seconds'], { type: 'number' }), { help: 'How long a failed profile rests before it is tried again.' })}
      </div>
      ${yamlField(['agent_config', 'llm_params'], 'LLM parameters', 'YAML: parameters for every profile, or keyed by profile name or "*".')}
    </section>
  </fieldset>`);
  fillYaml($('tab-model'));
}

function chainField(path, label, help) {
  const key = JSON.stringify(path);
  const chain = chainOf(shown(path));
  const id = nextId();
  const fallback = meta?.default_profile ? ` (${meta.default_profile})` : '';
  return field(path, label, { id, markup: html`
    <ol class="ae-chain" aria-label="${label}">
      ${chain.length ? chain.map((name, i) => chainRow(key, name, i, chain.length))
    : html`<li class="pk-muted">No profile: the default applies${fallback}.</li>`}
    </ol>
    <div class="ae-picker">
      <label class="pk-search">${icon('search')}<input id="${id}" class="pk-input pk-input--sm" type="search" autocomplete="off"
        placeholder="Add a profile: name, provider or model" aria-label="Add a profile to the ${label.toLowerCase()}"
        data-picker="${key}" data-key="picker:${key}"></label>
      <div class="pk-menu ae-picker-list" role="listbox" aria-label="Matching profiles" hidden></div>
    </div>` }, { help, group: true });
}

function chainRow(key, name, index, count) {
  const info = profileOf(name);
  const button = (act, iconName, label, off) => html`<button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm"
    data-chain="${act}" data-index="${index}" data-key="chain:${key}:${name}:${act}" ${disabledIf(off)}
    title="${label}" aria-label="${label}">${icon(iconName, { size: 'sm' })}</button>`;
  return html`<li class="ae-chain-row" data-profile="${name}">
    ${badge(index === 0 ? 'accent' : '', index === 0 ? '#1 Primary' : `Fallback ${index}`)}
    <span class="pk-grow ae-chain-name"><span class="pk-mono">${name}</span>
      ${info ? html`<span class="pk-muted">${info.provider} · ${info.model}</span>` : badge('danger', 'unknown profile')}</span>
    ${button('up', 'arrow-up', `Move ${name} up`, index === 0)}
    ${button('down', 'arrow-down', `Move ${name} down`, index === count - 1)}
    ${button('remove', 'x', `Remove ${name}`, false)}
  </li>`;
}

function editChain(path, edit) {
  const chain = [...chainOf(shown(path))];
  edit(chain);
  // an empty primary chain is refused by the loader: the inherited one applies instead
  if (!chain.length && (path.at(-1) === 'llm_profile' || !chainOf(inherited(path)).length)) unsetPath(own, path);
  else setPath(own, path, chain);
  keepFocus(drawModel);
  changed();
}

function chainAction(button) {
  const path = JSON.parse(button.closest('[data-field]').dataset.field);
  const index = Number(button.dataset.index);
  const act = button.dataset.chain;
  editChain(path, (chain) => {
    if (act === 'remove') {
      chain.splice(index, 1);
    } else {
      const other = act === 'up' ? index - 1 : index + 1;
      [chain[index], chain[other]] = [chain[other], chain[index]];
    }
  });
  if (act !== 'remove') return;
  const wrapper = fieldElement(path);
  const removes = wrapper.querySelectorAll('[data-chain="remove"]');
  focusFirst(removes[index], removes[index - 1], wrapper.querySelector('[data-picker]'));
}

function pickerMatches(input) {
  const chain = chainOf(shown(JSON.parse(input.dataset.picker)));
  const query = input.value.trim().toLowerCase();
  return (meta?.profiles ?? []).filter((one) => !chain.includes(one.name) && (!query
    || [one.name, one.provider, one.model, one.description].some((text) => (text || '').toLowerCase().includes(query))));
}

function drawPicker(input, open = true) {
  const list = input.closest('.ae-picker').querySelector('.ae-picker-list');
  const matches = open ? pickerMatches(input) : [];
  list.hidden = !matches.length;
  render(list, matches.map((one) => html`<button type="button" role="option" class="pk-menu-item" data-add-profile="${one.name}"
    aria-selected="false"><span class="pk-mono">${one.name}</span><span class="pk-muted pk-truncate">${one.provider} · ${one.model}</span></button>`));
}

function addProfile(input, name) {
  if (!name) return;
  const path = JSON.parse(input.dataset.picker);
  editChain(path, (chain) => chain.push(name));
  const again = fieldElement(path)?.querySelector('[data-picker]');
  if (!again) return;
  again.focus();
  drawPicker(again, false);
}

// ----------------------------------------------------------------------- tools

const toolsPath = (list) => ['agent_config', 'tools', list];
const wholeServer = (item, server) => item === server || item === `${server}/*`;

/** A tools list as the form sees it: its mode, the inherited list and the own entries (bare, and "!" removals). */
function listModel(list) {
  const path = toolsPath(list);
  const base = (inherited(path) ?? []).filter((item) => typeof item === 'string');
  const raw = hasPath(own, path) ? (getPath(own, path) ?? []) : null;
  let mode;
  if (raw === null) mode = listModes[list] ?? 'inherited';
  else if (raw.length && raw.every(prefixed)) mode = 'extend';
  else if (raw.some(prefixed)) mode = 'mixed';
  else mode = 'own';
  const strings = (raw ?? []).filter((item) => typeof item === 'string');
  const adds = mode === 'own' ? strings : strings.filter((item) => item.startsWith('+')).map((item) => item.slice(1));
  const removes = mode === 'own' ? [] : strings.filter((item) => item.startsWith('!')).map((item) => item.slice(1));
  return { list, path, mode, base, raw, adds, removes };
}

const treeItems = (entry) => new Set([entry.server, `${entry.server}/*`, ...entry.tools.map((tool) => `${entry.server}/${tool.name}`)]);
const inTree = (item) => catalog.some((entry) => treeItems(entry).has(item));

/**
 * One tool in a list's tree. The server says what grants (or blocks) it; the own entries decide at once what a click
 * changes. Checked by an own entry (the exact one, or the whole server) it can be unchecked here; checked by anything
 * else it is locked, and the title names the patterns.
 */
function toolView(model, server, tool) {
  const path = `${server}/${tool}`;
  const mine = (items) => items.filter((item) => item === path || wholeServer(item, server));
  const ownNow = mine(model.adds);
  const info = effective?.per_tool?.[path] ?? {};
  const sentMine = mine(effective?.sent?.[model.list] ?? []);
  const others = (patterns) => (patterns ?? []).filter((pattern) => !sentMine.includes(pattern));
  if (model.list === 'allowed' && info.blocked_by?.length) {
    return { checked: false, locked: !model.adds.includes(path), own: ownNow.length > 0, title: `Blocked by ${info.blocked_by.join(', ')}` };
  }
  const by = others(model.list === 'allowed' ? info.allowed_by : info.blocked_by);
  if (ownNow.length) return { checked: true, locked: false, own: true, title: '' };
  if (by.length) return { checked: true, locked: true, own: false, title: `${model.list === 'allowed' ? 'Granted' : 'Blocked'} by ${by.join(', ')}` };
  return { checked: false, locked: false, own: false, title: '' };
}

function serverView(model, entry) {
  const views = entry.tools.map((tool) => toolView(model, entry.server, tool.name));
  const count = views.filter((view) => view.checked).length;
  const ownServer = model.adds.some((item) => wholeServer(item, entry.server));
  // an own whole-server entry is "on" even with some tools blocked: a mixed box would only ever check on a click
  let state = 'off';
  if (ownServer || (entry.tools.length && count === entry.tools.length)) state = 'on';
  else if (count) state = 'mixed';
  const locked = state === 'on' && !ownServer && views.every((view) => view.locked);
  return { state, ownServer, locked, title: locked ? views.find((view) => view.title)?.title ?? '' : '' };
}

/** Written back in the list's own form: plain in an own list, "+"/"!" entries (in their order) when extending. */
function writeList(model, adds, removes = model.removes) {
  if (model.mode === 'own') {
    setPath(own, model.path, adds);
    return;
  }
  const raw = model.raw ?? [];
  const kept = raw.filter((item) => (item.startsWith('!') ? removes.includes(item.slice(1)) : adds.includes(item.slice(1))));
  const fresh = [
    ...adds.filter((item) => !raw.includes(`+${item}`)).map((item) => `+${item}`),
    ...removes.filter((item) => !raw.includes(`!${item}`)).map((item) => `!${item}`),
  ];
  const items = [...kept, ...fresh];
  if (items.length) {
    setPath(own, model.path, items);
  } else {
    unsetPath(own, model.path);
    listModes[model.list] = 'extend';
  }
}

function toolsChanged() {
  keepFocus(() => {
    drawToolLists();
    drawManagerRows();
  });
  changed();
}

async function setMode(list, mode) {
  const model = listModel(list);
  const entry = own;
  if (mode === model.mode) return;
  if (mode === 'inherited') {
    unsetPath(own, model.path);
    delete listModes[list];
  } else if (mode === 'own') {
    let items = model.base;
    if (model.mode !== 'inherited') {
      const answer = await currentEffective();
      if (!answer || own !== entry) {
        keepFocus(drawToolLists);  // the radio goes back
        return;
      }
      items = answer[list] ?? [];
    }
    setPath(own, model.path, [...items]);
  } else {
    let items = [];
    if (model.mode === 'own') {
      // what the own list had beyond the inherited one becomes additions, what it dropped removals (removals first)
      items = [
        ...model.base.filter((item) => !model.adds.includes(item)).map((item) => `!${item}`),
        ...model.adds.filter((item) => !model.base.includes(item)).map((item) => `+${item}`),
      ];
    } else if (model.mode === 'mixed') {
      items = model.raw.map((item) => (prefixed(item) || typeof item !== 'string' ? item : `+${item}`));  // most likely a missing "+"
    }
    if (items.length) setPath(own, model.path, items);
    else unsetPath(own, model.path);
    listModes[list] = 'extend';
  }
  toolsChanged();
}

/**
 * The box's new state decides: checked writes the whole server in place of its own single entries, unchecked
 * removes every own entry of the server. What inherited or foreign patterns grant stays (and locked).
 */
function toggleServer(list, server, on) {
  const model = listModel(list);
  const items = treeItems(catalog.find((one) => one.server === server));
  const rest = model.adds.filter((item) => !items.has(item));
  writeList(model, on ? [...rest, `${server}/*`] : rest);
  toolsChanged();
}

function toggleTool(list, server, tool) {
  const model = listModel(list);
  const item = `${server}/${tool}`;
  let adds;
  if (model.adds.includes(item)) {
    adds = model.adds.filter((one) => one !== item);
  } else if (model.adds.some((one) => wholeServer(one, server))) {
    // the whole server was on: its entry goes, the other tools come in one by one
    const others = catalog.find((one) => one.server === server).tools
      .map((one) => `${server}/${one.name}`).filter((one) => one !== item && !model.adds.includes(one));
    adds = [...model.adds.filter((one) => !wholeServer(one, server)), ...others];
  } else {
    adds = [...model.adds, item];
  }
  writeList(model, adds);
  toolsChanged();
}

function addPattern(input) {
  const list = input.dataset.patternAdd;
  const model = listModel(list);
  const error = input.closest('.pk-field').querySelector('[data-pattern-error]');
  const value = input.value.trim();
  if (!value) return;
  let problem = '';
  if (model.mode === 'own' && prefixed(value)) problem = '+ and ! entries belong to “Extend inherited”.';
  else if (value.startsWith('!') ? model.removes.includes(value.slice(1)) : model.adds.includes(value.replace(/^\+/, ''))) problem = 'That entry is listed already.';
  if (problem) {
    error.textContent = problem;
    error.hidden = false;
    return;
  }
  patternDrafts[list] = '';
  if (model.mode === 'extend' && value.startsWith('!')) writeList(model, model.adds, [...model.removes, value.slice(1)]);
  else writeList(model, [...model.adds, value.replace(/^\+/, '')]);
  toolsChanged();
}

function removePattern(button) {
  const { list, patternRemove: value } = button.dataset;
  const index = Number(button.dataset.index);
  const model = listModel(list);
  if (value.startsWith('!')) writeList(model, model.adds, model.removes.filter((item) => item !== value.slice(1)));
  else writeList(model, model.adds.filter((item) => item !== value));
  toolsChanged();
  const section = $(`tools-${list}`);
  const buttons = section.querySelectorAll('[data-pattern-remove]');
  focusFirst(buttons[index], buttons[index - 1], section.querySelector('[data-pattern-add]'));
}

function drawTools() {
  render($('tab-tools'), html`
    <div id="toolSummary" class="pk-card ae-summary" aria-live="polite">${summary()}</div>
    <div>
      <div class="ae-list-bar">
        <div class="pk-tabs" role="tablist" data-pk-tabs id="toolLists" aria-label="Tool lists">
          ${Object.entries(LISTS).map(([list, label]) => html`<button type="button" class="pk-tab" role="tab" id="toolTab-${list}"
            aria-selected="${String(list === toolList)}" aria-controls="tools-${list}" data-tab="${list}">${label}
            <span class="pk-tab-count" data-count-for="${list}"></span></button>`)}
        </div>
        <label class="pk-search">${icon('search')}<input type="search" class="pk-input pk-input--sm" data-tree-search data-key="tree-search"
          placeholder="Search servers and tools" aria-label="Search servers and tools" value="${treeQuery}"></label>
        <select class="pk-select pk-select--sm" data-tree-filter data-key="tree-filter" aria-label="Show servers">
          ${TREE_FILTERS.map(([value, label]) => option(value, treeFilter, label))}</select>
      </div>
      ${Object.keys(LISTS).map((list) => html`<section id="tools-${list}" class="ae-tools-list" role="tabpanel" aria-labelledby="toolTab-${list}"
        data-tools-list="${list}" ${list === toolList ? '' : 'hidden'}></section>`)}
    </div>`);
  initTabs($('tab-tools'));
  treeKept = null;
  drawToolLists();
}

function drawToolLists() {
  if (!$('tools-allowed')) return;
  for (const list of Object.keys(LISTS)) {
    render($(`tools-${list}`), listSection(listModel(list)));
    const count = effective?.[list];
    document.querySelector(`[data-count-for="${list}"]`).textContent = Array.isArray(count) ? String(count.length) : '';
  }
  $('tab-tools').querySelectorAll('input[data-mixed]').forEach((box) => { box.indeterminate = true; });
}

function listSection(model) {
  const { list, mode } = model;
  const fixed = mode === 'inherited' || mode === 'mixed';
  const chips = [
    ...model.adds.filter((item) => !inTree(item)).map((item) => chip(list, item, 'accent')),
    ...model.removes.map((item) => chip(list, `!${item}`, 'danger')),
  ].map((markup, index) => markup(index, fixed));
  const locked = mode === 'own' ? [] : model.base
    .map((item) => html`<span class="pk-badge ae-chip ae-chip--locked" title="inherited"><span class="pk-mono">${item}</span></span>`);
  return html`<fieldset class="ae-form" ${disabledIf(readOnly())}>
    <div class="pk-stack">
      <div class="pk-row ae-source" role="radiogroup" aria-label="Where the ${list} list comes from">
        <span class="pk-label">Source</span>
        ${MODES.map(([value, label]) => html`<label class="pk-radio"><input type="radio" name="mode-${list}" value="${value}"
          data-list-mode="${list}" data-key="mode:${list}:${value}" ${mode === value ? 'checked' : ''}> ${label}</label>`)}
      </div>
      ${mode === 'mixed' ? html`<p class="pk-error">This list mixes + and ! entries with plain ones, which the loader refuses. Pick a source, or fix it in the YAML tab.</p>`
    : html`<p class="pk-help">${MODE_HELP[mode]}${mode === 'inherited' ? ' Pick another source to change it.' : ''}</p>`}
      ${mode === 'own' ? '' : html`<div class="pk-row ae-source"><span class="pk-label">Inherited</span>
        <div class="ae-chips">${locked.length ? locked : html`<span class="pk-muted">Nothing.</span>`}</div></div>`}
    </div>
    ${catalogError ? html`<p class="pk-error">The tool catalogue could not be loaded: ${catalogError}</p>` : toolTree(model, fixed)}
    <div class="pk-field">
      <span class="pk-label">Patterns beyond the tree</span>
      <div class="ae-chips">
        ${chips.length ? chips : html`<span class="pk-muted">None.</span>`}
        <input class="pk-input pk-input--sm ae-chip-input" data-pattern-add="${list}" data-key="pattern:${list}" autocomplete="off"
          value="${patternDrafts[list] ?? ''}"
          placeholder="${mode === 'extend' ? 'Add a pattern (!x removes), Enter' : 'Add a pattern, e.g. web_*, Enter'}"
          aria-label="Add a ${list} pattern" ${disabledIf(fixed)}>
      </div>
      <span class="pk-error" data-pattern-error hidden></span>
      <details class="ae-hint"><summary>How patterns match</summary><p class="pk-help">${PATTERN_HELP}</p></details>
    </div>
  </fieldset>`;
}

function chip(list, value, kind) {
  return (index, off) => html`<span class="pk-badge pk-badge--${kind} ae-chip"><span class="pk-mono">${value}</span>
    <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-pattern-remove="${value}" data-list="${list}"
      data-index="${index}" data-key="chip:${list}:${value}" aria-label="Remove ${value}" title="Remove" ${disabledIf(off)}>${icon('x', { size: 'sm' })}</button></span>`;
}

/** The servers the search and the filter leave; "in this list" is a server with at least one checked tool. */
function toolTree(model, fixed) {
  const query = treeQuery.trim().toLowerCase();
  const hit = (tool) => `${tool.name} ${tool.description || ''}`.toLowerCase().includes(query);
  const serverHit = (entry) => `${entry.server} ${entry.type || ''}`.toLowerCase().includes(query);
  const views = new Map(catalog.map((entry) => [entry.server, serverView(model, entry)]));
  treeKept ??= { settled: effective?.key === canonical(effectiveBody()), lists: {} };
  treeKept.lists[model.list] ??= new Set(catalog
    .filter((entry) => (views.get(entry.server).state !== 'off') === (treeFilter === 'in')).map((entry) => entry.server));
  const kept = treeKept.lists[model.list];
  const entries = catalog.map((entry) => ({
    entry,
    view: views.get(entry.server),
    tools: !query || serverHit(entry) ? entry.tools : entry.tools.filter(hit),
  })).filter(({ entry, tools }) => (!query || serverHit(entry) || tools.length)
    && (treeFilter === 'all' || kept.has(entry.server)));
  if (!catalog.length) return html`<p class="pk-muted">No tool servers are running.</p>`;
  const count = entries.length === catalog.length ? plural(catalog.length, 'server') : `${entries.length} of ${plural(catalog.length, 'server')}`;
  return html`<div class="pk-stack ae-tree-box">
    <span class="pk-help" data-tree-shown="${entries.length}">${count}</span>
    ${entries.length ? html`<ul class="ae-tree" aria-label="${LISTS[model.list]} tools">
      ${entries.map(({ entry, view, tools }) => serverNode(model, entry, view, tools, fixed, Boolean(query)))}</ul>`
    : html`<p class="pk-muted">No server matches.</p>`}
  </div>`;
}

function serverNode(model, entry, view, tools, fixed, searching) {
  const { server } = entry;
  const open = entry.tools.length > 0 && (searching || expanded.has(`${model.list}:${server}`));
  const key = `tree:${model.list}:${server}`;
  return html`<li>
    <div class="ae-tree-row">
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-expand="${server}" data-key="${key}:open"
        aria-expanded="${String(open)}" aria-label="Tools of ${server}" ${disabledIf(!entry.tools.length || searching)}>${icon(open ? 'chevron-down' : 'chevron-right', { size: 'sm' })}</button>
      <label class="pk-check" title="${view.title}"><input type="checkbox" data-tree-server="${server}" data-key="${key}"
        ${view.state === 'on' ? 'checked' : ''} ${view.state === 'mixed' ? 'data-mixed' : ''} ${disabledIf(fixed || view.locked)}>
        <span class="pk-mono">${server}</span></label>
      <span class="pk-muted ae-tree-note">${entry.type || 'server'} · ${plural(entry.tools.length, 'tool')}</span>
    </div>
    ${open ? html`<ul class="ae-tree-tools">${tools.map((tool) => {
    const one = toolView(model, server, tool.name);
    return html`<li class="ae-tree-row">
        <label class="pk-check" title="${one.title}"><input type="checkbox" data-tree-tool="${tool.name}" data-server="${server}"
          data-key="${key}/${tool.name}" ${one.checked ? 'checked' : ''} ${disabledIf(fixed || one.locked)}>
          <span class="pk-mono">${tool.name}</span></label>
        ${one.locked ? html`<span class="pk-badge" data-lock>${one.title}</span>` : ''}
        <span class="pk-muted pk-truncate ae-tree-note" title="${tool.description || ''}">${tool.description || ''}</span>
      </li>`;
  })}</ul>` : ''}
  </li>`;
}

function summary() {
  if (effectiveError) return html`<span class="pk-error">${effectiveError}</span>`;
  if (!effective) return html`<span class="pk-spinner" role="status" aria-label="Counting tools"></span>`;
  const counts = Object.entries(effective.counts ?? {});
  const unmatched = effective.unmatched ?? [];
  const extra = unmatched.filter((pattern) => !Object.hasOwn(effective.counts ?? {}, pattern));
  const count = effective.tools?.length ?? 0;
  return html`<span><strong data-tool-count="${count}">${plural(count, 'tool')}</strong>
      <span class="pk-muted">${count === 1 ? 'reaches' : 'reach'} this agent</span></span>
    ${effective.allowed?.length ? '' : html`<span class="pk-badge pk-badge--warn">${icon('triangle-alert', { size: 'sm' })} No tools at all: the allowed list is empty</span>`}
    ${counts.length || extra.length ? html`<ul class="ae-counts" aria-label="Tools per allowed pattern">
      ${counts.map(([pattern, n]) => html`<li data-pattern="${pattern}" title="${plural(n, 'tool')}"><span class="pk-mono">${pattern}</span> <span class="pk-muted">${n}</span>
        ${unmatched.includes(pattern) ? badge('warn', 'matches nothing') : ''}</li>`)}
      ${extra.map((pattern) => html`<li data-pattern="${pattern}"><span class="pk-mono">${pattern}</span> ${badge('warn', 'matches nothing')}</li>`)}
    </ul>` : ''}`;
}

/** The tools tab's count: what reaches the agent, blank while unknown. */
function drawToolCount() {
  $('toolTabCount').textContent = effective ? String(effective.tools?.length ?? 0) : '';
}

function effectiveBody() {
  const body = {
    allowed: hasPath(own, toolsPath('allowed')) ? (getPath(own, toolsPath('allowed')) ?? []) : null,
    blocked: hasPath(own, toolsPath('blocked')) ? (getPath(own, toolsPath('blocked')) ?? []) : null,
  };
  if (!draft) body.name = detail.name;
  if (draft || own.type !== detail.own?.type) body.type = own.type ?? null;
  return body;
}

function scheduleEffective(now = false) {
  if (own === null) return;
  const body = effectiveBody();
  const key = canonical(body);
  if (key === effective?.key || key === effectivePending) return;
  effectivePending = key;
  clearTimeout(effectiveTimer);
  effectiveTimer = setTimeout(loadEffective, now ? 0 : 300);
}

/** The answer for the lists as they are now; null when it could not be had. */
async function loadEffective() {
  clearTimeout(effectiveTimer);
  if (own === null) return null;
  const body = effectiveBody();
  const key = canonical(body);
  effectivePending = key;
  const sent = { allowed: listModel('allowed').adds, blocked: listModel('blocked').adds };
  const { current, value, error } = await newest('effective', `${BASE}tools/effective`, { method: 'POST', json: body });
  // overtaken, or asked for lists the form no longer has (another agent meanwhile): the next answer counts
  if (!current || own === null || canonical(effectiveBody()) !== key) {
    if (current && effectivePending === key) effectivePending = null;  // coming back to these lists asks again
    return null;
  }
  effectivePending = null;
  if (treeKept && !treeKept.settled) treeKept = null;  // worked out without knowing the inherited grants
  if (error) {
    effective = null;
    effectiveError = `The effective tools are unknown: ${error.message}`;
  } else {
    effective = { ...value, key, sent };
    effectiveError = '';
  }
  if ($('toolSummary')) render($('toolSummary'), summary());
  drawToolCount();
  keepFocus(() => {
    drawToolLists();
    drawManagerRows();
  });
  return effective;
}

async function currentEffective() {
  if (effective && effective.key === canonical(effectiveBody())) return effective;
  const answer = await loadEffective();
  if (!answer) toast(effectiveError || 'The effective tools are not known yet: try again', { kind: 'warn' });
  return answer;
}

// ---------------------------------------------------------------------- prompt

/** The merge skips a null system_prompt: only a string of its own replaces the parent's. */
function derivedPromptMode() {
  const mine = ownValue(SYSTEM_PROMPT);
  return (typeof mine === 'string' ? mine : inherited(SYSTEM_PROMPT)) ? 'inline' : 'template';
}

function drawPrompt() {
  promptChoice ??= derivedPromptMode();
  skillKept = null;
  const mode = promptChoice;
  const parentPrompt = Boolean(inherited(SYSTEM_PROMPT));
  const radio = (value, label) => html`<label class="pk-radio"><input type="radio" name="promptMode" value="${value}"
    data-prompt-mode data-key="prompt-mode:${value}" ${mode === value ? 'checked' : ''}> ${label}</label>`;
  render($('tab-prompt'), html`<fieldset class="ae-form" ${disabledIf(readOnly())}>
    <section class="ae-section" aria-labelledby="promptTitle">
      <h3 class="ae-section-title" id="promptTitle">System prompt</h3>
      <div class="pk-row ae-source" role="radiogroup" aria-label="Where the system prompt comes from">
        <span class="pk-label">Source</span>
        ${radio('template', 'Template file')}${radio('inline', 'Inline prompt')}
      </div>
      ${mode === 'template' ? html`
        ${parentPrompt ? html`<p class="pk-help">The parent has an inline prompt: an empty one of this agent’s own lets the file apply.</p>` : ''}
        ${field(SYSTEM_TEMPLATE, 'Template file', input(SYSTEM_TEMPLATE, { mono: true, list: 'promptFiles' }),
          { help: './ and ../ are relative to the agent’s file, anything else to the repository root.' })}
        <datalist id="promptFiles">${(meta?.prompt_files ?? []).map((file) => html`<option value="${file}"></option>`)}</datalist>
        <div class="pk-field">
          <div class="pk-row"><span class="pk-label">Preview</span><span id="promptState"></span></div>
          <pre class="pk-code ae-preview" id="promptPreview" aria-label="Template preview"></pre>
        </div>`
      : field(SYSTEM_PROMPT, 'Inline prompt', textarea(SYSTEM_PROMPT, { rows: 14, mono: true, keepEmpty: !parentPrompt }),
        { help: parentPrompt ? 'A Jinja template; left empty, the parent’s prompt applies.' : 'A Jinja template. It wins over a template file.' })}
      ${yamlField(['agent_config', 'template_vars'], 'Template variables', 'YAML mapping, available in the prompt as Jinja variables.')}
    </section>
  </fieldset>
  <section class="ae-section" aria-labelledby="skillsTitle">
    <h3 class="ae-section-title" id="skillsTitle">Skills</h3>
    <div class="pk-row ae-filters">
      <label class="pk-search pk-grow">${icon('search')}<input type="search" class="pk-input pk-input--sm" data-skill-search
        data-key="skill-search" value="${skillQuery}" placeholder="Search skills" aria-label="Search skills"></label>
      <select class="pk-select pk-select--sm" data-skill-filter data-key="skill-filter" aria-label="Show skills">
        ${SKILL_FILTERS.map(([value, label]) => option(value, skillFilter, label))}</select>
    </div>
    <fieldset class="ae-form ae-skills" id="skillLists" ${disabledIf(readOnly())}>${skillLists()}</fieldset>
  </section>`);
  fillYaml($('tab-prompt'));
  if (mode === 'template') loadPreview();
}

function setPromptMode(mode) {
  promptChoice = mode;
  const parentPrompt = inherited(SYSTEM_PROMPT);
  const mine = ownValue(SYSTEM_PROMPT);
  if (mode === 'template') {
    if (typeof mine === 'string' && mine) inlineStash = mine;
    // null would be skipped by the merge and the parent's prompt would keep winning
    if (parentPrompt) setPath(own, SYSTEM_PROMPT, '');
    else unsetPath(own, SYSTEM_PROMPT);
  } else if (inlineStash !== null) {
    setPath(own, SYSTEM_PROMPT, inlineStash);
    inlineStash = null;
  } else if (!(typeof mine === 'string' && mine)) {
    if (parentPrompt) unsetPath(own, SYSTEM_PROMPT);
    else setPath(own, SYSTEM_PROMPT, '');
  }
  keepFocus(drawPrompt);
  changed();
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(loadPreview, 300);
}

async function loadPreview() {
  if (!$('promptPreview')) return;
  const path = shown(SYSTEM_TEMPLATE);
  if (!path) {
    render($('promptPreview'), '');
    render($('promptState'), badge('', 'no template file'));
    return;
  }
  const query = new URLSearchParams({ path });
  if (!draft) query.set('agent', detail.name);
  const { current, value, error } = await newest('prompt', `${BASE}prompt?${query}`);
  if (!current || !$('promptPreview')) return;
  render($('promptPreview'), error ? '' : value.text ?? '');
  render($('promptState'), error ? badge('danger', error.message) : value.exists ? badge('ok', 'found') : badge('warn', 'missing'));
}

const skillsOf = (value) => (Array.isArray(value)
  ? { always: value, on_demand: [] } : { always: value?.always ?? [], on_demand: value?.on_demand ?? [] });

/** Whether this agent sets the list itself. A bare own list sets "always" only; "on demand" stays inherited. */
function ownsSkillList(kind) {
  return Array.isArray(ownValue(SKILLS)) ? kind === 'always' : hasPath(own, [...SKILLS, kind]);
}

function skillList(kind) {
  if (!ownsSkillList(kind)) return skillsOf(inherited(SKILLS))[kind];
  const mine = ownValue(SKILLS);
  return Array.isArray(mine) ? mine : (getPath(own, [...SKILLS, kind]) ?? []);
}

function skillLists() {
  const query = skillQuery.trim().toLowerCase();
  const known = meta?.skills ?? [];
  const searched = known.filter((one) => !query || `${one.name} ${one.description || ''}`.toLowerCase().includes(query));
  skillKept ??= {};
  return [['always', 'Always', 'In the prompt from the start.'], ['on_demand', 'On demand', 'Only listed; the agent loads them with the skills tools.']]
    .map(([kind, label, help]) => {
      const list = skillList(kind);
      const locked = list.some(prefixed);
      const id = nextId();
      skillKept[kind] ??= new Set(known.filter((one) => list.includes(one.name) === (skillFilter === 'on')).map((one) => one.name));
      const matching = searched.filter((one) => skillFilter === 'all' || skillKept[kind].has(one.name));
      const count = matching.length === known.length ? plural(known.length, 'skill') : `${matching.length} of ${plural(known.length, 'skill')}`;
      return field([...SKILLS, kind], label, { id, markup: html`<div class="ae-checklist" role="group" aria-label="${label} skills">
        ${known.length ? html`<span class="pk-help" data-skills-shown="${kind}">${count}</span>` : ''}
        ${matching.length ? matching.map((one) => html`<label class="pk-check ae-check-row"><input type="checkbox" data-skill="${one.name}"
          data-kind="${kind}" data-key="skill:${kind}:${one.name}" ${list.includes(one.name) ? 'checked' : ''} ${disabledIf(locked)}>
          <span class="pk-mono">${one.name}</span><span class="pk-muted pk-truncate">${one.description || ''}</span></label>`)
    : html`<span class="pk-muted">${known.length ? 'No skill matches.' : 'No skills found.'}</span>`}
        ${list.filter((name) => !prefixed(name) && !known.some((one) => one.name === name)).map((name) => badge('warn', `${name}: unknown`))}
      </div>` }, { help: locked ? 'This list uses + or ! entries: edit it in the YAML tab.' : help, group: true,
        set: ownsSkillList(kind) });
    });
}

/** A bare skills list spelled out before one of the two lists changes, with the same meaning: "always" only. */
function spellOutSkills() {
  const mine = ownValue(SKILLS);
  if (Array.isArray(mine)) setPath(own, SKILLS, { always: mine });
}

function toggleSkill(box) {
  const { kind, skill } = box.dataset;
  const list = skillList(kind).filter((name) => name !== skill);
  if (box.checked) list.push(skill);
  spellOutSkills();
  setPath(own, [...SKILLS, kind], list);
  keepFocus(() => render($('skillLists'), skillLists()));
  changed();
}

// ----------------------------------------------------------------------- hooks

/** Whether the hook runs for this agent: its own or inherited override, else the hook's default; all off with the switch. */
function hookOn(hook) {
  if ((shown([...HOOKS, 'enabled']) ?? true) === false) return false;
  const value = shown([...HOOKS, 'overrides', hook.name, 'enabled']);
  return typeof value === 'boolean' ? value : Boolean(hook.enabled);
}

const HOOK_KEEP = {
  all: () => true,
  set: (hook) => hasPath(own, [...HOOKS, 'overrides', hook.name]),
  on: hookOn,
  off: (hook) => !hookOn(hook),
};

function drawHooks() {
  const hooks = meta?.hooks ?? [];
  const types = [...new Set(hooks.flatMap((hook) => hook.types))].sort();
  // the filters work on a read-only agent as well: outside the fieldsets that lock it
  render($('tab-hooks'), html`<fieldset class="ae-form" ${disabledIf(readOnly())}>
      ${field([...HOOKS, 'enabled'], 'Hooks', toggle([...HOOKS, 'enabled'], 'Run lifecycle hooks for this agent', true))}
    </fieldset>
    <div class="pk-stack">
      ${hooks.length ? html`<div class="pk-row ae-filters">
        <label class="pk-search pk-grow">${icon('search')}<input type="search" class="pk-input pk-input--sm" data-hook-search data-key="hook-search"
          value="${hookQuery}" placeholder="Search hooks" aria-label="Search hooks"></label>
        <select class="pk-select pk-select--sm" data-hook-type data-key="hook-type" aria-label="Hook type">
          ${option('', hookType, 'All types')}${types.map((type) => option(type, hookType))}</select>
        <select class="pk-select pk-select--sm" data-hook-filter data-key="hook-filter" aria-label="Show hooks">
          ${HOOK_FILTERS.map(([value, label]) => option(value, hookFilter, label))}</select>
      </div>` : ''}
      <fieldset id="hookTable" class="ae-form ae-plain" ${disabledIf(readOnly())}></fieldset>
    </div>`);
  drawHookTable();
}

function drawHookTable() {
  const hooks = meta?.hooks ?? [];
  const query = hookQuery.trim().toLowerCase();
  const keep = HOOK_KEEP[hookFilter] ?? HOOK_KEEP.all;
  const matching = hooks.filter((hook) => (!hookType || hook.types.includes(hookType)) && keep(hook)
    && (!query || `${hook.name} ${hook.types.join(' ')} ${hook.description || ''}`.toLowerCase().includes(query)));
  const overrides = shown([...HOOKS, 'overrides']) ?? {};
  const unknown = Object.keys(overrides).filter((name) => !hooks.some((hook) => hook.name === name));
  const count = matching.length === hooks.length ? plural(hooks.length, 'hook') : `${matching.length} of ${plural(hooks.length, 'hook')}`;
  render($('hookTable'), html`
    ${!hooks.length ? empty('plug', 'No hooks registered', 'The running app has no lifecycle hooks.')
    : html`<span class="pk-help" data-hooks-shown="${matching.length}">${count}</span>
      ${matching.length ? html`<div class="pk-table-wrap"><table class="pk-table ae-hooks">
        <thead><tr><th>Hook</th><th>Default</th><th>For this agent</th><th><span class="pk-sr-only">Custom settings</span></th></tr></thead>
        <tbody>${matching.map(hookRow)}</tbody>
      </table></div>` : html`<p class="pk-muted">No hook matches.</p>`}`}
    ${unknown.length ? html`<p class="pk-help">Overrides for hooks the app does not know: ${unknown.join(', ')}. Edit them in the YAML tab.</p>` : ''}`);
  fillYaml($('hookTable'));
}

function hookRow(hook) {
  const base = [...HOOKS, 'overrides', hook.name];
  const mine = getPath(own, base) ?? {};
  const theirs = inherited(base) ?? {};
  const state = Object.hasOwn(mine, 'enabled') ? String(mine.enabled) : '';
  const fallback = Object.hasOwn(theirs, 'enabled')
    ? `${theirs.enabled ? 'On' : 'Off'} (inherited)` : `Default (${hook.enabled ? 'on' : 'off'})`;
  const open = openHooks.has(hook.name);
  const key = `hook:${hook.name}`;
  return html`<tr>
    <td><div class="pk-mono">${hook.name}</div>
      <div class="pk-row">${hook.types.map((type) => badge('', type))}<span class="pk-muted">${hook.description || ''}</span></div></td>
    <td>${hook.enabled ? badge('ok', 'on') : badge('', 'off')}</td>
    <td><select class="pk-select pk-select--sm" data-hook-enabled="${hook.name}" data-key="${key}:enabled" aria-label="${hook.name} for this agent">
      ${option('', state, fallback)}${option('true', state, 'On')}${option('false', state, 'Off')}</select></td>
    <td><button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-hook-open="${hook.name}" data-key="${key}:open"
      aria-expanded="${String(open)}" title="Custom settings" aria-label="Custom settings of ${hook.name}">${icon('settings', { size: 'sm' })}</button></td>
  </tr>
  ${open ? html`<tr><td colspan="4">${yamlField(base, `Custom settings of ${hook.name}`, '', { hook: hook.name })}
    <span class="pk-help">YAML mapping of the hook’s own settings for this agent.</span></td></tr>` : ''}`;
}

function setHookEnabled(name, value) {
  const path = [...HOOKS, 'overrides', name, 'enabled'];
  if (value === undefined) unsetPath(own, path);
  else setPath(own, path, value);
  changed();
}

function setHookCustom(name, custom) {
  const base = [...HOOKS, 'overrides', name];
  const mine = getPath(own, base) ?? {};
  const next = { ...(Object.hasOwn(mine, 'enabled') ? { enabled: mine.enabled } : {}), ...custom };
  if (Object.keys(next).length) setPath(own, base, next);
  else unsetPath(own, base);
}

// ------------------------------------------------------------------------ yaml

function drawYaml() {
  render($('tab-yaml'), html`<fieldset class="ae-form" ${disabledIf(readOnly())}>
    <div class="pk-field">
      <div class="pk-row">
        <label class="pk-label pk-grow" for="yamlText">The entry as it is written to the file</label>
        <button type="button" class="pk-btn pk-btn--sm" id="yamlReload">${icon('refresh-cw', { size: 'sm' })} Show the form’s entry</button>
        <button type="button" class="pk-btn pk-btn--primary pk-btn--sm" id="yamlApply">${icon('check', { size: 'sm' })} Apply to form</button>
      </div>
      <span class="pk-help">Leaving this tab applies what was typed. Settings without a form control (timeouts, loop detection,
        escalation, self_tool_descriptions, plugin settings) are edited here.</span>
      ${codeBox(html`<textarea id="yamlText" class="pk-textarea pk-input--mono ae-yaml-full" rows="24" spellcheck="false" wrap="off"
        data-key="yaml-text"></textarea>`)}
      <p class="pk-error" id="yamlError" hidden></p>
    </div>
  </fieldset>`);
  if (yamlDraft !== null && yamlProblem) showYamlError(yamlProblem);
  refreshYaml();
}

/** The entry as YAML; typed text is never overwritten, except by "Show the form's entry" (force). */
async function refreshYaml(force = false) {
  const area = $('yamlText');
  if (!area || own === null) return;
  if (force) {
    yamlDraft = null;
    showYamlError('');
    updateHead();
  }
  if (yamlDraft !== null) {
    setCode(area, yamlDraft);
    return;
  }
  const key = canonical(own);
  if (key === yamlFor && yamlRendered !== null) {
    setCode(area, yamlRendered);
    return;
  }
  setCode(area, '');
  const { current, value, error } = await newest('yaml', `${BASE}yaml`, { method: 'POST', json: { entry: own } });
  if (!current || yamlDraft !== null || own === null || !$('yamlText')) return;
  if (canonical(own) !== key) {
    refreshYaml();  // the entry changed while this one was rendered: render the new one
    return;
  }
  if (error) {
    showYamlError(error.message);
    return;
  }
  yamlRendered = value.yaml;
  yamlFor = key;
  setCode($('yamlText'), value.yaml);
}

function typedYaml(area) {
  yamlDraft = yamlRendered !== null && yamlFor === canonical(own) && area.value === yamlRendered ? null : area.value;
  updateHead();
}

function showYamlError(text) {
  if (yamlDraft !== null || !text) yamlProblem = text;
  $('yamlError').textContent = text;
  $('yamlError').hidden = !text;
  if (text) $('yamlText').setAttribute('aria-invalid', 'true');
  else $('yamlText').removeAttribute('aria-invalid');
}

/** The typed text becomes the entry. False when it does not parse: the error shows beneath it. */
async function applyYaml() {
  if (yamlDraft === null) return true;
  const text = yamlDraft;
  const { current, value, error } = await newest('yamlApply', `${BASE}yaml/parse`, { method: 'POST', json: { yaml: text } });
  if (!current || yamlDraft !== text) return false;  // typed on meanwhile: that text is the one to apply
  const problem = error ? error.message : isMapping(value.entry) ? '' : 'The entry must be a mapping of keys';
  if (problem) {
    yamlProblem = problem;
    if ($('yamlText')) showYamlError(problem);
    return false;
  }
  const typeChanged = value.entry.type !== own?.type;
  own = value.entry;
  yamlDraft = null;
  yamlProblem = '';
  inlineStash = null;
  yamlRendered = null;
  yamlFor = null;
  listModes = {};
  promptChoice = null;
  drawEditor();
  if (typeChanged) refreshInherited();
  return true;
}

/** Leaving the YAML tab with typed text applies it first; text that does not parse keeps the tab open. */
function guardYamlTab(event) {
  if (yamlDraft === null || leavingYaml) return;
  const tabs = [...$('tabs').querySelectorAll('[role="tab"]')];
  let next = null;
  if (event.type === 'click') {
    next = event.target.closest('[role="tab"]');
  } else {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    const index = tabs.indexOf(event.target.closest('[role="tab"]'));
    if (step && index >= 0) next = tabs[(index + step + tabs.length) % tabs.length];
  }
  if (!next || next.dataset.tab === 'yaml') return;
  event.stopPropagation();
  event.preventDefault();
  applyYaml().then((ok) => {
    if (!ok) {
      $('yamlText')?.focus();
      return;
    }
    leavingYaml = true;
    next.click();
    leavingYaml = false;
    next.focus();
  });
}

// --------------------------------------------------------------------- actions

function showDiff({ title, diff, confirmLabel, danger = false, notes = [] }) {
  const box = $('diffDialog');
  $('diffTitle').textContent = title;
  render($('diffNotes'), notes.map((note) => html`<li>${note}</li>`));
  $('diffNotes').hidden = !notes.length;
  render($('diffBody'), diff.replace(/\n$/, '').split('\n').map((line) => {
    const kind = /^(\+\+\+|---)/.test(line) ? 'file' : line.startsWith('@@') ? 'hunk'
      : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : '';
    return html`<span class="ae-diff-line${kind ? ` ae-diff-${kind}` : ''}">${line || ' '}</span>`;
  }));
  $('diffWrite').textContent = confirmLabel;
  $('diffWrite').className = `pk-btn ${danger ? 'pk-btn--danger' : 'pk-btn--primary'}`;
  box.returnValue = '';
  box.showModal();
  // Enter confirms what is safe: a destructive write starts on Cancel
  (danger ? $('diffCancel') : $('diffWrite')).focus();
  return new Promise((resolve) => {
    box.addEventListener('close', () => resolve(box.returnValue === 'write'), { once: true });
  });
}

async function save() {
  if (busy || own === null || readOnly()) return;
  setBusy(true);
  let problem = null;
  try {
    problem = await settleYaml();
    if (problem) return;
    const creating = Boolean(draft);
    const name = currentName();
    const url = creating ? `${BASE}agents` : `${BASE}agents/${encodeURIComponent(name)}`;
    const method = creating ? 'POST' : 'PUT';
    const body = (dryRun) => (creating
      ? { name, entry: own, ...(draft.source ? { source: draft.source } : {}), dry_run: dryRun }
      : { entry: own, version: detail.version, dry_run: dryRun });
    const preview = await api(url, { method, json: body(true), quiet: true });
    if (!preview.diff) {
      toast('Nothing to write: the file says this already', { kind: 'info' });
      return;
    }
    const title = creating ? `Create ${name}` : `Save ${name}`;
    const where = creating ? `Creates ${preview.file}.` : `Changes ${detail.file}.`;
    if (!(await showDiff({ title, diff: preview.diff, confirmLabel: creating ? 'Create file' : 'Write file', notes: [where] }))) return;
    const answer = await api(url, { method, json: body(false), quiet: true });
    toast(creating ? `${name} created in ${answer.file ?? preview.file}` : `${name} saved to ${detail.file}`, { kind: 'ok' });
    draft = null;
    selected = name;
    loaded = canonical(own);  // until the reload lands, the form counts as saved
    // a manager in the same file has a new version now
    await Promise.all([loadList(), loadDetail({ force: true }), loadManagers()]);
  } catch (error) {
    await failed(error);
  } finally {
    setBusy(false);
    if (problem) reveal(problem);
  }
}

function revert() {
  own = clone(pristine.own);
  detail.inherited = clone(pristine.inherited);
  resetForm();
  drawEditor();
}

async function removeAgent() {
  if (!(await managerMayGo()) || !detail || busy) return;
  const { name, file, version } = detail;
  const url = `${BASE}agents/${encodeURIComponent(name)}`;
  setBusy(true);
  try {
    const preview = await api(url, { method: 'DELETE', json: { version, dry_run: true }, quiet: true });
    const notes = [
      preview.deleted_file ? `${file} defines nothing else and is deleted with it.` : `Removes the entry from ${file}; the rest of the file stays.`,
      ...(preview.notes ?? []),
    ];
    if (!(await showDiff({ title: `Delete ${name}`, diff: preview.diff, confirmLabel: 'Delete', danger: true, notes }))) return;
    const answer = await api(url, { method: 'DELETE', json: { version, dry_run: false }, quiet: true });
    const extra = answer.notes ?? [];
    toast([`${name} deleted`, answer.deleted_file ? `${file} removed` : '', ...extra].filter(Boolean).join('. '),
      { kind: extra.length ? 'warn' : 'ok' });
    if (selected === name) {
      selected = null;
      detail = null;
      own = null;
      managerEdit = null;
      resetForm();
      drawEditor();
    }
    await Promise.all([loadList(), loadManagers()]);
  } catch (error) {
    await failed(error);
  } finally {
    setBusy(false);
  }
}

function openCreate(start = 'blank', source = '') {
  const form = $('createForm');
  form.reset();
  $('createError').hidden = true;
  const agents = (rows ?? []).filter((row) => row.file);
  render(form.elements.source, agents.map((row) => option(row.name, source)));
  form.elements.start.value = start;
  if (source) form.elements.name.value = `${source}_${start === 'copy' ? 'copy' : 'child'}`;
  syncCreate();
  $('createDialog').showModal();
}

function syncCreate() {
  const fields = $('createForm').elements;
  fields.source.disabled = fields.start.value === 'blank';
}

function createError(text) {
  $('createError').textContent = text;
  $('createError').hidden = false;
}

async function submitCreate(event) {
  event.preventDefault();
  const fields = $('createForm').elements;
  const name = fields.name.value.trim();
  const start = fields.start.value;
  const source = fields.source.value;
  if (rows?.some((row) => row.name === name)) {
    createError(`An agent named “${name}” exists already`);
    return;
  }
  if (start !== 'blank' && !source) {
    createError('Pick the agent to start from');
    return;
  }
  $('createSubmit').disabled = true;
  try {
    let entry;
    let values;
    if (start === 'copy') {
      const parent = await api(`${BASE}agents/${encodeURIComponent(source)}`, { quiet: true });
      if (!parent.own) throw new Error(`${source} is not defined on disk`);
      if (parent.form_reason) throw new Error(`${source} cannot be copied here: ${parent.form_reason}`);
      entry = clone(parent.own);
      values = parent.inherited;
    } else {
      entry = { type: start === 'blank' ? 'basic_agent' : source, enabled: true };
      values = await inheritedFor(entry.type);
      if (values === null) return;  // a later question for the inherited values is on its way
    }
    if (!(await leave())) return;
    $('createDialog').close();
    startDraft(name, start === 'copy' ? source : null, entry, values);
  } catch (error) {
    if (!aborted(error)) createError(error.message);
  } finally {
    $('createSubmit').disabled = false;
  }
}

function startDraft(name, source, entry, values) {
  draft = { name, source };
  shownView += 1;
  managerEdit = null;
  selected = null;
  detail = {
    name, file: null, files: [], version: null, editable: true, readonly_reason: null, form_reason: null, own: null, parent: null,
    inherited: values ?? {}, effective: {}, state: 'new', changed: [], reload_fields: [], restart: true,
    children: [], spawnable: [], prompt: null,
  };
  own = entry;
  pristine = { own: clone(entry), inherited: clone(detail.inherited) };
  loaded = canonical(entry);
  resetForm();
  drawList();
  drawEditor();
}

async function reloadConfig() {
  const fields = (meta?.reload_fields ?? []).join(', ');
  reloading = true;
  const buttons = () => [$('reloadConfig'), ...document.querySelectorAll('[data-reload]')];
  buttons().forEach((button) => { button.disabled = true; });
  try {
    if (!(await confirm(`Re-read the config files and apply to the running agents what needs no restart${fields ? ` (${fields})` : ''}? Everything else still needs a restart.`,
      { title: 'Reload config', confirmLabel: 'Reload' }))) return;
    const answer = await api(RELOAD_CONFIG, { method: 'POST' });
    const errors = answer.report?.errors ?? [];
    const count = answer.report?.refreshed?.length ?? 0;
    if (errors.length) {
      toast(`Config reloaded with errors: ${errors.map((one) => `${one.server}: ${one.error}`).join('; ')}`, { kind: 'warn' });
    } else {
      toast(`Config reloaded: ${plural(count, 'server')} refreshed`, { kind: 'ok' });
    }
    await refreshAll();
    // unsaved edits keep the form as it is: only what the running app has is asked for again
    if (dirty()) await refreshLiveState();
  } catch {
    // shown by api()
  } finally {
    reloading = false;
    buttons().forEach((button) => { button.disabled = false; });
  }
}

/** The shown agent's relation to the running app, read again without touching the form. */
async function refreshLiveState() {
  const name = selected;
  if (!name || draft || !detail) return;
  const { current, value, error } = await newest('live', `${BASE}agents/${encodeURIComponent(name)}`);
  if (!current || error || selected !== name || draft || !detail) return;
  for (const key of ['state', 'changed', 'reload_fields', 'restart']) detail[key] = value[key];
  keepFocus(() => {
    drawHead();
    drawBanners();
    updateHead();
  });
}

async function copyPath(button) {
  const text = button.dataset.copy;
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch {
    // a frame without clipboard permission: the selection route still works there
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.className = 'pk-sr-only';
    document.body.append(area);
    area.select();
    try { copied = document.execCommand('copy'); } catch { copied = false; }
    area.remove();
    button.focus();
  }
  toast(copied ? 'Path copied' : 'The clipboard is not available here', { kind: copied ? 'ok' : 'warn' });
}

function changed() {
  updateHead();
  scheduleEffective();
}

// ---------------------------------------------------------------------- wiring

function onBound(control) {
  const path = JSON.parse(control.dataset.path);
  const kind = control.dataset.bind;
  if (kind === 'bool') {
    setPath(own, path, control.checked);
  } else if (kind === 'number') {
    if (control.value === '') unsetPath(own, path);
    else if (Number.isFinite(control.valueAsNumber)) setPath(own, path, control.valueAsNumber);
    else return;
  } else if (control.value === '' && kind === 'text' && control.dataset.keepEmpty === undefined) {
    unsetPath(own, path);
  } else {
    setPath(own, path, control.value);
  }
  syncField(control.closest('[data-field]'));
  const where = path.join('.');
  if (where === 'type') {
    drawHead();
    refreshInherited();
  } else if (where === 'metadata.visibility') {
    $('visibilityHelp').textContent = VISIBILITY[control.value] ?? '';
  } else if (where === SYSTEM_TEMPLATE.join('.')) {
    schedulePreview();
  }
  changed();
}

function resetField(button) {
  const wrapper = button.closest('[data-field]');
  const key = wrapper.dataset.field;
  const path = JSON.parse(key);
  if (path.length > SKILLS.length && path[1] === SKILLS[1]) spellOutSkills();
  unsetPath(own, path);
  yamlFieldDrafts.delete(`yaml:${key}`);  // a reset drops text typed into the field, too
  const isType = path.length === 1 && path[0] === 'type';
  if (isType) drawHead();
  const panel = wrapper.closest('[role="tabpanel"]');
  drawTab(panel.id.replace('tab-', ''));
  panel.querySelector(`[data-field="${CSS.escape(key)}"]`)?.querySelector('input, select, textarea, button:not([data-reset])')?.focus();
  if (isType) refreshInherited();
  changed();
}

const editor = $('editor');

editor.addEventListener('input', (event) => {
  const target = event.target;
  const data = target.dataset;
  paint(target);
  if (data.bind === 'text' || data.bind === 'number') onBound(target);
  else if (data.picker !== undefined) drawPicker(target);
  else if (data.treeSearch !== undefined) {
    treeQuery = target.value;
    treeKept = null;
    drawToolLists();
  } else if (data.skillSearch !== undefined) {
    skillQuery = target.value;
    skillKept = null;
    render($('skillLists'), skillLists());
  } else if (data.managerSetting !== undefined) {
    setManagerValue(target);
  } else if (data.managerSearch !== undefined) {
    managerQuery = target.value;
    managerKept = null;
    keepFocus(drawManagerEditor);
  } else if (data.newManager !== undefined) {
    newManagerName = target.value;
    newManagerError('');
  } else if (data.hookSearch !== undefined) {
    hookQuery = target.value;
    drawHookTable();
  } else if (data.yaml !== undefined) {
    typedYamlField(target);
  } else if (data.patternAdd !== undefined) {
    patternDrafts[data.patternAdd] = target.value;
    target.closest('.pk-field').querySelector('[data-pattern-error]').hidden = true;
  } else if (target.id === 'yamlText') {
    typedYaml(target);
    if (!$('yamlError').hidden) showYamlError('');  // the error was about the text before
  }
});

editor.addEventListener('change', (event) => {
  const target = event.target;
  const data = target.dataset;
  if (data.bind === 'select' || data.bind === 'bool') onBound(target);
  else if (data.listMode !== undefined) setMode(data.listMode, target.value);
  else if (data.treeServer !== undefined) toggleServer(target.closest('[data-tools-list]').dataset.toolsList, data.treeServer, target.checked);
  else if (data.treeTool !== undefined) toggleTool(target.closest('[data-tools-list]').dataset.toolsList, data.server, data.treeTool);
  else if (data.skill !== undefined) toggleSkill(target);
  else if (data.promptMode !== undefined) setPromptMode(target.value);
  else if (data.hookEnabled !== undefined) setHookEnabled(data.hookEnabled, target.value === '' ? undefined : target.value === 'true');
  else if (data.hookType !== undefined) {
    hookType = target.value;
    drawHookTable();
  } else if (data.hookFilter !== undefined) {
    hookFilter = target.value;
    drawHookTable();
  } else if (data.skillFilter !== undefined) {
    skillFilter = target.value;
    skillKept = null;
    render($('skillLists'), skillLists());
  } else if (data.treeFilter !== undefined) {
    treeFilter = target.value;
    treeKept = null;
    keepFocus(drawToolLists);
  }
  else if (data.spawn !== undefined) toggleSpawn(target);
  else if (data.useManager !== undefined) useManager(data.useManager, target.checked);
  else if (data.managerAgent !== undefined) toggleManagerAgent(target);
  else if (data.managerSetting !== undefined && target.tagName === 'SELECT') setManagerValue(target);
  else if (data.managerFilter !== undefined) {
    managerFilter = target.value;
    managerKept = null;
    keepFocus(drawManagerEditor);
  }
});

editor.addEventListener('click', (event) => {
  const target = event.target.closest('button');
  if (!target || target.disabled) return;
  const data = target.dataset;
  if (target.id === 'save') save();
  else if (target.id === 'revert') revert();
  else if (target.id === 'yamlApply') applyYaml();
  else if (target.id === 'yamlReload') refreshYaml(true);
  else if (data.reset !== undefined) resetField(target);
  else if (data.goto) choose(data.goto);
  else if (data.copy) copyPath(target);
  else if (data.menu) {
    if ($('moreMenu').matches(':popover-open')) $('moreMenu').hidePopover();
    if (data.menu === 'delete') removeAgent();
    else openCreate(data.menu === 'duplicate' ? 'copy' : 'inherit', detail.name);
  } else if (data.chain) {
    chainAction(target);
  } else if (data.addProfile) {
    addProfile(target.closest('.ae-picker').querySelector('[data-picker]'), data.addProfile);
  } else if (data.tagRemove !== undefined) {
    removeTag(target);
  } else if (data.expand) {
    const key = `${target.closest('[data-tools-list]').dataset.toolsList}:${data.expand}`;
    if (!expanded.delete(key)) expanded.add(key);
    keepFocus(drawToolLists);
  } else if (data.reload !== undefined) {
    reloadConfig();
  } else if (data.configure) {
    openManager(data.configure);
  } else if (data.pickAgents !== undefined) {
    managerEdit.own.allowed_agents = [];
    managerKept = null;
    keepFocus(drawManagerEditor);
    managerChanged();
  } else if (data.revertManager !== undefined) {
    revertManager();
  } else if (data.saveManager !== undefined) {
    saveManager();
  } else if (data.createManager !== undefined) {
    createManager();
  } else if (data.patternRemove !== undefined) {
    removePattern(target);
  } else if (data.hookOpen) {
    if (!openHooks.delete(data.hookOpen)) openHooks.add(data.hookOpen);
    keepFocus(drawHookTable);
  }
});

editor.addEventListener('keydown', (event) => {
  const target = event.target;
  if (target.dataset.picker !== undefined) {
    if (event.key === 'Enter') {
      event.preventDefault();
      addProfile(target, pickerMatches(target)[0]?.name);
    } else if (event.key === 'Escape') {
      drawPicker(target, false);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      target.closest('.ae-picker').querySelector('[data-add-profile]')?.focus();
    }
  } else if (event.key === 'Enter' && target.dataset.tagAdd !== undefined) {
    event.preventDefault();
    const tag = target.value.trim();
    if (tag && !(shown(['metadata', 'tags']) ?? []).includes(tag)) editTags((tags) => [...tags, tag]);
  } else if (event.key === 'Enter' && target.dataset.newManager !== undefined) {
    event.preventDefault();
    createManager();
  } else if (event.key === 'Enter' && target.dataset.patternAdd !== undefined) {
    event.preventDefault();
    addPattern(target);
  }
});

editor.addEventListener('focusin', (event) => {
  if (event.target.dataset.picker !== undefined) drawPicker(event.target);
});

editor.addEventListener('focusout', (event) => {
  const target = event.target;
  const picker = target.closest('.ae-picker');
  if (picker && !picker.contains(event.relatedTarget)) drawPicker(picker.querySelector('[data-picker]'), false);
  if (target.dataset.yaml !== undefined) parseYamlDraft(target.dataset.key);
});

// scroll does not bubble: caught on the way down, the coloured copy follows its textarea
editor.addEventListener('scroll', (event) => followScroll(event.target), true);

// a click on a match must not blur the input first: the list would be gone before the click lands
editor.addEventListener('pointerdown', (event) => {
  if (event.target.closest('[data-add-profile]')) event.preventDefault();
});

// caught before the kit's own tab handling on the same list
$('tabs').addEventListener('click', guardYamlTab, true);
$('tabs').addEventListener('keydown', guardYamlTab, true);

$('tabs').addEventListener('tabchange', (event) => {
  topOfTab($(`tab-${event.detail.tab}`));
  if (event.detail.tab === 'yaml') refreshYaml();
});

editor.addEventListener('tabchange', (event) => {
  if (event.target.id === 'toolLists') toolList = event.detail.tab;
});

$('list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-name]');
  if (button && event.detail < 2) choose(button.dataset.name);
});

// <details> announces its toggle on itself only: caught on the way down
$('list').addEventListener('toggle', (event) => {
  const group = event.target.dataset?.group;
  if (!group) return;
  if (event.target.open) collapsed.delete(group);
  else collapsed.add(group);
}, true);

$('search').addEventListener('input', drawList);
$('filter').addEventListener('change', drawList);
$('create').addEventListener('click', () => {
  if (!busy) openCreate();
});
$('reloadConfig').addEventListener('click', reloadConfig);
$('createForm').addEventListener('submit', submitCreate);
$('createForm').addEventListener('change', syncCreate);
$('createCancel').addEventListener('click', () => $('createDialog').close());
document.addEventListener('refresh', refreshAll);

loadList();
