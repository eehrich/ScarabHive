// System panel: health, event bus, running requests (admin), MCP servers (admin).
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

async function loadOverview() {
  let health;
  try {
    health = await api('/health', { quiet: true });
  } catch (error) {
    unavailable($('overviewStats'), error);
    return;
  }
  render($('overviewStats'), [
    stat('Status', health.status),
    stat('Uptime', uptime(health.uptime_seconds)),
    stat('Version', health.version),
    stat('Python', health.python_version),
  ]);
  render($('packages'), Object.entries(health.packages || {})
    .map(([name, version]) => html`<dt>${name}</dt><dd class="pk-mono">${version}</dd>`));
  let meta;
  try {
    meta = await api('/status/meta', { quiet: true });
  } catch (error) {
    unavailable($('busStats'), error);
    return;
  }
  const bus = [
    ['Subscribers', meta.subscribers], ['Events published', meta.publish_attempted],
    ['Events delivered', meta.delivered], ['Handlers', meta.handlers_count],
  ];
  render($('busStats'), bus.map(([k, v]) => html`<dt>${k}</dt><dd class="pk-mono">${v ?? '—'}</dd>`));
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
    data = await api(`/mcp/status${forceRefresh ? '?force_refresh=true' : ''}`, { quiet: true });
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
