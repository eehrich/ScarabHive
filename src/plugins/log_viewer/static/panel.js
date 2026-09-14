// Logs: the last entries of a configured log file and its rotations, filtered by level and search.
import { api, html, render, icon, trusted } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);
const STORE = `settings:${BASE}`;
const KINDS = { critical: 'danger', error: 'danger', warning: 'warn', info: 'info', debug: '' };

let load = 0;
let busy = false;
/** The names in the file select; null until the first list came in. */
let files = null;
/** The file and filters of the entries shown: a new one starts at the newest entry. */
let shownQuery = null;

const levelButtons = () => [...document.querySelectorAll('.lv-levels [data-level]')];
const pressed = (button) => button.getAttribute('aria-pressed') === 'true';
const press = (button, on) => button.setAttribute('aria-pressed', String(on));
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;

function size(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// -------------------------------------------------------------------- settings

function save() {
  try {
    localStorage.setItem(STORE, JSON.stringify({
      file: $('file').value, lines: $('lines').value, search: $('search').value,
      levels: levelButtons().filter(pressed).map((button) => button.dataset.level), follow: pressed($('follow')),
    }));
  } catch { /* storage blocked: the settings last as long as the page */ }
}

function restore() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORE)); } catch { /* none usable */ }
  if (!saved) return null;
  if ([...$('lines').options].some((option) => option.value === saved.lines)) $('lines').value = saved.lines;
  $('search').value = saved.search || '';
  if (Array.isArray(saved.levels)) levelButtons().forEach((button) => press(button, saved.levels.includes(button.dataset.level)));
  if (typeof saved.follow === 'boolean') press($('follow'), saved.follow);
  return saved.file;
}

const wanted = restore();

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while a load is on its way would queue another one behind it
  const mine = ++load;
  busy = true;
  try {
    const { logs } = await api(`${BASE}logs/list`, { quiet: true });
    if (mine !== load) return;  // a later load was started since
    const existing = logs.filter((log) => log.exists);
    drawFiles(existing.map((log) => log.name));
    const log = existing.find((one) => one.name === $('file').value);
    if (!log) return clear(empty('file-text', 'No log files', 'None of the configured log files exists yet.'));
    const levels = levelButtons().filter(pressed).map((button) => button.dataset.level);
    if (!levels.length) return clear(empty('filter', 'No level chosen', 'Choose at least one level to see entries.'));
    const query = new URLSearchParams({ lines: $('lines').value });
    if (levels.length < levelButtons().length) {
      query.set('levels', levels.flatMap((level) => (level === 'error' ? ['error', 'critical'] : [level])).join());
    }
    if ($('search').value) query.set('search', $('search').value);
    const { entries } = await api(`${BASE}logs/content/${encodeURIComponent(log.name)}?${query}`, { quiet: true });
    if (mine !== load) return;
    drawEntries(entries, log, query);
  } catch (error) {
    if (mine !== load) return;
    // nothing shown before stays, as if it were current
    clear(empty('circle-alert', 'The log could not be loaded', error.message));
  } finally {
    if (mine === load) busy = false;
  }
}

function drawFiles(names) {
  if (files && names.join('\n') === files.join('\n')) return;  // redrawn, an open select would close under the viewer
  const keep = files ? $('file').value : wanted;
  files = names;
  render($('file'), names.map((name) => html`<option value="${name}">${name}</option>`));
  $('file').value = names.includes(keep) ? keep : (names[0] ?? '');
  $('file').hidden = !names.length;
}

function clear(content) {
  render($('log'), content);
  $('shown').textContent = '';
  $('updated').textContent = '';
  shownQuery = null;
}

// --------------------------------------------------------------------- entries

function entry({ timestamp, level, message }, opened) {
  const [first, ...more] = message.split('\n');
  const key = `${timestamp} ${first}`;
  const kind = KINDS[level];
  return html`<div class="lv-entry" data-level="${level || ''}">
    <span class="pk-muted" title="${timestamp || ''}">${timestamp ? timestamp.slice(11, 19) : ''}</span>
    <span>${level ? html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${level.toUpperCase()}</span>` : ''}</span>
    ${more.length
      ? html`<details class="lv-message" data-key="${key}"${opened.has(key) ? trusted(' open') : ''}><summary>${first}</summary>${more.join('\n')}</details>`
      : html`<span class="lv-message">${first}</span>`}
  </div>`;
}

function drawEntries(entries, log, query) {
  const box = $('log');
  const key = `${log.name}?${query}`;
  const top = box.scrollTop;
  const opened = new Set([...box.querySelectorAll('details[open]')].map((details) => details.dataset.key));  // a refresh leaves them open
  render(box, entries.length
    ? entries.map((one) => entry(one, opened))
    : empty('file-text', 'No entries', query.has('levels') || query.has('search') ? 'Nothing matches the search and levels.' : 'The log is empty.'));
  box.scrollTop = pressed($('follow')) || key !== shownQuery ? box.scrollHeight : top;
  shownQuery = key;
  const count = `${entries.length.toLocaleString()} ${entries.length === 1 ? 'entry' : 'entries'}`;
  $('shown').textContent = `${count} · ${size(log.size)}${log.rotation_count > 1 ? ` in ${log.rotation_count} files` : ''}`;
  $('updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

// ---------------------------------------------------------------------- wiring

const changed = () => {
  save();
  refresh();
};
let typing = null;

document.addEventListener('refresh', refresh);
$('file').addEventListener('change', changed);
$('lines').addEventListener('change', changed);
$('search').addEventListener('input', () => {
  clearTimeout(typing);
  typing = setTimeout(changed, 300);
});
levelButtons().forEach((button) => button.addEventListener('click', () => {
  press(button, !pressed(button));
  changed();
}));
$('follow').addEventListener('click', () => {
  const on = !pressed($('follow'));
  press($('follow'), on);
  save();
  if (on) $('log').scrollTop = $('log').scrollHeight;
});

refresh();
