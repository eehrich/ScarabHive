// ComfyUI: the servers' state, the jobs still to finish with a way to cancel them, and the last ones finished.
import { api, html, render, icon, confirm, toast } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);

/** The last answer drawn, or null: not loaded, or it could not be. */
let shown = null;
/** The jobs a cancel is on its way for: their buttons stay disabled through every redraw. */
const cancelling = new Set();
let load = 0;
let busy = false;

const STATUSES = {
  queued: { icon: 'clock', kind: '' },
  pending: { icon: 'clock', kind: '' },
  running: { icon: 'loader-circle', kind: 'info' },
  completed: { icon: 'circle-check', kind: 'ok' },
  failed: { icon: 'circle-x', kind: 'danger' },
  cancelled: { icon: 'ban', kind: '' },
};
const OUTPUTS = { images: 'image', audio: 'music', video: 'clapperboard', text: 'file-text' };

const number = (value) => value.toLocaleString();
const short = (id) => id.slice(0, 8);
const when = (stamp) => (stamp ? new Date(stamp).toLocaleString() : '—');
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value) =>
  html`<div class="pk-stat" data-stat="${key}"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${number(value)}</div></div>`;
const badge = (status) => {
  const { icon: name, kind } = STATUSES[status];
  return html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${icon(name, { size: 'sm' })} ${status}</span>`;
};

function duration(seconds) {
  if (seconds === null) return '—';
  const whole = Math.round(seconds);  // rounded before it is split: 119.7 s is 2m 0s, not 1m 60s
  if (whole < 60) return `${whole}s`;
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${whole % 60}s`;
  return `${Math.floor(whole / 3600)}h ${Math.floor((whole % 3600) / 60)}m`;
}

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while a load is on its way would queue another one behind it
  const mine = ++load;
  busy = true;
  let answer;
  try {
    answer = await api(`${BASE}jobs`, { quiet: true });
  } catch (error) {
    if (mine !== load) return;  // a later load was started since
    // nothing shown before stays, as if it were current
    shown = null;
    render($('servers'), '');
    render($('stats'), '');
    $('updated').textContent = '';
    render($('jobs'), empty('circle-alert', 'Jobs could not be loaded', error.message));
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;
  shown = answer;
  drawServers();
  drawStats();
  drawJobs();
  $('updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

// -------------------------------------------------------------------- drawing

function drawServers() {
  render($('servers'), shown.servers.map((server) => html`<div class="pk-row" data-server="${server.address}">
    <span class="pk-dot pk-dot--${server.online ? 'ok' : 'danger'}"></span>
    <span class="pk-mono">${server.address}</span>
    <span class="pk-muted">${server.online
      ? `${number(server.queue_running)} running · ${number(server.queue_pending)} queued`
      : `Offline: ${server.error}`}</span>
  </div>`));
}

function drawStats() {
  const { stats } = shown;
  render($('stats'), [
    stat('active', 'Active', stats.queued + stats.pending + stats.running),
    stat('completed', 'Completed', stats.completed),
    stat('failed', 'Failed', stats.failed),
    stat('cancelled', 'Cancelled', stats.cancelled),
    stat('total', 'Total', stats.total),
  ]);
}

function activeRow(job) {
  const pending = cancelling.has(job.prompt_id);
  return html`<tr data-job="${job.prompt_id}">
    <td class="pk-mono" title="${job.prompt_id}">${short(job.prompt_id)}</td>
    <td class="cu-workflow">${job.workflow}</td>
    <td>${badge(job.status)}</td>
    <td class="cu-time" data-sort-value="${job.submitted_at}">${when(job.submitted_at)}</td>
    <td class="pk-num" data-sort-value="${job.seconds}">${duration(job.seconds)}</td>
    <td class="pk-num"><button type="button" class="pk-btn pk-btn--ghost pk-btn--sm" data-cancel="${job.prompt_id}" ${pending ? html`disabled` : ''}>${icon('ban', { size: 'sm' })} Cancel</button></td>
  </tr>`;
}

function recentRow(job) {
  const outputs = Object.entries(job.outputs);
  return html`<tr data-job="${job.prompt_id}">
    <td class="pk-mono" title="${job.prompt_id}">${short(job.prompt_id)}</td>
    <td class="cu-workflow">${job.workflow}</td>
    <td data-sort-value="${job.status}">${badge(job.status)}${job.error ? html`<div class="cu-error pk-truncate" title="${job.error}">${job.error}</div>` : ''}</td>
    <td class="cu-time" data-sort-value="${job.completed_at}">${when(job.completed_at)}</td>
    <td class="pk-num" data-sort-value="${job.seconds}">${duration(job.seconds)}</td>
    <td class="pk-num" data-sort-value="${outputs.reduce((sum, [, count]) => sum + count, 0) || ''}">${outputs.length
      ? outputs.map(([kind, count]) => html`<span class="cu-output" title="${kind}">${icon(OUTPUTS[kind] || 'paperclip', { size: 'sm' })} ${number(count)}</span>`)
      : '—'}</td>
  </tr>`;
}

// a head's order is the one the tracker lists the rows in, until the viewer picks another
const table = (name, head, rows) => html`<div class="pk-table-wrap"><table class="pk-table" data-pk-sort="${name}">
  <thead><tr>${head.map(([label, numeric, order]) =>
    html`<th${numeric ? html` class="pk-num"` : ''}${order ? html` aria-sort="${order}"` : ''}>${label}</th>`)}</tr></thead>
  <tbody>${rows}</tbody>
</table></div>`;

function drawJobs() {
  // drawn anew, a cancel button keeps the keyboard focus
  const focused = document.activeElement?.closest('#jobs button[data-cancel]')?.dataset.cancel;
  render($('jobs'), html`
    <section class="pk-stack" id="active">
      <h2 class="cu-heading">Active jobs</h2>
      ${shown.active.length
        ? table('active', [['Job'], ['Workflow'], ['Status'], ['Submitted', false, 'ascending'], ['Time', true], ['']], shown.active.map(activeRow))
        : empty('workflow', 'No active jobs', 'Nothing is queued or running.')}
    </section>
    <section class="pk-stack" id="recent">
      <h2 class="cu-heading">Recently finished</h2>
      ${shown.recent.length
        ? table('recent', [['Job'], ['Workflow'], ['Status'], ['Finished', false, 'descending'], ['Duration', true], ['Outputs', true]], shown.recent.map(recentRow))
        : empty('history', 'No finished jobs yet')}
    </section>`);
  if (focused !== undefined) [...$('jobs').querySelectorAll('button[data-cancel]')].find((button) => button.dataset.cancel === focused)?.focus();
}

// --------------------------------------------------------------------- actions

async function cancel(button) {
  const id = button.dataset.cancel;
  const { workflow } = shown.active.find((job) => job.prompt_id === id);
  cancelling.add(id);
  button.disabled = true;  // until answered: a second click would ask again
  try {
    if (!await confirm(`Cancel the job ${short(id)} (${workflow})? A running job is interrupted on its ComfyUI server.`,
      { title: 'Cancel job', confirmLabel: 'Cancel job', danger: true })) return;
    await api(`${BASE}jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
    toast(`Job ${short(id)} is cancelled`, { kind: 'ok' });
  } catch {
    // shown by api()
  } finally {
    cancelling.delete(id);
    refresh();  // loaded anew either way: a refusal means the row was out of date
  }
}

// ---------------------------------------------------------------------- wiring

document.addEventListener('refresh', refresh);
$('jobs').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-cancel]');
  // not the second click of a double click: on a table drawn anew in between it would hit the button now in its place
  if (button && !button.disabled && event.detail < 2) cancel(button);
});

refresh();
