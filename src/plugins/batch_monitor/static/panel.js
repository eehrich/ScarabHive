// Batch Queues: requests waiting to be batched, the jobs running at the providers and those just finished.
import { api, html, render, emptyState, stat, status } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);

let load = 0;
let busy = false;

// A job's status in the kit's set, for its icon and tone; its own word stays the label.
const STATUSES = {
  pending: 'pending',
  submitted: 'running',
  validating: 'running',
  in_progress: 'running',
  finalizing: 'running',
  cancelling: 'running',  // not over yet: the provider is still at it
  completed: 'success',
  failed: 'failed',
  expired: 'failed',
  cancelled: 'canceled',
};
const FINISHED = ['completed', 'failed', 'expired', 'cancelled'];

const number = (value) => value.toLocaleString();
const badge = (name) => status(STATUSES[name] || 'pending', { label: name.replace('_', ' ') });

function duration(seconds) {
  if (seconds === null) return '—';
  const tenths = Math.round(seconds * 10) / 10;
  if (tenths < 60) return `${tenths}s`;
  const whole = Math.round(tenths);  // rounded before it is split: 119.7 s is 2m 0s, not 1m 60s
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${whole % 60}s`;
  return `${Math.floor(whole / 3600)}h ${Math.floor((whole % 3600) / 60)}m`;
}

function tokens(count) {
  if (!count) return '—';
  if (count < 1000) return String(count);
  const thousands = Math.round(count / 100) / 10;  // rounded before the unit is picked: 999,960 is 1.00M, not 1000.0K
  if (thousands < 1000) return `${thousands.toFixed(1)}K`;
  return `${(count / 1000000).toFixed(2)}M`;
}

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while a load is on its way would queue another one behind it
  const mine = ++load;
  busy = true;
  let queues;
  let metrics;
  try {
    [queues, metrics] = await Promise.all([api(`${BASE}queues`, { quiet: true }), api(`${BASE}metrics`, { quiet: true })]);
  } catch (error) {
    if (mine !== load) return;  // a later load was started since
    // nothing shown before stays, as if it were current
    render($('stats'), '');
    $('updated').textContent = '';
    render($('queues'), error.status === 503
      ? emptyState('layers', 'Batch processing is off', error.message)
      : emptyState('circle-alert', 'Batch queues could not be loaded', error.message));
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;
  drawStats(queues.queues, metrics);
  drawQueues(queues);
  $('updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

function drawStats(queues, metrics) {
  const running = queues.flatMap((queue) => queue.jobs).filter((job) => !FINISHED.includes(job.status)).length;
  const time = metrics.processing_time;
  render($('stats'), [
    stat('Queues', number(queues.length), { key: 'queues' }),
    stat('Jobs running', number(running), { key: 'running' }),
    stat('Jobs completed', number(metrics.completed_jobs), { key: 'jobs', sub: `${number(metrics.failed_jobs)} failed` }),
    stat('Requests completed', number(metrics.total_completed_requests), { key: 'requests', sub: `${number(metrics.total_failed_requests)} failed` }),
    stat('Processing time', duration(time.mean), { key: 'time', sub: time.mean === null ? '' : `min ${duration(time.min)} · max ${duration(time.max)}` }),
  ]);
}

// ---------------------------------------------------------------------- queues

function jobRow(queue, job) {
  return html`<tr data-job="${job.job_id}">
    <td class="pk-mono">${queue.queue_key}</td>
    <td class="pk-mono pk-muted" title="${job.provider_job_id || ''}">${job.job_id}</td>
    <td>${badge(job.status)}</td>
    <td class="pk-num" data-sort-value="${job.total_requests ? job.completed_count : ''}">${job.total_requests ? `${number(job.completed_count)} / ${number(job.total_requests)}` : '—'}${
      job.failed_count ? html` <span class="bm-failed">${number(job.failed_count)} failed</span>` : ''}</td>
    <td class="pk-num" data-sort-value="${job.estimated_input_tokens || ''}">${tokens(job.estimated_input_tokens)}</td>
    <td class="pk-num" data-sort-value="${job.elapsed_seconds}">${duration(job.elapsed_seconds)}</td>
  </tr>`;
}

// requests collected for the next job: it is submitted when the collection window closes; nothing done, nothing elapsed
function pendingRow(queue, window) {
  return html`<tr data-pending="${queue.queue_key}">
    <td class="pk-mono">${queue.queue_key}</td>
    <td class="pk-muted">—</td>
    <td>${badge('pending')}</td>
    <td class="pk-num" data-sort-value="">${number(queue.pending_requests)} waiting</td>
    <td class="pk-num" data-sort-value="${queue.pending_estimated_tokens || ''}">${tokens(queue.pending_estimated_tokens)}</td>
    <td class="pk-num pk-muted" data-sort-value="" title="Collection window">~${window}s</td>
  </tr>`;
}

function drawQueues({ queues, collection_window_seconds: window }) {
  // the server lists the queues by key: the Queue column's order until the viewer picks another
  render($('queues'), queues.length
    ? html`<div class="pk-table-wrap"><table class="pk-table" data-pk-sort="jobs">
        <thead><tr><th aria-sort="ascending">Queue</th><th>Job</th><th>Status</th><th class="pk-num">Progress</th><th class="pk-num">Input tokens</th><th class="pk-num">Time</th></tr></thead>
        <tbody>${queues.map((queue) => [
          queue.jobs.map((job) => jobRow(queue, job)),
          queue.pending_requests ? pendingRow(queue, window) : '',
        ])}</tbody>
      </table></div>`
    : emptyState('layers', 'No batch jobs', 'Nothing is waiting, running or finished in the last three minutes.'));
}

// ---------------------------------------------------------------------- wiring

document.addEventListener('refresh', refresh);
refresh();
