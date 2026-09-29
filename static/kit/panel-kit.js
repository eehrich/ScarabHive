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
  // a redraw gives the focus back to the element with the same data-key (a sort head has one, a row to choose too)
  const focused = document.activeElement;
  const key = element.contains(focused) ? focused.closest('[data-key]')?.dataset.key : undefined;
  element.innerHTML = fragment(content);
  sortTables(element);
  keyRows(element);
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
    if (typeof v === 'string') return v ? html`<span class="pk-json-string">${v}</span>` : muted(word('jsonEmpty'));
    if (typeof v !== 'object') return html`<span class="pk-json-number">${String(v)}</span>`;
    if (Array.isArray(v)) {
      return v.length ? html`<ul class="pk-json-list">${v.map((item) => html`<li>${node(item)}</li>`)}</ul>` : muted(word('jsonNoItems'));
    }
    const entries = Object.entries(v);
    if (!entries.length) return muted(word('jsonNoFields'));
    return html`<div class="pk-json-object">${entries.map(([key, item]) => {
      const nested = item !== null && typeof item === 'object' && Object.keys(item).length > 0;
      return html`<div class="pk-json-field${nested ? ' pk-json-field--nested' : ''}"><span class="pk-json-key">${key}</span>${node(item)}</div>`;
    })}</div>`;
  };
  return html`<div class="pk-json">${node(value)}</div>`;
}

const YAML_KEY = /^((?:"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^ \t\r#'"[\]{},](?:[ \t]*(?:[^ \t\r:#]|:(?![ \t]|$)|(?<![ \t])#))*))([ \t]*:)(?=[ \t]|$)/;
const YAML_FLOW_KEY = /^((?:[^ \t\r:]|:(?![ \t]|$))(?:[ \t]*(?:[^ \t\r:]|:(?![ \t]|$)))*)([ \t]*:)(?=[ \t]|$)([ \t]*)([^]*)$/;
const YAML_WORD = /^(?:true|false|yes|no|on|off|null|~)$/i;
const YAML_NUMBER = /^(?:[-+]?(?:\d[\d_]*(?:\.[\d_]*)?|\.\d+)(?:e[-+]?\d+)?|0x[\da-f_]+|0o[0-7_]+|[-+]?\.inf|\.nan)$/i;
// a flow collection in pieces: brackets and commas, quoted strings, a comment, and the plain text between them
const YAML_FLOW = /[[\]{},]|"(?:[^"\\]|\\.)*"?|'(?:[^']|'')*'?|(?<=\s)#.*|[^[\]{},"'#]+|#/g;

const yamlSpan = (kind, value) => (kind && value ? html`<span class="pk-yaml-${kind}">${value}</span>` : value);
const yamlKind = (value) => (YAML_NUMBER.test(value) ? 'number' : YAML_WORD.test(value) ? 'word' : '');

/** Where a quoted scalar opened with `quote` ends (after its closing quote), searching from `start`; -1 if not here. */
function quoteEnd(text, quote, start) {
  for (let i = start; i < text.length; i += 1) {
    if (quote === '"' && text[i] === '\\') i += 1;
    else if (text[i] === quote && quote === "'" && text[i + 1] === "'") i += 1;
    else if (text[i] === quote) return i + 1;
  }
  return -1;
}

function yamlFlowPiece(piece) {
  const inner = piece.trim();
  if (!inner) return piece;
  const lead = piece.slice(0, piece.length - piece.trimStart().length);
  const trail = piece.slice(piece.trimEnd().length);
  const pair = inner.match(YAML_FLOW_KEY);
  if (!pair) return [lead, yamlSpan(yamlKind(inner), inner), trail];
  return [lead, yamlSpan('key', pair[1]), yamlSpan('punct', pair[2]), pair[3], yamlSpan(yamlKind(pair[4]), pair[4]), trail];
}

/** One line: its markup, the column a block scalar it opens must go deeper than, a quote it leaves open. */
function yamlLine(line, openQuote) {
  const parts = [];
  let rest = line;
  let column = 0;
  const take = (length, kind = '') => {
    parts.push(yamlSpan(kind, rest.slice(0, length)));
    rest = rest.slice(length);
    column += length;
  };
  const blank = () => take(rest.match(/^[ \t]*/)[0].length);
  const done = (block = null, quote = '') => {  // what follows a node: spaces, a comment, or text as it is
    blank();
    take(rest.length, rest.startsWith('#') ? 'comment' : '');
    return { parts, block, quote };
  };
  if (openQuote) {
    const end = quoteEnd(rest, openQuote, 0);
    take(end < 0 ? rest.length : end, 'string');
    return done(null, end < 0 ? openQuote : '');
  }
  blank();
  let owner = column;
  if (column === 0 && /^(?:---|\.\.\.)(?=[ \t]|$)/.test(rest)) take(3, 'punct');
  else if (column === 0 && rest.startsWith('%')) take(rest.length, 'tag');
  blank();
  while (/^[-?](?=[ \t]|$)/.test(rest)) {
    owner = column;
    take(1, 'punct');
    blank();
  }
  const key = rest.match(YAML_KEY);
  if (key) {
    owner = column;
    take(key[1].length, 'key');
    take(key[2].length, 'punct');
    blank();
  }
  for (let prop = rest.match(/^[!&]\S*/); prop; prop = rest.match(/^[!&]\S*/)) {
    take(prop[0].length, prop[0][0] === '!' ? 'tag' : 'anchor');
    blank();
  }
  if (rest.startsWith('#')) return done();
  const indicator = rest.match(/^[|>][-+1-9]*(?=[ \t]|$)/);
  if (indicator) {
    take(indicator[0].length, 'punct');
    return done(owner);
  }
  if (rest[0] === '"' || rest[0] === "'") {
    const quote = rest[0];
    const end = quoteEnd(rest, quote, 1);
    take(end < 0 ? rest.length : end, 'string');
    return done(null, end < 0 ? quote : '');
  }
  const alias = rest.match(/^\*[^\s,[\]{}]+/);
  if (alias) {
    take(alias[0].length, 'anchor');
    return done();
  }
  if (rest[0] === '[' || rest[0] === '{') {
    for (const piece of rest.match(YAML_FLOW)) {
      if (/^[[\]{},]$/.test(piece)) parts.push(yamlSpan('punct', piece));
      else if (piece[0] === '"' || piece[0] === "'") parts.push(yamlSpan('string', piece));
      else if (piece[0] === '#' && piece.length > 1) parts.push(yamlSpan('comment', piece));
      else parts.push(yamlFlowPiece(piece));
    }
    return { parts, block: null, quote: '' };
  }
  const cut = rest.search(/[ \t]#/);
  const value = (cut < 0 ? rest : rest.slice(0, cut)).trimEnd();
  take(value.length, yamlKind(value));
  return done();
}

/**
 * YAML text coloured (.pk-yaml-*), the text itself unchanged: for a <pre>, or a copy beneath a textarea. Keys,
 * quoted strings, numbers, true/false/null, comments, tags and anchors; a block scalar (| or >) with every line that
 * belongs to it, blank lines included, and a quoted string over its lines.
 */
export function yamlCode(text) {
  const out = [];
  let block = null;  // a block scalar goes on while its lines sit deeper than this column, or are blank
  let quote = '';
  String(text ?? '').split('\n').forEach((line, index) => {
    if (index) out.push('\n');
    const depth = line.search(/\S/);
    if (block !== null && (depth < 0 || depth > block)) {
      out.push(yamlSpan('string', line));
      return;
    }
    const scanned = yamlLine(line, quote);
    out.push(scanned.parts);
    ({ block, quote } = scanned);
  });
  return html`${out}`;
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
const latestCalls = new Map();  // name -> the controller of the newest call under that name

/**
 * fetch() for panel code: same-origin cookie auth, JSON in and out, errors as
 * ApiError (and a toast unless quiet), requests aborted when the panel goes.
 *
 * latest: a name for a kind of load. A newer call under the same name aborts
 * the older one, which rejects with an AbortError (never toasted) -- an answer
 * overtaken by a later one can never be drawn. Check with isAborted(error).
 * Not with raw: the caller reads that body after api() returned, out of its reach.
 */
export async function api(path, { method = 'GET', json, body, headers = {}, quiet = false, raw = false, latest = '' } = {}) {
  if (raw && latest) throw new Error('api(): latest cannot guard a raw response');
  const controller = new AbortController();
  inflight.add(controller);
  if (latest) {
    latestCalls.get(latest)?.abort();
    latestCalls.set(latest, controller);
  }
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
      controller.signal.throwIfAborted();  // abandoned while the body was read: an abort, not an error to show
      throw new ApiError(response.status, detail);
    }
    if (raw) return response;
    const type = response.headers.get('content-type') || '';
    // awaited here, so a broken body lands in the catch below like any other failure
    const answer = await (type.includes('application/json') ? response.json() : response.text());
    controller.signal.throwIfAborted();
    return answer;
  } catch (error) {
    if (error.name !== 'AbortError' && !quiet) {
      toast(describe(error), { kind: 'error' });
    }
    throw error;
  } finally {
    inflight.delete(controller);
    if (latest && latestCalls.get(latest) === controller) latestCalls.delete(latest);
  }
}

/** True for a call that was abandoned (a newer `latest` call, or the panel going away): nothing to show. */
export const isAborted = (error) => error?.name === 'AbortError';

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

// Timers that skipped a tick while the panel was out of sight: coming back into view
// catches each up once. Without it a panel brought back showed what it knew when it
// went away, until its next tick -- up to a minute for a slow one --, and a click on
// refresh looked like the thing that made auto-refresh work.
const whenVisibleAgain = new Set();

function visibleAgain() {
  if (isVisible()) whenVisibleAgain.forEach((catchUp) => catchUp());
}

document.addEventListener('visibilitychange', visibleAgain);

function setVisible(value) {
  const was = visible;
  visible = value !== false;
  if (!was && visible) visibleAgain();
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
/** 'session' or 'all' -- what <pk-session all> is set to; without one a panel asks about a session. */
let scope = 'session';

function setSession(value) {
  const changed = value?.id !== currentSession?.id;
  currentSession = value || null;
  if (changed) listeners.session.forEach((fn) => fn(currentSession));
}

/** The session a link sent this panel to, or null. Read afresh: setQuery() may have changed it. */
const pinnedSession = () => new URLSearchParams(location.search).get('session_id') || null;

export const session = {
  /** The id of the shell's active session, or null; onChange gets {id, title} or null. */
  get id() { return currentSession?.id ?? null; },
  /** The shell's active session as {id, title}, or null. */
  get current() { return currentSession; },
  /** The session a link pinned this panel to (?session_id=), or null: then it does not follow the chat. */
  get pinned() { return pinnedSession(); },
  /** The session a panel shows: the pinned one, else the chat's -- null for all sessions, and for none open. */
  get shown() { return scope === 'all' ? null : pinnedSession() || currentSession?.id || null; },
  /** 'session' or 'all': which of the two <pk-session all> is set to. */
  get scope() { return scope; },
  onChange(fn) { listeners.session.add(fn); return () => listeners.session.delete(fn); },
  /** Drop the pin and follow the chat's session again. The panel reloads: every view of it named the old session. */
  follow() {
    const query = new URLSearchParams(location.search);
    query.delete('session_id');
    const rest = query.toString();  // whatever else a link carried stays: only the session is dropped
    const path = rest ? `${location.pathname}?${rest}` : location.pathname;
    // Told before the reload, so a restored panel follows the chat too. The gap: if a beforeunload
    // prompt then keeps the page, the shell has forgotten the pin while this panel still shows it.
    // No panel with <pk-session> has unsaved input today, and none may have without closing that.
    navigate(path);
    location.replace(path);
  },
};

/**
 * Called with the new theme when the viewer switches it, here or anywhere in the shell -- and with 'system' when the
 * system's colours change while the theme follows them.
 */
export function onThemeChange(fn) {
  listeners.theme.add(fn);
  return () => listeners.theme.delete(fn);
}

const systemDark = matchMedia('(prefers-color-scheme: dark)');
systemDark.addEventListener('change', () => {
  if (currentTheme() === 'system') listeners.theme.forEach((fn) => fn('system'));
});

/** Whether the page shows dark colours now: for a canvas or an SVG that cannot read the tokens. */
export function isDark() {
  const theme = currentTheme();
  return theme === 'dark' || (theme === 'system' && systemDark.matches);
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

// The kit's own words, in the language the page declares (<html lang>; the writer panels are German).
const WORDS = {
  en: {
    notice: 'Notice', confirm: 'Confirm', input: 'Input', cancel: 'Cancel',
    refresh: 'Refresh', refreshInterval: 'Refresh interval', refreshEvery: 'Refresh every', off: 'Off',
    refreshingEvery: (s) => `Refreshing every ${s} -- click to pause`, refreshEveryTitle: (s) => `Refresh every ${s}`,
    copied: 'Copied', copyFailed: 'Could not copy', previous: 'Previous', next: 'Next',
    jsonEmpty: 'empty', jsonNoItems: 'no items', jsonNoFields: 'no fields',
    pageOf: (page, pages) => `Page ${page} of ${pages}`,
    sessionScope: 'Scope', chatSession: 'Chat session', chatSessionTitle: 'The session open in the chat',
    allSessions: 'All sessions', sessionNamed: (id) => `Session ${id}`,
    pinnedTitle: (id) => `A link sent this panel to session ${id}: it does not follow the chat`,
    followChat: 'Follow the chat', followChatTitle: 'Show the session open in the chat again',
    openInShell: 'Open in ScarabHive', openInShellTitle: 'Open ScarabHive with this panel docked beside the chat',
  },
  de: {
    notice: 'Hinweis', confirm: 'Bestätigen', input: 'Eingabe', cancel: 'Abbrechen',
    refresh: 'Neu laden', refreshInterval: 'Takt', refreshEvery: 'Neu laden alle', off: 'Aus',
    refreshingEvery: (s) => `Lädt alle ${s} neu -- klicken zum Anhalten`, refreshEveryTitle: (s) => `Alle ${s} neu laden`,
    copied: 'Kopiert', copyFailed: 'Kopieren fehlgeschlagen', previous: 'Zurück', next: 'Weiter',
    jsonEmpty: 'leer', jsonNoItems: 'keine Einträge', jsonNoFields: 'keine Felder',
    pageOf: (page, pages) => `Seite ${page} von ${pages}`,
    sessionScope: 'Welche Session', chatSession: 'Chat-Session', chatSessionTitle: 'Die Session, die im Chat offen ist',
    allSessions: 'Alle Sessions', sessionNamed: (id) => `Session ${id}`,
    pinnedTitle: (id) => `Ein Link hat dieses Panel auf Session ${id} gesetzt: es folgt dem Chat nicht`,
    followChat: 'Dem Chat folgen', followChatTitle: 'Wieder die Session zeigen, die im Chat offen ist',
    openInShell: 'In ScarabHive öffnen', openInShellTitle: 'ScarabHive öffnen, dieses Panel neben dem Chat',
  },
};
const word = (key) => (WORDS[document.documentElement.lang.slice(0, 2).toLowerCase()] || WORDS.en)[key];

export function alert(message, { title = word('notice') } = {}) {
  return dialog({ title, message, actions: [{ label: 'OK', value: true, primary: true }] });
}

export async function confirm(message, { title = word('confirm'), confirmLabel = word('confirm'), danger = false } = {}) {
  const value = await dialog({
    title, message,
    actions: [{ label: word('cancel'), value: false }, { label: confirmLabel, value: true, primary: !danger, danger }],
  });
  return value === true;
}

export function prompt(message, { title = word('input'), value = '', placeholder = '', confirmLabel = 'OK' } = {}) {
  return dialog({
    title, message, input: { value, placeholder },
    actions: [{ label: word('cancel'), value: null }, { label: confirmLabel, value: 'input', primary: true }],
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
export function placeMenu(menu, box, { matchWidth = false } = {}) {
  if (matchWidth) menu.style.width = `${box.width}px`;  // as wide as the field it belongs to
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

/** Tell the shell the viewer's preferences changed (saved already): the chat shows things their way at once. */
export function announcePreferences(preferences) { tell('pk:preferences', { preferences }); }

export function currentTheme() {
  const theme = document.documentElement.dataset.theme;
  return THEMES.includes(theme) ? theme : 'system';
}

/** Tell the shell the page's path (by default the one it shows now), so a restored panel opens there. */
export function navigate(path = location.pathname + location.search) { tell('pk:navigate', { path }); }
export function setTitle(text) { tell('pk:title', { text }); document.title = text; }

/**
 * Open a session in the chat: the shell switches the chat to it. False where there is no chat to switch -- a panel
 * opened in a browser tab of its own; the caller then says what to do instead.
 */
export function openSession(sessionId) {
  if (!framed) return false;
  tell('pk:open-session', { session_id: String(sessionId) });
  return true;
}

let dirty = false;
/** Unsaved input: until the page says it is clean again, the shell asks before it closes or reloads the panel, a tab before it is left. */
export function setDirty(value) {
  const now = Boolean(value);
  // listened for only while there is something to lose: a beforeunload listener keeps a page out of the back/forward cache
  if (now && !dirty) window.addEventListener('beforeunload', keepPage);
  if (!now && dirty) window.removeEventListener('beforeunload', keepPage);
  dirty = now;
  tell('pk:dirty', { dirty });
}
const keepPage = (event) => event.preventDefault();

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
  let missed = false;
  const tick = () => {
    missed = !isVisible();
    if (!missed) fn();
  };
  // once, however many ticks went by: it is the panel's state that is late, not a count.
  // The beat starts over from here, or a tick due a moment later would load it all again.
  const catchUp = () => {
    if (!missed) return;
    missed = false;
    clearInterval(timer);
    timer = setInterval(tick, period);
    fn();
  };
  return {
    get running() { return timer !== null; },
    start() {
      if (timer !== null) return;
      timer = setInterval(tick, period);
      whenVisibleAgain.add(catchUp);
    },
    stop() {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
      missed = false;
      whenVisibleAgain.delete(catchUp);
    },
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
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="now" title="${word('refresh')}">${icon('refresh-cw')}</button>
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--sm pk-refresh-auto" data-act="auto" aria-pressed="false"></button>
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--sm pk-refresh-pick" data-act="interval"
              popovertarget="${menuId}" title="${word('refreshInterval')}" aria-label="${word('refreshInterval')}" aria-expanded="false">${icon('chevron-down', { size: 'sm' })}</button>
      <div id="${menuId}" class="pk-menu pk-refresh-menu" popover>
        <div class="pk-menu-label">${word('refreshEvery')}</div>
        ${steps.map((s) => html`<button type="button" class="pk-menu-item" data-interval="${s}" aria-pressed="false">${icon('check', { size: 'sm' })}${seconds(s)}</button>`)}
        <hr class="pk-menu-separator">
        <button type="button" class="pk-menu-item" data-interval="off" aria-pressed="false">${icon('check', { size: 'sm' })}${word('off')}</button>
      </div>`);
    // detail.auto: the timer fired, not the viewer -- a costly reload may skip that
    const fire = (auto) => this.dispatchEvent(new CustomEvent('refresh', { bubbles: true, detail: { auto } }));
    const toggle = this.querySelector('[data-act="auto"]');
    const menu = this.querySelector('.pk-menu');
    const set = ({ interval, on }, remember) => {
      if (this.auto) this.auto.stop();
      this.auto = autoRefresh(() => fire(true), interval * 1000);
      if (on) this.auto.start();
      // Off says off: the interval alone, in both states, told a paused panel from a
      // live one by its tint only -- and a paused panel looked like one whose refresh
      // did not work. What it would refresh at stays in the title.
      toggle.textContent = on ? seconds(interval) : word('off');
      toggle.title = (on ? word('refreshingEvery') : word('refreshEveryTitle'))(seconds(interval));
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

// ---------------------------------------------------------- session scope

/** Enough of a session id to recognise it; the whole one is in the title. */
const shortId = (id) => (id.length > 10 ? `${id.slice(0, 8)}…` : id);

/**
 * Which session the panel shows -- and the way back to the chat. In the toolbar:
 *
 *   <pk-session></pk-session>       says when a link pinned the panel to a session, and offers to follow again
 *   <pk-session all></pk-session>   and lets the viewer ask about every session instead
 *
 * Following the chat without that choice there is nothing to say, and the element stays out of the way.
 *
 * `sessionscope` (bubbles) fires whenever what the panel should show changes -- the viewer picked a scope, the
 * chat switched session, the pin was dropped. The panel then asks `session.shown` and `session.scope`, nothing
 * else: which session that is, and whether one was named at all, is this element's business.
 *
 * One per page: the scope is the page's, so a second element would show a scope it does not follow. The
 * catalogue (/ui/kit) puts both forms side by side to show them, which is the one place that does not hold.
 */
class SessionScope extends HTMLElement {
  connectedCallback() {
    // before the guard: moved in the DOM, an element is disconnected and connected again, and its watch was dropped
    this.stopWatching = session.onChange(() => this.announce());
    if (this.wired) {
      this.announce();  // the chat may have switched while the element hung loose
      return;
    }
    this.wired = true;
    const pinned = session.pinned;
    const all = this.hasAttribute('all');
    // A tab the shell opened for a panel set to all sessions (?session_scope=all) starts there, and a shell that
    // docks this page again hears it
    if (all && new URLSearchParams(location.search).get('session_scope') === 'all') {
      scope = 'all';
      tell('pk:scope', { scope });
    }
    // Nothing to say: the panel follows the chat, and the chat is right there.
    this.hidden = !pinned && !all;
    // A name for the session shown, or the plain word for it: the label is what the viewer reads as "which session".
    const named = pinned
      ? html`${icon('pin', { size: 'sm' })}${word('sessionNamed')(shortId(pinned))}`
      : word('chatSession');
    const title = pinned ? word('pinnedTitle')(pinned) : word('chatSessionTitle');
    render(this, html`
      ${all
        ? html`<div class="pk-row" role="group" aria-label="${word('sessionScope')}">
            <button type="button" class="pk-btn pk-btn--sm" data-scope="session" aria-pressed="${String(scope === 'session')}" title="${title}">${named}</button>
            <button type="button" class="pk-btn pk-btn--sm" data-scope="all" aria-pressed="${String(scope === 'all')}">${word('allSessions')}</button>
          </div>`
        : html`<span class="pk-badge" title="${title}">${named}</span>`}
      ${pinned ? html`<button type="button" class="pk-btn pk-btn--ghost pk-btn--sm" data-act="follow"
                              title="${word('followChatTitle')}">${icon('pin-off', { size: 'sm' })}${word('followChat')}</button>` : ''}`);
    // What the panel already draws by itself at load; only a change from here is news. The scope belongs in it:
    // switched to one session while none is open, nothing is shown any more -- the same "nothing" by another name.
    this.told = this.state();
    this.addEventListener('click', (event) => {
      if (event.target.closest('[data-act="follow"]')) return session.follow();
      const button = event.target.closest('[data-scope]');
      if (!button) return;
      scope = button.dataset.scope;  // the scope already shown asks nothing: announce() has nothing to tell
      tell('pk:scope', { scope });  // a tab the shell opens for this panel shows the same
      // The address's ?session_scope=all was where the page started: picked anew, a reload starts as any page does
      // (the shell drops it from its path once the chat's session is picked; the link into the shell asks the scope)
      const query = new URLSearchParams(location.search);
      if (query.has('session_scope')) {
        query.delete('session_scope');
        const rest = query.toString();
        history.replaceState(history.state, '', rest ? `${location.pathname}?${rest}` : location.pathname);
      }
      this.querySelectorAll('[data-scope]').forEach((one) => one.setAttribute('aria-pressed', String(one === button)));
      this.announce();
    });
  }

  disconnectedCallback() { this.stopWatching?.(); }

  state() { return `${scope}:${session.shown ?? ''}`; }

  announce() {
    if (this.told === this.state()) return;
    this.told = this.state();
    this.dispatchEvent(new CustomEvent('sessionscope', {
      bubbles: true, detail: { id: session.shown, scope, pinned: session.pinned },
    }));
  }
}

// ----------------------------------------------------------- page helpers

/** The plugin's address for a panel script served from its static folder: pluginBase(import.meta.url). */
export function pluginBase(moduleUrl) {
  return new URL('..', moduleUrl).pathname.replace(/\/$/, '');
}

// errorText(error): "404: Not Found" -- what a failed api() call says in its toast, for a page that shows it in place
export { describe as errorText };

/** Abandon the running api() call of that `latest` name, e.g. when the viewer clears what it would fill. */
export function abandon(name) {
  latestCalls.get(name)?.abort();
  latestCalls.delete(name);
}

const drawnMarkup = new WeakMap();

/**
 * render(), but only when the markup changed: an unchanged answer keeps scroll, focus, selection and open
 * <details>. Returns whether it drew. Draw an element either with update() or with render(), not both.
 */
export function update(element, content) {
  const markup = fragment(content);
  if (drawnMarkup.get(element) === markup) return false;
  drawnMarkup.set(element, markup);
  render(element, trusted(markup));
  return true;
}

const WITH_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;
// without a zone a time is UTC, as the databases write it: read as local time, a clock change would turn two round
const instant = (value) => Date.parse(value.length > 10 && !WITH_ZONE.test(value) ? `${value.replace(' ', 'T')}Z` : value);
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const STORED_TIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;
const STEPS = [['second', 60], ['minute', 60], ['hour', 24], ['day', Infinity]];

/**
 * A stored timestamp in the viewer's time and language. The databases write UTC without a zone
 * ("2026-09-16 19:21:29"), so a value without one is read as UTC; a date alone stays that date.
 * relative: "vor 5 Minuten" instead of the time.
 */
export function localTime(value, { relative = false, seconds = false } = {}) {
  if (value === null || value === undefined || value === '') return '';
  const text = String(value).trim();
  const lang = document.documentElement.lang || undefined;
  if (DATE_ONLY.test(text)) {
    const [year, month, day] = text.split('-').map(Number);
    return new Date(year, month - 1, day).toLocaleDateString(lang, { dateStyle: 'short' });
  }
  if (!STORED_TIME.test(text)) return text;  // "12" or "2026" are no stored times
  const date = new Date(instant(text));
  if (Number.isNaN(date.getTime())) return text;
  if (!relative) return date.toLocaleString(lang, { dateStyle: 'short', timeStyle: seconds ? 'medium' : 'short' });
  const format = new Intl.RelativeTimeFormat(lang, { numeric: 'auto' });
  let amount = (date.getTime() - Date.now()) / 1000;
  if (Math.abs(amount) < 60) return format.format(0, 'second');  // "now" for the whole first minute: no redraw each tick
  for (const [unit, size] of STEPS) {
    const rounded = Math.round(amount);
    if (Math.abs(rounded) < size) return format.format(rounded, unit);  // 59.6 minutes are an hour, not "60 minutes"
    amount /= size;
  }
  return text;
}

/** The filled-in fields of a form as query parameters; empty ones are left out. */
export function formQuery(form) {
  return new URLSearchParams([...new FormData(form)].filter(([, value]) => typeof value === 'string' && value.trim()));
}

/** Put params into the page's URL without a reload and tell the shell, so a restored panel opens this view. */
export function setQuery(params) {
  const query = new URLSearchParams(params).toString();
  history.replaceState(history.state, '', query ? `${location.pathname}?${query}` : location.pathname);
  navigate();
}

const busyControls = new WeakSet();

/**
 * Run fn once at a time for these controls (one, an array or a NodeList): they are disabled while it runs, and a
 * call meanwhile (a double click) returns undefined without starting fn. Afterwards every one of them is enabled -- a page with its own disabled
 * state sets it again after withBusy. A control a redraw replaces while fn runs is not the one guarded.
 */
export async function withBusy(controls, fn) {
  // a <select> or <form> is iterable itself: only a list of controls is spread
  const many = Array.isArray(controls) || controls instanceof NodeList || controls instanceof HTMLCollection;
  const list = (many ? [...controls] : [controls]).filter(Boolean);
  if (list.some((control) => busyControls.has(control))) return undefined;
  list.forEach((control) => { busyControls.add(control); control.disabled = true; });
  try {
    return await fn();
  } finally {
    list.forEach((control) => { busyControls.delete(control); control.disabled = false; });
  }
}

/** Copy text to the clipboard and say how it went. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(String(text));
    toast(word('copied'), { kind: 'ok' });
    return true;
  } catch (error) {
    toast(`${word('copyFailed')}: ${error.message || error}`, { kind: 'error' });
    return false;
  }
}

const CALLOUT_KINDS = ['danger', 'warn', 'info', 'ok'];

/** A message in place (element is a .pk-callout): shown with a text, hidden with an empty one. */
export function notice(element, text, { kind = 'danger' } = {}) {
  element.classList.add('pk-callout');
  CALLOUT_KINDS.forEach((one) => element.classList.toggle(`pk-callout--${one}`, one === kind));
  element.setAttribute('role', kind === 'danger' ? 'alert' : 'status');
  if (element.textContent !== (text || '')) element.textContent = text || '';  // unchanged: not announced again
  element.hidden = !text;
}

/** The empty state: an icon, a title and, if given, a line of explanation. */
export function emptyState(iconName, title, text = '') {
  return html`<div class="pk-empty">${icon(iconName)}<div class="pk-empty-title">${title}</div>${text && html`<div>${text}</div>`}</div>`;
}

/** Placeholder lines while something loads. */
export function skeleton(lines = 3) {
  return html`<div class="pk-stack">${Array.from({ length: lines }, () => html`<span class="pk-skeleton"></span>`)}</div>`;
}

/** Select the tab named `name` (its data-tab) in a [data-pk-tabs] list, as a click would, without the event. */
export function selectTab(list, name) {
  const tab = [...list.querySelectorAll('[role="tab"]')].find((one) => one.dataset.tab === name);
  if (tab) showTab(list, tab);
  return Boolean(tab);
}

/**
 * <pk-pager page="2" pages="7">: previous and next with "Seite 2 von 7"; a step fires `page` ({detail: {page}})
 * and moves the pager. Hidden while there is a single page. Set page and pages as properties or attributes.
 */
class Pager extends HTMLElement {
  static observedAttributes = ['page', 'pages'];

  get page() { return Math.min(Math.max(1, Math.floor(Number(this.getAttribute('page')) || 1)), this.pages); }
  set page(value) { this.setAttribute('page', String(value)); }
  get pages() { return Math.max(1, Math.floor(Number(this.getAttribute('pages')) || 1)); }
  set pages(value) { this.setAttribute('pages', String(value)); }

  connectedCallback() {
    if (!this.drawn) {
      this.drawn = true;
      render(this, html`
        <button type="button" class="pk-btn pk-btn--sm" data-step="-1">${icon('chevron-left')}${word('previous')}</button>
        <span class="pk-pager-label"></span>
        <button type="button" class="pk-btn pk-btn--sm" data-step="1">${word('next')}${icon('chevron-right')}</button>`);
      this.addEventListener('click', (event) => {
        const button = event.target.closest('[data-step]');
        if (!button || button.disabled) return;
        this.page = this.page + Number(button.dataset.step);
        if (button.disabled) this.querySelector('[data-step]:not([disabled])')?.focus();  // the end was reached
        this.dispatchEvent(new CustomEvent('page', { bubbles: true, detail: { page: this.page } }));
      });
    }
    this.show();
  }

  attributeChangedCallback() { if (this.drawn) this.show(); }

  show() {
    const { page, pages } = this;
    this.hidden = pages <= 1;
    this.querySelector('.pk-pager-label').textContent = word('pageOf')(page, pages);
    this.querySelector('[data-step="-1"]').disabled = page <= 1;
    this.querySelector('[data-step="1"]').disabled = page >= pages;
  }
}

/*
 * <table class="pk-table" data-pk-select>: a body row with data-id (and tabindex="0") is chosen by a click or by
 * Enter/Space; it is marked aria-selected and the table fires `rowselect` ({detail: {id}}). A click on a control
 * inside the row stays the control's. selectRow() marks a row again after a redraw, without the event. Such a row
 * keeps the keyboard focus through a redraw: render() keys it by its data-id.
 */
function keyRows(root) {
  root.querySelectorAll('table[data-pk-select] > tbody > tr[data-id]:not([data-key])')
    .forEach((row) => { row.dataset.key = `row:${row.dataset.id}`; });
}

export function selectRow(table, id) {
  let found = false;
  for (const row of table.tBodies[0]?.rows ?? []) {
    const on = row.dataset.id !== undefined && row.dataset.id === String(id);
    row.setAttribute('aria-selected', String(on));
    found ||= on;
  }
  return found;
}

// what answers a click itself: a click on one does not choose its row, a head holding one is not made a sort button
const CONTROLS = 'a[href], button, input, select, textarea, label, summary';

function chooseRow(event) {
  const target = event.target instanceof Element ? event.target : null;
  const row = target?.closest('tr[data-id]');
  const table = row?.closest('table[data-pk-select]');
  if (!table || row.parentElement !== table.tBodies[0]) return;
  const control = target.closest(CONTROLS);
  if (event.type === 'click' ? control && row.contains(control)
    : !['Enter', ' '].includes(event.key) || target !== row) return;
  if (event.type === 'keydown') event.preventDefault();
  selectRow(table, row.dataset.id);
  table.dispatchEvent(new CustomEvent('rowselect', { bubbles: true, detail: { id: row.dataset.id } }));
}

document.addEventListener('click', chooseRow);
document.addEventListener('keydown', chooseRow);

// ------------------------------------------------------------------ tabs

function showTab(list, tab) {
  list.querySelectorAll('[role="tab"]').forEach((t) => {
    const on = t === tab;
    t.setAttribute('aria-selected', String(on));
    t.tabIndex = on ? 0 : -1;
    const panel = document.getElementById(t.getAttribute('aria-controls'));
    if (panel) panel.hidden = !on;
  });
  keepInSight(list, tab);
}

/**
 * A vertical mouse wheel over a row that scrolls sideways moves the row (the browser takes only Shift or a
 * trackpad's sideways swipe for that). A row that fits, a sideways swipe and Ctrl (zoom) are left to the browser,
 * and so is the wheel once the row is at its end: the page around it scrolls on.
 */
export function wheelScrollsAcross(row) {
  row.addEventListener('wheel', (event) => {
    if (!event.deltaY || event.deltaX || event.shiftKey || event.ctrlKey || row.scrollWidth <= row.clientWidth) return;
    const before = row.scrollLeft;
    row.scrollLeft += event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;  // Firefox counts lines
    if (row.scrollLeft !== before) event.preventDefault();
  }, { passive: false });
}

/** A row of .pk-tabs: the wheel turns it, and a tab cut short says its whole text on hover (a title of its own stays). */
function wireTabRow(row) {
  wheelScrollsAcross(row);
  row.addEventListener('pointerover', (event) => {
    const tab = event.target.closest?.('.pk-tab');
    if (!tab || (tab.title && !('pkAutoTitle' in tab.dataset))) return;
    if (tab.scrollWidth > tab.clientWidth) {
      tab.title = tab.textContent.replace(/\s+/g, ' ').trim();
      tab.dataset.pkAutoTitle = '';
    } else if ('pkAutoTitle' in tab.dataset) {
      tab.removeAttribute('title');
      delete tab.dataset.pkAutoTitle;
    }
  });
}

/**
 * Scrolls a sideways row just far enough that `item` is seen whole (its start, where it is wider than the row).
 * Not scrollIntoView: that moves every scrolling box around it too, up to the shell.
 */
export function keepInSight(row, item) {
  const box = row.getBoundingClientRect();
  const it = item.getBoundingClientRect();
  if (it.left < box.left) row.scrollLeft -= box.left - it.left;
  else if (it.right > box.right) row.scrollLeft += Math.min(it.right - box.right, it.left - box.left);
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
      wireTabRow(list);
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

// ------------------------------------------------------------ side panes

const sidebarKey = (sidebar) => `pk.sidebar:${location.pathname}${sidebar.id ? `#${sidebar.id}` : ''}`;
const sidebarsReady = new WeakSet();
// the browser resizes a .pk-sidebar by writing its width inline: kept for the next visit of the panel
const keepSidebarWidth = new MutationObserver((changes) => changes.forEach(({ target }) => {
  try {
    if (target.style.width) localStorage.setItem(sidebarKey(target), target.style.width);
  } catch { /* the width lasts this page only */ }
}));

/**
 * Gives every .pk-sidebar under root the width the viewer last dragged it to, and keeps the next one. A button with
 * data-pk-sidebar-toggle and aria-controls naming a .pk-sidebar folds that pane away and back, as the chat's
 * sessions pane does; the panel keeps that choice too. Runs at start.
 */
export function initSidebars(root = document) {
  root.querySelectorAll('.pk-sidebar').forEach((sidebar) => {
    if (sidebarsReady.has(sidebar)) return;
    sidebarsReady.add(sidebar);
    try {
      const width = localStorage.getItem(sidebarKey(sidebar));
      if (width) sidebar.style.width = width;
    } catch { /* storage unavailable */ }
    keepSidebarWidth.observe(sidebar, { attributes: true, attributeFilter: ['style'] });
  });
  root.querySelectorAll('[data-pk-sidebar-toggle]').forEach((button) => {
    const sidebar = document.getElementById(button.getAttribute('aria-controls') || '');
    if (!sidebar || sidebarsReady.has(button)) return;
    sidebarsReady.add(button);
    const openKey = `${sidebarKey(sidebar)}:open`;
    const show = (open, remember) => {
      sidebar.hidden = !open;
      button.setAttribute('aria-expanded', String(open));
      if (!remember) return;
      try { localStorage.setItem(openKey, String(open)); } catch { /* it stays so for this page only */ }
    };
    let stored = null;
    try { stored = localStorage.getItem(openKey); } catch { /* storage unavailable */ }
    show(stored !== 'false', false);
    button.addEventListener('click', () => show(sidebar.hidden, true));
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
 * or with a control of its own stays plain. aria-sort in the markup is the order until the viewer picks one --
 * their pick is kept for this panel page and outlives a reload. A <thead> with one row, one <tbody>, no colspan.
 */
const sortChoices = new Map();  // table name -> { key, dir } or null, this page's picks
const sortKey = () => `pk.sort:${location.pathname}`;

/** What the viewer picked on this panel page before, by table name; a broken entry counts as none. */
function storedSorts() {
  try {
    const stored = JSON.parse(localStorage.getItem(sortKey()));
    return stored && typeof stored === 'object' ? stored : {};
  } catch { /* no storage or no JSON: the markup's order stands */ }
  return {};
}

function storedSort(name) {
  const stored = storedSorts()[name];
  return typeof stored?.key === 'string' && (stored.dir === 1 || stored.dir === -1)
    ? { key: stored.key, dir: stored.dir } : null;
}

// A name a panel builds from what the viewer typed (writer_admin's SQL panel names its table after the query) is
// no table they will see again, and it would carry that text into the browser's store: only a plain name is kept,
// and only the last few of them.
const KEPT_SORTS = 12;
const PLAIN_NAME = /^[\w.:-]{1,40}$/;

function rememberSort(name, choice) {
  sortChoices.set(name, choice);
  if (!PLAIN_NAME.test(name)) return;
  try {
    const all = storedSorts();
    delete all[name];  // and in again at the end: the picks fall out oldest first
    all[name] = choice;
    const kept = Object.keys(all).slice(-KEPT_SORTS).map((one) => [one, all[one]]);
    localStorage.setItem(sortKey(), JSON.stringify(Object.fromEntries(kept)));
  } catch { /* no storage: the pick lasts this page only */ }
}

const renderedAt = new WeakMap();  // row -> its place as rendered, which breaks ties
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
const EMPTY_CELLS = new Set(['', '-', '–', '—']);

const headKey = (th) => th.textContent.trim();

const cellValue = (cell) => (cell ? cell.dataset.sortValue ?? cell.textContent : '').trim();

const ISO_DATE = /^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}|$)/;

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
  const name = table.dataset.pkSort;
  if (!sortChoices.has(name)) sortChoices.set(name, storedSort(name));  // what they picked before, read once
  const chosen = sortChoices.get(name);
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
  rememberSort(table.dataset.pkSort, { key, dir });
  sortTable(table);
});

// ---------------------------------------------------------------- start

// defined last: a custom element on the page is drawn at once, and render() needs everything above
customElements.define('pk-pager', Pager);
customElements.define('pk-refresh', RefreshControl);
customElements.define('pk-session', SessionScope);

// the height inside the scroll area's padding, for a pane stuck in it (.pk-split): the viewport does not know the
// page head above it, and a sticky offset counts from the padding's inner edge
const bodySize = new ResizeObserver((entries) => entries.forEach(({ target, contentRect }) => {
  target.style.setProperty('--pk-body-height', `${contentRect.height}px`);
}));

/**
 * A panel in a browser tab of its own gets the way into the shell: a link in its header that opens ScarabHive with
 * this page docked beside the chat (the shell's ?panel=). It points at the page as it stands when followed, a query
 * set since load included, and at all sessions while <pk-session all> shows them (?session_scope=all, however the
 * page got there). Runs at start; in a frame there is a shell already.
 */
export function initShellLink(root = document) {
  if (framed) return;
  root.querySelectorAll('.pk-page-header').forEach((header) => {
    if (header.querySelector(':scope > [data-pk-shell-link]')) return;
    const link = document.createElement('a');
    link.className = 'pk-btn pk-btn--ghost pk-btn--sm';
    link.dataset.pkShellLink = '';
    link.title = word('openInShellTitle');
    render(link, html`${icon('panel-right', { size: 'sm' })} ${word('openInShell')}`);
    const aim = () => {
      const page = new URL(location.href);
      if (scope === 'all') page.searchParams.set('session_scope', 'all');
      link.href = `/?panel=${encodeURIComponent(page.pathname + page.search)}`;
    };
    aim();
    // the click itself too: activated without a pointer or focus first, the link would follow the load-time page
    ['pointerdown', 'focus', 'click'].forEach((type) => link.addEventListener(type, aim));
    header.appendChild(link);
  });
}

function start() {
  initShellLink();
  initTabs();
  // a row of page links (a nav of a.pk-tab) scrolls as tabs do, its current page in sight
  document.querySelectorAll('.pk-tabs:not([data-pk-tabs])').forEach((row) => {
    wireTabRow(row);
    const here = row.querySelector('[aria-current="page"]');
    if (here) keepInSight(row, here);
  });
  initSidebars();
  sortTables(document);
  keyRows(document);
  document.querySelectorAll('.pk-page-body').forEach((body) => bodySize.observe(body));
  if (framed) post('pk:ready');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start);
} else {
  start();
}
