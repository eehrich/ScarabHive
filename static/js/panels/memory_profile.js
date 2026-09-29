// Memory Profile: process memory, object counts of the latest snapshot, growth against a baseline, tracemalloc.
// The summary is cheap and read-only, so the auto refresh asks for it. What walks the heap -- a snapshot,
// a baseline -- runs only on a click.
import { api, html, render, icon, confirm, toast, ApiError } from '/static/kit/panel-kit.js';

const BASE = '/debug/memory';
const $ = (id) => document.getElementById(id);

/** The last answer drawn, or null: not loaded, or it could not be. */
let shown = null;
/** What the stats row and the cards were drawn from, serialized: an unchanged part draws nothing. */
let drawnStats = null;
let drawnCards = null;
let load = 0;
let busy = false;
/** The actions on their way, by key: their buttons stay disabled until the answer is drawn. */
const pending = new Set();

const ACTIONS = {
  snapshot: {
    path: 'snapshot',
    done: (r) => `Snapshot taken: ${count(r.objects)} objects`,
  },
  gc: {
    path: 'gc',
    ask: ['Run a full garbage collection now? The server pauses while it runs.',
      { title: 'Collect garbage', confirmLabel: 'Collect garbage' }],
    done: (r) => `Collected ${count(r.collected_objects)} objects, freed ${mb(r.freed_mb)}`,
  },
  baseline: {
    path: 'baseline',
    ask: ['Replace the baseline with the object counts of now? Growth is then measured from this moment.',
      { title: 'Set baseline', confirmLabel: 'Replace baseline', danger: true }],
    done: () => 'Baseline set',
  },
  'trace-start': {
    path: 'tracemalloc/start',
    ask: ['Start tracemalloc? Every allocation is traced from now on, which slows the server and costs memory until it is stopped.',
      { title: 'Start tracemalloc', confirmLabel: 'Start' }],
    done: (r) => (r.status === 'started' ? 'tracemalloc started' : 'tracemalloc was already running'),
  },
  'trace-stop': {
    path: 'tracemalloc/stop',
    ask: ['Stop tracemalloc? The traces collected so far are dropped.',
      { title: 'Stop tracemalloc', confirmLabel: 'Stop', danger: true }],
    done: (r) => (r.status === 'stopped' ? 'tracemalloc stopped' : 'tracemalloc was not running'),
  },
};

const count = (value) => (value == null ? '—' : value.toLocaleString());
const mb = (value, digits = 1) => (value == null ? '—' : `${value.toFixed(digits)} MB`);
const signed = (value, format) => `${value > 0 ? '+' : ''}${format(value)}`;
const time = (stamp) => new Date(stamp).toLocaleTimeString();
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value, more = '') =>
  html`<div class="pk-stat" data-stat="${key}"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div>${
    more ? html`<div class="pk-muted">${more}</div>` : ''}</div>`;
const card = (id, title, badge, body) => html`<section class="pk-card" id="${id}">
  <div class="pk-card-head"><h3 class="pk-card-title">${title}</h3>${badge}</div>${body}</section>`;
const table = (head, rows) => html`<div class="pk-table-wrap"><table class="pk-table">
  <thead><tr>${head.map(([label, numeric]) => html`<th class="${numeric ? 'pk-num' : ''}">${label}</th>`)}</tr></thead>
  <tbody>${rows}</tbody></table></div>`;

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while a load is on its way would queue another one behind it
  const mine = ++load;
  busy = true;
  let answer;
  try {
    answer = await api(BASE, { quiet: true });
  } catch (error) {
    if (mine !== load) return;  // a later load was started since
    // nothing shown before stays, as if it were current
    shown = null;
    drawnStats = null;
    drawnCards = null;
    render($('stats'), '');
    $('updated').textContent = '';
    const status = error instanceof ApiError ? error.status : 0;
    render($('cards'), status === 403 ? empty('shield', 'Administrators only', error.message)
      : status === 401 ? empty('shield', 'Not signed in', error.message)
      : status === 404
        ? empty('hard-drive', 'Memory profiling is off', 'Start the server with AGENT_ENABLE_MEMORY_PROFILING=1.')
        : empty('circle-alert', 'Memory profile could not be loaded', error.message));
    syncButtons();
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;
  $('updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  shown = answer;
  // process memory, gc counters and traced memory move on every answer: they live in the stats row alone.
  // The cards change only with a snapshot, a baseline or tracemalloc switched -- unchanged, they stay as they
  // are, and a selection or the focus in them with them.
  const stats = JSON.stringify([answer.memory, answer.gc, answer.tracemalloc, answer.snapshot?.objects, answer.trend]);
  if (stats !== drawnStats) {
    drawnStats = stats;
    drawStats();
  }
  const cards = JSON.stringify([answer.snapshot, answer.allocations, answer.baseline, answer.trend, answer.tracemalloc.active]);
  if (cards !== drawnCards) {
    drawnCards = cards;
    drawCards();
  }
}

// -------------------------------------------------------------------- drawing

function drawStats() {
  const { memory, trend, gc, tracemalloc: tracing } = shown;
  const growth = trend.status === 'analyzed' ? `${signed(trend.memory_mb.growth_rate_mb_per_hour, (v) => v.toFixed(1))} MB/h` : '—';
  render($('stats'), [
    stat('rss', 'RSS', mb(memory.rss_mb, 0)),
    stat('vms', 'Virtual', mb(memory.vms_mb, 0)),
    stat('percent', 'Memory %', memory.percent == null ? '—' : `${memory.percent.toFixed(1)} %`),
    stat('objects', 'Objects', count(shown.snapshot?.objects)),
    stat('snapshots', 'Snapshots', count(trend.snapshots)),
    stat('growth', 'Growth rate', growth),
    stat('gc', 'GC generations 0 · 1 · 2', gc.counts.map(count).join(' · '), `thresholds ${gc.thresholds.map(count).join(' · ')}`),
    stat('garbage', 'Uncollectable', count(gc.garbage)),
    stat('traced', 'Traced by tracemalloc', tracing.active ? mb(tracing.current_mb) : '—', tracing.active ? `peak ${mb(tracing.peak_mb)}` : ''),
  ]);
}

function drawCards() {
  // drawn anew, a button keeps the keyboard focus: render() finds it again by its data-key
  render($('cards'), [tracingCard(), objectsCard(), baselineCard(), leaksCard(), trendCard()]);
  syncButtons();
}

function tracingCard() {
  const { tracemalloc: tracing, allocations } = shown;  // what it has traced so far is in the stats row
  const button = tracing.active
    ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--danger" data-act="trace-stop" data-key="tracing">${icon('square', { size: 'sm' })} Stop</button>`
    : html`<button type="button" class="pk-btn pk-btn--sm" data-act="trace-start" data-key="tracing">${icon('play', { size: 'sm' })} Start</button>`;
  let body;
  if (allocations) {
    // recorded by a snapshot asked for; the periodic ones since record none, so they may be older than the latest
    body = html`<p class="pk-secondary" data-recorded="${allocations.recorded_at}">Largest allocation sites, recorded at ${
      time(allocations.recorded_at)}.${tracing.active ? ' Take a snapshot for current ones.' : ''}</p>${table(
      [['Location'], ['Size', true], ['Blocks', true]],
      allocations.sites.map((a) => html`<tr><td class="pk-mono">${a.file}</td><td class="pk-num">${a.size_kb.toFixed(1)} KB</td><td class="pk-num">${count(a.count)}</td></tr>`))}`;
  } else {
    body = html`<p class="pk-secondary">${tracing.active
      ? 'Take a snapshot to record the largest allocation sites.'
      : 'Traces every allocation with its file and line. Slows the server: start it for a short look only.'}</p>`;
  }
  return card('tracing', 'tracemalloc', html`
    <span class="pk-badge${tracing.active ? ' pk-badge--warn' : ''}">${tracing.active ? 'Tracing' : 'Off'}</span>
    <span class="pk-grow"></span>${button}`, body);
}

function objectsCard() {
  const { snapshot } = shown;
  return card('objects', 'Objects by type', snapshot ? html`<span class="pk-muted">snapshot of ${time(snapshot.taken_at)}</span>` : '',
    snapshot
      ? table([['Type'], ['Count', true]], snapshot.top_objects.map((o) => html`<tr data-type="${o.type}"><td class="pk-mono">${o.type}</td><td class="pk-num">${count(o.count)}</td></tr>`))
      : empty('hard-drive', 'No snapshot yet', 'Take a snapshot to count the objects by type.'));
}

function baselineCard() {
  const { baseline } = shown;
  let body;
  if (!baseline) body = empty('rotate-ccw', 'No baseline set');
  else if (baseline.changes === null) body = empty('hard-drive', 'Not compared yet', 'Take a snapshot to measure the growth since the baseline.');
  else if (!baseline.changes.length) body = empty('circle-check', 'No change since the baseline');
  else {
    body = table([['Type'], ['Change', true]], baseline.changes.map((c) => html`<tr data-type="${c.type}">
      <td class="pk-mono">${c.type}</td><td class="pk-num">${signed(c.change, count)}</td></tr>`));
  }
  return card('baseline', 'Growth since the baseline', baseline ? html`<span class="pk-muted">set ${time(baseline.set_at)}</span>` : '', body);
}

function leaksCard() {
  const { trend } = shown;
  let body;
  if (trend.status !== 'analyzed') body = empty('history', 'Needs two snapshots');
  else if (!trend.likely_leaks.length) body = empty('circle-check', 'No type grew steadily');
  else {
    body = table([['Type'], ['Growth', true], ['Intervals grown', true]], trend.likely_leaks.map((l) => html`<tr data-type="${l.type}">
      <td class="pk-mono">${l.type}</td><td class="pk-num">+${count(l.total_growth)}</td><td class="pk-num">${count(l.count)}</td></tr>`));
  }
  return card('leaks', 'Types that grew steadily', '', body);
}

function trendCard() {
  const { trend } = shown;
  if (trend.status !== 'analyzed') return card('trend', 'Trend', '', empty('history', 'Needs two snapshots'));
  const m = trend.memory_mb;
  return card('trend', 'Trend', '', html`<dl class="pk-kv">
    <dt>Time span</dt><dd>${(trend.time_span_seconds / 60).toFixed(1)} min</dd>
    <dt>First snapshot</dt><dd>${mb(m.start)}</dd>
    <dt>Latest snapshot</dt><dd>${mb(m.end)}</dd>
    <dt>Growth</dt><dd>${signed(m.growth, (v) => mb(v))}</dd>
  </dl>`);
}

/** Every action button as it is now: off while its action is on its way, or while nothing is loaded. */
function syncButtons() {
  // data-act: the kit's sort heads carry a data-key too
  for (const button of document.querySelectorAll('button[data-act][data-key]')) {
    button.disabled = !shown || pending.has(button.dataset.key);
  }
}

// --------------------------------------------------------------------- actions

async function act(button) {
  const { act: action, key } = button.dataset;
  const spec = ACTIONS[action];
  const had = document.activeElement === button;
  pending.add(key);
  button.disabled = true;  // until answered and drawn: a second click would ask again
  try {
    if (spec.ask && !await confirm(...spec.ask)) return;
    const result = await api(`${BASE}/${spec.path}`, { method: 'POST' });
    toast(spec.done(result), { kind: 'ok' });
  } catch {
    // shown by api()
  } finally {
    await refresh();
    pending.delete(key);
    syncButtons();
    // disabled, the button lost the focus; it goes back to the button of this action, drawn anew or not
    if (had && (!document.activeElement || document.activeElement === document.body)) {
      document.querySelector(`button[data-act][data-key="${CSS.escape(key)}"]`)?.focus();
    }
  }
}

// ---------------------------------------------------------------------- wiring

document.addEventListener('refresh', refresh);
document.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-act][data-key]');
  // not the second click of a double click: on cards drawn anew in between it would hit the button now in its place
  if (button && !button.disabled && event.detail < 2) act(button);
});

refresh();
