// Context Engineer: the compactions of the session open in the chat (or the one a link names, ?session_id=), with what
// its stores hold and its core memory facts -- or the compactions of all sessions.
import { api, html, render, icon, session } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);
const pinned = new URLSearchParams(location.search).get('session_id');

let scope = 'session';
let drawn = null;
let load = 0;
let busy = false;

/** The session asked about: null for all sessions -- and in session scope when none is open. */
const scoped = () => (scope === 'session' ? pinned || session.id : null);

const LAYERS = {
  T: ['T', 'info', 'Pre-Layer T: a new tool result larger than its share of the context window was stored on arrival'],
  P: ['P', 'accent', 'Pre-Layer P: over the message limit, the oldest messages were removed'],
  M: ['M', 'info', 'Media: only the newest messages with media keep it'],
  B: ['B', 'danger', 'Byte limit: the request was over its size limit'],
  1: ['L1', 'ok', 'Layer 1, reversible: tool results and attached files stored, inline media evicted'],
  2: ['L2', 'warn', 'Layer 2, semi-reversible: old messages archived with summaries'],
  3: ['L3', 'danger', 'Layer 3, irreversible: very old messages dropped'],
};

const number = (value) => Number(value ?? 0).toLocaleString();
const time = (seconds) => new Date(seconds * 1000).toLocaleString();
const megabytes = (bytes) => `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
const media = (event) => (event.media_always_compacted || 0) + (event.media_deduplicated || 0) + (event.media_compacted_after_event || 0);
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value, sub = '') => html`<div class="pk-stat" data-stat="${key}">
  <div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div>${sub ? html`<div class="pk-muted ce-sub">${sub}</div>` : ''}
</div>`;
const layer = (name) => {
  const [label, kind, title] = LAYERS[name] || [String(name), '', `Layer ${name}`];
  return html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}" title="${title}">${label}</span>`;
};

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while the last answer is on its way would only discard it
  const id = scoped();
  const mine = ++load;
  if (scope === 'session' && !id) {
    busy = false;
    show(null, null);
    render($('stats'), empty('message-square', 'No session open', 'Open a session in the chat, or show all sessions.'));
    return;
  }
  busy = true;
  let history;
  let stores;
  try {
    [history, stores] = await Promise.all([
      api(`${BASE}history${id ? `?session_id=${encodeURIComponent(id)}` : ''}`, { quiet: true }),
      id ? api(`${BASE}session?session_id=${encodeURIComponent(id)}`, { quiet: true }) : null,
    ]);
  } catch (error) {
    if (mine === load) {  // nothing of the scope shown before stays, as if it were this one's
      show(null, null);
      render($('stats'), empty('circle-alert', 'The context engineering could not be loaded', error.message));
    }
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;  // another session or scope was asked for since
  show(history, stores);
}

function show(history, stores) {
  $('eventsSection').hidden = !history;
  $('factsSection').hidden = !stores;
  $('shown').textContent = history && history.events.length < history.stats.events
    ? `the newest ${history.events.length} of ${number(history.stats.events)}` : '';
  $('shown').hidden = !$('shown').textContent;
  const key = JSON.stringify([scope, history, stores]);
  if (key === drawn) return;  // unchanged: nothing is drawn anew
  drawn = key;
  if (!history) {
    render($('events'), '');
    render($('facts'), '');
    return;
  }
  drawStats(history.stats, stores);
  render($('events'), history.events.length ? table(history.events) : empty('layers', 'No compaction yet'));
  render($('facts'), !stores ? '' : stores.core_memory.facts.length ? facts(stores.core_memory.facts)
    : html`<div class="pk-muted">No facts stored</div>`);
}

function drawStats(stats, stores) {
  const kinds = stats.media_always_compacted + stats.media_deduplicated + stats.media_compacted_after_event;
  render($('stats'), [
    stat('events', 'Compactions', number(stats.events)),
    stat('saved', 'Tokens saved', number(stats.tokens_saved)),
    stat('reduction', 'Average reduction', stats.average_reduction === null ? '–' : `${stats.average_reduction.toFixed(1)}%`),
    stat('media', 'Media compacted', number(kinds), kinds
      ? `${number(stats.media_always_compacted)} window · ${number(stats.media_deduplicated)} duplicates · ${number(stats.media_compacted_after_event)} after events` : ''),
    stores && [
      stat('tool_results', 'Stored tool results', number(stores.tool_results.count), `${number(stores.tool_results.tokens)} tokens`),
      stat('archived', 'Archived messages', number(stores.archived.count), `${number(stores.archived.tokens)} tokens`),
      stat('facts', 'Core memory facts', number(stores.core_memory.facts.length),
        `${number(stores.core_memory.tokens)} / ${number(stores.core_memory.max_tokens)} tokens`),
    ],
  ]);
}

function table(events) {
  const all = scope === 'all';
  // sorted by Time until the viewer picks a column: the server sends the newest first
  return html`<div class="pk-table-wrap"><table class="pk-table ce-table" data-pk-sort="compactions">
    <thead><tr><th aria-sort="descending">Time</th><th>Agent</th>${all ? html`<th>Session</th>` : ''}<th class="pk-num">Before</th><th class="pk-num">After</th>
      <th class="pk-num">Saved</th><th class="pk-num">Reduction</th><th class="pk-num" title="Tool results moved to the store">Stored</th>
      <th class="pk-num">Media</th><th data-pk-nosort>Layers</th></tr></thead>
    <tbody>${events.map((event) => html`<tr>
      <td class="pk-mono" data-sort-value="${event.timestamp ?? ''}">${time(event.timestamp)}</td>
      <td class="ce-clip" title="${event.agent_name || ''}">${event.agent_name || '–'}</td>
      ${all ? html`<td class="pk-mono ce-clip" title="${event.session_id || ''}">${event.session_id || '–'}</td>` : ''}
      <td class="pk-num" data-sort-value="${event.original_tokens ?? ''}">${number(event.original_tokens)}</td>
      <td class="pk-num" data-sort-value="${event.final_tokens ?? ''}">${number(event.final_tokens)}</td>
      <td class="pk-num" data-sort-value="${event.tokens_saved ?? ''}">${number(event.tokens_saved)}</td>
      <td class="pk-num" data-sort-value="${event.reduction_percent ?? ''}">${Number(event.reduction_percent ?? 0).toFixed(1)}%</td>
      <td class="pk-num" data-sort-value="${event.tool_results_stored || ''}">${event.tool_results_stored ? number(event.tool_results_stored) : '–'}</td>
      <td class="pk-num" data-sort-value="${media(event) || ''}">${media(event) ? `${number(media(event))}${event.media_bytes_saved ? ` (${megabytes(event.media_bytes_saved)})` : ''}` : '–'}</td>
      <td><span class="pk-row ce-layers">${(event.layers_applied || []).map(layer)}</span></td>
    </tr>`)}</tbody>
  </table></div>`;
}

function facts(list) {
  return html`<div class="pk-table-wrap"><table class="pk-table ce-facts" data-pk-sort="facts">
    <thead><tr><th>Category</th><th class="pk-num">Importance</th><th>Fact</th></tr></thead>
    <tbody>${list.map((fact) => html`<tr>
      <td><span class="pk-badge">${fact.category}</span></td>
      <td class="pk-num">${Number(fact.importance).toFixed(2)}</td>
      <td class="ce-fact">${fact.content}</td>
    </tr>`)}</tbody>
  </table></div>`;
}

// ---------------------------------------------------------------------- wiring

document.querySelectorAll('[data-scope]').forEach((button) => button.addEventListener('click', () => {
  if (scope === button.dataset.scope) return;
  scope = button.dataset.scope;
  document.querySelectorAll('[data-scope]').forEach((other) => other.setAttribute('aria-pressed', String(other === button)));
  refresh();
}));
document.addEventListener('refresh', refresh);
session.onChange(() => {
  if (scope === 'session' && !pinned) refresh();
});

refresh();
