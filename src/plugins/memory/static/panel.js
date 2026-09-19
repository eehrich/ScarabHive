// Memory: the long-term memories the agents keep for the session open in the chat (or the one a link names, ?session_id=).
import { api, html, render, icon, confirm, session } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);

/** The memories shown, or null: no session open, or they could not be loaded. */
let memories = null;
/** The session the memories shown belong to. */
let shownFor = null;
/** The search the memories shown answer, '' for the list. */
let searched = '';
let drawn = null;
let load = 0;
let busy = false;
/** The memory the drawer shows: its row gets the focus back on close, or the row now in its place. */
let opened = null;
let deleting = false;

const number = (value) => Number(value ?? 0).toLocaleString();
const percent = (ratio) => `${Math.round(ratio * 100)}%`;
const time = (stamp) => new Date(stamp).toLocaleString();
const badge = (kind, content) => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${content}</span>`;
const importance = (value) => badge(value >= 8 ? 'warn' : value >= 5 ? 'info' : '', `${value}/10`);
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value) =>
  html`<div class="pk-stat" data-stat="${key}"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div></div>`;
const rows = () => [...$('memories').querySelectorAll('tr[data-memory]')];

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while the last answer is on its way would only discard it
  const id = session.shown;
  const query = searched;
  const mine = ++load;
  if (!id) {
    busy = false;
    show(null, null, '');
    $('searchForm').hidden = true;
    render($('stats'), empty('message-square', 'No session open', 'Open a session in the chat to see the memories its agents keep.'));
    return;
  }
  $('searchForm').hidden = false;
  busy = true;
  // a tick keeps the results of a search and brings only the figures: each search embeds its query anew
  const again = !(event?.detail?.auto && query && shownFor === id);
  const scope = `session_id=${encodeURIComponent(id)}`;
  let stats;
  let answer;
  try {
    [stats, answer] = await Promise.all([
      api(`${BASE}stats?${scope}`, { quiet: true }),
      !again ? null
        : query ? api(`${BASE}memories/search?${scope}`, { method: 'POST', json: { query }, quiet: true })
          : api(`${BASE}memories?${scope}`, { quiet: true }),
    ]);
  } catch (error) {
    if (mine === load) {  // nothing of the session or search shown before stays, as if it were this one's
      show(null, null, '');
      render($('stats'), empty('circle-alert', query ? 'The search failed' : 'Memories could not be loaded', error.message));
    }
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;  // another session or search was asked for since
  if (answer) show(query ? answer.results : answer.memories, id, query, answer.total);
  drawStats(stats);
}

function drawStats(stats) {
  render($('stats'), [
    stat('total', 'Memories', number(stats.total_memories)),
    stat('importance', 'Average importance', stats.avg_importance === null ? '–' : stats.avg_importance.toFixed(1)),
    stat('accesses', 'Accesses', number(stats.total_accesses)),
  ]);
}

// -------------------------------------------------------------------- memories

function show(list, sessionId, query, total = 0) {
  const sameSession = sessionId === shownFor;  // memory ids repeat across sessions: mem_004 of another is another memory
  memories = list;
  shownFor = sessionId;
  $('shown').textContent = !list ? ''
    : query ? `${list.length} found for “${query}”`
      : list.length < total ? `The ${list.length} most recently accessed of ${number(total)} memories` : '';
  $('shown').hidden = !$('shown').textContent;
  const key = JSON.stringify([sessionId, query, list]);
  if (key === drawn) return;  // unchanged: the rows, and the focus on one, stay
  drawn = key;
  const focused = document.activeElement?.closest?.('#memories tr[data-memory]');
  const place = focused ? rows().indexOf(focused) : -1;
  render($('memories'), !list ? ''
    : list.length ? table(list, query)
      : empty('database', query ? 'No memory matches the search' : 'No memories in this session'));
  if (focused) focusRow(sameSession ? focused.dataset.memory : null, place);
}

/** The row of the memory, or the one now in its place when it is gone (or of another session: id null). */
function focusRow(id, place) {
  const now = rows();
  (now.find((row) => row.dataset.memory === id) ?? now[Math.min(place, now.length - 1)] ?? $('query')).focus();
}

function table(list, query) {
  // sorted by the last column until the viewer picks one: the server sends the most recently accessed, or the closest, first
  return html`<div class="pk-table-wrap"><table class="pk-table mm-table" data-pk-sort="memories">
    <thead><tr><th>Memory</th><th class="pk-num">Importance</th><th class="pk-num">Accesses</th>
      ${query ? html`<th class="pk-num" aria-sort="descending">Match</th>` : html`<th aria-sort="descending">Last accessed</th>`}</tr></thead>
    <tbody>${list.map((memory) => html`<tr tabindex="0" data-memory="${memory.memory_id}">
      <td class="mm-memory" data-sort-value="${memory.title}">
        <div class="mm-title">${memory.title}</div>
        <div class="mm-content">${memory.content}</div>
        <div class="pk-row mm-meta"><span class="pk-mono pk-muted">${memory.memory_id}</span>${memory.keywords.map((word) => badge('', word))}</div>
      </td>
      <td class="pk-num" data-sort-value="${memory.importance ?? ''}">${importance(memory.importance)}</td>
      <td class="pk-num" data-cell="accesses" data-sort-value="${memory.access_count ?? ''}">${number(memory.access_count)}</td>
      ${query ? html`<td class="pk-num" data-cell="match" data-sort-value="${memory.similarity ?? ''}">${percent(memory.similarity)}</td>`
        : html`<td class="pk-mono mm-time" data-sort-value="${Date.parse(memory.accessed_at) || ''}">${time(memory.accessed_at)}</td>`}
    </tr>`)}</tbody>
  </table></div>`;
}

// ---------------------------------------------------------------------- detail

function openMemory(row) {
  const memory = memories.find((one) => one.memory_id === row.dataset.memory);
  opened = { id: memory.memory_id, title: memory.title, session: shownFor, place: rows().indexOf(row) };
  $('detailTitle').textContent = memory.title;
  render($('detailBody'), html`<dl class="pk-kv">
      <dt>ID</dt><dd class="pk-mono">${memory.memory_id}</dd>
      <dt>Importance</dt><dd>${importance(memory.importance)}</dd>
      <dt>Accesses</dt><dd>${number(memory.access_count)}</dd>
      ${memory.similarity === undefined
        ? html`<dt>Created</dt><dd>${time(memory.created_at)}</dd><dt>Last accessed</dt><dd>${time(memory.accessed_at)}</dd>`
        : html`<dt>Match</dt><dd>${percent(memory.similarity)}</dd>`}
      ${memory.keywords.length ? html`<dt>Keywords</dt><dd class="pk-row mm-keywords">${memory.keywords.map((word) => badge('', word))}</dd>` : ''}
    </dl>
    <h3 class="mm-heading">Content</h3>
    <div class="mm-full" data-part="content">${memory.content}</div>`);
  $('detailBody').scrollTop = 0;
  if (!$('detail').open) $('detail').showModal();
}

async function deleteOpened() {
  if (deleting) return;
  deleting = true;
  const { id, title, session: sessionId } = opened;
  const button = $('deleteButton');
  try {
    if (!await confirm(`Delete the memory “${title}”? The agents of the session lose it.`,
      { title: 'Delete memory', confirmLabel: 'Delete', danger: true })) return;
    button.disabled = true;
    // the session of the memory opened, whichever is open by now; api() shows a failure, and a refusal means it was gone
    await api(`${BASE}memories/${encodeURIComponent(id)}?session_id=${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
      .catch(() => {});
    $('detail').close();
    await refresh();
  } finally {
    button.disabled = false;
    deleting = false;
  }
}

// ---------------------------------------------------------------------- wiring

function clearSearch() {
  $('query').value = '';
  searched = '';
  $('clearSearch').hidden = true;
}

document.addEventListener('refresh', refresh);
document.addEventListener('sessionscope', () => {
  // a search belongs to its session; an open drawer stays: closed, it would take the focus from the chat
  // (a dialog gives it back to where it was before it opened), and its Delete still names the memory's own session
  clearSearch();
  refresh();
});
$('searchForm').addEventListener('submit', (event) => {
  event.preventDefault();
  searched = $('query').value.trim();
  $('clearSearch').hidden = !searched;
  refresh();
});
$('clearSearch').addEventListener('click', () => {
  clearSearch();
  $('query').focus();  // hidden, the button had let go of it
  refresh();
});
$('memories').addEventListener('click', (event) => {
  const row = event.target.closest('tr[data-memory]');
  if (row) openMemory(row);
});
$('memories').addEventListener('keydown', (event) => {
  const row = event.target.closest('tr[data-memory]');
  if (row && (event.key === 'Enter' || event.key === ' ')) {
    event.preventDefault();
    openMemory(row);
  }
});
$('deleteButton').addEventListener('click', deleteOpened);
$('detail').addEventListener('close', () => focusRow(opened.session === shownFor ? opened.id : null, opened.place));
$('detail').addEventListener('click', (event) => {
  const drawer = $('detail');
  const box = drawer.getBoundingClientRect();
  const outside = event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
  if (event.target.closest('[data-close]') || (event.target === drawer && outside)) drawer.close();
});

refresh();
