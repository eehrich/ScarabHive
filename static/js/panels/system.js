// System panel: status with its reasons (admin; others see liveness), running requests (admin), tool servers (admin).
// Only the visible tab loads; a tab the viewer may not see is asked once.
import { api, html, render, icon, confirm, toast, trusted, ApiError } from '/static/kit/panel-kit.js';

const $ = (id) => document.getElementById(id);
let servers = [];
let activeTab = 'overview';
const refused = new Set();
/** request ids a cancel was asked for and not yet answered: a refresh must not offer them again */
const cancelling = new Set();

function uptime(seconds) {
  const s = Math.floor(seconds || 0);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function stat(label, value) {
  return html`<div class="pk-stat"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div></div>`;
}

/** Each part of the panel says for itself why it is empty: role, missing route, or failure. */
function unavailable(target, error) {
  const status = error instanceof ApiError ? error.status : 0;
  const text = status === 403 ? 'Administrators only'
    : status === 404 ? 'Not available on this server'
    : `Could not be loaded: ${error.message}`;
  render(target, html`<div class="pk-empty">${icon(status === 403 ? 'shield' : 'circle-alert')}<div class="pk-empty-title">${text}</div></div>`);
  return status === 403 || status === 404;
}

const STATUS = {
  ok: { label: 'Healthy', badge: 'ok', dot: 'ok' },
  warn: { label: 'Needs attention', badge: 'warn', dot: 'warn' },
  error: { label: 'Problems', badge: 'danger', dot: 'danger' },
};
const CHECK_NAMES = { servers: 'Servers', config: 'Agent config', llm: 'LLM rate limits', deploy: 'Deployed code' };

/**
 * Everyone may ask /health: it only says the server answers. The reasons behind a status are
 * admin data -- refused (403) to others, and absent (404) when the server runs without authentication.
 */
async function loadLiveness(why) {
  let health;
  try {
    health = await api('/health', { quiet: true });
  } catch (error) {
    unavailable($('statusCard'), error);
    return;
  }
  render($('statusCard'), html`
    <div class="pk-card-head"><span class="pk-dot pk-dot--ok"></span><h3 class="pk-card-title">Server answers</h3></div>
    <p class="pk-secondary" style="margin:0">${why}</p>`);
  render($('overviewStats'), [stat('Uptime', uptime(health.uptime_seconds)), stat('Version', health.version)]);
}

async function loadOverview() {
  let data;
  try {
    data = await api('/admin/system', { quiet: true });
  } catch (error) {
    if (error instanceof ApiError && (error.status === 403 || error.status === 401)) {
      await loadLiveness('Why the server is healthy or not is shown to administrators.');
      return;
    }
    if (error instanceof ApiError && error.status === 404) {
      await loadLiveness('Status details are not available on this server (authentication off, or an older server version).');
      return;
    }
    unavailable($('statusCard'), error);
    return;
  }
  const status = STATUS[data.status] || STATUS.error;
  render($('statusCard'), html`
    <div class="pk-card-head">
      <span class="pk-dot pk-dot--${status.dot}"></span>
      <h3 class="pk-card-title">Status</h3>
      <span class="pk-badge pk-badge--${status.badge}">${status.label}</span>
    </div>
    <dl class="pk-kv">${data.checks.map((check) => html`
      <dt><span class="pk-dot pk-dot--${(STATUS[check.level] || STATUS.error).dot}"></span> ${CHECK_NAMES[check.name] || check.name}</dt>
      <dd>${check.detail}</dd>`)}</dl>`);

  const commit = data.build.commit;
  const running = await api('/admin/active-sessions', { quiet: true }).then((r) => r.total).catch(() => null);
  render($('overviewStats'), [
    stat('Uptime', uptime(data.build.uptime_seconds)),
    stat('Servers', `${data.servers.running} / ${data.servers.declared}`),
    stat('Agents', data.servers.agents),
    stat('Hooks on', `${data.hooks.enabled} / ${data.hooks.registered}`),
    stat('Running requests', running ?? '—'),
  ]);

  const problems = data.servers.problems;
  $('problemsCard').hidden = !problems.length;
  render($('problems'), problems.map((problem) => html`<li class="pk-mono">${problem}</li>`));
  const findings = data.servers.config_findings || [];
  $('findingsCard').hidden = !findings.length;
  render($('findings'), findings.map((finding) => html`<li class="pk-mono">${finding}</li>`));

  $('processCard').hidden = false;
  const started = data.build.started_at ? new Date(data.build.started_at * 1000).toLocaleString() : '—';
  render($('process'), [
    ['Started', started],
    ['Commit', commit ? `${commit.hash.slice(0, 8)} ${commit.subject}` : '—'],
    ['Memory', data.process.memory_mb == null ? '—' : `${data.process.memory_mb} MB`],
    ['Threads', data.process.threads],
    ['Async tasks', data.process.async_tasks],
    ['PID', data.process.pid],
    ['Python', data.build.python],
  ].map(([k, v]) => html`<dt>${k}</dt><dd class="pk-mono">${v}</dd>`));
}

async function loadRequests() {
  let data;
  try {
    data = await api('/admin/active-sessions', { quiet: true });
  } catch (error) {
    if (unavailable($('tab-requests'), error)) refused.add('requests');
    return;
  }
  $('requestCount').textContent = data.total || '';
  if (!data.sessions.length) {
    render($('tab-requests'), html`<div class="pk-empty">${icon('circle-check')}<div class="pk-empty-title">Nothing running</div></div>`);
    return;
  }
  render($('tab-requests'), html`
    <div class="pk-table-wrap"><table class="pk-table">
      <thead><tr><th>User</th><th>Agent</th><th class="pk-num">Running</th><th>Status</th><th>Request</th><th></th></tr></thead>
      <tbody>${data.sessions.map((s) => html`
        <tr>
          <td>${s.user_id}</td><td>${s.agent_name}</td><td class="pk-num">${uptime(s.duration_seconds)}</td>
          <td><span class="pk-badge pk-badge--${s.status === 'running' ? 'info' : 'warn'}">${s.status}</span></td>
          <td class="pk-mono">${s.request_id.slice(0, 12)}</td>
          <td><button class="pk-btn pk-btn--sm pk-btn--ghost" data-cancel="${s.request_id}" ${cancelling.has(s.request_id) ? trusted('disabled') : ''}>${icon('ban')} Cancel</button></td>
        </tr>`)}</tbody>
    </table></div>`);
}

/** The cancel button of a request as the table shows it now -- a refresh may have drawn it anew. */
function setCancelling(requestId, busy) {
  if (busy) cancelling.add(requestId);
  else cancelling.delete(requestId);
  const button = $('tab-requests').querySelector(`[data-cancel="${CSS.escape(requestId)}"]`);
  if (button) button.disabled = busy;
}

async function cancelRequest(requestId) {
  if (!await confirm(`Cancel request ${requestId.slice(0, 12)}?`, { title: 'Cancel request', confirmLabel: 'Cancel request', danger: true })) return;
  setCancelling(requestId, true);  // the server waits for the run to stop before it answers
  let result;
  try {
    result = await api(`/admin/active-sessions/${encodeURIComponent(requestId)}/cancel`, { method: 'POST' });
  } catch {
    return;  // api() has shown the failure
  } finally {
    setCancelling(requestId, false);
  }
  if (result.status === 'cancelled') toast('Request cancelled', { kind: 'ok' });
  else toast('The request had already finished');
  loadRequests();
}

function renderServers() {
  const needle = $('serverFilter').value.trim().toLowerCase();
  const matches = (server) => !needle || [server.name, server.type, server.description, ...(server.tools || [])]
    .some((text) => String(text || '').toLowerCase().includes(needle));
  const shown = servers.filter(matches);
  if (!shown.length) {
    render($('serverList'), html`<div class="pk-empty">${icon('server')}<div class="pk-empty-title">No servers</div></div>`);
    return;
  }
  render($('serverList'), shown.map((server) => {
    const up = server.connected || server.reachable;
    return html`
      <div class="pk-card">
        <div class="pk-card-head">
          <span class="pk-dot pk-dot--${up ? 'ok' : 'danger'}"></span>
          <h3 class="pk-card-title">${server.name}</h3>
          ${server.type ? html`<span class="pk-badge">${server.type}</span>` : ''}
          <span class="pk-grow"></span><span class="pk-muted">${server.tool_count || 0} tools</span>
        </div>
        ${server.description ? html`<p class="pk-secondary" style="margin:0 0 var(--space-2)">${server.description}</p>` : ''}
        ${server.error ? html`<p class="pk-error" style="margin:0">${server.error}</p>` : ''}
        ${(server.tools || []).length ? html`<div class="pk-row">${server.tools.map((t) => html`<span class="pk-badge pk-mono">${t}</span>`)}</div>` : ''}
      </div>`;
  }));
}

async function loadServers(forceRefresh = false) {
  const button = $('serverRefresh');
  button.disabled = true;
  let data;
  try {
    data = await api(`/tools/status${forceRefresh ? '?force_refresh=true' : ''}`, { quiet: true });
  } catch (error) {
    if (unavailable($('serverList'), error)) refused.add('servers');
    return;
  } finally {
    button.disabled = false;
  }
  if (data.error) {
    render($('serverList'), html`<div class="pk-empty">${icon('circle-alert')}<div class="pk-empty-title">${data.error}</div></div>`);
    return;
  }
  servers = data.servers || [];
  $('serverCount').textContent = servers.length || '';
  renderServers();
}

const loaders = { overview: loadOverview, requests: loadRequests, servers: () => loadServers(false) };

function loadActiveTab() {
  if (!refused.has(activeTab)) loaders[activeTab]();
}

document.querySelector('[data-pk-tabs]').addEventListener('tabchange', (event) => {
  activeTab = event.detail.tab;
  loadActiveTab();
});
document.addEventListener('refresh', (event) => {
  // the status check probes every external server: on a click, not on every tick
  if (event.detail.auto && activeTab === 'servers') return;
  loadActiveTab();
});
$('serverFilter').addEventListener('input', renderServers);
$('serverRefresh').addEventListener('click', () => loadServers(true));
$('tab-requests').addEventListener('click', (event) => {
  const button = event.target.closest('[data-cancel]');
  if (button) cancelRequest(button.dataset.cancel);
});
loadActiveTab();
