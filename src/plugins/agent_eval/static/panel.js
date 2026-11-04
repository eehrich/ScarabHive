// Evals: start a suite, read its runs, compare arms and baselines.
import {
  api, html, update, icon, confirm, toast, emptyState, errorText, isAborted, localTime, selectRow, withBusy,
  pluginBase, stat, status, initTabs, keepInSight, scrollerOf,
} from '/static/kit/panel-kit.js';

const BASE = pluginBase(import.meta.url);
const $ = (id) => document.getElementById(id);

let suites = [];
let selected = new URLSearchParams(location.search).get('run');
let against = '';   // '' = the run's own reference arm, 'baseline' = the suite's baseline
let openRecord = null;
let tab = 'summary';  // the detail's tab, kept through every redraw and from run to run

const pct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`);
const num = (v, digits = 2) => (v == null ? '—' : Number(v).toFixed(digits));
const usd = (v) => (v == null ? '—' : `$${Number(v).toFixed(4)}`);
const signed = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(3)}`);
// A run's status in the kit's set of run states; its own word stays the label.
const STATUS = { done: 'success', running: 'running', stopped: 'canceled', incomplete: 'skipped', failed: 'failed', died: 'failed' };
const runStatus = (name) => status(STATUS[name] || 'pending', { label: name, pill: true });
const badge = (text, kind = '') => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${text}</span>`;
// pass rate as a tone: all passed, none, or in between
const toneName = (passes, runs) => (!runs ? '' : passes === runs ? 'ok' : passes ? 'warn' : 'danger');
const tone = (passes, runs) => (runs ? `pk-text--${toneName(passes, runs)}` : '');
const planned = (m) => (m.arms?.length || 0) * (m.tasks || 0) * (m.repeats || 0);

// ------------------------------------------------------------------- suites

async function loadSuites() {
  try {
    suites = (await api(`${BASE}/suites`, { quiet: true })).suites;
  } catch (error) {
    toast(`Suites could not be read: ${errorText(error)}`, { kind: 'error' });
    return;
  }
  const options = suites.map((s) => (s.error
    ? html`<option value="${s.path}" disabled title="${s.error}">${s.path} (broken)</option>`
    : html`<option value="${s.path}" title="${s.description || ''}">${s.name} · ${s.agent} · ${
      s.arms.length * s.tasks * s.repeats} runs</option>`));
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

// *done*: the agent runs that counted -- a stopped, skipped or refused one is written down too, but never ran
function progress(done, total, state) {
  if (!total) return '';
  const kind = state === 'running' ? '' : state === 'done' ? ' pk-progress--ok' : ' pk-progress--warn';
  return html`<progress class="pk-progress${kind}" max="${total}" value="${Math.min(done, total)}"
    aria-label="${done} of ${total} runs"></progress>`;
}

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
    <thead><tr><th aria-sort="descending">Run</th><th>Status</th><th class="pk-num">Passed</th>
      <th class="pk-num">Cost</th></tr></thead>
    <tbody>${runs.map((r) => {
      const total = planned(r);
      return html`<tr data-id="${r.run_id}" tabindex="0">
      <td data-sort-value="${r.run_id}"><div><strong>${r.suite}</strong> ${r.baseline ? badge('baseline', 'info') : ''}</div>
        <div class="pk-muted">${localTime(r.started || '', { relative: true })} · ${r.agent} · ${r.arms.join(' / ')}</div></td>
      <td data-sort-value="${r.status}">${runStatus(r.status)}
        ${r.status === 'running' ? html`<div class="pk-muted">${r.scored} of ${total}</div>${progress(r.scored, total, r.status)}` : ''}</td>
      <td class="pk-num" data-sort-value="${r.scored ? r.passed / r.scored : ''}"><span class="${tone(r.passed, r.scored)}">${
        r.passed}/${r.scored}</span></td>
      <td class="pk-num" data-sort-value="${r.cost}">${usd(r.cost)}</td>
    </tr>`;
    })}</tbody></table></div>`);
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
  if (update($('detail'), detail(rep))) initTabs($('detail'));
}

function detail(rep) {
  const m = rep.meta;
  const ref = rep.against ? `${rep.reference_arm} of ${rep.against} (baseline)` : `${rep.reference_arm} (same run)`;
  const skipped = Object.entries(rep.skipped).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(', ');
  const records = rep.records;
  const scored = records.filter((r) => r.scored);
  const passed = scored.filter((r) => r.passed).length;
  // the runs that spent, as the Arms table costs them: a refused one carries a cost of 0 and ran nothing; one
  // the judge left unjudged ran
  const spent = records.filter((r) => r.scored || r.status === 'judge_failed');
  const priced = spent.filter((r) => r.cost != null);
  const cost = priced.reduce((sum, r) => sum + Number(r.cost), 0);
  const unpriced = spent.length - priced.length;
  const notCounted = records.length - scored.length;
  const waiting = records.filter((r) => r.judge === 'pending').length;
  const total = planned(m);
  const comparable = rep.baseline && rep.baseline !== m.run_id;
  const tabButton = (name, label, count) => html`<button class="pk-tab" role="tab" data-tab="${name}" data-key="tab:${name}"
    aria-selected="${String(tab === name)}" aria-controls="ev-${name}">${label}${
    count != null ? html` <span class="pk-tab-count">${count}</span>` : ''}</button>`;
  const pane = (name, content) => html`<section id="ev-${name}" role="tabpanel" class="pk-stack" ${tab === name ? '' : 'hidden'}>${content}</section>`;
  return html`
    <div class="pk-row">
      <div class="pk-grow"><h2 class="pk-card-title">${m.suite} ${rep.baseline === m.run_id ? badge('baseline', 'info') : ''}</h2>
        <div class="pk-muted"><span class="pk-mono">${m.run_id}</span> · ${m.agent} · ${m.tasks} task(s) × ${m.repeats}
          · started ${localTime(m.started, { relative: true })}</div></div>
      ${runStatus(m.status)}
      ${rep.baseline !== m.run_id && m.status === 'done'
        ? html`<button type="button" class="pk-btn pk-btn--sm" data-act="baseline">${icon('flag', { size: 'sm' })} Make baseline</button>` : ''}
      ${m.status === 'running' ? html`<button type="button" class="pk-btn pk-btn--sm pk-btn--danger" data-act="stop">${icon('square', { size: 'sm' })} Stop</button>` : ''}
    </div>
    <div class="pk-stats">
      ${stat('Runs', `${scored.length} / ${total}`, { sub: html`${progress(scored.length, total, m.status)}
        ${notCounted ? html`<div>${notCounted} not counted${waiting ? `, ${waiting} waiting for the judge` : ''}</div>` : ''}` })}
      ${stat('Passed', `${passed} / ${scored.length}`, { tone: toneName(passed, scored.length),
        sub: scored.length ? pct(passed / scored.length) : 'none scored yet' })}
      ${stat('Cost', usd(cost), { sub: [priced.length && `${usd(cost / priced.length)} per run`,
        unpriced && `${unpriced} run(s) unknown`].filter(Boolean).join(' · ') })}
    </div>
    <div class="pk-filters">
      ${comparable ? html`<label class="pk-muted" for="against">Compare against</label>
      <select class="pk-select" id="against">
        <option value="" ${against ? '' : 'selected'}>its own reference arm</option>
        <option value="baseline" ${against ? 'selected' : ''}>the suite's baseline</option>
      </select>` : ''}
      <span class="pk-muted">reference: ${ref}</span>
    </div>
    ${skipped ? html`<div class="pk-callout pk-callout--warn">Not counted: ${skipped}</div>` : ''}
    ${(rep.warnings || []).map((w) => html`<div class="pk-callout pk-callout--warn">${w}</div>`)}
    ${rep.judge ? html`<div class="pk-callout pk-callout--info">Judge: ${rep.judge.judged} of ${rep.judge.products}
      product(s) judged in ${rep.judge.calls} call(s), ${rep.judge.cost != null ? usd(rep.judge.cost) : 'cost unknown'}
      (not in the arms' cost).${rep.judge.problems.map((p) => html`<div class="pk-muted">${p}</div>`)}</div>` : ''}
    <div class="pk-tabs" role="tablist" data-pk-tabs>
      ${tabButton('summary', 'Arms')}${tabButton('tasks', 'Tasks')}${tabButton('records', 'Runs', records.length)}
    </div>
    ${pane('summary', arms(rep))}
    ${pane('tasks', matrix(rep))}
    ${pane('records', recordList(records))}`;
}

function verdict(c, kind) {
  if (!c || !c.reference_tasks) return '';  // the reference has no tasks of this kind: nothing to judge
  if (!c.tasks) {
    return html`<div class="pk-callout pk-callout--warn"><strong>${kind}: no common tasks</strong> —
      the reference has ${c.reference_tasks}, this arm ran none of them.</div>`;
  }
  const kindTone = c.verdict === 'better' ? 'ok' : c.verdict === 'worse' ? 'danger' : 'info';
  const decides = c.needed == null
    ? `${c.moved} task(s) moved; with so few no outcome can be significant, it takes six or more (ties do not count).`
    : `Of the ${c.moved} task(s) that moved, it takes ${c.needed} moving the same way to decide.`;
  return html`<div class="pk-callout pk-callout--${kindTone}"><strong>${kind}: ${c.verdict}</strong> —
    ${c.better_tasks} better / ${c.worse_tasks} worse of ${c.tasks} task(s), p = ${num(c.p, 3)};
    mean difference ${signed(c.mean_diff)}${c.ci95 ? ` (95% ${signed(c.ci95[0])} … ${signed(c.ci95[1])})` : ', every task alike'}.
    <div class="pk-muted">${decides}</div></div>`;
}

function arms(rep) {
  const entries = Object.entries(rep.arms);
  return html`<div class="pk-table-wrap"><table class="pk-table">
    <thead><tr><th>Arm</th><th class="pk-num">Runs</th><th class="pk-num">Pass</th><th class="pk-num">Mean score</th>
      <th class="pk-num">Cost</th><th class="pk-num">p90 / run</th></tr></thead>
    <tbody>${entries.map(([name, a]) => html`<tr><td>${name}${name === rep.reference_arm && !rep.against
      ? html` <span class="pk-muted">(reference)</span>` : ''}</td><td class="pk-num">${a.runs}</td>
      <td class="pk-num">${pct(a.pass_rate)}</td><td class="pk-num">${num(a.mean_score)}${a.pooled_score != null
        ? html`<div class="pk-muted" title="every test or expectation counted alike">pooled ${num(a.pooled_score)}</div>` : ''}</td>
      <td class="pk-num">${usd(a.cost)}${a.cost_unknown_runs ? html`<div class="pk-muted">+ ${a.cost_unknown_runs} run(s) unknown</div>` : ''}</td>
      <td class="pk-num">${usd(a.cost_p90)}</td></tr>`)}</tbody>
  </table></div>
  ${entries.filter(([, a]) => a.learn).map(([name, a]) => html`<div class="pk-card pk-stack">
    <h3 class="pk-card-title">${name} against ${rep.reference_arm}</h3>
    ${verdict(a.learn, 'learn tasks')}${verdict(a.holdout, 'holdout tasks')}
    ${a.regressions?.length ? html`<div class="pk-callout pk-callout--danger">Regressions: ${a.regressions.join(', ')}</div>` : ''}
  </div>`)}
  ${entries.filter(([, a]) => a.length).map(([name, a]) => html`<div class="pk-muted">${name}: products of
    ${a.length.mean_chars} characters on average; of two of a task, the longer scored higher ${a.length.longer_higher}×,
    lower ${a.length.longer_lower}× (p = ${num(a.length.p, 3)}).</div>`)}
  ${entries.filter(([, a]) => Object.keys(a.fallbacks || {}).length).map(([name, a]) => html`<div class="pk-callout pk-callout--warn">
    ${name}: ${Object.entries(a.fallbacks).map(([agent, models]) => `${agent} fell back to ${models.join(', ')}`).join('; ')}
    -- those runs did not measure this arm's model alone.</div>`)}
  ${entries.filter(([, a]) => Object.keys(a.escalated || {}).length).map(([name, a]) => html`<div class="pk-callout pk-callout--info">
    ${name}: ${Object.entries(a.escalated).map(([agent, models]) => `${agent} also ran on its advanced model ${models.join(', ')}`).join('; ')}
    -- an escalation, or a fallback past its chain.</div>`)}`;
}

function matrix(rep) {
  const names = Object.keys(rep.arms);
  const tasks = [...new Set(names.flatMap((n) => Object.keys(rep.arms[n].tasks)))].sort();
  if (!tasks.length) return emptyState('clock', 'No task finished yet');
  const cell = (t) => (t
    ? html`<td class="pk-num" data-sort-value="${t.mean}"><span class="${tone(t.passes, t.runs)}">${t.passes}/${t.runs}</span> · ${num(t.mean)}</td>`
    : html`<td class="pk-num">—</td>`);
  return html`<div class="pk-table-wrap"><table class="pk-table" data-pk-sort="tasks">
    <thead><tr><th>Task</th><th>Kind</th>${names.map((n) => html`<th class="pk-num">${n}</th>`)}</tr></thead>
    <tbody>${tasks.map((task) => {
      const kind = names.map((n) => rep.arms[n].tasks[task]?.kind).find(Boolean) || '';
      return html`<tr><td>${task}</td><td>${kind}</td>${names.map((n) => cell(rep.arms[n].tasks[task]))}</tr>`;
    })}</tbody></table></div>`;
}

function recordList(list) {
  if (!list.length) return emptyState('clock', 'No runs finished yet');
  return html`${openRecord !== null && list[openRecord] ? record(list[openRecord]) : ''}
    <div class="pk-table-wrap"><table class="pk-table" data-pk-select data-pk-sort="records">
    <thead><tr><th class="pk-num">#</th><th>Arm</th><th>Task</th><th class="pk-num">Rep</th><th>Status</th>
      <th class="pk-num">Score</th><th class="pk-num">Cost</th><th class="pk-num">Time</th><th>Model</th></tr></thead>
    <tbody>${list.map((r, i) => html`<tr data-id="${i}" tabindex="0" aria-selected="${String(i === openRecord)}">
      <td class="pk-num">${r.n}</td><td>${r.arm}</td><td>${r.task}</td><td class="pk-num">${r.rep}</td>
      <td>${badge(r.status, r.scored ? (r.passed ? 'ok' : 'danger') : 'warn')}</td>
      <td class="pk-num" data-sort-value="${r.score ?? ''}">${r.scored ? num(r.score) : '—'}</td>
      <td class="pk-num" data-sort-value="${r.cost ?? ''}">${usd(r.cost)}</td>
      <td class="pk-num" data-sort-value="${r.duration_s ?? ''}">${r.duration_s != null ? `${num(r.duration_s, 1)} s` : '—'}</td>
      <td class="pk-mono">${usedModels(r)}</td></tr>`)}</tbody></table></div>`;
}

/** What a run's agents really ran on (the usage store's), else the model its agent was built with. */
function usedModels(r) {
  const used = [...new Set(Object.values(r.models || {}).flatMap((m) => Object.keys(m)))];
  return used.length ? used.join(', ') : (r.model || '');
}

function record(r) {
  return html`<div class="pk-card pk-stack" id="record">
    <div class="pk-row"><h3 class="pk-card-title pk-grow">#${r.n} ${r.arm} / ${r.task}</h3>
      <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="close" data-key="record-close" aria-label="Close">${icon('x', { size: 'sm' })}</button></div>
    <div class="pk-muted pk-mono">${r.request_id || ''} · ${r.dir}</div>
    ${r.errors?.length ? html`<div class="pk-callout pk-callout--danger">${r.errors.join('\n')}</div>` : ''}
    <div class="pk-muted">Check</div><pre class="pk-code">${r.detail || ''}</pre>
    <div class="pk-muted">Answer (end)</div><pre class="pk-code">${r.answer || ''}</pre>
  </div>`;
}

// ------------------------------------------------------------------ actions

async function act(button) {
  if (button.dataset.act === 'close') { openRecord = null; loadDetail(); return; }
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
$('detail').addEventListener('rowselect', async (event) => {
  openRecord = Number(event.detail.id);
  await loadDetail();
  const card = $('record');
  if (card) keepInSight(scrollerOf(card), card);
});
$('detail').addEventListener('tabchange', (event) => { tab = event.detail.tab; });
$('detail').addEventListener('click', (event) => {
  const button = event.target.closest('[data-act]');
  if (button && !button.disabled) act(button);
});
$('detail').addEventListener('change', (event) => {
  if (event.target.id === 'against') { against = event.target.value; loadDetail(); }
});
$('start').addEventListener('click', start);
document.addEventListener('refresh', refresh);
loadSuites();
refresh();
