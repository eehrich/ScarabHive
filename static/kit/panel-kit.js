// ScarabHive panel kit -- what a panel script imports instead of writing its own.
//
//   import { api, html, render, toast, confirm, session } from '/static/kit/panel-kit.js';
//
// Inside the shell a panel talks to it over postMessage (pk:* messages, see
// PROTOCOL_VERSION); opened directly in a browser tab, the same calls work on
// their own. Components and tokens live in kit.css; the catalogue is /ui/kit.

export const PROTOCOL_VERSION = 1;

// A frame counts as "in the shell" once the shell answered with pk:init --
// any other framer would leave dialogs unanswered and toasts unseen.
const framed = window.parent !== window;
const host = framed ? window.parent : null;
let inShell = false;
const listeners = { session: new Set(), visibility: new Set(), theme: new Set() };
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
  if (value === null || value === undefined || value === false) return '';
  return escapeHtml(value);
}

/** Tagged template: every interpolated value is escaped unless it is itself html`...`. */
export function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, i) => { out += fragment(value) + strings[i + 1]; });
  return new SafeHtml(out);
}

/** Markup that is already safe (e.g. the server's sanitised HTML). */
export function trusted(markup) { return new SafeHtml(String(markup)); }

export function render(element, content) {
  element.innerHTML = fragment(content);
}

/** A collapsible JSON tree for .pk-json. Objects and arrays deeper than `open` levels start closed. */
export function jsonView(value, { open = 1 } = {}) {
  const node = (v, depth) => {
    if (v === null || v === undefined) return html`<span class="pk-json-null">${String(v)}</span>`;
    if (typeof v === 'string') return html`<span class="pk-json-string">"${v}"</span>`;
    if (typeof v === 'number' || typeof v === 'boolean') return html`<span class="pk-json-number">${String(v)}</span>`;
    const isArray = Array.isArray(v);
    const entries = isArray ? v.map((item, i) => [i, item]) : Object.entries(v);
    if (!entries.length) return html`${isArray ? '[]' : '{}'}`;
    const rows = entries.map(([key, item]) => html`<div>${isArray ? '' : html`<span class="pk-json-key">${key}</span>: `}${node(item, depth + 1)}</div>`);
    const summary = isArray ? `[${entries.length}]` : `{${entries.length}}`;
    return html`<details ${depth < open ? trusted('open') : ''}><summary>${summary}</summary>${rows}</details>`;
  };
  return html`<div class="pk-json">${node(value, 0)}</div>`;
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
  host.postMessage({ type, v: PROTOCOL_VERSION, ...payload }, window.location.origin);
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
    case 'pk:dialog-result': {
      const resolve = pending.get(message.id);
      pending.delete(message.id);
      if (resolve) resolve(message.value);
      break;
    }
    default:
      break;
  }
});

function applyTheme(theme) {
  if (!theme || theme === document.documentElement.dataset.theme) return;
  document.documentElement.dataset.theme = theme;
  listeners.theme.forEach((fn) => fn(theme));
}

function setVisible(value) {
  const next = value !== false;
  if (next === visible) return;
  visible = next;
  listeners.visibility.forEach((fn) => fn(visible));
}

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
  const next = value || null;
  if ((next && next.id) === (currentSession && currentSession.id)) {
    currentSession = next;
    return;
  }
  currentSession = next;
  listeners.session.forEach((fn) => fn(currentSession));
}

export const session = {
  /** {id, title} of the shell's active session, or null. */
  get current() { return currentSession; },
  get id() { return currentSession ? currentSession.id : null; },
  onChange(fn) { listeners.session.add(fn); return () => listeners.session.delete(fn); },
};

/** Called with the new theme when the viewer switches it, here or anywhere in the shell. */
export function onThemeChange(fn) {
  listeners.theme.add(fn);
  return () => listeners.theme.delete(fn);
}

export function onVisibilityChange(fn) {
  listeners.visibility.add(fn);
  return () => listeners.visibility.delete(fn);
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
  return inShell ? request('pk:dialog', { dialog: spec }) : localDialog(spec);
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
  const icons = { ok: () => icon('circle-check'), warn: () => icon('triangle-alert'),
                  error: () => icon('circle-alert'), info: () => icon('info') };
  const known = Object.hasOwn(icons, kind) ? kind : 'info';
  const item = doc.createElement('div');
  item.className = `pk-toast pk-toast--${known}`;
  render(item, html`${icons[known]()}<div class="pk-grow">${message}</div>`);
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

function localDialog(spec) { return showDialog(document, spec); }

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
export function setBadge(count) { tell('pk:badge', { count }); }
/** Open another panel from the catalogue, e.g. openPanel('message_debugger', '?request_id=...'). */
export function openPanel(panel, path = '') { tell('pk:open', { panel, path }); }

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
  const control = {
    get running() { return timer !== null; },
    start() { if (timer === null) timer = setInterval(tick, period); },
    stop() { if (timer !== null) { clearInterval(timer); timer = null; } },
  };
  return control;
}

class RefreshControl extends HTMLElement {
  connectedCallback() {
    const requested = Number(this.getAttribute('interval'));
    const interval = Number.isFinite(requested) && requested >= 1 ? requested : 5;
    render(this, html`
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="now" title="Refresh">${icon('refresh-cw')}</button>
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--sm pk-refresh-auto" data-act="auto" aria-pressed="false"
              title="Refresh every ${interval}s">${interval}s</button>`);
    // detail.auto: the timer fired, not the viewer -- a costly reload may skip that
    const fire = (auto) => this.dispatchEvent(new CustomEvent('refresh', { bubbles: true, detail: { auto } }));
    this.auto = autoRefresh(() => fire(true), interval * 1000);
    this.querySelector('[data-act="now"]').addEventListener('click', () => fire(false));
    const toggle = this.querySelector('[data-act="auto"]');
    toggle.addEventListener('click', () => {
      if (this.auto.running) this.auto.stop(); else this.auto.start();
      toggle.setAttribute('aria-pressed', String(this.auto.running));
    });
  }

  disconnectedCallback() { if (this.auto) this.auto.stop(); }
}

customElements.define('pk-refresh', RefreshControl);

// ------------------------------------------------------------------ tabs

/** Wires every [data-pk-tabs] under root: click and arrow keys switch panels. */
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

// ---------------------------------------------------------------- start

function start() {
  initTabs();
  if (framed) post('pk:ready');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start);
} else {
  start();
}
