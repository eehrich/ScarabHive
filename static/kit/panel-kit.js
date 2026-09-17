// ScarabHive panel kit -- what a panel script imports instead of writing its own.
//
//   import { api, html, render, toast, confirm, session } from '/static/kit/panel-kit.js';
//
// Inside the shell a panel talks to it over postMessage (pk:* messages);
// opened directly in a browser tab, the same calls work on their own.
// Components and tokens live in kit.css; the catalogue is /ui/kit.

// A frame counts as "in the shell" once the shell answered with pk:init --
// any other framer would leave dialogs unanswered and toasts unseen.
const framed = window.parent !== window;
const host = framed ? window.parent : null;
let inShell = false;
const listeners = { session: new Set(), theme: new Set() };
const pending = new Map();
let nextId = 1;

// ----------------------------------------------------------------- html

class SafeHtml {
  constructor(value) { this.value = value; }
  toString() { return this.value; }
}

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

function fragment(value) {
  if (value instanceof SafeHtml) return value.value;
  if (Array.isArray(value)) return value.map(fragment).join('');
  if (value == null || value === false) return '';
  return escapeHtml(value);
}

/** Tagged template: every interpolated value is escaped unless it is itself html`...`. */
export function html(strings, ...values) {
  return new SafeHtml(String.raw({ raw: strings }, ...values.map(fragment)));
}

/** Markup that is already safe (e.g. the server's sanitised HTML). */
export function trusted(markup) { return new SafeHtml(String(markup)); }

export function render(element, content) {
  // a redraw gives the focus back to the element with the same data-key (a sort head has one)
  const focused = document.activeElement;
  const key = element.contains(focused) ? focused.closest('[data-key]')?.dataset.key : undefined;
  element.innerHTML = fragment(content);
  sortTables(element);
  if (key !== undefined) [...element.querySelectorAll('[data-key]')].find((one) => one.dataset.key === key)?.focus();
}

/**
 * JSON as a reader takes it in (.pk-json): every level shown, a nested object indented under its key,
 * an array as a list, strings as text -- no quotes, their line breaks kept. Keys stay as they are.
 */
export function jsonView(value) {
  const muted = (text) => html`<span class="pk-json-null">${text}</span>`;
  const node = (v) => {
    if (v === null || v === undefined) return muted(String(v));
    if (typeof v === 'string') return v ? html`<span class="pk-json-string">${v}</span>` : muted('empty');
    if (typeof v !== 'object') return html`<span class="pk-json-number">${String(v)}</span>`;
    if (Array.isArray(v)) {
      return v.length ? html`<ul class="pk-json-list">${v.map((item) => html`<li>${node(item)}</li>`)}</ul>` : muted('no items');
    }
    const entries = Object.entries(v);
    if (!entries.length) return muted('no fields');
    return html`<div class="pk-json-object">${entries.map(([key, item]) => {
      const nested = item !== null && typeof item === 'object' && Object.keys(item).length > 0;
      return html`<div class="pk-json-field${nested ? ' pk-json-field--nested' : ''}"><span class="pk-json-key">${key}</span>${node(item)}</div>`;
    })}</div>`;
  };
  return html`<div class="pk-json">${node(value)}</div>`;
}

export function icon(name, { size = '', label = '' } = {}) {
  const cls = size ? ` pk-icon--${escapeHtml(size)}` : '';
  const aria = label ? ` role="img" aria-label="${escapeHtml(label)}"` : ' aria-hidden="true"';
  return new SafeHtml(`<svg class="pk-icon${cls}"${aria}><use href="/static/kit/icons.svg#${escapeHtml(name)}"/></svg>`);
}

// ------------------------------------------------------------------ api

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

const inflight = new Set();

/**
 * fetch() for panel code: same-origin cookie auth, JSON in and out, errors as
 * ApiError (and a toast unless quiet), requests aborted when the panel goes.
 */
export async function api(path, { method = 'GET', json, body, headers = {}, quiet = false, raw = false } = {}) {
  const controller = new AbortController();
  inflight.add(controller);
  try {
    // Built inside the try: a bad header value or a cyclic body is a failure
    // like any other, not an escape past the toast and the cleanup.
    const init = { method, headers: new Headers(headers), credentials: 'same-origin', signal: controller.signal };
    if (json !== undefined) {
      if (!init.headers.has('Content-Type')) init.headers.set('Content-Type', 'application/json');
      init.body = JSON.stringify(json);
    } else if (body !== undefined) {
      init.body = body;
    }
    const response = await fetch(path, init);
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail ?? detail; } catch { /* not JSON */ }
      throw new ApiError(response.status, detail);
    }
    if (raw) return response;
    const type = response.headers.get('content-type') || '';
    // awaited here, so a broken body lands in the catch below like any other failure
    return await (type.includes('application/json') ? response.json() : response.text());
  } catch (error) {
    if (error.name !== 'AbortError' && !quiet) {
      toast(describe(error), { kind: 'error' });
    }
    throw error;
  } finally {
    inflight.delete(controller);
  }
}

function describe(error) {
  if (error instanceof ApiError) {
    const detail = Array.isArray(error.detail)
      ? error.detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
      : error.message;
    return `${error.status}: ${detail}`;
  }
  return error.message || String(error);
}

window.addEventListener('pagehide', () => inflight.forEach((c) => c.abort()));

// -------------------------------------------------------------- protocol

function post(type, payload = {}) {
  host.postMessage({ type, ...payload }, window.location.origin);
}

function request(type, payload) {
  const id = nextId++;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    post(type, { ...payload, id });
  });
}

window.addEventListener('message', (event) => {
  if (!framed || event.source !== host || event.origin !== window.location.origin) return;
  const message = event.data || {};
  if (!inShell && message.type !== 'pk:init') return;
  switch (message.type) {
    case 'pk:init':
      inShell = true;
      applyTheme(message.theme);
      setVisible(message.visible);
      setSession(message.session);
      flushOutbox();
      break;
    case 'pk:theme':
      applyTheme(message.theme);
      break;
    case 'pk:session':
      setSession(message.session);
      break;
    case 'pk:visibility':
      setVisible(message.visible);
      break;
    case 'pk:dialog-result':
      pending.get(message.id)?.(message.value);
      pending.delete(message.id);
      break;
  }
});

function applyTheme(theme) {
  if (!theme || theme === document.documentElement.dataset.theme) return;
  document.documentElement.dataset.theme = theme;
  listeners.theme.forEach((fn) => fn(theme));
}

function setVisible(value) { visible = value !== false; }

// What a panel says about itself before the handshake -- typically right at
// load -- is kept (the latest of each) and sent once the shell has answered.
const outbox = new Map();

function tell(type, payload) {
  if (inShell) post(type, payload);
  else if (framed) outbox.set(type, payload);
}

function flushOutbox() {
  outbox.forEach((payload, type) => post(type, payload));
  outbox.clear();
}

// --------------------------------------------------------------- session

let currentSession = null;
let visible = true;

function setSession(value) {
  const changed = value?.id !== currentSession?.id;
  currentSession = value || null;
  if (changed) listeners.session.forEach((fn) => fn(currentSession));
}

export const session = {
  /** The id of the shell's active session, or null; onChange gets {id, title} or null. */
  get id() { return currentSession?.id ?? null; },
  onChange(fn) { listeners.session.add(fn); return () => listeners.session.delete(fn); },
};

/** Called with the new theme when the viewer switches it, here or anywhere in the shell. */
export function onThemeChange(fn) {
  listeners.theme.add(fn);
  return () => listeners.theme.delete(fn);
}

export function isVisible() { return visible && !document.hidden; }

// ------------------------------------------------------ dialogs and toasts

/**
 * One dialog for every question: the shell shows it over the whole app; a
 * panel opened on its own shows it itself. Resolves with the pressed action's
 * value (or the prompt's text), or null when dismissed.
 */
export function dialog({ title, message = '', actions, input = null }) {
  const spec = { title, message, actions, input };
  return inShell ? request('pk:dialog', { dialog: spec }) : showDialog(document, spec);
}

export function alert(message, { title = 'Notice' } = {}) {
  return dialog({ title, message, actions: [{ label: 'OK', value: true, primary: true }] });
}

export async function confirm(message, { title = 'Confirm', confirmLabel = 'Confirm', danger = false } = {}) {
  const value = await dialog({
    title, message,
    actions: [{ label: 'Cancel', value: false }, { label: confirmLabel, value: true, primary: !danger, danger }],
  });
  return value === true;
}

export function prompt(message, { title = 'Input', value = '', placeholder = '', confirmLabel = 'OK' } = {}) {
  return dialog({
    title, message, input: { value, placeholder },
    actions: [{ label: 'Cancel', value: null }, { label: confirmLabel, value: 'input', primary: true }],
  });
}

export function toast(message, { kind = 'info' } = {}) {
  if (inShell) {
    post('pk:toast', { message: String(message), kind });
    return;
  }
  showToast(document, String(message), kind);
}

/** Shared by the shell and by a panel outside it: the same markup either way. */
export function showToast(doc, message, kind = 'info') {
  let region = doc.querySelector('.pk-toasts');
  if (!region) {
    region = doc.createElement('div');
    region.className = 'pk-toasts';
    region.setAttribute('role', 'status');
    region.setAttribute('aria-live', 'polite');
    doc.body.appendChild(region);
  }
  const icons = { ok: 'circle-check', warn: 'triangle-alert', error: 'circle-alert', info: 'info' };
  const known = Object.hasOwn(icons, kind) ? kind : 'info';
  const item = doc.createElement('div');
  item.className = `pk-toast pk-toast--${known}`;
  render(item, html`${icon(icons[known])}<div class="pk-grow">${message}</div>`);
  region.appendChild(item);
  setTimeout(() => item.remove(), kind === 'error' ? 8000 : 4000);
}

/** Shared dialog renderer: returns a promise for the chosen value. */
export function showDialog(doc, { title, message, actions, input }) {
  return new Promise((resolve) => {
    const element = doc.createElement('dialog');
    element.className = 'pk-dialog';
    const buttons = (actions || []).map((action, i) => {
      const variant = action.danger ? ' pk-btn--danger' : action.primary ? ' pk-btn--primary' : '';
      return html`<button type="button" class="pk-btn${trusted(variant)}" data-index="${i}">${action.label}</button>`;
    });
    render(element, html`
      <form method="dialog">
        <div class="pk-dialog-head"><h2 class="pk-dialog-title">${title}</h2></div>
        <div class="pk-dialog-body">
          ${message ? html`<p style="margin:0 0 var(--space-3)">${message}</p>` : ''}
          ${input ? html`<input class="pk-input" name="value" value="${input.value || ''}" placeholder="${input.placeholder || ''}">` : ''}
        </div>
        <div class="pk-dialog-actions">${buttons}</div>
      </form>`);
    let result = null;
    const field = element.querySelector('input[name="value"]');
    element.querySelectorAll('[data-index]').forEach((button) => {
      button.addEventListener('click', () => {
        const action = actions[Number(button.dataset.index)];
        result = action.value === 'input' ? (field ? field.value : '') : action.value;
        element.close();
      });
    });
    if (field) {
      field.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        result = field.value;
        element.close();
      });
    }
    element.addEventListener('close', () => { element.remove(); resolve(result); });
    doc.body.appendChild(element);
    element.showModal();
    // Enter confirms what is safe: a destructive dialog starts on its other button.
    (field || element.querySelector('.pk-btn--primary')
      || element.querySelector('[data-index]:not(.pk-btn--danger)') || element).focus();
  });
}

// ------------------------------------------------------------------ menus

/**
 * Put a popover menu at a box (a button's getBoundingClientRect()): below it,
 * or above when there is more room there; right-aligned in the right half of
 * the window.
 */
export function placeMenu(menu, box) {
  const gap = 4;
  const margin = 8;
  // client sizes: a fixed position is measured inside the scrollbars
  const width = document.documentElement.clientWidth;
  const height = document.documentElement.clientHeight;
  const rightHalf = box.left + box.width / 2 > width / 2;
  const left = Math.max(margin, box.left);
  const right = Math.max(margin, width - box.right);
  const roomBelow = height - box.bottom - gap - margin;
  const roomAbove = box.top - gap - margin;
  const up = roomBelow < 240 && roomAbove > roomBelow;
  Object.assign(menu.style, {
    position: 'fixed',
    margin: '0',
    left: rightHalf ? 'auto' : `${left}px`,
    right: rightHalf ? `${right}px` : 'auto',
    top: up ? 'auto' : `${box.bottom + gap}px`,
    bottom: up ? `${height - box.top + gap}px` : 'auto',
    maxWidth: `${width - (rightHalf ? right : left) - margin}px`,
    maxHeight: `${Math.max(up ? roomAbove : roomBelow, 0)}px`,
  });
}

// A .pk-menu opened by a button appears at that button -- popovers have no
// anchor of their own in every browser yet. The event names the button where
// the browser supports it (ToggleEvent.source); otherwise the menu's
// popovertarget button stands in. beforetoggle does not bubble, so it is
// caught on the way down.
document.addEventListener('beforetoggle', (event) => {
  const menu = event.target;
  if (event.newState !== 'open' || !(menu instanceof HTMLElement) || !menu.matches('.pk-menu[id]')) return;
  const button = event.source || document.querySelector(`[popovertarget="${CSS.escape(menu.id)}"]`);
  if (button) placeMenu(menu, button.getBoundingClientRect());
}, true);

// Native dialogs are not allowed: they block the page and look foreign, and
// in the shell's sandbox confirm() silently returned false. Loud, not silent.
for (const name of ['alert', 'confirm', 'prompt']) {
  window[name] = () => {
    const text = `Native ${name}() is not allowed -- use the kit's ${name}() from panel-kit.js`;
    console.error(text);
    toast(text, { kind: 'error' });
    return name === 'confirm' ? false : undefined;
  };
}

// ------------------------------------------------------------ navigation

export const THEMES = ['system', 'light', 'dark'];

/** The viewer's theme choice: remembered in the cookie the server paints with, applied everywhere. */
export function setTheme(theme) {
  if (!THEMES.includes(theme)) throw new Error(`unknown theme ${theme}`);
  document.cookie = `ui_theme=${theme}; path=/; max-age=31536000; samesite=lax`;
  applyTheme(theme);
  tell('pk:set-theme', { theme });
}

export function currentTheme() {
  const theme = document.documentElement.dataset.theme;
  return THEMES.includes(theme) ? theme : 'system';
}

export function navigate(path) { tell('pk:navigate', { path }); }
export function setTitle(text) { tell('pk:title', { text }); document.title = text; }

// ---------------------------------------------------------- auto refresh

/**
 * Run fn every ms while the panel is visible. Returns {start, stop, running}.
 * Pauses while the window is minimised or the tab hidden.
 */
export function autoRefresh(fn, ms) {
  // One second to one day: below that a refresh loop hammers the server,
  // above it setInterval's 32-bit limit wraps around to ~4 ms.
  const period = Math.min(Math.max(Number(ms) || 0, 1000), 86_400_000);
  let timer = null;
  const tick = () => { if (isVisible()) fn(); };
  return {
    get running() { return timer !== null; },
    start() { if (timer === null) timer = setInterval(tick, period); },
    stop() { if (timer !== null) { clearInterval(timer); timer = null; } },
  };
}

const REFRESH_STEPS = [5, 10, 30, 60];
let refreshMenus = 0;
const seconds = (s) => (s >= 60 && s % 60 === 0 ? `${s / 60}m` : `${s}s`);

// The viewer's choice per panel page (interval and on/off) outlives a reload;
// the template's interval and auto attribute are only the page's defaults.
function refreshChoice(key) {
  try {
    const stored = JSON.parse(localStorage.getItem(key));
    const interval = Number(stored && stored.interval);
    if (Number.isFinite(interval) && interval >= 1 && typeof stored.on === 'boolean') return { interval, on: stored.on };
  } catch { /* no storage or a broken entry: the defaults stand */ }
  return null;
}

class RefreshControl extends HTMLElement {
  connectedCallback() {
    const requested = Number(this.getAttribute('interval'));
    const fallback = Number.isFinite(requested) && requested >= 1 ? requested : 5;
    const key = `pk.refresh:${location.pathname}`;
    const choice = refreshChoice(key) || { interval: fallback, on: this.hasAttribute('auto') };
    const steps = [...new Set([...REFRESH_STEPS, fallback, choice.interval])].sort((a, b) => a - b);
    const menuId = `pk-refresh-menu-${++refreshMenus}`;
    render(this, html`
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="now" title="Refresh">${icon('refresh-cw')}</button>
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--sm pk-refresh-auto" data-act="auto" aria-pressed="false"></button>
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--sm pk-refresh-pick" data-act="interval"
              popovertarget="${menuId}" title="Refresh interval" aria-label="Refresh interval" aria-expanded="false">${icon('chevron-down', { size: 'sm' })}</button>
      <div id="${menuId}" class="pk-menu pk-refresh-menu" popover>
        <div class="pk-menu-label">Refresh every</div>
        ${steps.map((s) => html`<button type="button" class="pk-menu-item" data-interval="${s}" aria-pressed="false">${icon('check', { size: 'sm' })}${seconds(s)}</button>`)}
        <hr class="pk-menu-separator">
        <button type="button" class="pk-menu-item" data-interval="off" aria-pressed="false">${icon('check', { size: 'sm' })}Off</button>
      </div>`);
    // detail.auto: the timer fired, not the viewer -- a costly reload may skip that
    const fire = (auto) => this.dispatchEvent(new CustomEvent('refresh', { bubbles: true, detail: { auto } }));
    const toggle = this.querySelector('[data-act="auto"]');
    const menu = this.querySelector('.pk-menu');
    const set = ({ interval, on }, remember) => {
      if (this.auto) this.auto.stop();
      this.auto = autoRefresh(() => fire(true), interval * 1000);
      if (on) this.auto.start();
      toggle.textContent = seconds(interval);
      toggle.title = on ? `Refreshing every ${seconds(interval)} -- click to pause` : `Refresh every ${seconds(interval)}`;
      toggle.setAttribute('aria-pressed', String(on));
      menu.querySelectorAll('[data-interval]').forEach((item) => {
        const value = item.dataset.interval;
        item.setAttribute('aria-pressed', String(on ? Number(value) === interval : value === 'off'));
      });
      this.choice = { interval, on };
      if (remember) {
        try { localStorage.setItem(key, JSON.stringify(this.choice)); } catch { /* the choice lasts this page only */ }
      }
    };
    this.querySelector('[data-act="now"]').addEventListener('click', () => fire(false));
    toggle.addEventListener('click', () => set({ ...this.choice, on: !this.choice.on }, true));
    const pick = this.querySelector('[data-act="interval"]');
    menu.addEventListener('toggle', (event) => pick.setAttribute('aria-expanded', String(event.newState === 'open')));
    menu.addEventListener('click', (event) => {
      const item = event.target.closest('[data-interval]');
      if (!item) return;
      const value = item.dataset.interval;
      set(value === 'off' ? { ...this.choice, on: false } : { interval: Number(value), on: true }, true);
      if (menu.matches(':popover-open')) menu.hidePopover();  // hidePopover() on a closed popover throws
    });
    set(choice, false);
  }

  disconnectedCallback() { if (this.auto) this.auto.stop(); }
}

// ------------------------------------------------------------------ tabs

function showTab(list, tab) {
  list.querySelectorAll('[role="tab"]').forEach((t) => {
    const on = t === tab;
    t.setAttribute('aria-selected', String(on));
    t.tabIndex = on ? 0 : -1;
    const panel = document.getElementById(t.getAttribute('aria-controls'));
    if (panel) panel.hidden = !on;
  });
}

/**
 * Makes every [data-pk-tabs] under root work: click and arrow keys switch
 * panels and fire `tabchange` ({detail: {tab}}). Listeners sit on the list
 * and read its tabs at event time, so tabs rendered later work too; calling
 * this again only brings the panels in line with the selected tab.
 */
export function initTabs(root = document) {
  root.querySelectorAll('[data-pk-tabs]').forEach((list) => {
    if (!list.dataset.pkTabsReady) {
      list.dataset.pkTabsReady = 'true';
      const choose = (tab) => {
        showTab(list, tab);
        list.dispatchEvent(new CustomEvent('tabchange', { detail: { tab: tab.dataset.tab }, bubbles: true }));
      };
      list.addEventListener('click', (event) => {
        const tab = event.target.closest('[role="tab"]');
        if (tab && list.contains(tab)) choose(tab);
      });
      list.addEventListener('keydown', (event) => {
        const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        const tabs = [...list.querySelectorAll('[role="tab"]')];
        const index = tabs.indexOf(event.target.closest('[role="tab"]'));
        if (!step || index < 0) return;
        const next = tabs[(index + step + tabs.length) % tabs.length];
        next.focus();
        choose(next);
      });
    }
    const tabs = [...list.querySelectorAll('[role="tab"]')];
    if (tabs.length) showTab(list, tabs.find((t) => t.getAttribute('aria-selected') === 'true') || tabs[0]);
  });
}

// ---------------------------------------------------------------- sortable tables

/*
 * <table class="pk-table" data-pk-sort="calls">: a click on a column head sorts the rows -- a number column
 * biggest first, any other A to Z, a click on the column sorted by reverses -- and the choice holds through every
 * render() of the table. The name tells a page's tables apart; the choice follows the head's text, so a column
 * shown only sometimes does not shift it. A cell sorts by its data-sort-value, else by its text; a column of
 * numbers as numbers, of ISO timestamps as points in time (without a zone: UTC), any other all naturally ("B9"
 * before "B10"), an empty cell or a lone dash last in either direction. A head with data-pk-nosort, without text
 * or with a control of its own stays plain. aria-sort in the markup is the order until the viewer picks one.
 * A <thead> with one row, one <tbody>, no colspan.
 */
const sortChoices = new Map();  // table name -> { key, dir }
const renderedAt = new WeakMap();  // row -> its place as rendered, which breaks ties
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
const EMPTY_CELLS = new Set(['', '-', '–', '—']);

const headKey = (th) => th.textContent.trim();

const cellValue = (cell) => (cell ? cell.dataset.sortValue ?? cell.textContent : '').trim();

const ISO_DATE = /^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}|$)/;
const WITH_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;
// what answers a click itself: a head holding one is not made a sort button
const CONTROLS = 'a[href], button, input, select, textarea, label, summary';

// without a zone a time is UTC, as the databases write it: read as local time, a clock change would turn two round
const instant = (value) => Date.parse(value.length > 10 && !WITH_ZONE.test(value) ? `${value.replace(' ', 'T')}Z` : value);

/**
 * How a column's values compare: as numbers or as points in time when all of them are, else all as text. Decided
 * pair by pair, a column of both would have no order ("-10" < "-5" < "-5x" < "-10").
 */
function comparer(values) {
  if (values.every((value) => Number.isFinite(Number(value)))) return (a, b) => Number(a) - Number(b);
  // as text, "10:00:00+00:00" would follow "10:00:00.5+00:00", and a later time with another offset an earlier one
  if (values.every((value) => ISO_DATE.test(value) && Number.isFinite(instant(value)))) return (a, b) => instant(a) - instant(b);
  return collator.compare;
}

/** The viewer's choice while its column is shown, else the one the markup names. */
function sortChoice(table, heads) {
  const chosen = sortChoices.get(table.dataset.pkSort);
  if (chosen && heads.some((th) => headKey(th) === chosen.key)) return chosen;
  const marked = heads.find((th) => ['ascending', 'descending'].includes(th.getAttribute('aria-sort')));
  return marked ? { key: headKey(marked), dir: marked.getAttribute('aria-sort') === 'ascending' ? 1 : -1 } : null;
}

function sortTable(table) {
  const heads = [...(table.tHead?.rows[0]?.cells ?? [])];
  const body = table.tBodies[0];
  if (!heads.length || !body) return;
  for (const th of heads) {
    // wrapped already, or holding a control of its own: a control inside a button is not allowed
    if (th.hasAttribute('data-pk-nosort') || !headKey(th) || th.querySelector(`${CONTROLS}, [tabindex]`)) continue;
    const button = Object.assign(document.createElement('button'), { type: 'button', className: 'pk-sort' });
    button.dataset.key = `sort:${table.dataset.pkSort}:${headKey(th)}`;  // render() gives the focus back by it
    button.append(...th.childNodes);
    th.append(button);
  }
  const choice = sortChoice(table, heads);
  if (!choice) return;
  const column = heads.findIndex((th) => headKey(th) === choice.key);
  heads.forEach((th) => th.removeAttribute('aria-sort'));
  heads[column].setAttribute('aria-sort', choice.dir > 0 ? 'ascending' : 'descending');
  const rows = [...body.rows].map((row, i) => {
    if (!renderedAt.has(row)) renderedAt.set(row, i);
    const value = cellValue(row.cells[column]);
    return { row, value, empty: EMPTY_CELLS.has(value), at: renderedAt.get(row) };
  });
  const compare = comparer(rows.filter((one) => !one.empty).map((one) => one.value));
  rows.sort((a, b) => a.empty - b.empty || (a.empty ? 0 : choice.dir * compare(a.value, b.value)) || a.at - b.at);
  body.append(...rows.map(({ row }) => row));
}

/** The sortable tables in root, and the one root sits in (a render into a tbody). */
function sortTables(root) {
  [...root.querySelectorAll('table[data-pk-sort]'), root.closest?.('table[data-pk-sort]')].filter(Boolean).forEach(sortTable);
}

document.addEventListener('click', (event) => {
  // the whole head cell, not only its text: the button is what the keyboard reaches
  const th = event.target.closest?.('th');
  const table = th?.querySelector(':scope > .pk-sort') && th.closest('table[data-pk-sort]');
  if (!table) return;
  const current = sortChoice(table, [...th.parentElement.cells]);
  const key = headKey(th);
  const dir = current?.key === key ? -current.dir : th.classList.contains('pk-num') ? -1 : 1;
  sortChoices.set(table.dataset.pkSort, { key, dir });
  sortTable(table);
});

// ---------------------------------------------------------------- start

// defined last: a custom element on the page is drawn at once, and render() needs everything above
customElements.define('pk-refresh', RefreshControl);

function start() {
  initTabs();
  sortTables(document);
  if (framed) post('pk:ready');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start);
} else {
  start();
}
