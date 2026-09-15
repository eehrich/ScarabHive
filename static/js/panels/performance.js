// Performance panel (admin): running and slowest requests, time per route, event loop lag, async tasks, threads.
// Everything comes from one GET /debug/profile; collecting garbage and resetting the stats are POSTs, asked first.
import { api, html, render, icon, confirm, toast } from '/static/kit/panel-kit.js';

const $ = (id) => document.getElementById(id);
const REPORT = '/debug/profile';

const CARDS = ['active', 'slow', 'routes', 'tasks', 'threads'];
/**
 * What each card was drawn from, as a key; empty when nothing is drawn. Lag, memory and how long a request
 * has been running change on every answer: the stats row is drawn each time and running times are set in
 * place, while a card is built anew -- losing a selection in it -- only when what it lists changed.
 */
let drawn = {};
let load = 0;
let busy = false;

const number = (value) => Number(value).toLocaleString();
const ms = (value) => (value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`);
const mb = (value) => (typeof value === 'number' ? `${Math.round(value)} MB` : '—');
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value, kind = '') =>
  html`<div class="pk-stat" data-stat="${key}"><div class="pk-stat-label">${label}</div><div class="pk-stat-value${kind ? ` pk-${kind}` : ''}">${value}</div></div>`;
const card = (key, title, body, count = null) => html`<div class="pk-card" data-card="${key}">
  <div class="pk-card-head"><h3 class="pk-card-title">${title}</h3>${count === null ? '' : html`<span class="pk-badge">${count}</span>`}</div>
  ${body}</div>`;
const table = (head, rows) => html`<div class="pk-table-wrap"><table class="pk-table">
  <thead><tr>${head.map(([label, numeric]) => html`<th class="${numeric ? 'pk-num' : ''}">${label}</th>`)}</tr></thead>
  <tbody>${rows}</tbody></table></div>`;

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while a load is on its way would queue another one behind it
  const mine = ++load;
  busy = true;
  let report;
  try {
    report = await api(REPORT, { quiet: true });
  } catch (error) {
    if (mine !== load) return;  // a later load was started since
    // nothing shown before stays, as if it were current
    drawn = {};
    $('actions').hidden = true;
    render($('stats'), '');
    CARDS.forEach((name) => render($(`card-${name}`), ''));
    $('updated').textContent = '';
    render($('notice'), error.status === 403 ? empty('shield', 'Administrators only')
      : error.status === 401 ? empty('shield', 'Not signed in', error.message)
      : error.status === 404 ? empty('gauge', 'Profiling is off', error.message)
      : empty('circle-alert', 'Performance data could not be loaded', error.message));
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;
  $('updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  $('actions').hidden = false;
  render($('notice'), '');
  // this panel's own request for the report is always running while it is made: not shown, not a change
  report.requests.active = report.requests.active.filter((request) => request.path !== REPORT);
  draw(report);
}

// -------------------------------------------------------------------- drawing

function draw(report) {
  const { requests, async_tasks: tasks, event_loop: loop, memory, threads } = report;
  const slow = report.thresholds.slow_request_ms;
  const lagKind = (value) => (value >= 100 ? 'error' : '');
  render($('stats'), [
    stat('requests', 'Running requests', number(requests.active.length)),
    stat('tasks', 'Async tasks', number(tasks.total_count)),
    stat('lag', 'Event loop lag', ms(loop.current_ms), lagKind(loop.current_ms)),
    stat('peak', 'Peak lag', ms(loop.max_ms), lagKind(loop.max_ms)),
    stat('memory', 'Memory (RSS)', mb(memory.rss_mb)),
    stat('threads', 'Threads', `${number(threads.active_count)} / ${number(threads.count)} active`),
  ]);

  // the report this panel polls counts itself on every answer: its row goes last, and its numbers are set in place
  const byPath = Object.entries(requests.stats_by_path);
  const routes = [...byPath.filter(([route]) => route !== REPORT).sort(([, a], [, b]) => b.count * b.avg_ms - a.count * a.avg_ms),
    ...byPath.filter(([route]) => route === REPORT)];
  const routeNumbers = (s) => [number(s.count), ms(s.avg_ms), ms(s.min_ms), ms(s.max_ms),
    s.slow_count ? `${number(s.slow_count)} (${s.slow_pct.toFixed(1)} %)` : '0'];
  const tasksTitle = () => (tasks.tasks.length < tasks.total_count ? `Async tasks (first ${number(tasks.tasks.length)})` : 'Async tasks');
  const running = (r) => html`<td class="pk-num${r.duration_ms > slow ? ' pk-error' : ''}" data-running>${ms(r.duration_ms)}</td>`;
  const cards = {
    // the running times change on every answer: not part of the key, set in place below
    active: [[slow, requests.active.map((r) => [r.id, r.method, r.path])], () => card('active', 'Running requests', requests.active.length
      ? table([['Method'], ['Path'], ['Running for', true]], requests.active.map((r) => html`<tr data-request="${r.id}">
          <td class="pk-mono">${r.method}</td><td class="pk-mono">${r.path}</td>${running(r)}</tr>`))
      : empty('circle-check', 'Nothing running'))],
    slow: [[slow, requests.slow_recent], () => card('slow', `Slowest requests over ${ms(slow)}`, requests.slow_recent.length
      ? table([['Method'], ['Path'], ['Status', true], ['Took', true]], requests.slow_recent.map((r) => html`<tr>
          <td class="pk-mono">${r.method}</td><td class="pk-mono">${r.path}</td>
          <td class="pk-num">${r.status ?? '—'}</td><td class="pk-num">${ms(r.duration_ms)}</td></tr>`))
      : empty('circle-check', 'No slow requests recorded'))],
    routes: [[routes.map(([route, s]) => (route === REPORT ? route : [route, s]))], () => card('routes', 'Time per route', routes.length
      ? table([['Route'], ['Requests', true], ['Average', true], ['Min', true], ['Max', true], ['Slow', true]],
        routes.map(([route, s]) => html`<tr data-route="${route}">
          <td class="pk-mono">${route}</td>${routeNumbers(s).map((value, i) => html`<td class="pk-num${i === 4 && s.slow_count ? ' pk-error' : ''}">${value}</td>`)}</tr>`))
      : empty('history', 'No requests counted yet'))],
    // a task is started per request, under a new name each time: the card is keyed on the coroutines
    // listed (the server sorts by them before it cuts the list); count and names are set in place
    tasks: [tasks.tasks.map((t) => t.coro), () => card('tasks', tasksTitle(),
      tasks.tasks.length
        ? table([['Name'], ['Coroutine']], tasks.tasks.map((t) => html`<tr><td class="pk-mono">${t.name}</td><td class="pk-mono">${t.coro}</td></tr>`))
        : empty('circle-check', 'No async tasks'), number(tasks.total_count))],
    threads: [threads, () => card('threads', 'Threads', table([['Name'], ['State'], ['Daemon']], threads.threads.map((t) => html`<tr data-thread="${t.name}">
        <td class="pk-mono">${t.name}</td>
        <td>${t.idle ? html`<span class="pk-badge" title="${t.idle_reason}">idle</span>` : html`<span class="pk-badge pk-badge--info">active</span>`}</td>
        <td>${t.daemon ? 'yes' : 'no'}</td></tr>`)), number(threads.count))],
  };
  for (const [name, [what, build]] of Object.entries(cards)) {
    const key = JSON.stringify(what);
    if (key === drawn[name]) continue;  // unchanged: this card stays as it is
    drawn[name] = key;
    render($(`card-${name}`), build());
  }
  for (const r of requests.active) {
    const cell = $('card-active').querySelector(`tr[data-request="${CSS.escape(r.id)}"] [data-running]`);
    cell.textContent = ms(r.duration_ms);
    cell.classList.toggle('pk-error', r.duration_ms > slow);
  }
  const own = requests.stats_by_path[REPORT];
  const ownRow = own && $('card-routes').querySelector(`tr[data-route="${CSS.escape(REPORT)}"]`);
  if (ownRow) {
    routeNumbers(own).forEach((value, i) => { ownRow.children[i + 1].textContent = value; });
    ownRow.children[5].classList.toggle('pk-error', own.slow_count > 0);
  }
  const taskCard = $('card-tasks');
  taskCard.querySelector('.pk-card-title').textContent = tasksTitle();
  taskCard.querySelector('.pk-card-head .pk-badge').textContent = number(tasks.total_count);
  taskCard.querySelectorAll('tbody tr').forEach((row, i) => { row.children[0].textContent = tasks.tasks[i].name; });
}

// --------------------------------------------------------------------- actions

const ACTIONS = {
  reset: {
    question: 'Forget the time per route and the slowest requests? Running requests stay.',
    options: { title: 'Reset stats', confirmLabel: 'Reset stats', danger: true },
    path: '/debug/profile/reset',
    done: () => 'Stats reset',
  },
};

async function act(button) {
  const action = ACTIONS[button.dataset.act];
  const focused = document.activeElement === button;
  button.disabled = true;  // until drawn anew: a second click would ask again
  try {
    if (!await confirm(action.question, action.options)) return;
    toast(action.done(await api(action.path, { method: 'POST' })), { kind: 'ok' });
  } catch {
    // shown by api()
  } finally {
    await refresh();
    button.disabled = false;
    // disabled, the button let go of the focus -- also when the dialog closed and wanted to give it back
    if (focused && (document.activeElement === document.body || document.activeElement === null)) button.focus();
  }
}

// ---------------------------------------------------------------------- wiring

document.addEventListener('refresh', refresh);
$('actions').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-act]');
  // not the second click of a double click: the first one already asks
  if (button && !button.disabled && event.detail < 2) act(button);
});

refresh();
