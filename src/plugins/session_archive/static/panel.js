// Session Archive: the conversations the sweep put away, and the way back.
import { api, html, render, icon, confirm, toast } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);

/** The archived conversations, or null: not loaded, or they could not be. */
let archived = null;
let retentionDays = null;
let load = 0;
let busy = false;
/** Restores and deletes on their way, by session id: their buttons stay off until answered. */
const pending = new Set();

const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value) =>
  html`<div class="pk-stat" data-stat="${key}"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div></div>`;
const day = (stamp) => (stamp ? new Date(stamp).toLocaleDateString() : '');
const mb = (bytes) => `${((bytes || 0) / 1e6).toFixed(1)} MB`;
const entryUrl = (id) => `${BASE}archived/${encodeURIComponent(id)}`;

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while the last answer is on its way would only discard it
  const mine = ++load;
  busy = true;
  let listed;
  try {
    listed = await api(`${BASE}archived`, { quiet: true });
  } catch (error) {
    if (mine === load) {  // nothing shown before stays, as if it were still so
      archived = null;
      render($('stats'), '');
      render($('archived'), empty('circle-alert', 'The archive could not be read', error.message));
    }
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;
  archived = listed.archived;
  retentionDays = listed.retention_days;
  draw();
}

// -------------------------------------------------------------------- drawing

function draw() {
  if (!archived) return;
  const sessions = archived.reduce((sum, entry) => sum + (entry.session_count || 0), 0);
  const bytes = archived.reduce((sum, entry) => sum + (entry.bytes || 0), 0);
  render($('stats'), [
    stat('trees', 'Conversations', archived.length),
    stat('sessions', 'Sessions', sessions),
    stat('size', 'On disk', mb(bytes)),
    stat('retention', 'Archived after', retentionDays ? `${retentionDays} days` : '—'),
  ]);

  if (!archived.length) {
    render($('archived'), empty(
      'history', 'Nothing archived yet',
      retentionDays
        ? `Conversations move here once every session in them is older than ${retentionDays} days.`
        : 'Conversations move here once they are old enough.'));
    return;
  }

  render($('archived'), html`<div class="pk-table-wrap"><table class="pk-table">
    <thead><tr>
      <th>Conversation</th><th>Agent</th><th class="pk-num">Sessions</th>
      <th class="pk-num">Size</th><th>Last used</th><th>Archived</th><th></th>
    </tr></thead>
    <tbody>${archived.map(row)}</tbody>
  </table></div>`);
}

function row(entry) {
  const id = entry.session_id;
  const off = pending.has(id);
  const button = (act, name, label, danger = false) => html`<button type="button"
    class="pk-btn pk-btn--icon pk-btn--sm${danger ? ' pk-btn--danger' : ''}"
    data-act="${act}" data-id="${id}" title="${label}" aria-label="${label}"
    ${off ? 'disabled' : ''}>${icon(name, { size: 'sm' })}</button>`;
  return html`<tr data-entry="${id}">
    <td><div>${entry.title || id}</div><div class="pk-muted pk-mono">${id}</div></td>
    <td>${entry.agent_name || ''}</td>
    <td class="pk-num">${entry.session_count || 0}</td>
    <td class="pk-num">${mb(entry.bytes)}</td>
    <td>${day(entry.updated_at)}</td>
    <td>${day(entry.archived_at)}</td>
    <td>${button('restore', 'arrow-left', `Restore “${entry.title || id}”`)}
      ${button('forget', 'trash-2', `Delete “${entry.title || id}” for good`, true)}</td>
  </tr>`;
}

// --------------------------------------------------------------------- actions

async function act(button) {
  const id = button.dataset.id;
  const entry = archived.find((one) => one.session_id === id);
  const name = entry?.title || id;
  pending.add(id);
  draw();  // the WHOLE row goes off, not just the button clicked: deleting an
           // archive while its restore is on its way would race the two
  try {
    if (button.dataset.act === 'forget') {
      if (!await confirm(
        `Delete the archived conversation “${name}” and its ${entry?.session_count || 0} sessions? `
        + 'There is no copy after this.',
        { title: 'Delete archive', confirmLabel: 'Delete', danger: true })) return;
      await api(entryUrl(id), { method: 'DELETE' });
      toast(`“${name}” deleted`, { kind: 'ok' });
    } else {
      const result = await api(`${entryUrl(id)}/restore`, { method: 'POST' });
      toast(`“${name}” restored (${result.restored} sessions)`, { kind: 'ok' });
    }
  } catch {
    // shown by api()
  } finally {
    pending.delete(id);
    await refresh();
  }
}

async function sweep() {
  const button = $('sweep');
  button.disabled = true;
  try {
    const report = await api(`${BASE}sweep`, { method: 'POST' });
    toast(report.trees
      ? `Archived ${report.trees} conversation(s), ${report.sessions} sessions`
      : 'Nothing was old enough to archive', { kind: report.trees ? 'ok' : 'info' });
    if (report.errors?.length) toast(report.errors[0], { kind: 'warn' });
  } catch {
    // shown by api()
  } finally {
    button.disabled = false;
    await refresh();
  }
}

// ----------------------------------------------------------------------- wiring

$('archived').addEventListener('click', (event) => {
  const button = event.target.closest('[data-act]');
  if (button && !button.disabled) act(button);
});
$('sweep').addEventListener('click', sweep);
document.addEventListener('refresh', refresh);
refresh();
