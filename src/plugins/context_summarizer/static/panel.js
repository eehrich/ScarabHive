// Context Summarizer: the summarization runs of the session open in the chat (or the one a link names, ?session_id=),
// or of all sessions -- applied, rejected or skipped, with the messages a run replaced and the summaries it wrote.
import { api, html, render, icon, confirm, session } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);

/** The events shown, newest first, or null: no session open, or they could not be loaded. */
let events = null;
let drawn = null;
let load = 0;
let busy = false;
let opening = 0;
/** The run the drawer shows: its row gets the focus back on close, also when redrawn meanwhile. */
let opened = null;
let clearing = false;

const STATUSES = {
  success: { label: 'applied', kind: 'ok' },
  rejected: { label: 'rejected', kind: 'warn' },
  skipped: { label: 'skipped', kind: '' },
};
const REASONS = {
  insufficient_old_messages: 'too few older messages',
  insufficient_potential_reduction: 'the older messages are too small to reach the minimum reduction',
  insufficient_reduction: 'the summaries fell short of the minimum reduction',
};

const number = (value) => Number(value ?? 0).toLocaleString();
const percent = (ratio) => `${(Number(ratio ?? 0) * 100).toFixed(1)}%`;
const time = (stamp) => new Date(stamp).toLocaleString();
const status = (event) => STATUSES[event.status] || { label: event.status, kind: '' };
const reason = (event) => REASONS[event.reason] || event.reason || '';
const badge = (event) => html`<span class="pk-badge${status(event).kind ? ` pk-badge--${status(event).kind}` : ''}">${status(event).label}</span>`;
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value, sub = '') => html`<div class="pk-stat" data-stat="${key}">
  <div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div>${sub ? html`<div class="pk-muted cs-sub">${sub}</div>` : ''}
</div>`;

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while the last answer is on its way would only discard it
  const id = session.shown;
  const mine = ++load;
  if (session.scope === 'session' && !id) {
    busy = false;
    show(null);
    render($('stats'), empty('message-square', 'No session open', 'Open a session in the chat, or show all sessions.'));
    return;
  }
  busy = true;
  let answer;
  try {
    answer = await api(`${BASE}history${id ? `?session_id=${encodeURIComponent(id)}` : ''}`, { quiet: true });
  } catch (error) {
    if (mine === load) {  // nothing of the scope shown before stays, as if it were this one's
      show(null);
      render($('stats'), empty('circle-alert', 'The history could not be loaded', error.message));
    }
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;  // another session or scope was asked for since
  show(answer.events);
  drawStats(answer.stats);
}

function drawStats(stats) {
  render($('stats'), [
    stat('events', 'Runs', number(stats.events),
      stats.events ? `${stats.applied} applied · ${stats.rejected} rejected · ${stats.skipped} skipped` : ''),
    stat('saved', 'Tokens saved', number(stats.tokens_saved)),
    stat('summarized', 'Messages summarized', number(stats.messages_summarized)),
    stat('reduction', 'Average reduction', stats.average_reduction === null ? '–' : percent(stats.average_reduction),
      stats.applied ? 'of the runs applied' : ''),
  ]);
  $('shown').textContent = events.length < stats.events ? `the newest ${events.length} of ${stats.events} runs` : '';
  $('shown').hidden = !$('shown').textContent;
}

// ---------------------------------------------------------------------- events

function show(list) {
  events = list;
  if (!list) $('shown').hidden = true;
  const key = JSON.stringify([session.scope, list]);
  if (key === drawn) return;  // unchanged: the rows, and the focus on one, stay
  drawn = key;
  const focused = document.activeElement?.closest?.('#events tr[data-id]')?.dataset.id;
  render($('events'), !list ? '' : !list.length ? empty('scroll-text', 'No summarization yet') : table(list));
  if (focused) $('events').querySelector(`tr[data-id="${CSS.escape(focused)}"]`)?.focus();
}

function table(list) {
  const all = session.scope === 'all';
  // newest first, as the server lists them, until the viewer picks another order
  return html`<div class="pk-table-wrap"><table class="pk-table cs-table" data-pk-sort="runs">
    <thead><tr><th aria-sort="descending">Time</th><th>Run</th>${all ? html`<th>Session</th>` : ''}<th class="pk-num">Messages</th>
      <th class="pk-num">Tokens</th><th class="pk-num">Saved</th><th class="pk-num">Reduction</th></tr></thead>
    <tbody>${list.map((event) => html`<tr tabindex="0" data-id="${event.id}" data-status="${event.status}">
      <td class="pk-mono" data-sort-value="${event.timestamp}">${time(event.timestamp)}</td>
      <td data-sort-value="${status(event).label}">${badge(event)}${event.status !== 'success' && event.reason ? html` <span class="pk-muted cs-reason">${reason(event)}</span>` : ''}</td>
      ${all ? html`<td class="pk-mono cs-session" title="${event.session_id || ''}">${event.session_id || '–'}</td>` : ''}
      <td class="pk-num" data-sort-value="${event.original_message_count}">${number(event.original_message_count)} → ${number(event.summarized_message_count)}</td>
      <td class="pk-num" data-sort-value="${event.original_tokens || ''}">${event.original_tokens ? html`${number(event.original_tokens)} → ${number(event.new_tokens)}` : '–'}</td>
      <td class="pk-num" data-sort-value="${event.tokens_saved}">${number(event.tokens_saved)}</td>
      <td class="pk-num" data-sort-value="${event.status === 'skipped' ? '' : event.reduction_ratio}">${event.status === 'skipped' ? '–' : percent(event.reduction_ratio)}</td>
    </tr>`)}</tbody>
  </table></div>`;
}

// ---------------------------------------------------------------------- detail

const messages = (list) => (list?.length
  ? list.map((message) => html`<div class="cs-message">
      <div class="cs-role">${message.role}${message.name ? ` · ${message.name}` : ''}</div>
      <div class="cs-content">${message.content}</div>
    </div>`)
  : html`<div class="pk-muted">None recorded</div>`);

async function openEvent(id) {
  const mine = ++opening;
  let event;
  try {
    event = await api(`${BASE}events/${encodeURIComponent(id)}`);
  } catch {
    return;  // api() has shown the failure
  }
  if (mine !== opening) return;  // another run was opened since
  const chunks = event.summary_stats?.total_chunks;
  const facts = [
    ['Run', badge(event)], event.reason && ['Reason', reason(event)],
    ['Session', event.session_id || '–'], ['Request', event.request_id || '–'],
    ['Messages', `${number(event.original_message_count)} → ${number(event.summarized_message_count)} (${number(event.messages_summarized)} summarized)`],
    ['Tokens', `${number(event.original_tokens)} → ${number(event.new_tokens)} (${number(event.tokens_saved)} saved)`],
    ['Reduction', event.status === 'skipped' ? '–' : percent(event.reduction_ratio)],
    chunks && ['Chunks', `${number(event.summary_stats.successful_chunks)} of ${number(chunks)} summarized${event.summary_stats.failed_chunks ? `, ${number(event.summary_stats.failed_chunks)} failed` : ''}`],
  ].filter(Boolean);
  $('detailTitle').textContent = `${status(event).label} · ${time(event.timestamp)}`;
  render($('detailBody'), html`<dl class="pk-kv">${facts.map(([label, value]) => html`<dt>${label}</dt><dd>${value}</dd>`)}</dl>
    <h3 class="cs-heading">Summaries <span class="pk-muted">${(event.after_messages || []).length}</span></h3>
    <div class="pk-stack" data-part="after">${messages(event.after_messages)}</div>
    <h3 class="cs-heading">Messages summarized <span class="pk-muted">${(event.before_messages || []).length}</span></h3>
    <div class="pk-stack" data-part="before">${messages(event.before_messages)}</div>`);
  $('detailBody').scrollTop = 0;
  opened = id;
  if (!$('detail').open) $('detail').showModal();
}

// --------------------------------------------------------------------- actions

async function clearHistory() {
  if (clearing) return;
  clearing = true;
  const button = $('clearButton');
  try {
    if (!await confirm('Forget every summarization run of every session? New runs are recorded again.',
      { title: 'Clear the summarization history', confirmLabel: 'Clear', danger: true })) return;
    const had = document.activeElement === button;
    button.disabled = true;
    await api(`${BASE}clear`, { method: 'POST' }).catch(() => {});  // api() shows a failure
    await refresh();
    button.disabled = false;
    if (had && document.activeElement === document.body) button.focus();  // disabled, the button had let go of it
  } finally {
    clearing = false;
  }
}

// ---------------------------------------------------------------------- wiring

document.addEventListener('refresh', refresh);
document.addEventListener('sessionscope', refresh);  // another session, or all of them: <pk-session> says when
$('clearButton').addEventListener('click', clearHistory);
$('events').addEventListener('click', (event) => {
  const row = event.target.closest('tr[data-id]');
  if (row) openEvent(row.dataset.id);
});
$('events').addEventListener('keydown', (event) => {
  const row = event.target.closest('tr[data-id]');
  if (row && (event.key === 'Enter' || event.key === ' ')) {
    event.preventDefault();
    openEvent(row.dataset.id);
  }
});
$('detail').addEventListener('close', () => {
  // while modal the focus is in the drawer; the row that opened it may have been drawn anew, no longer there to take it back
  $('events').querySelector(`tr[data-id="${CSS.escape(String(opened))}"]`)?.focus();
});
$('detail').addEventListener('click', (event) => {
  const drawer = $('detail');
  const box = drawer.getBoundingClientRect();
  const outside = event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
  if (event.target.closest('[data-close]') || (event.target === drawer && outside)) drawer.close();
});

refresh();
