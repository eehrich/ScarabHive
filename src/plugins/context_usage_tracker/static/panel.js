// Context & Cost Usage: tokens, cache and cost per call, agent and model -- for the session open in the chat (or the
// one a link names, ?session_id=), its sub-agents included, or for all sessions.
import { api, html, render, icon, trusted, confirm, session, onThemeChange } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);
const pinned = new URLSearchParams(location.search).get('session_id');

let scope = 'session';
let usage = { latest: null, agents: {}, statistics: {}, history: [] };
let load = 0;
let busy = false;
let agentSort = { key: 'total_cost', dir: -1 };
let chart = null;
/** the calls the table shows, newest first: a row opens its index */
let callRows = [];

/** The session asked about: null for all sessions -- and in session scope when none is open. */
const scoped = () => (scope === 'session' ? pinned || session.id : null);

// ------------------------------------------------------------------ formatting

const number = (value) => Number(value ?? 0).toLocaleString();
const percent = (value) => `${Number(value ?? 0).toFixed(1)}%`;
const time = (seconds) => new Date(seconds * 1000).toLocaleTimeString();
const dateTime = (seconds) => new Date(seconds * 1000).toLocaleString();
// stored in USD, shown in cents: a call's price like $0.001 reads badly, 0.1¢ does not
const cents = (usd, digits = 3) => `${(Number(usd ?? 0) * 100).toFixed(digits)}¢`;
const dash = html`<span class="pk-muted">–</span>`;
const latency = (ms) => (ms === null || ms === undefined ? dash : ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`);
/** A cost: estimates (llm_pricing.yaml) are marked, a billed price stays plain. */
const cost = (usd, estimated) => (usd === null || usd === undefined
  ? dash
  : html`<span class="${estimated ? 'cu-estimate' : 'cu-cost'}" title="${estimated ? 'includes estimates from llm_pricing.yaml, not billed prices' : ''}">${estimated ? '~' : ''}${cents(usd)}</span>`);

const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;

const stat = (key, label, value, sub = '') => html`<div class="pk-stat" data-stat="${key}">
  <div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div>${sub ? html`<div class="pk-muted cu-sub">${sub}</div>` : ''}
</div>`;

// ----------------------------------------------------------------------- data

async function refresh(event) {
  const auto = Boolean(event?.detail?.auto);
  if (auto && busy) return;  // a tick while the last answer is on its way would only discard it
  const id = scoped();
  const mine = ++load;
  if (scope === 'session' && !id) {
    busy = false;
    drawNoSession();
    return;
  }
  busy = true;
  const query = id ? `?session_id=${encodeURIComponent(id)}` : '';
  let answers;
  try {
    answers = await Promise.all([api(`${BASE}usage${query}`, { quiet: true }), api(`${BASE}history${query}`, { quiet: true })]);
  } catch (error) {
    if (mine === load) {  // nothing of the scope shown before stays, as if it were this one's
      usage = { latest: null, agents: {}, statistics: {}, history: [] };
      drawAll({ stats: false });
      render($('stats'), empty('circle-alert', 'Usage could not be loaded', error.message));
    }
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;  // another session or scope was asked for since
  const [current, { history }] = answers;
  usage = {
    latest: current.latest && Object.keys(current.latest).length ? current.latest : null,
    agents: current.agents || {},
    statistics: current.statistics || {},
    history: history || [],
  };
  drawAll();
}

function drawNoSession() {
  usage = { latest: null, agents: {}, statistics: {}, history: [] };
  drawAll({ stats: false });
  render($('stats'), empty('message-square', 'No session open', 'Open a session in the chat, or show all sessions.'));
}

function drawAll({ stats = true } = {}) {
  if (stats) drawStats();
  drawOverview();
  drawAgents();
  drawLlms();
  drawCalls();
  drawChart();  // last: the numbers do not wait for the drawing
}

// ---------------------------------------------------------------- stat cards

function drawStats() {
  const totals = usage.statistics.totals || {};
  const calls = usage.history.length;
  const estimated = totals.cost_estimated_calls || 0;
  const billed = (totals.cost_known_calls || 0) - estimated;
  const latest = usage.latest;
  render($('stats'), [
    stat('cost', 'Total cost', html`${estimated ? '~' : ''}${cents(totals.cost)}`,
      calls ? (estimated ? `${billed} billed · ${estimated} estimated` : `${billed} of ${calls} calls billed`) : ''),
    stat('calls', 'Calls', number(calls), scope === 'session' ? 'this session and its sub-agents' : 'all sessions'),
    stat('output', 'Output tokens', number(totals.completion_tokens), `prompt ${number(totals.prompt_tokens)}`),
    stat('cached', 'Cached', number(totals.cached_tokens),
      totals.prompt_tokens ? `${percent(totals.cache_hit_rate)} of the prompt` : ''),
    stat('writes', 'Cache writes', number(totals.cache_write_tokens)),
    stat('context', 'Context now', latest ? number(latest.total_tokens) : '–',
      latest ? `${percent(latest.usage_percentage)} of ${number(latest.context_window)}${latest.is_stale ? ' · stale' : ''}` : ''),
  ]);
}

/**
 * An agent select: its choices follow the calls, and a choice stays -- also through an answer without that agent,
 * a failed load, an empty scope. Not redrawn while open.
 */
function fillAgents(select) {
  const chosen = select.value;
  const names = [...new Set(usage.history.map((call) => call.agent_name))].sort();
  const all = chosen && !names.includes(chosen) ? [chosen, ...names] : names;
  const key = JSON.stringify(all);
  if (select.dataset.options === key || document.activeElement === select) return;
  select.dataset.options = key;
  render(select, [html`<option value="">All agents</option>`,
    all.map((name) => html`<option value="${name}" ${name === chosen ? trusted('selected') : ''}>${name}</option>`)]);
}

// ------------------------------------------------------------------ overview

function drawOverview() {
  const { tokens, usage_percentage: share } = usage.statistics;
  if (!tokens) {
    render($('overview'), empty('chart-column', 'No calls recorded'));
    return;
  }
  render($('overview'), html`<div class="pk-table-wrap"><table class="pk-table">
    <thead><tr><th></th><th class="pk-num">Current</th><th class="pk-num">Min</th><th class="pk-num">Max</th><th class="pk-num">Average</th></tr></thead>
    <tbody>
      <tr><td>Context tokens</td><td class="pk-num">${number(tokens.current)}</td><td class="pk-num">${number(tokens.min)}</td>
        <td class="pk-num">${number(tokens.max)}</td><td class="pk-num">${number(Math.round(tokens.avg))}</td></tr>
      <tr><td>Context used</td><td class="pk-num">${percent(share.current)}</td><td class="pk-num">${percent(share.min)}</td>
        <td class="pk-num">${percent(share.max)}</td><td class="pk-num">${percent(share.avg)}</td></tr>
    </tbody>
  </table></div>`);
}

/** A kit token as a colour the canvas takes: the tokens are light-dark() expressions, which only CSS resolves. */
function token(name) {
  const probe = document.createElement('span');
  probe.style.color = `var(${name})`;
  document.body.append(probe);
  const value = getComputedStyle(probe).color;
  probe.remove();
  return value;
}

function drawChart() {
  const select = $('chartAgent');
  fillAgents(select);
  const calls = usage.history.filter((call) => !select.value || call.agent_name === select.value).slice(-60);
  const line = (label, values, color, dashes = []) => ({
    label, data: values, borderColor: color, backgroundColor: color, borderWidth: 1.5, pointRadius: 0,
    tension: 0.25, borderDash: dashes, yAxisID: 'tokens',
  });
  const datasets = [
    line('total', calls.map((call) => call.total_tokens ?? 0), token('--accent')),
    line('prompt', calls.map((call) => call.prompt_tokens ?? 0), token('--warn'), [4, 3]),
    line('output', calls.map((call) => call.completion_tokens ?? 0), token('--text-secondary'), [2, 2]),
    line('cached', calls.map((call) => call.cached_tokens ?? 0), token('--info'), [6, 3]),
    { ...line('cost ¢', calls.map((call) => (call.cost == null ? null : call.cost * 100)), token('--ok')),
      yAxisID: 'cost', pointRadius: 2, spanGaps: true },
  ];
  const labels = calls.map((call) => time(call.timestamp));
  // every colour on every draw, so a draw after a theme change leaves nothing in the old one
  const muted = token('--text-muted');
  const grid = token('--border-subtle');
  const options = {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: { legend: { labels: { color: muted, boxWidth: 14 } } },
    scales: {
      x: { ticks: { color: muted, maxTicksLimit: 10 }, grid: { color: grid } },
      tokens: { position: 'left', ticks: { color: muted }, grid: { color: grid } },
      cost: { position: 'right', ticks: { color: token('--ok') }, grid: { drawOnChartArea: false } },
    },
  };
  if (chart) {
    // Chart.js keeps a line hidden in the legend in a meta bound to the dataset object, and these are new ones: carry it over
    const hidden = new Set(chart.data.datasets.filter((_, i) => !chart.isDatasetVisible(i)).map((set) => set.label));
    for (const set of datasets) set.hidden = hidden.has(set.label);
    chart.data = { labels, datasets };
    chart.options = options;
    chart.update();
    return;
  }
  chart = new window.Chart($('chart'), { type: 'line', data: { labels, datasets }, options });
}

// the chart draws with the theme's colours: a theme change draws it anew -- the viewer's choice, or the system's
// light and dark under the theme "system"
onThemeChange(drawChart);
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', drawChart);

// ---------------------------------------------------------------------- agents

const AGENT_COLUMNS = [
  ['agent_name', 'Agent', false], ['total_calls', 'Calls', true], ['total_tokens', 'Σ Tokens', true],
  ['total_prompt_tokens', 'Σ Prompt', true], ['total_completion_tokens', 'Σ Output', true],
  ['total_cached_tokens', 'Σ Cached', true], ['total_cache_write_tokens', 'Σ Writes', true],
  ['total_cost', 'Σ Cost', true], ['avg_cost', 'Ø Cost', true], ['avg_latency', 'Ø Latency', true],
  ['peak_tokens', 'Peak', true], ['last_activity', 'Last', true],
];

function agentValue(agent, key) {
  if (key === 'avg_cost') return agent.cost_known_calls ? (agent.total_cost ?? 0) / agent.cost_known_calls : null;
  if (key === 'avg_latency') return agent.latency_calls ? agent.total_latency_ms / agent.latency_calls : null;
  return agent[key] ?? 0;
}

/**
 * The share of the prompt read from the cache. All-time rows carry a pair counted in lockstep (the all-time sums
 * began at different times, so their ratio can pass 100%); a session's rows are sums over the same calls.
 */
function cacheShare(agent) {
  if ('cache_rate_prompt_tokens' in agent) {
    return agent.cache_rate_prompt_tokens ? (agent.cache_rate_cached_tokens / agent.cache_rate_prompt_tokens) * 100 : null;
  }
  return agent.total_prompt_tokens ? (agent.total_cached_tokens / agent.total_prompt_tokens) * 100 : null;
}

function drawAgents() {
  const rows = Object.values(usage.agents);
  $('agentsCount').textContent = rows.length || '';
  if (!rows.length) {
    render($('agents'), empty('users', 'No agent activity yet'));
    return;
  }
  const { key, dir } = agentSort;
  rows.sort((a, b) => {
    const left = agentValue(a, key) ?? -Infinity;
    const right = agentValue(b, key) ?? -Infinity;
    const order = typeof left === 'string' || typeof right === 'string' ? String(left).localeCompare(String(right)) : left - right;
    return dir * order;
  });
  const sortState = (column) => (column === key ? (dir === 1 ? 'ascending' : 'descending') : 'none');
  render($('agents'), html`<div class="pk-table-wrap"><table class="pk-table cu-table">
    <thead><tr>${AGENT_COLUMNS.map(([column, label, numeric]) => html`<th class="${numeric ? 'pk-num' : ''}" aria-sort="${sortState(column)}" data-sort="${column}">${label}</th>`)}</tr></thead>
    <tbody>${rows.map((agent) => {
      const share = cacheShare(agent);
      const estimated = agent.cost_estimated_calls > 0;
      return html`<tr>
        <td title="${agent.agent_id}">${agent.agent_name}</td>
        <td class="pk-num">${number(agent.total_calls)}</td>
        <td class="pk-num">${number(agent.total_tokens)}</td>
        <td class="pk-num">${number(agent.total_prompt_tokens)}</td>
        <td class="pk-num">${number(agent.total_completion_tokens)}</td>
        <td class="pk-num">${number(agent.total_cached_tokens)} <span class="pk-muted">${share === null ? '(–)' : `(${share.toFixed(0)}%)`}</span></td>
        <td class="pk-num">${number(agent.total_cache_write_tokens)}</td>
        <td class="pk-num">${cost(agent.total_cost ?? 0, estimated)}</td>
        <td class="pk-num">${cost(agentValue(agent, 'avg_cost'), estimated)}</td>
        <td class="pk-num">${latency(agentValue(agent, 'avg_latency'))}</td>
        <td class="pk-num">${number(agent.peak_tokens)}</td>
        <td class="pk-num pk-muted">${agent.last_activity ? time(agent.last_activity) : '–'}</td>
      </tr>`;
    })}</tbody>
  </table></div>`);
}

// ------------------------------------------------------------------------ LLMs

function drawLlms() {
  const models = new Map();
  for (const call of usage.history) {
    const name = call.model || '–';
    if (!models.has(name)) models.set(name, { name, calls: 0, total: 0, prompt: 0, output: 0, cached: 0, cost: 0, costCalls: 0, estimated: 0, latencies: [] });
    const model = models.get(name);
    model.calls += 1;
    model.total += call.total_tokens || 0;
    model.prompt += call.prompt_tokens || 0;
    model.output += call.completion_tokens || 0;
    model.cached += call.cached_tokens || 0;
    if (call.cost != null) {
      model.cost += call.cost;
      model.costCalls += 1;
      if (call.cost_is_estimate) model.estimated += 1;
    }
    if (call.latency_ms != null) model.latencies.push(call.latency_ms);
  }
  const rows = [...models.values()].sort((a, b) => b.calls - a.calls);
  $('llmsCount').textContent = rows.length || '';
  if (!rows.length) {
    render($('llms'), empty('cpu', 'No LLM calls recorded'));
    return;
  }
  const average = (values) => (values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null);
  const p95 = (values) => {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor(0.95 * sorted.length))];
  };
  render($('llms'), html`<div class="pk-table-wrap"><table class="pk-table cu-table">
    <thead><tr><th>Model</th><th class="pk-num">Calls</th><th class="pk-num">Σ Tokens</th><th class="pk-num">Σ Prompt</th>
      <th class="pk-num">Σ Output</th><th class="pk-num">Σ Cached</th><th class="pk-num">Σ Cost</th><th class="pk-num">Ø Cost</th>
      <th class="pk-num">Ø Latency</th><th class="pk-num">p95 Latency</th></tr></thead>
    <tbody>${rows.map((model) => html`<tr>
      <td class="pk-mono">${model.name}</td>
      <td class="pk-num">${number(model.calls)}</td>
      <td class="pk-num">${number(model.total)}</td>
      <td class="pk-num">${number(model.prompt)}</td>
      <td class="pk-num">${number(model.output)}</td>
      <td class="pk-num">${number(model.cached)} <span class="pk-muted">(${model.prompt ? ((model.cached / model.prompt) * 100).toFixed(0) : 0}%)</span></td>
      <td class="pk-num">${cost(model.costCalls ? model.cost : null, model.estimated > 0)}</td>
      <td class="pk-num">${cost(model.costCalls ? model.cost / model.costCalls : null, model.estimated > 0)}</td>
      <td class="pk-num">${latency(average(model.latencies))}</td>
      <td class="pk-num">${latency(p95(model.latencies))}</td>
    </tr>`)}</tbody>
  </table></div>`);
}

// ----------------------------------------------------------------------- calls

function drawCalls() {
  const select = $('callAgent');
  fillAgents(select);
  const limit = Number($('callLimit').value);
  const matching = usage.history.filter((call) => !select.value || call.agent_name === select.value);
  callRows = matching.slice(-limit).reverse();  // newest first
  $('callsCount').textContent = usage.history.length || '';
  $('callsShown').textContent = matching.length ? `${callRows.length} of ${matching.length}` : '';
  if (!callRows.length) {
    render($('calls'), empty('history', 'No calls recorded'));
    return;
  }
  const own = scoped();
  render($('calls'), html`<div class="pk-table-wrap"><table class="pk-table cu-table cu-calls">
    <thead><tr><th>Time</th><th>Agent</th><th>Session · Request</th><th>Model</th><th class="pk-num">Prompt</th>
      <th class="pk-num">Output</th><th class="pk-num">Cached</th><th class="pk-num">Writes</th><th class="pk-num">Context</th>
      <th class="pk-num">Latency</th><th class="pk-num">Cost</th></tr></thead>
    <tbody>${callRows.map((call, index) => html`<tr tabindex="0" data-index="${index}">
      <td class="pk-mono">${time(call.timestamp)}</td>
      <td>${own && call.session_id !== own ? html`<span class="pk-muted" title="a sub-agent's call">↳ </span>` : ''}${call.agent_name}</td>
      <td class="pk-mono cu-ids" title="${`session: ${call.session_id || '–'}\nrequest: ${call.request_id || '–'}`}">${call.session_id || '–'}${call.request_id ? ` · ${call.request_id}` : ''}</td>
      <td class="pk-mono">${call.model || '–'}</td>
      <td class="pk-num">${number(call.prompt_tokens)}</td>
      <td class="pk-num">${number(call.completion_tokens)}</td>
      <td class="pk-num">${number(call.cached_tokens)}</td>
      <td class="pk-num">${number(call.cache_write_tokens)}</td>
      <td class="pk-num pk-muted">${percent(call.usage_percentage)}</td>
      <td class="pk-num">${latency(call.latency_ms)}</td>
      <td class="pk-num">${cost(call.cost, call.cost_is_estimate)}</td>
    </tr>`)}</tbody>
  </table></div>`);
}

function openCall(index) {
  const call = callRows[index];
  const facts = [
    ['Time', dateTime(call.timestamp)], ['Agent', call.agent_name], ['Agent id', call.agent_id],
    ['Model', call.model || '–'], ['Session', call.session_id || '–'], ['Request', call.request_id || '–'],
    ['Prompt tokens', number(call.prompt_tokens)], ['Output tokens', number(call.completion_tokens)],
    ['Total tokens', number(call.total_tokens)], ['Cached (read)', number(call.cached_tokens)],
    ['Cache writes', number(call.cache_write_tokens)], ['Tool definitions', `${number(call.tool_definition_tokens)} tokens`],
    ['Messages', number(call.message_count)], ['Latency', latency(call.latency_ms)],
    ['Context', `${number(call.total_tokens)} of ${number(call.context_window)} (${percent(call.usage_percentage)})`],
    ['Cost', call.cost == null ? '–' : `${call.cost_is_estimate ? '~' : ''}${cents(call.cost, 4)}${call.cost_is_estimate ? ' (estimated, llm_pricing.yaml)' : ''}`],
  ];
  $('detailTitle').textContent = `${call.agent_name} · ${time(call.timestamp)}`;
  render($('detailBody'), html`<dl class="pk-kv">${facts.map(([label, value]) => html`<dt>${label}</dt><dd class="cu-value">${value}</dd>`)}</dl>`);
  $('detail').showModal();
}

// --------------------------------------------------------------------- wiring

async function clearHistory() {
  const asked = await confirm('Delete every recorded call and all agent statistics, of every session?',
    { title: 'Clear the usage history', confirmLabel: 'Clear', danger: true });
  if (!asked) return;
  try {
    await api(`${BASE}clear`, { method: 'POST' });
  } catch {
    return;  // api() has shown the failure
  }
  refresh();
}

document.querySelectorAll('[data-scope]').forEach((button) => button.addEventListener('click', () => {
  scope = button.dataset.scope;
  document.querySelectorAll('[data-scope]').forEach((other) => other.setAttribute('aria-pressed', String(other === button)));
  refresh();
}));
document.addEventListener('refresh', refresh);
session.onChange(() => {
  if (scope === 'session' && !pinned) refresh();
});
$('clearButton').addEventListener('click', clearHistory);
$('chartAgent').addEventListener('change', drawChart);
$('callAgent').addEventListener('change', drawCalls);
$('callLimit').addEventListener('change', drawCalls);
$('agents').addEventListener('click', (event) => {
  const header = event.target.closest('[data-sort]');
  if (!header) return;
  const key = header.dataset.sort;
  agentSort = { key, dir: agentSort.key === key ? -agentSort.dir : -1 };
  drawAgents();
});
$('calls').addEventListener('click', (event) => {
  const row = event.target.closest('tr[data-index]');
  if (row) openCall(Number(row.dataset.index));
});
$('calls').addEventListener('keydown', (event) => {
  const row = event.target.closest('tr[data-index]');
  if (row && (event.key === 'Enter' || event.key === ' ')) {
    event.preventDefault();
    openCall(Number(row.dataset.index));
  }
});
$('detail').addEventListener('click', (event) => {
  const drawer = $('detail');
  const box = drawer.getBoundingClientRect();
  const outside = event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
  if (event.target.closest('[data-close]') || (event.target === drawer && outside)) drawer.close();
});

refresh();
