// Evals: start a suite, read its runs, compare arms and baselines.
import {
  api, html, update, icon, confirm, toast, notice, emptyState, errorText, isAborted, localTime, selectRow, withBusy,
  pluginBase,
} from '/static/kit/panel-kit.js';

const BASE = pluginBase(import.meta.url);
const $ = (id) => document.getElementById(id);

let suites = [];
let selected = new URLSearchParams(location.search).get('run');
let against = '';   // '' = the run's own reference arm, 'baseline' = the suite's baseline
let openRecord = null;
let recordsOpen = false;  // the Runs list stays open through every redraw once opened

const pct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`);
const num = (v, digits = 2) => (v == null ? '—' : Number(v).toFixed(digits));
const usd = (v) => (v == null ? '—' : `$${Number(v).toFixed(4)}`);
const signed = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(3)}`);
const STATUS_KIND = { done: 'ok', running: 'info', stopped: 'warn', incomplete: 'warn', failed: 'danger', died: 'danger' };
const badge = (text, kind = '') => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${text}</span>`;

// ------------------------------------------------------------------- suites

async function loadSuites() {
  try {
    suites = (await api(`${BASE}/suites`, { quiet: true })).suites;
  } catch (error) {
    toast(`Suites could not be read: ${errorText(error)}`, { kind: 'error' });
    return;
  }
  const options = suites.map((s) => html`<option value="${s.path}" ${s.error ? 'disabled' : ''}>${
    s.error ? `${s.path} (broken)` : `${s.name} — ${s.agent}, ${s.tasks} task(s), ${s.arms.length} arm(s)`}</option>`);
  update($('suite'), options.length ? options : html`<option value="" disabled selected>No suites found below evals/</option>`);
  $('start').disabled = !suites.some((s) => !s.error);
}

async function start() {
  const path = $('suite').value;
  const suite = suites.find((s) => s.path === path);
  if (!suite) return;
  if (!await confirm(`Run “${suite.name}”: ${suite.arms.length} arm(s) × ${suite.tasks} task(s) × ${suite.repeats} repeat(s)`
    + ` = ${suite.arms.length * suite.tasks * suite.repeats} agent runs. They cost what the agent costs.`,
  { title: 'Start eval run', confirmLabel: 'Run' })) return;
  await withBusy([$('start')], async () => {
    const { run_id: runId } = await api(`${BASE}/runs`, { method: 'POST', json: { suite: path } });
    toast(`Run ${runId} started`, { kind: 'ok' });
    selected = runId;
    openRecord = null;
    against = '';
    await refresh();
  });
}

// --------------------------------------------------------------------- runs

async function loadRuns() {
  let runs;
  try {
    runs = (await api(`${BASE}/runs`, { quiet: true, latest: 'runs' })).runs;
  } catch (error) {
    if (!isAborted(error)) update($('runs'), emptyState('circle-alert', 'Runs could not be read', errorText(error)));
    return;
  }
  if (!runs.length) {
    update($('runs'), emptyState('flask-conical', 'No runs yet', 'Pick a suite above and press Run, or use agent-eval run <suite.yaml>.'));
    return;
  }
  update($('runs'), html`<div class="pk-table-wrap"><table class="pk-table" data-pk-select data-pk-sort="runs">
    <thead><tr><th aria-sort="descending">Run</th><th>Suite</th><th>Status</th>
      <th class="pk-num">Passed</th><th class="pk-num">Cost</th></tr></thead>
    <tbody>${runs.map((r) => html`<tr data-id="${r.run_id}" tabindex="0">
      <td data-sort-value="${r.run_id}"><div class="pk-mono">${r.run_id}</div>
        <div class="pk-muted">${r.agent} · ${r.arms.join(', ')}</div></td>
      <td>${r.suite} ${r.baseline ? badge('baseline', 'info') : ''}</td>
      <td>${badge(r.status, STATUS_KIND[r.status])}</td>
      <td class="pk-num" data-sort-value="${r.scored ? r.passed / r.scored : ''}">${r.passed}/${r.scored}</td>
      <td class="pk-num" data-sort-value="${r.cost}">${usd(r.cost)}</td>
    </tr>`)}</tbody></table></div>`);
  if (selected) selectRow($('runs').querySelector('table'), selected);
}

// ------------------------------------------------------------------- detail

async function loadDetail() {
  if (!selected) {
    update($('detail'), emptyState('arrow-left', 'Pick a run', 'Its arms, tasks and runs show here.'));
    return;
  }
  let rep;
  try {
    rep = await api(`${BASE}/runs/${encodeURIComponent(selected)}${against ? `?against=${against}` : ''}`,
      { quiet: true, latest: 'detail' });
  } catch (error) {
    if (isAborted(error)) return;
    // 404: started a moment ago, its directory is not there yet. Anything else -- the runner died before it
    // got that far, among others -- with the server's reason.
    update($('detail'), error.status === 404
      ? emptyState('clock', `Run ${selected} is starting`, 'Its first records appear once the runner has set it up.')
      : emptyState('circle-alert', `Run ${selected} could not be read`, errorText(error)));
    return;
  }
  update($('detail'), detail(rep));
}

function detail(rep) {
  const m = rep.meta;
  const ref = rep.against ? `${rep.reference_arm} of ${rep.against} (baseline)` : `${rep.reference_arm} (same run)`;
  const skipped = Object.entries(rep.skipped).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(', ');
  return html`
    <div class="pk-row">
      <div class="pk-grow"><div class="pk-mono">${m.run_id}</div>
        <div class="pk-muted">${m.suite} · ${m.agent} · ${m.tasks} task(s) × ${m.repeats} · started ${localTime(m.started, { relative: true })}</div></div>
      ${badge(m.status, STATUS_KIND[m.status])}
      ${rep.baseline === m.run_id ? badge('baseline', 'info')
        : m.status === 'done' ? html`<button type="button" class="pk-btn pk-btn--sm" data-act="baseline">${icon('flag', { size: 'sm' })} Make baseline</button>` : ''}
      ${m.status === 'running' ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--danger" data-act="stop">${icon('square', { size: 'sm' })} Stop</button>` : ''}
    </div>
    <div class="pk-row">
      <label class="pk-muted" for="against">Compare against</label>
      <select class="pk-select" id="against">
        <option value="" ${against ? '' : 'selected'}>its own reference arm</option>
        <option value="baseline" ${against ? 'selected' : ''} ${rep.baseline && rep.baseline !== m.run_id ? '' : 'disabled'}>the suite's baseline</option>
      </select>
      <span class="pk-muted">reference: ${ref}</span>
    </div>
    ${skipped ? html`<div class="pk-callout pk-callout--warn">Not counted: ${skipped}</div>` : ''}
    ${(rep.warnings || []).map((w) => html`<div class="pk-callout pk-callout--warn">${w}</div>`)}
    ${arms(rep)}
    ${matrix(rep)}
    ${records(rep.records)}`;
}

function verdict(c, kind) {
  if (!c || !c.reference_tasks) return '';  // the reference has no tasks of this kind: nothing to judge
  if (!c.tasks) {
    return html`<div class="pk-callout pk-callout--warn"><strong>${kind}: no common tasks</strong> —
      the reference has ${c.reference_tasks}, this arm ran none of them.</div>`;
  }
  const tone = c.verdict === 'better' ? 'ok' : c.verdict === 'worse' ? 'danger' : 'info';
  const decides = c.needed == null
    ? `${c.moved} task(s) moved; with so few no outcome can be significant, it takes six or more (ties do not count).`
    : `Of the ${c.moved} task(s) that moved, it takes ${c.needed} moving the same way to decide.`;
  return html`<div class="pk-callout pk-callout--${tone}"><strong>${kind}: ${c.verdict}</strong> —
    ${c.better_tasks} better / ${c.worse_tasks} worse of ${c.tasks} task(s), p = ${num(c.p, 3)};
    mean difference ${signed(c.mean_diff)}${c.ci95 ? ` (95% ${signed(c.ci95[0])} … ${signed(c.ci95[1])})` : ', every task alike'}.
    ${decides}</div>`;
}

function arms(rep) {
  const entries = Object.entries(rep.arms);
  return html`<div class="pk-table-wrap"><table class="pk-table">
    <thead><tr><th>Arm</th><th class="pk-num">Runs</th><th class="pk-num">Pass</th><th class="pk-num">Mean score</th>
      <th class="pk-num">Cost</th><th class="pk-num">p90 / run</th></tr></thead>
    <tbody>${entries.map(([name, a]) => html`<tr><td>${name}</td><td class="pk-num">${a.runs}</td>
      <td class="pk-num">${pct(a.pass_rate)}</td><td class="pk-num">${num(a.mean_score)}</td>
      <td class="pk-num">${usd(a.cost)}${a.cost_unknown_runs ? html`<div class="pk-muted">+ ${a.cost_unknown_runs} run(s) unknown</div>` : ''}</td>
      <td class="pk-num">${usd(a.cost_p90)}</td></tr>`)}</tbody>
  </table></div>
  ${entries.filter(([, a]) => a.learn).map(([name, a]) => html`<div class="pk-stack">
    <div><strong>${name}</strong> against ${rep.reference_arm}</div>
    ${verdict(a.learn, 'learn tasks')}${verdict(a.holdout, 'holdout tasks')}
    ${a.regressions?.length ? html`<div class="pk-callout pk-callout--danger">Regressions: ${a.regressions.join(', ')}</div>` : ''}
  </div>`)}`;
}

function matrix(rep) {
  const names = Object.keys(rep.arms);
  const tasks = [...new Set(names.flatMap((n) => Object.keys(rep.arms[n].tasks)))].sort();
  if (!tasks.length) return '';
  const cell = (t) => (t ? html`<td class="pk-num" data-sort-value="${t.mean}">${t.passes}/${t.runs} · ${num(t.mean)}</td>` : html`<td class="pk-num">—</td>`);
  return html`<div class="pk-table-wrap"><table class="pk-table" data-pk-sort="tasks">
    <thead><tr><th>Task</th><th>Kind</th>${names.map((n) => html`<th class="pk-num">${n}</th>`)}</tr></thead>
    <tbody>${tasks.map((task) => {
      const kind = names.map((n) => rep.arms[n].tasks[task]?.kind).find(Boolean) || '';
      return html`<tr><td>${task}</td><td>${kind}</td>${names.map((n) => cell(rep.arms[n].tasks[task]))}</tr>`;
    })}</tbody></table></div>`;
}

function records(list) {
  if (!list.length) return emptyState('clock', 'No runs finished yet');
  return html`<details class="pk-details" id="records" ${recordsOpen ? 'open' : ''}><summary>Runs (${list.length})</summary>
    <div class="pk-table-wrap"><table class="pk-table" data-pk-sort="records">
    <thead><tr><th class="pk-num">#</th><th>Arm</th><th>Task</th><th class="pk-num">Rep</th><th>Status</th>
      <th class="pk-num">Score</th><th class="pk-num">Cost</th><th class="pk-num">Time</th><th>Model</th></tr></thead>
    <tbody>${list.map((r, i) => html`<tr data-record="${i}" tabindex="0">
      <td class="pk-num">${r.n}</td><td>${r.arm}</td><td>${r.task}</td><td class="pk-num">${r.rep}</td>
      <td>${badge(r.status, r.scored ? (r.passed ? 'ok' : 'danger') : 'warn')}</td>
      <td class="pk-num" data-sort-value="${r.score ?? ''}">${r.scored ? num(r.score) : '—'}</td>
      <td class="pk-num" data-sort-value="${r.cost ?? ''}">${usd(r.cost)}</td>
      <td class="pk-num" data-sort-value="${r.duration_s ?? ''}">${r.duration_s != null ? `${num(r.duration_s, 1)} s` : '—'}</td>
      <td class="pk-mono">${r.model || ''}</td></tr>`)}</tbody></table></div>
    ${openRecord !== null && list[openRecord] ? record(list[openRecord]) : ''}</details>`;
}

function record(r) {
  return html`<div class="pk-card pk-stack">
    <div><strong>#${r.n} ${r.arm} / ${r.task}</strong> <span class="pk-muted pk-mono">${r.request_id || ''} · ${r.dir}</span></div>
    ${r.errors?.length ? html`<div class="pk-callout pk-callout--danger">${r.errors.join('\n')}</div>` : ''}
    <div class="pk-muted">Check</div><pre class="pk-code">${r.detail || ''}</pre>
    <div class="pk-muted">Answer (end)</div><pre class="pk-code">${r.answer || ''}</pre>
  </div>`;
}

// ------------------------------------------------------------------ actions

async function act(button) {
  await withBusy([button], async () => {
    if (button.dataset.act === 'baseline') {
      await api(`${BASE}/runs/${encodeURIComponent(selected)}/baseline`, { method: 'POST' });
      toast('Baseline set', { kind: 'ok' });
    } else if (button.dataset.act === 'stop') {
      if (!await confirm('Start no further runs? Running ones finish.', { title: 'Stop run', confirmLabel: 'Stop', danger: true })) return;
      await api(`${BASE}/runs/${encodeURIComponent(selected)}/stop`, { method: 'POST' });
      toast('No further runs start', { kind: 'info' });
    }
  });
  await refresh();
}

async function refresh() {
  await Promise.all([loadRuns(), loadDetail()]);
}

$('runs').addEventListener('rowselect', (event) => {
  selected = event.detail.id;
  openRecord = null;
  against = '';  // a baseline of one suite means nothing for a run of another
  loadDetail();
});
$('detail').addEventListener('click', (event) => {
  const button = event.target.closest('[data-act]');
  if (button && !button.disabled) { act(button); return; }
  const row = event.target.closest('[data-record]');
  if (row) { openRecord = Number(row.dataset.record); loadDetail(); }
});
$('detail').addEventListener('change', (event) => {
  if (event.target.id === 'against') { against = event.target.value; loadDetail(); }
});
$('detail').addEventListener('toggle', (event) => {
  if (event.target.id === 'records') recordsOpen = event.target.open;
}, true);
$('start').addEventListener('click', start);
document.addEventListener('refresh', refresh);
loadSuites();
refresh();
