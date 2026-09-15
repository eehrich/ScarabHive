// Message Debugger: the messages agents send to their LLM (turns) and the raw provider requests behind them.
// Opened from a chat answer (?request_id=) or a session (?session_id=), both lists start filtered to it; a
// request takes the calls under it along (tool calls, sub-agents).
import { api, html, render, icon, jsonView, trusted, alert, confirm, toast } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const PAGE = 50;
const MOST = 500;  // the API's largest page: a list shows at most the newest 500 entries its filters match
const $ = (id) => document.getElementById(id);

/**
 * Each list: the rows it shows, the total its filters match, how many it wants (a page more per "Load more"),
 * the number of its latest load and whether one is on its way.
 */
const lists = {
  turns: { path: 'turns', field: 'turns', rows: [], total: 0, want: PAGE, load: 0, busy: false, drawn: '' },
  requests: { path: 'llm-requests', field: 'requests', rows: [], total: 0, want: PAGE, load: 0, busy: false, drawn: '' },
};
let activeTab = 'turns';
let statsLoad = 0;
let statsBusy = false;
/** The entry the drawer shows ({tab, id}) -- still marked in its list once the drawer is closed. */
let shown = null;
let detailLoad = 0;
/** What the copy buttons of the drawer copy, by index. */
let copies = [];

const params = new URLSearchParams(location.search);
$('filterSession').value = params.get('session_id') || '';
$('filterRequest').value = params.get('request_id') || '';

// ------------------------------------------------------------------ formatting

const number = (value) => (value === null || value === undefined ? '' : Number(value).toLocaleString());
const duration = (ms) => (!ms ? '' : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);
const head = (id) => (id ? id.slice(0, 8) : '');

/** A request id, short: its first characters and, for a call under a request (`<id>_001`, `<id>_sub_...`), the rest. */
function requestLabel(id) {
  if (!id) return '';
  const under = id.indexOf('_');
  if (under < 0) return head(id);
  return `${id.slice(0, Math.min(under, 8))}${under > 8 ? '…' : ''}${id.slice(under)}`;
}

function time(ms, full = false) {
  if (!ms) return '';
  const date = new Date(ms);
  const clock = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  if (full) return `${date.toLocaleDateString()} ${clock}.${String(Math.floor(ms) % 1000).padStart(3, '0')}`;
  if (date.toDateString() === new Date().toDateString()) return clock;
  return `${date.toLocaleDateString([], { day: '2-digit', month: '2-digit' })} ${clock}`;
}

/** Share of the prompt read from the provider's cache: OpenAI-style usage counts it in prompt_tokens, Anthropic's beside input_tokens. */
function cached(usage) {
  if (!usage) return '';
  const read = usage.prompt_tokens_details?.cached_tokens ?? usage.cache_read_input_tokens ?? 0;
  const prompt = usage.prompt_tokens
    ?? (usage.input_tokens ?? 0) + (usage.cache_read_input_tokens ?? 0) + (usage.cache_creation_input_tokens ?? 0);
  return read > 0 && prompt > 0 ? `${Math.round((read / prompt) * 100)}%` : '';
}

const cost = (usage) => (typeof usage?.cost === 'number' ? `$${usage.cost.toFixed(4)}` : '');

function parseJson(text) {
  try { return JSON.parse(text); } catch { return undefined; }
}

const isTree = (value) => value !== null && typeof value === 'object';

const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;

const stat = (label, value) =>
  html`<div class="pk-stat"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div></div>`;

// ---------------------------------------------------------------- statistics

/** A select's choices; a chosen value the statistics no longer name (pruned away) stays chosen. */
function fillOptions(select, values) {
  const chosen = select.value;
  const all = chosen && !values.includes(chosen) ? [chosen, ...values] : values;
  const key = JSON.stringify(all);
  if (select.dataset.options === key) return;  // drawing an open select anew would close it
  select.dataset.options = key;
  const first = select.options[0].textContent;
  render(select, [html`<option value="">${first}</option>`,
    all.map((value) => html`<option value="${value}" ${value === chosen ? trusted('selected') : ''}>${value}</option>`)]);
}

async function loadStats({ auto = false } = {}) {
  if (auto && statsBusy) return;  // a tick while the last answer is on its way would only discard it
  const load = ++statsLoad;
  statsBusy = true;
  let stats;
  try {
    stats = await api(`${BASE}stats`, { quiet: true });
  } catch (error) {
    if (load === statsLoad) render($('stats'), empty('circle-alert', 'Statistics could not be loaded', error.message));
    return;
  } finally {
    if (load === statsLoad) statsBusy = false;
  }
  if (load !== statsLoad) return;
  render($('stats'), [
    stat('Turns', number(stats.total_turns)),
    stat('LLM requests', number(stats.total_llm_requests)),
    stat('Sessions', number(stats.unique_session_count)),
    stat('Agents', number(stats.unique_agents.length)),
    stat('Errors', number(stats.error_count)),
    stat('Database', `${number(stats.db_size_mb)} MB`),
  ]);
  fillOptions($('filterAgent'), stats.unique_agents);
  fillOptions($('filterProvider'), stats.unique_providers);
}

// --------------------------------------------------------------------- lists

function query(tab, limit) {
  const filters = {
    agent_name: $('filterAgent').value,
    session_id: $('filterSession').value.trim(),
    request_id: $('filterRequest').value.trim(),
    ...(tab === 'turns'
      ? { snapshot_type: $('filterType').value }
      : { provider: $('filterProvider').value, direction: $('filterDirection').value }),
  };
  const search = new URLSearchParams({ limit: String(limit) });
  Object.entries(filters).forEach(([name, value]) => { if (value) search.set(name, value); });
  return search;
}

const filtered = (tab) => [...query(tab, 1).keys()].length > 1;
const selected = (tab, id) => String(shown !== null && shown.tab === tab && shown.id === id);

const snapshotBadge = (type) => (type === 'pre_llm'
  ? html`<span class="pk-badge pk-badge--info">${icon('arrow-up', { size: 'sm' })} input</span>`
  : html`<span class="pk-badge pk-badge--ok">${icon('arrow-down', { size: 'sm' })} output</span>`);

const directionBadge = (entry) => (entry.direction === 'request'
  ? html`<span class="pk-badge pk-badge--info">${icon('arrow-up', { size: 'sm' })} request</span>`
  : html`<span class="pk-badge pk-badge--ok">${icon('arrow-down', { size: 'sm' })} response</span>`);

const reason = (text) => html`<span class="md-reason" title="${text}">${text}</span>`;

function status(entry) {
  if (entry.direction === 'request') return '';
  if (entry.finish_reason === 'retry') {
    const [, attempt = '', why = entry.error || ''] = /^\[RETRY (\d+\/\d+)\]\s*(.*)$/s.exec(entry.error || '') || [];
    return html`<span class="pk-badge pk-badge--warn">retry ${attempt}</span> ${reason(why)}`;
  }
  if (entry.error) return html`<span class="pk-badge pk-badge--danger">error</span> ${reason(entry.error)}`;
  return entry.finish_reason ? html`<span class="pk-badge">${entry.finish_reason}</span>` : '';
}

const requestCell = (id) => html`<td class="pk-mono" title="${id}">${requestLabel(id)}</td>`;

function turnRows(rows) {
  return html`<div class="pk-table-wrap"><table class="pk-table md-entries">
    <thead><tr><th>Time</th><th>Type</th><th>Agent</th><th class="pk-num">Step</th><th class="pk-num">Messages</th>
      <th class="pk-num">Tokens</th><th class="pk-num">Cached</th><th class="pk-num">Cost</th><th>Session</th><th>Request</th></tr></thead>
    <tbody>${rows.map((turn) => html`<tr tabindex="0" data-id="${turn.id}" aria-selected="${selected('turns', turn.id)}">
      <td class="pk-mono" title="${time(turn.timestamp_ms, true)}">${time(turn.timestamp_ms)}</td>
      <td>${snapshotBadge(turn.snapshot_type)}</td>
      <td>${turn.agent_name}</td>
      <td class="pk-num">${turn.step}</td>
      <td class="pk-num">${number(turn.message_count)}</td>
      <td class="pk-num">${number(turn.total_tokens)}</td>
      <td class="pk-num">${cached(turn.usage_json)}</td>
      <td class="pk-num">${cost(turn.usage_json)}</td>
      <td class="pk-mono" title="${turn.session_id}">${head(turn.session_id)}</td>
      ${requestCell(turn.request_id)}
    </tr>`)}</tbody>
  </table></div>`;
}

function requestRows(rows) {
  return html`<div class="pk-table-wrap"><table class="pk-table md-entries">
    <thead><tr><th>Time</th><th>Direction</th><th>Agent</th><th>Model</th><th class="pk-num">Duration</th>
      <th class="pk-num">Tokens</th><th class="pk-num">Cost</th><th>Status</th><th>Request</th></tr></thead>
    <tbody>${rows.map((entry) => html`<tr tabindex="0" data-id="${entry.id}" aria-selected="${selected('requests', entry.id)}">
      <td class="pk-mono" title="${time(entry.timestamp_ms, true)}">${time(entry.timestamp_ms)}</td>
      <td>${directionBadge(entry)}</td>
      <td>${entry.agent_name}</td>
      <td><span class="pk-muted">${entry.provider}</span> ${entry.model}${entry.served_by ? html` <span class="pk-muted md-served-by">via ${entry.served_by}</span>` : ''}${entry.is_streaming ? html` <span class="pk-badge">stream</span>` : ''}</td>
      <td class="pk-num">${duration(entry.duration_ms)}</td>
      <td class="pk-num">${number(entry.usage_json?.total_tokens)}</td>
      <td class="pk-num">${cost(entry.usage_json)}</td>
      <td>${status(entry)}</td>
      ${requestCell(entry.request_id)}
    </tr>`)}</tbody>
  </table></div>`;
}

function draw(tab) {
  const list = lists[tab];
  // unchanged rows are not drawn anew: a refresh keeps the row the keyboard is on
  const drawn = JSON.stringify([list.total, list.rows.map((row) => row.id)]);
  if (drawn === list.drawn) return;
  list.drawn = drawn;
  $(`${tab}Count`).textContent = number(list.total);
  if (!list.rows.length) {
    render($(tab), filtered(tab)
      ? empty('filter', 'Nothing matches the filters')
      : tab === 'turns'
        ? empty('history', 'No turns captured yet', 'The messages agents send to their LLM show up here.')
        : empty('history', 'No LLM requests captured yet', 'The raw requests to the LLM providers and their responses show up here.'));
    return;
  }
  const more = list.rows.length < Math.min(list.total, MOST);
  render($(tab), html`${tab === 'turns' ? turnRows(list.rows) : requestRows(list.rows)}
    <div class="pk-row md-footer">
      <span class="pk-muted">${number(list.rows.length)} of ${number(list.total)}</span>
      ${more ? html`<button type="button" class="pk-btn pk-btn--sm" data-more>Load more</button>` : ''}
      ${!more && list.total > list.rows.length ? html`<span class="pk-muted">Narrow the filters to reach older entries</span>` : ''}
    </div>`);
  if (shown?.tab === tab && $('detail').open) updateSteps();
}

/**
 * Load the newest entries of a list, as many as it wants -- from the top each time, so entries captured
 * meanwhile neither repeat nor push others out of reach; `more` wants a page more. With `countOnly` just its
 * total, for the count on the tab not shown. A tick of the auto refresh skips a list still loading: with
 * answers slower than the tick, every answer would be outdated on arrival.
 */
async function loadList(tab, { more = false, countOnly = false, auto = false } = {}) {
  const list = lists[tab];
  if (auto && list.busy) return;
  if (more) list.want = Math.min(MOST, list.want + PAGE);
  const load = ++list.load;
  list.busy = true;
  let data;
  try {
    data = await api(`${BASE}${list.path}?${query(tab, countOnly ? 1 : list.want)}`, { quiet: true });
  } catch (error) {
    if (load === list.load && !countOnly) {
      list.drawn = '';
      render($(tab), empty('circle-alert', 'Could not be loaded', error.message));
    }
    return;
  } finally {
    if (load === list.load) list.busy = false;
  }
  if (load !== list.load) return;  // a newer load -- other filters, a refresh -- draws this list
  if (countOnly) {
    $(`${tab}Count`).textContent = number(data.total);
    return;
  }
  list.rows = data[list.field];
  list.total = data.total;
  draw(tab);
}

const otherTab = () => (activeTab === 'turns' ? 'requests' : 'turns');

function refresh(event) {
  const auto = Boolean(event?.detail?.auto);
  loadStats({ auto });
  loadList(activeTab, { auto });
  loadList(otherTab(), { countOnly: true, auto });
}

/** Filters changed: the lists start over from their first page, and show nothing of the filters before. */
function startOver(tabs) {
  for (const tab of tabs) {
    Object.assign(lists[tab], { rows: [], want: PAGE, drawn: '' });
    $(`${tab}Count`).textContent = '';
    render($(tab), html`<span class="pk-skeleton"></span>`);
    loadList(tab, { countOnly: tab !== activeTab });
  }
}

// -------------------------------------------------------------------- drawer

const copyButton = (value, label = 'Copy as JSON') => html`<button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm"
  data-copy="${copies.push(value) - 1}" aria-label="${label}" title="${label}">${icon('copy')}</button>`;

const section = (title, value, body) => html`<section class="pk-stack md-section">
  <div class="pk-row"><h3 class="pk-card-title pk-grow">${title}</h3>${copyButton(value)}</div>
  ${body}
</section>`;

const facts = (pairs) => html`<dl class="pk-kv">${pairs
  .filter(([, value]) => value !== '' && value !== null && value !== undefined)
  .map(([label, value]) => html`<dt>${label}</dt><dd>${value}</dd>`)}</dl>`;

const idFact = (kind, id) => (id ? html`<span class="pk-row md-id"><span class="pk-mono">${id}</span>
  <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-filter="${kind}" data-value="${id}"
    aria-label="Show only this ${kind}" title="Show only this ${kind}">${icon('filter')}</button></span>` : '');

const ROLE_BADGES = { system: 'pk-badge--accent', user: 'pk-badge--info', assistant: 'pk-badge--ok' };
// fields a message card shows in its own place; every other field of the snapshot is listed generically
const MESSAGE_FIELDS = new Set(['index', 'role', 'content', 'content_length', 'estimated_tokens',
  'tool_calls', 'tool_call_count', 'tool_call_id', 'is_tool_result']);

function messageContent(message) {
  const { content } = message;
  if (content === null || content === undefined || content === '') return '';
  if (typeof content !== 'string') return jsonView(content);  // the parts of a multimodal message
  const tree = message.is_tool_result ? parseJson(content) : undefined;
  return isTree(tree) ? jsonView(tree) : html`<pre class="pk-code">${content}</pre>`;
}

function toolCall(call) {
  const args = call.function?.arguments;
  const tree = typeof args === 'string' ? parseJson(args) : args;
  return html`<div class="md-tool-call">
    <div class="pk-row">${icon('wrench', { size: 'sm' })}<span class="pk-mono">${call.function?.name || '?'}</span>
      ${call.id ? html`<span class="pk-mono pk-muted">${call.id}</span>` : ''}</div>
    ${isTree(tree) ? jsonView(tree) : html`<pre class="pk-code">${args ?? ''}</pre>`}
  </div>`;
}

function messageCard(message) {
  const extras = Object.entries(message).filter(([key, value]) => !MESSAGE_FIELDS.has(key) && value !== null && value !== undefined);
  return html`<article class="md-message" data-role="${message.role || ''}">
    <div class="pk-row">
      <span class="pk-badge ${ROLE_BADGES[message.role] || ''}">${message.role || 'unknown'}</span>
      ${message.is_tool_result ? html`<span class="pk-badge">tool result</span>` : ''}
      ${message.tool_call_id ? html`<span class="pk-mono pk-muted">${message.tool_call_id}</span>` : ''}
      <span class="pk-grow"></span>
      ${message.content_length ? html`<span class="pk-muted">${number(message.content_length)} chars</span>` : ''}
      ${message.estimated_tokens ? html`<span class="pk-muted">${number(message.estimated_tokens)} tokens</span>` : ''}
      ${copyButton(message, 'Copy message as JSON')}
    </div>
    ${messageContent(message)}
    ${(message.tool_calls || []).map(toolCall)}
    ${extras.some(([, value]) => !isTree(value))
      ? html`<div class="pk-row">${extras.filter(([, value]) => !isTree(value)).map(([key, value]) => html`<span class="pk-badge">${key}: ${String(value)}</span>`)}</div>`
      : ''}
    ${extras.filter(([, value]) => isTree(value)).map(([key, value]) => html`<details class="md-extra"><summary>${key}</summary>${jsonView(value)}</details>`)}
  </article>`;
}

const entryName = (tab, id) => `${tab === 'turns' ? 'Turn' : 'LLM request log entry'} #${id}`;

function drawTurn(turn) {
  const messages = turn.messages_json || [];
  $('detailTitle').textContent = `${entryName('turns', turn.id)} · ${turn.snapshot_type === 'pre_llm' ? 'input' : 'output'} · ${turn.agent_name}`;
  render($('detailBody'), html`
    ${facts([
      ['Agent', turn.agent_name], ['Step', turn.step], ['Messages', number(turn.message_count)],
      ['Tokens', number(turn.total_tokens)], ['Context window', number(turn.context_window)],
      ['Time', time(turn.timestamp_ms, true)], ['Session', idFact('session', turn.session_id)],
      ['Request', idFact('request', turn.request_id)],
    ])}
    ${messages.length ? section(`Messages (${messages.length})`, messages, html`<div class="pk-stack">${messages.map(messageCard)}</div>`) : ''}
    ${turn.llm_response_json ? section('LLM response', turn.llm_response_json, jsonView(turn.llm_response_json)) : ''}`);
}

function drawRequest(entry) {
  $('detailTitle').textContent = `${entry.direction === 'request' ? 'Request' : 'Response'} #${entry.id} · ${entry.provider}/${entry.model}`;
  render($('detailBody'), html`
    ${facts([
      ['Agent', entry.agent_name], ['Provider', entry.provider], ['Served by', entry.served_by], ['Model', entry.model],
      ['URL', entry.url],
      ['Streaming', entry.is_streaming ? 'yes' : 'no'], ['Duration', duration(entry.duration_ms)],
      ['Finish reason', entry.finish_reason], ['Time', time(entry.timestamp_ms, true)],
      ['Session', idFact('session', entry.session_id)], ['Request', idFact('request', entry.request_id)],
    ])}
    ${entry.error ? section('Error', entry.error, html`<pre class="pk-code md-error-text">${entry.error}</pre>`) : ''}
    ${entry.usage_json ? section('Usage', entry.usage_json, jsonView(entry.usage_json)) : ''}
    ${entry.payload_json ? section('Request payload', entry.payload_json, jsonView(entry.payload_json)) : ''}
    ${entry.response_json ? section('Response data', entry.response_json, jsonView(entry.response_json)) : ''}`);
}

function markSelected() {
  for (const tab of Object.keys(lists)) {
    $(tab).querySelectorAll('tr[data-id]').forEach((row) => row.setAttribute('aria-selected', selected(tab, Number(row.dataset.id))));
  }
}

function updateSteps() {
  const { rows } = lists[shown.tab];
  const index = rows.findIndex((row) => row.id === shown.id);
  $('detail').querySelector('[data-step="-1"]').disabled = index <= 0;
  $('detail').querySelector('[data-step="1"]').disabled = index < 0 || index >= rows.length - 1;
}

async function openDetail(tab, id) {
  const load = ++detailLoad;
  shown = { tab, id };
  markSelected();
  const drawer = $('detail');
  if (!drawer.open) {
    $('detailTitle').textContent = '';
    render($('detailBody'), html`<span class="pk-skeleton"></span>`);
    drawer.showModal();
  }
  updateSteps();
  let entry;
  try {
    entry = await api(`${BASE}${lists[tab].path}/${id}`, { quiet: true });
  } catch (error) {
    if (load === detailLoad) {
      $('detailTitle').textContent = entryName(tab, id);
      render($('detailBody'), empty('circle-alert', 'Could not be loaded', error.message));
    }
    return;
  }
  if (load !== detailLoad) return;  // a later entry was asked for
  copies = [];
  if (tab === 'turns') drawTurn(entry);
  else drawRequest(entry);
  $('detailBody').scrollTop = 0;
}

function step(delta) {
  const { rows } = lists[shown.tab];
  const next = rows[rows.findIndex((row) => row.id === shown.id) + delta];
  if (next) openDetail(shown.tab, next.id);
}

async function copy(value) {
  try {
    await navigator.clipboard.writeText(typeof value === 'string' ? value : JSON.stringify(value, null, 2));
    toast('Copied', { kind: 'ok' });
  } catch (error) {
    toast(`Could not copy: ${error.message}`, { kind: 'error' });
  }
}

// ------------------------------------------------------------------- actions

async function prune() {
  const asked = await confirm(
    'Strip the oldest raw payloads and drop the oldest turn snapshots until the database is well below its size cap, '
    + 'then compact the file. Cost data is kept. On a large database this takes minutes.',
    { title: 'Prune the database', confirmLabel: 'Prune' });
  if (!asked) return;
  const button = $('actionsButton');
  button.disabled = true;
  toast('Pruning the database…');
  let result;
  try {
    result = await api(`${BASE}prune?vacuum=true`, { method: 'POST' });
  } catch {
    return;  // api() has shown the failure
  } finally {
    button.disabled = false;
  }
  loadStats();
  startOver(Object.keys(lists));
  const done = [`Stripped ${number(result.stripped)} payloads`, `dropped ${number(result.turns_deleted)} turns`];
  if (result.requests_deleted) done.push(`deleted ${number(result.requests_deleted)} old cost rows`);
  const file = result.vacuum_error ? `Compacting the file failed: ${result.vacuum_error}`
    : result.vacuumed ? `The file was compacted: ${number(result.freed_mb)} MB freed, ${number(result.size_after_mb)} MB now.`
      : 'The file had nothing to compact.';
  alert(`${done.join(', ')}. ${file}`, { title: 'Database pruned' });  // stays until read: the prune took its time
}

async function clearAll() {
  const asked = await confirm('Delete every captured turn and LLM request log, cost history included? This cannot be undone.',
    { title: 'Clear all captured data', confirmLabel: 'Clear all', danger: true });
  if (!asked) return;
  let result;
  try {
    result = await api(`${BASE}clear`, { method: 'DELETE' });
  } catch {
    return;  // api() has shown the failure
  }
  toast(`Deleted ${number(result.turns_deleted)} turns and ${number(result.requests_deleted)} LLM request logs`, { kind: 'ok' });
  if ($('detail').open) $('detail').close();
  loadStats();
  startOver(Object.keys(lists));
}

// -------------------------------------------------------------------- wiring

document.querySelector('[data-pk-tabs]').addEventListener('tabchange', (event) => {
  activeTab = event.detail.tab;
  loadList(activeTab);
});
document.addEventListener('refresh', refresh);

$('filterAgent').addEventListener('change', () => startOver(Object.keys(lists)));
for (const id of ['filterSession', 'filterRequest']) {
  let typing = null;
  $(id).addEventListener('input', () => {
    clearTimeout(typing);
    typing = setTimeout(() => startOver(Object.keys(lists)), 300);
  });
}
$('filterType').addEventListener('change', () => startOver(['turns']));
$('filterProvider').addEventListener('change', () => startOver(['requests']));
$('filterDirection').addEventListener('change', () => startOver(['requests']));

for (const tab of Object.keys(lists)) {
  $(tab).addEventListener('click', (event) => {
    if (event.target.closest('[data-more]')) {
      loadList(tab, { more: true });
      return;
    }
    const row = event.target.closest('tr[data-id]');
    if (row) openDetail(tab, Number(row.dataset.id));
  });
  $(tab).addEventListener('keydown', (event) => {
    const row = event.target.closest('tr[data-id]');
    if (row && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      openDetail(tab, Number(row.dataset.id));
    }
  });
}

$('detail').addEventListener('click', (event) => {
  const drawer = $('detail');
  if (event.target === drawer) {
    const box = drawer.getBoundingClientRect();
    const outside = event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
    if (outside) drawer.close();  // a click on the backdrop
    return;
  }
  const button = event.target.closest('button');
  if (!button) return;
  if (button.hasAttribute('data-close')) drawer.close();
  else if (button.dataset.copy !== undefined) copy(copies[Number(button.dataset.copy)]);
  else if (button.dataset.step) step(Number(button.dataset.step));
  else if (button.dataset.filter) {
    // a request lies in one session: the session filter shows all of its requests, the request filter one
    $('filterSession').value = button.dataset.filter === 'session' ? button.dataset.value : '';
    $('filterRequest').value = button.dataset.filter === 'request' ? button.dataset.value : '';
    drawer.close();
    startOver(Object.keys(lists));
  }
});

$('actions').addEventListener('click', (event) => {
  const item = event.target.closest('[data-action]');
  if (!item) return;
  $('actions').hidePopover();
  if (item.dataset.action === 'prune') prune();
  else clearAll();
});

refresh();
