// Security Audit: the requests the server's audit kept, newest first -- who sent them and what they were answered.
import { api, html, render, icon, ApiError } from '/static/kit/panel-kit.js';

const $ = (id) => document.getElementById(id);
const STATUS_KINDS = { 2: 'ok', 3: 'info', 4: 'warn', 5: 'danger' };
const METHOD_KINDS = { GET: 'info', POST: 'ok', PUT: 'warn', PATCH: 'warn', DELETE: 'danger' };

let load = 0;
let busy = false;
/** The question and the answer drawn, as text: the same again draws nothing. */
let drawn = null;
/** The status button a click took the keyboard from while the buttons were off, or null. */
let refocus = null;

const statusButtons = () => [...document.querySelectorAll('#statuses [data-status]')];
const pressed = (button) => button.getAttribute('aria-pressed') === 'true';
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value) =>
  html`<div class="pk-stat" data-stat="${key}"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div></div>`;

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while a load is on its way would only queue another behind it
  const mine = ++load;
  busy = true;
  try {
    const chosen = statusButtons().filter(pressed).map((button) => button.dataset.status);
    if (!chosen.length) {
      draw('none', () => empty('filter', 'No status chosen', 'Choose at least one status class to see requests.'));
      return;
    }
    const query = new URLSearchParams({ limit: $('limit').value });
    if ($('category').value) query.set('category', $('category').value);
    if (chosen.length < statusButtons().length) chosen.forEach((status) => query.append('status', status));
    const { entries } = await api(`/admin/security/audit?${query}`, { quiet: true });
    if (mine !== load) return;  // a later load was started since
    draw(`${query} ${JSON.stringify(entries)}`, () => table(entries), entries);
  } catch (error) {
    if (mine !== load) return;
    // nothing shown before stays, as if it were current
    const status = error instanceof ApiError ? error.status : 0;
    draw(`error ${status} ${error.message}`, () => (status === 401 || status === 403
      ? empty('shield', 'Administrators only', 'The audit log is shown to administrators.')
      : status === 404
        ? empty('shield', 'The audit log is off', 'This server keeps no security audit log.')
        : empty('circle-alert', 'The audit log could not be loaded', error.message)));
  } finally {
    if (mine === load) {
      busy = false;
      release();
    }
  }
}

// -------------------------------------------------------------------- drawing

function draw(key, content, entries = null) {
  if (key === drawn) return;
  drawn = key;
  render($('stats'), entries ? stats(entries) : '');
  render($('entries'), content());
}

function stats(entries) {
  return [
    stat('shown', 'Requests', entries.length),
    stat('refused', 'Refused', entries.filter((entry) => entry.status_code === 401 || entry.status_code === 403).length),
    stat('errors', 'Server errors', entries.filter((entry) => !entry.status_code || entry.status_code >= 500).length),
    stat('users', 'Users', new Set(entries.map((entry) => entry.user_id)).size),
  ];
}

const badge = (kind, content) => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${content}</span>`;

function table(entries) {
  if (!entries.length) return empty('shield', 'No requests', 'Nothing kept matches the filters.');
  return html`<div class="pk-table-wrap"><table class="pk-table">
    <thead><tr><th>Time</th><th>Category</th><th>Method</th><th>Path</th><th>User</th><th>Status</th><th class="pk-num">Duration</th><th>Client</th></tr></thead>
    <tbody>${entries.map((entry) => html`<tr>
      <td class="pk-muted" title="${entry.timestamp}">${new Date(entry.timestamp).toLocaleString()}</td>
      <td>${badge('', entry.category)}</td>
      <td>${badge(METHOD_KINDS[entry.method], entry.method)}</td>
      <td class="pk-mono">${entry.path}</td>
      <td>${entry.user_id}</td>
      <td>${entry.status_code
        ? badge(STATUS_KINDS[Math.floor(entry.status_code / 100)] || 'danger', entry.status_code)
        : badge('danger', 'No answer')}</td>
      <td class="pk-num">${entry.duration_ms.toFixed(1)} ms</td>
      <td class="pk-mono pk-muted">${entry.client_ip}</td>
    </tr>`)}</tbody>
  </table></div>`;
}

// ---------------------------------------------------------------------- wiring

/** The status buttons come back on once the load a click started is drawn -- with the keyboard where it was. */
function release() {
  statusButtons().forEach((button) => { button.disabled = false; });
  const button = statusButtons().find((one) => one.dataset.status === refocus);
  refocus = null;
  if (button && (!document.activeElement || document.activeElement === document.body)) button.focus();
}

$('statuses').addEventListener('click', (event) => {
  const button = event.target.closest('[data-status]');
  // not the second click of a double click: answered quickly, the buttons are back on for it and it would take the choice back
  if (!button || event.detail > 1) return;
  if (document.activeElement === button) refocus = button.dataset.status;  // disabled, the button lets go of it
  button.setAttribute('aria-pressed', String(!pressed(button)));
  statusButtons().forEach((one) => { one.disabled = true; });
  refresh();
});
$('category').addEventListener('change', () => refresh());
$('limit').addEventListener('change', () => refresh());
document.addEventListener('refresh', refresh);

refresh();
