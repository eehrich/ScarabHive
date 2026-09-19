// Sub-Agents: the sub-agents this manager spawned in the session open in the chat (or the one a link names,
// ?session_id=) -- what each is doing, its transcript, and archiving one.
import { api, html, render, icon, confirm, session } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);
/** Messages per page loaded above the transcript's tail: fewer once the server has capped a page. */
let page = 50;

/** The sub-agents listed, or null: no session open, or they could not be loaded. */
let agents = null;
let phase = null;
/** The session the list shown belongs to: an action acts on it, whatever the chat has switched to since. */
let shownFor = null;
let load = 0;
let busy = false;
/** The sub-agents an archive is asked or on its way for: their button stays disabled across redraws. */
const archiving = new Set();
/** The transcript in the drawer: {id, session, data, messages, start}. Its card gets the focus back on close. */
let opened = null;
let opening = 0;

const STATES = {
  active: { label: 'Active', kind: 'ok' },  // stored: running or idle, which only the list's activity tells
  running: { label: 'Running', kind: 'info' },
  idle: { label: 'Idle', kind: 'ok' },
  interrupted: { label: 'Interrupted', kind: 'warn' },
  archived: { label: 'Archived', kind: '' },
  cancelled: { label: 'Cancelled', kind: '' },
  failed: { label: 'Failed', kind: 'danger' },
};
const OPEN = ['running', 'idle', 'interrupted'];
const OVER = ['completed', 'cancelled', 'canceled', 'failed', 'error'];  // an activity naming one is over (as the server judges it)

/** Running, idle or the stored status: an active sub-agent runs while it reports an activity that is not over. */
function state(agent) {
  if (agent.status === 'running' || agent.status === 'pending') return 'running';
  if (agent.status !== 'active') return agent.status;
  const activity = (agent.current_activity || '').toLowerCase();
  return activity && !OVER.some((word) => activity.includes(word)) ? 'running' : 'idle';
}

const described = (name) => STATES[name] || { label: name, kind: '' };
const number = (value) => Number(value ?? 0).toLocaleString();
const time = (stamp) => (stamp ? new Date(stamp).toLocaleString() : '–');
const badge = (kind, content) => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${content}</span>`;
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value) =>
  html`<div class="pk-stat" data-stat="${key}"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${number(value)}</div></div>`;

function ago(stamp) {
  if (!stamp) return 'never';
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(stamp)) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while the last answer is on its way would only discard it
  const id = session.shown;
  const mine = ++load;
  if (!id) {
    busy = false;
    show(null, null, null);
    render($('stats'), empty('message-square', 'No session open', 'Open a session in the chat to see its sub-agents.'));
    return;
  }
  busy = true;
  let answer;
  try {
    answer = await api(`${BASE}sub-agents?session_id=${encodeURIComponent(id)}`, { quiet: true });
  } catch (error) {
    if (mine === load) {  // nothing of the session shown before stays, as if it were this one's
      show(null, null, null);
      render($('stats'), empty('circle-alert', 'Sub-agents could not be loaded', error.message));
    }
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;  // another session was asked for since
  show(answer.instances, answer.phase, id);
  const count = (name) => agents.filter((agent) => state(agent) === name).length;
  render($('stats'), [
    stat('total', 'Sub-agents', agents.length),
    stat('running', 'Running', count('running')),
    stat('idle', 'Idle', count('idle')),
    stat('interrupted', 'Interrupted', count('interrupted')),
    stat('closed', 'Archived or ended', agents.filter((agent) => !OPEN.includes(state(agent))).length),
  ]);
}

function show(list, phaseInfo, sessionId) {
  agents = list;
  phase = phaseInfo;
  shownFor = sessionId;
  drawPhase();
  drawAgents();
}

function drawPhase() {
  $('phase').hidden = !phase;
  if (!phase) return;
  const names = (list) => (list.includes('*') ? 'every registered agent' : list.join(', ') || 'none');
  const narrowed = names(phase.agents) !== names(phase.allowed_agents);
  render($('phase'), html`Phase ${badge('accent', phase.current || 'not set')}
    <span>spawnable: <span class="pk-mono" data-part="agents">${names(phase.agents)}</span></span>
    ${narrowed ? html`<span>of <span class="pk-mono" data-part="allowed">${names(phase.allowed_agents)}</span></span>` : ''}`);
}

// ---------------------------------------------------------------------- agents

function card(agent) {
  const name = state(agent);
  const { label, kind } = described(name);
  const id = agent.instance_id;
  // what the tool archives from; one archived, cancelled or failed has nothing to archive
  const archivable = agent.status === 'active' || agent.status === 'interrupted';
  return html`<article class="pk-card sa-agent" data-id="${id}" data-state="${name}">
    <div class="pk-card-head">
      ${badge(kind, html`${name === 'running' ? html`<span class="pk-spinner sa-spinner"></span>` : ''}${label}`)}
      <h3 class="pk-card-title pk-mono pk-grow sa-id">${id}</h3>
      ${badge('', agent.agent_type)}
    </div>
    <div class="pk-row pk-muted sa-meta">
      <span>${number(agent.message_count)} messages</span>
      <span title="${time(agent.last_used)}">used ${ago(agent.last_used)}</span>
    </div>
    ${agent.task_summary ? html`<p class="sa-task">${agent.task_summary}</p>` : ''}
    ${agent.current_activity ? html`<div class="sa-activity">${icon('activity', { size: 'sm' })}<span>${agent.current_activity}</span></div>` : ''}
    <div class="pk-row sa-actions">
      <button type="button" class="pk-btn pk-btn--sm" data-act="transcript" data-id="${id}">${icon('scroll-text', { size: 'sm' })} Transcript</button>
      ${archivable ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--ghost" data-act="archive" data-id="${id}"
        ${archiving.has(id) ? 'disabled' : ''}>${icon('trash-2', { size: 'sm' })} Archive</button>` : ''}
    </div>
  </article>`;
}

const button = (act, id) => $('agents').querySelector(`button[data-act="${act}"][data-id="${CSS.escape(id)}"]`);

function drawAgents() {
  const focused = document.activeElement?.closest?.('#agents button[data-act]');  // drawn anew, it keeps the focus
  const filter = $('show').value;
  const listed = (agents || []).filter((agent) => filter === 'all'
    || (filter === 'running' ? state(agent) === 'running' : OPEN.includes(state(agent))));
  render($('agents'), !agents ? '' : listed.length ? listed.map(card)
    : agents.length ? empty('filter', 'No sub-agent matches', `${agents.length} hidden by the filter`)
      : empty('workflow', 'No sub-agents in this session'));
  // only when this frame has the focus: gone to the shell, the frame's activeElement is its body
  if (focused) button(focused.dataset.act, focused.dataset.id)?.focus();
}

// --------------------------------------------------------------------- actions

async function archive(id) {
  const sessionId = shownFor;  // the session of the card clicked
  archiving.add(id);
  drawAgents();  // disabled until answered: a second click would ask again
  try {
    if (!await confirm(`Archive the sub-agent “${id}”? Its history stays, and its coordinator can still continue it.`,
      { title: 'Archive sub-agent', confirmLabel: 'Archive', danger: true })) return;
    // api() shows a failure; loaded anew either way
    await api(`${BASE}sub-agents/${encodeURIComponent(id)}?session_id=${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
      .catch(() => {});
    await refresh();
  } finally {
    archiving.delete(id);
    drawAgents();
    // disabled, the button had let go of the focus: back to it, to its card archived, or to the filter with the card gone
    if (document.hasFocus() && document.activeElement === document.body) (button('archive', id) || button('transcript', id) || $('show')).focus();
  }
}

// ------------------------------------------------------------------ transcript

const entry = (message) => html`<div class="sa-message" data-index="${message.index}">
  <div class="sa-role">${message.role}${message.tool_name ? ` · ${message.tool_name}` : ''}</div>
  ${message.content ? html`<div class="sa-content">${message.content}</div>` : ''}
  ${(message.tool_calls || []).map((call) => html`<div class="sa-call">${call.name || '?'}(${call.arguments || ''})</div>`)}
</div>`;

function drawTranscript() {
  const { data, messages, start } = opened;
  $('detailTitle').textContent = data.instance_id;
  render($('detailBody'), html`<dl class="pk-kv">
      <dt>Session</dt><dd class="pk-mono">${opened.session}</dd>
      <dt>Agent</dt><dd>${data.agent_type}</dd>
      <dt>Status</dt><dd>${described(data.status).label}</dd>
      <dt>Created</dt><dd>${time(data.created_at)}</dd>
      <dt>Last used</dt><dd>${time(data.last_used)}</dd>
      <dt>Task</dt><dd>${data.task_summary || '–'}</dd>
    </dl>
    <h3 class="sa-heading">Transcript <span class="pk-muted" data-part="shown">${messages.length < data.message_count
      ? `the last ${number(messages.length)} of ${number(data.message_count)} messages` : `${number(messages.length)} messages`}</span></h3>
    ${start ? html`<button type="button" class="pk-btn pk-btn--sm" data-act="earlier">${icon('arrow-up', { size: 'sm' })} Earlier messages</button>` : ''}
    <div class="pk-stack" data-part="messages">${messages.length ? messages.map(entry) : html`<div class="pk-muted">No messages</div>`}</div>`);
}

async function openTranscript(id) {
  const mine = ++opening;
  const sessionId = shownFor;
  let data;
  try {
    data = await api(`${BASE}sub-agents/${encodeURIComponent(id)}?session_id=${encodeURIComponent(sessionId)}`);
  } catch {
    return;  // api() has shown the failure
  }
  if (mine !== opening || sessionId !== session.shown) return;  // another opened since, or the session left
  opened = { id, session: sessionId, data, messages: data.messages, start: data.window.start_index };
  drawTranscript();
  if (!$('detail').open) $('detail').showModal();
  $('detailBody').scrollTop = $('detailBody').scrollHeight;  // the tail: the newest at the bottom
}

async function loadEarlier(control) {
  const mine = opening;
  const { id, session: sessionId, start } = opened;
  control.disabled = true;  // once
  let offset;
  let data;
  for (;;) {
    offset = Math.max(0, start - page);
    try {
      data = await api(`${BASE}sub-agents/${encodeURIComponent(id)}?session_id=${encodeURIComponent(sessionId)}&offset=${offset}&limit=${start - offset}`);
    } catch {
      if (mine === opening) control.disabled = false;
      return;
    }
    if (mine !== opening) return;  // the drawer shows another one by now
    const { returned } = data.window;
    if (!returned || returned >= start - offset) break;
    page = returned;  // capped by the server's info_max_limit: ask the page that ends where the shown ones start
  }
  const body = $('detailBody');
  const below = body.scrollHeight - body.scrollTop;  // what was read stays where it was
  opened = { ...opened, messages: [...data.messages, ...opened.messages], start: offset };
  drawTranscript();
  body.scrollTop = body.scrollHeight - below;
  (body.querySelector('[data-act="earlier"]') || body).focus();  // drawn anew, the button had let go of the focus
}

// ---------------------------------------------------------------------- wiring

document.addEventListener('refresh', refresh);
// an open transcript stays: it names its session, and closing it would take the focus from the chat
document.addEventListener('sessionscope', refresh);
$('show').addEventListener('change', drawAgents);
$('agents').addEventListener('click', (event) => {
  const control = event.target.closest('button[data-act]');
  // not the second click of a double click: on a card drawn anew in between it would hit the button now in its place
  if (!control || event.detail > 1) return;
  if (control.dataset.act === 'archive') archive(control.dataset.id);
  else openTranscript(control.dataset.id);
});
$('detailBody').addEventListener('click', (event) => {
  const control = event.target.closest('button[data-act="earlier"]');
  if (control) loadEarlier(control);
});
$('detail').addEventListener('close', () => {
  if (opened) button('transcript', opened.id)?.focus();  // the card may have been drawn anew meanwhile
});
$('detail').addEventListener('click', (event) => {
  const drawer = $('detail');
  const box = drawer.getBoundingClientRect();
  const outside = event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
  if (event.target.closest('[data-close]') || (event.target === drawer && outside)) drawer.close();
});

refresh();
