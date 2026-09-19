// Todos: the task list the agents keep for the session open in the chat (or the one a link names, ?session_id=).
import { api, html, render, icon, confirm, session } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);

/** The tasks shown, or null: no session open, or they could not be loaded. */
let tasks = null;
/** The session the tasks shown belong to. */
let shownFor = null;
let load = 0;
let busy = false;

const STATUSES = {
  'not-started': { label: 'Not started', icon: 'square', kind: '' },
  'in-progress': { label: 'In progress', icon: 'loader-circle', kind: 'info' },
  blocked: { label: 'Blocked', icon: 'ban', kind: 'warn' },
  completed: { label: 'Completed', icon: 'circle-check', kind: 'ok' },
  cancelled: { label: 'Cancelled', icon: 'circle-x', kind: '' },
};
const PRIORITIES = { critical: 'danger', high: 'warn', medium: 'info', low: '' };

const number = (value) => value.toLocaleString();
const badge = (kind, content) => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${content}</span>`;
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value, more = '') =>
  html`<div class="pk-stat" data-stat="${key}"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div>${more}</div>`;

function ago(stamp) {
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
    show(null, null);
    render($('stats'), empty('message-square', 'No session open', "Open a session in the chat to see its agents' tasks."));
    return;
  }
  busy = true;
  let listed;
  try {
    listed = await api(`${BASE}tasks?session_id=${encodeURIComponent(id)}`, { quiet: true });
  } catch (error) {
    if (mine === load) {  // nothing of the session shown before stays, as if it were this one's
      show(null, null);
      render($('stats'), empty('circle-alert', 'Tasks could not be loaded', error.message));
    }
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;  // another session was asked for since
  show(listed.tasks, id);
  drawStats();
}

function show(list, sessionId) {
  tasks = list;
  shownFor = sessionId;
  drawTasks();
}

function drawStats() {
  const count = (status) => tasks.filter((task) => task.status === status).length;
  const done = count('completed');
  render($('stats'), [
    stat('total', 'Tasks', number(tasks.length)),
    stat('not-started', 'Not started', number(count('not-started'))),
    stat('in-progress', 'In progress', number(count('in-progress'))),
    stat('blocked', 'Blocked', number(count('blocked'))),
    stat('cancelled', 'Cancelled', number(count('cancelled'))),
    stat('completed', 'Completed',
      html`${number(done)} <span class="pk-muted td-share">${tasks.length ? Math.round((done / tasks.length) * 100) : 0}%</span>`,
      html`<progress class="pk-progress" value="${done}" max="${tasks.length || 1}"></progress>`),
  ]);
}

// ----------------------------------------------------------------------- tasks

function taskCard(task) {
  const status = STATUSES[task.status];
  const button = (act, name, label, ghost = false) =>
    html`<button type="button" class="pk-btn pk-btn--sm${ghost ? ' pk-btn--ghost' : ''}" data-act="${act}" data-task="${task.task_id}">${icon(name, { size: 'sm' })} ${label}</button>`;
  // not started but waiting on another task: started, the server would block it at once; it completes like a blocked one
  const waiting = task.status === 'not-started' && task.is_blocked;
  const actions = [
    task.status === 'not-started' && !waiting && button('start', 'play', 'Start'),
    (task.status === 'in-progress' || task.status === 'blocked' || waiting) && button('complete', 'check', 'Complete'),
    !task.blocks.length && button('delete', 'trash-2', 'Delete', true),  // the server refuses a task others depend on
  ].filter(Boolean);
  return html`<article class="pk-card td-task" data-task="${task.task_id}" data-status="${task.status}">
    <div class="pk-card-head">
      ${badge(status.kind, html`${icon(status.icon, { size: 'sm' })} ${status.label}`)}
      ${waiting ? badge('warn', 'waiting') : ''}
      <span class="pk-mono pk-muted">${task.task_id}</span>
      <h3 class="pk-card-title pk-grow">${task.title}</h3>
      ${badge(PRIORITIES[task.priority], task.priority)}
    </div>
    <div class="pk-row pk-muted td-meta">
      <span>created ${ago(task.created_at)}</span>
      ${task.started_at ? html`<span>started ${ago(task.started_at)}</span>` : ''}
      ${task.tags.map((tag) => badge('', tag))}
    </div>
    ${task.description ? html`<p class="td-description">${task.description}</p>` : ''}
    <div class="pk-row td-progress">
      <progress class="pk-progress pk-grow" value="${task.progress}" max="100"></progress>
      <span class="pk-muted">${task.progress}%</span>
    </div>
    ${task.depends_on.length ? html`<div class="pk-muted">Depends on <span class="pk-mono">${task.depends_on.join(', ')}</span></div>` : ''}
    ${task.blocks.length ? html`<div class="pk-muted">Blocks <span class="pk-mono">${task.blocks.join(', ')}</span></div>` : ''}
    ${actions.length ? html`<div class="pk-row td-actions">${actions}</div>` : ''}
  </article>`;
}

function drawTasks() {
  if (!tasks) {
    $('shown').textContent = '';
    render($('tasks'), '');
    return;
  }
  const status = $('statusFilter').value;
  const priority = $('priorityFilter').value;
  const shown = tasks.filter((task) => (!status || task.status === status) && (!priority || task.priority === priority));
  $('shown').textContent = tasks.length ? `${shown.length} of ${tasks.length} tasks` : '';
  render($('tasks'), shown.length
    ? shown.map(taskCard)
    : empty('list-todo', tasks.length ? 'No task matches the filters' : 'No tasks in this session'));
}

// --------------------------------------------------------------------- actions

async function act(button) {
  const { act: action, task: taskId } = button.dataset;
  // the session and task of the card clicked: the list may be another session's by the time the answer is in
  const sessionId = shownFor;
  const task = tasks.find((one) => one.task_id === taskId);
  if (action === 'delete' && !await confirm(`Delete the task “${task.title}”?`,
    { title: 'Delete task', confirmLabel: 'Delete', danger: true })) return;
  button.disabled = true;  // until the card is drawn anew: a second click would be refused as out of date
  const [path, method] = { start: ['/start', 'POST'], complete: ['/complete', 'POST'], delete: ['', 'DELETE'] }[action];
  // api() shows a failure; loaded anew either way, as a refusal means the card was out of date
  await api(`${BASE}tasks/${encodeURIComponent(taskId)}${path}?session_id=${encodeURIComponent(sessionId)}`, { method })
    .catch(() => {});
  refresh();
}

// ---------------------------------------------------------------------- wiring

document.addEventListener('refresh', refresh);
document.addEventListener('sessionscope', refresh);
$('statusFilter').addEventListener('change', drawTasks);
$('priorityFilter').addEventListener('change', drawTasks);
$('tasks').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-act]');
  // not the second click of a double click: on a card drawn anew in between it would hit the button now in its place
  if (button && event.detail < 2) act(button);
});

refresh();
