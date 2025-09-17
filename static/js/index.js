// Client-side UI logic extracted from templates/index.html
const chat = document.getElementById('chat');
const form = document.getElementById('f');
const runBtn = document.getElementById('runBtn');
const statusMetrics = document.getElementById('statusMetrics');

// Status metrics update function
async function updateStatusMetrics() {
  try {
    const response = await fetch('/status/meta');
    if (response.ok) {
      const data = await response.json();
      const html = `
        <div class="metric-item">
          <span class="metric-label">Subscribers</span>
          <span class="metric-value">${data.subscribers || 0}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Events Published</span>
          <span class="metric-value">${data.publish_attempted || 0}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Events Delivered</span>
          <span class="metric-value">${data.delivered || 0}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Events Suppressed</span>
          <span class="metric-value">${(data.suppressed_rate || 0) + (data.suppressed_debounce || 0)}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Max RPS</span>
          <span class="metric-value">${data.config.AGENT_STATUS_MAX_RPS || 'Unlimited'}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Debounce MS</span>
          <span class="metric-value">${data.config.AGENT_STATUS_DEBOUNCE_MS || 'None'}</span>
        </div>
      `;
      const sidebarMeta = document.getElementById('statusMetrics');
      const floatingMeta = document.getElementById('floatingStatusMetrics');
      if (sidebarMeta) sidebarMeta.innerHTML = html;
      if (floatingMeta) floatingMeta.innerHTML = html;
    }
  } catch (error) {
    if (statusMetrics) statusMetrics.innerHTML = `
      <div class="metric-item">
        <span class="metric-label">Status</span>
        <span class="metric-value">Error loading</span>
      </div>
    `;
  }
}

// MCP servers update function with state preservation and filtering
async function updateMCPServers() {
  try {
    const response = await fetch('/mcp/status');
    if (response.ok) {
      const data = await response.json();

      const totalServers = data.total_servers || 0;
      const totalTools = data.total_tools || 0;
      const servers = data.servers || [];

      // Get current filter value
      const filterInput = document.getElementById('mcpFilterInput');
      const filterText = filterInput ? filterInput.value.toLowerCase().trim() : '';

      // Preserve state before update
      const mcpMetrics = document.getElementById('floatingMCPMetrics');
      let scrollPosition = 0;
      let expandedStates = {};

      if (mcpMetrics) {
        const serversList = mcpMetrics.querySelector('.mcp-servers-list');
        if (serversList) {
          // Save scroll position
          scrollPosition = serversList.scrollTop;

          // Save expanded states
          const detailElements = serversList.querySelectorAll('[id^="details-"]');
          detailElements.forEach(el => {
            const serverId = el.id.replace('details-', '');
            expandedStates[serverId] = el.style.display !== 'none';
          });
        }
      }

      let connectedServers = 0;
      let html = '';
      let filteredCount = 0;

      // Enhanced styling for servers with better tool display
      for (let i = 0; i < servers.length; i++) {
        const server = servers[i];
        if (server.connected) connectedServers++;

        // Filter logic - check server name and tool names
        let matchesFilter = !filterText;
        if (filterText) {
          // Check server name
          if (server.name && server.name.toLowerCase().includes(filterText)) {
            matchesFilter = true;
          }
          // Check tool names
          if (!matchesFilter && server.tools) {
            const toolsToCheck = server.detailed_tools || server.tools;
            matchesFilter = toolsToCheck.some(tool => {
              const toolName = typeof tool === 'string' ? tool : tool.name;
              return toolName && toolName.toLowerCase().includes(filterText);
            });
          }
        }

        if (!matchesFilter) continue;
        filteredCount++;

        const statusClass = server.connected ? 'connected' : 'disconnected';
        const statusText = server.connected ? 'Connected' : 'Disconnected';

        // Better tool display with descriptions
        const toolsList = server.tools && server.tools.length > 0
          ? server.detailed_tools && server.detailed_tools.length > 0
            ? server.detailed_tools.map(tool => {
                // Highlight matching tools
                let toolName = tool.name;
                let toolDesc = tool.description || `Tool for ${server.name.toLowerCase()}`;
                if (filterText) {
                  if (toolName.toLowerCase().includes(filterText)) {
                    const regex = new RegExp(`(${filterText})`, 'gi');
                    toolName = toolName.replace(regex, '<mark>$1</mark>');
                  }
                }
                return `
                  <li class="tool-item ${tool.blocked ? 'tool-blocked' : ''}">
                    <div class="tool-name">
                      ${toolName}
                      ${tool.blocked ? '<span class="tool-status blocked">BLOCKED</span>' : ''}
                    </div>
                    <div class="tool-description">${toolDesc}</div>
                  </li>`;
              }).join('')
            : server.tools.map(tool => {
                let toolName = tool;
                if (filterText && toolName.toLowerCase().includes(filterText)) {
                  const regex = new RegExp(`(${filterText})`, 'gi');
                  toolName = toolName.replace(regex, '<mark>$1</mark>');
                }
                return `
                  <li class="tool-item">
                    <div class="tool-name">${toolName}</div>
                    <div class="tool-description">Tool for ${server.name.toLowerCase()}</div>
                  </li>`;
              }).join('')
          : '<li class="tool-item"><div class="tool-name">No tools available</div></li>';

        // Check if this server was expanded before
        const wasExpanded = expandedStates[server.id] || false;
        const displayStyle = wasExpanded ? 'block' : 'none';

        // Highlight matching server names
        let serverName = server.name;
        if (filterText && serverName && serverName.toLowerCase().includes(filterText)) {
          const regex = new RegExp(`(${filterText})`, 'gi');
          serverName = serverName.replace(regex, '<mark>$1</mark>');
        }

        html += `
          <div class="mcp-server" onclick="toggleServerDetails('${server.id}')">
            <div class="mcp-server-info">
              <div class="server-name-type">
                <strong>${serverName}</strong>
                <span class="server-type">(${server.type})</span>
              </div>
              <span class="mcp-status ${statusClass}">${statusText}</span>
            </div>
            <div class="tool-count">${server.tool_count || 0} tools <span class="expand-indicator ${wasExpanded ? 'expanded' : ''}" id="indicator-${server.id}"></span></div>
            <div class="server-details" id="details-${server.id}" style="display: ${displayStyle};">
              <div class="tools-list">
                <h4>Available Tools:</h4>
                <ul class="tools-container">${toolsList}</ul>
              </div>
              ${server.url ? `<div class="server-url"><strong>URL:</strong> ${server.url}</div>` : ''}
              ${server.error ? `<div class="server-error"><strong>Error:</strong> ${server.error}</div>` : ''}
            </div>
          </div>`;
      }

      const summaryHtml = `
        <div class="mcp-summary">
          <div class="metric-item">
            <span class="metric-label">Total Servers</span>
            <span class="metric-value">${totalServers}</span>
          </div>
          <div class="metric-item">
            <span class="metric-label">Connected</span>
            <span class="metric-value">${connectedServers}</span>
          </div>
          <div class="metric-item">
            <span class="metric-label">Total Tools</span>
            <span class="metric-value">${totalTools}</span>
          </div>
          ${filterText ? `
          <div class="metric-item">
            <span class="metric-label">Filtered</span>
            <span class="metric-value">${filteredCount}/${totalServers}</span>
          </div>` : ''}
        </div>
        <div class="mcp-servers-list">
          ${html || (filterText ? '<div class="no-servers">No servers match the filter</div>' : '<div class="no-servers">No MCP servers configured</div>')}
        </div>
      `;

      if (mcpMetrics) {
        mcpMetrics.innerHTML = summaryHtml;

        // Restore scroll position after DOM update
        const serversList = mcpMetrics.querySelector('.mcp-servers-list');
        if (serversList && typeof scrollPosition === 'number') {
          // Temporarily disable smooth scrolling to avoid animated jump
          const prevBehavior = serversList.style.scrollBehavior || '';
          serversList.style.scrollBehavior = 'auto';
          requestAnimationFrame(() => {
            serversList.scrollTop = scrollPosition;
            // Restore original behavior on next frame to keep UX smooth for user actions
            requestAnimationFrame(() => {
              serversList.style.scrollBehavior = prevBehavior;
            });
          });
        }
      }

      // Update button to show it's working
      const mcpButton = document.getElementById('mcpToggleBtn');
      if (mcpButton) {
        mcpButton.textContent = `MCP-Servers (${totalServers})`;
        // Re-add the badge span (textContent removes it)
        const badgeSpan = document.createElement('span');
        badgeSpan.id = 'mcpConnected';
        badgeSpan.className = connectedServers > 0 ? 'mcp-connected connected' : 'mcp-connected';
        badgeSpan.title = `${connectedServers}/${totalServers} servers connected`;
        badgeSpan.setAttribute('aria-hidden', 'true');
        mcpButton.appendChild(badgeSpan);
      }

    }
  } catch (error) {
    console.error('MCP Update: Error:', error);
  }
}// Floating status panel
const statusPanel = document.createElement('div');
statusPanel.id = 'floatingStatusPanel';
statusPanel.className = 'floating-panel';
statusPanel.style.display = 'none';
statusPanel.innerHTML = `
  <div class="floating-panel-header" id="floatingStatusHeader">
    <span>Status & Metrics</span>
    <div style="margin-left:8px;flex:1"></div>
    <button id="floatingCloseBtn" title="Close" aria-label="Close">✕</button>
  </div>
  <div class="floating-panel-body" id="floatingStatusBody">
    <div class="status-metrics" id="floatingStatusMetrics">
      <div class="metric-item"><span class="metric-label">Loading...</span><span class="metric-value">...</span></div>
    </div>
  </div>
  <div class="resize-handle"></div>
`;
document.body.appendChild(statusPanel);

// Floating MCP panel
const mcpPanel = document.createElement('div');
mcpPanel.id = 'floatingMCPPanel';
mcpPanel.className = 'floating-panel';
mcpPanel.style.display = 'none';
mcpPanel.innerHTML = `
  <div class="floating-panel-header" id="floatingMCPHeader">
    <span>MCP Servers & Tools</span>
    <div style="margin-left:8px;flex:1"></div>
    <input id="mcpFilterInput" type="text" placeholder="Filter servers/tools..." class="filter-input" title="Filter by server or tool name" />
    <button id="mcpRefreshBtn" class="icon-btn" title="Refresh now" aria-label="Refresh now">
      <svg class="mcp-refresh-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M21 12a9 9 0 10-2.6 6.1" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M21 3v6h-6" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span class="spinner icon-spinner" aria-hidden="true"></span>
    </button>
    <label class="toggle-switch" title="Auto-refresh">
      <input id="mcpAutoRefreshToggle" type="checkbox" />
      <span class="toggle-slider"></span>
    </label>
    <input id="mcpAutoRefreshInterval" class="number-input" type="number" min="5" step="5" value="30" title="Auto-refresh interval (seconds)" />
    <button id="floatingMCPCloseBtn" title="Close" aria-label="Close">✕</button>
  </div>
  <div class="floating-panel-body" id="floatingMCPBody">
    <div class="mcp-metrics" id="floatingMCPMetrics">
      <div class="metric-item"><span class="metric-label">Loading...</span><span class="metric-value">...</span></div>
    </div>
  </div>
  <div class="resize-handle"></div>
`;
document.body.appendChild(mcpPanel);

// Try to restore saved panel state (position/size/visible/auto-refresh)
function loadPanelState(id) {
  try {
    const raw = localStorage.getItem('panelState:' + id);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) { return null; }
}

function savePanelState(id, state) {
  try {
    localStorage.setItem('panelState:' + id, JSON.stringify(state));
  } catch (e) { /* ignore */ }
}

function applyPanelState(panel, state) {
  if (!panel || !state) return;
  if (state.left !== undefined) panel.style.left = state.left + 'px';
  if (state.top !== undefined) panel.style.top = state.top + 'px';
  if (state.width !== undefined) panel.style.width = state.width + 'px';
  if (state.height !== undefined) panel.style.height = state.height + 'px';
  if (state.visible) panel.style.display = 'block';
}

// Restore MCP panel state if present
const savedMcp = loadPanelState('floatingMCPPanel');
if (savedMcp) applyPanelState(mcpPanel, savedMcp);

// Auto-refresh control
let mcpAutoRefreshTimer = null;
function startMcpAutoRefresh(intervalSec) {
  stopMcpAutoRefresh();
  const ms = Math.max(5000, (intervalSec || 30) * 1000);
  mcpAutoRefreshTimer = setInterval(() => { updateMCPServers().catch(()=>{}); }, ms);
}
function stopMcpAutoRefresh() {
  if (mcpAutoRefreshTimer) { clearInterval(mcpAutoRefreshTimer); mcpAutoRefreshTimer = null; }
}

// Initialize auto-refresh UI from preferences
const autoToggle = document.getElementById('mcpAutoRefreshToggle');
const autoIntervalInput = document.getElementById('mcpAutoRefreshInterval');
try {
  const pref = loadPanelState('floatingMCPPanel') || {};
  if (pref.autoRefresh) {
    if (autoToggle) autoToggle.checked = true;
    const interval = pref.autoRefreshInterval || 30;
    if (autoIntervalInput) autoIntervalInput.value = interval;
    startMcpAutoRefresh(interval);
  }
} catch (e) {}

if (autoToggle) {
  autoToggle.addEventListener('change', () => {
    const enabled = autoToggle.checked;
    const interval = parseInt(autoIntervalInput.value || '30', 10);
    // persist
    const state = loadPanelState('floatingMCPPanel') || {};
    state.autoRefresh = enabled;
    state.autoRefreshInterval = interval;
    savePanelState('floatingMCPPanel', state);
    if (enabled) startMcpAutoRefresh(interval); else stopMcpAutoRefresh();
  });
}
if (autoIntervalInput) {
  autoIntervalInput.addEventListener('change', () => {
    const interval = parseInt(autoIntervalInput.value || '30', 10);
    const state = loadPanelState('floatingMCPPanel') || {};
    state.autoRefreshInterval = interval; savePanelState('floatingMCPPanel', state);
    if (autoToggle && autoToggle.checked) startMcpAutoRefresh(interval);
  });
}

const statusToggleBtn = document.getElementById('statusToggleBtn');
const floatingCloseBtn = document.getElementById('floatingCloseBtn');
const floatingStatusMetrics = document.getElementById('floatingStatusMetrics');

// MCP panel elements
const mcpToggleBtn = document.getElementById('mcpToggleBtn');
const floatingMCPCloseBtn = document.getElementById('floatingMCPCloseBtn');
const floatingMCPMetrics = document.getElementById('floatingMCPMetrics');
const mcpRefreshBtn = document.getElementById('mcpRefreshBtn');

if (mcpRefreshBtn) {
  mcpRefreshBtn.addEventListener('click', async (ev) => {
    try {
      mcpRefreshBtn.classList.add('loading');
      mcpRefreshBtn.setAttribute('aria-busy', 'true');
      mcpRefreshBtn.disabled = true;
      await updateMCPServers();
    } catch (e) {
      console.error('Manual MCP refresh failed', e);
    } finally {
      mcpRefreshBtn.classList.remove('loading');
      mcpRefreshBtn.removeAttribute('aria-busy');
      mcpRefreshBtn.disabled = false;
    }
  });
}

// Filter input event listener
const mcpFilterInput = document.getElementById('mcpFilterInput');
if (mcpFilterInput) {
  let filterTimeout;
  mcpFilterInput.addEventListener('input', () => {
    // Debounce the filter to avoid excessive updates while typing
    clearTimeout(filterTimeout);
    filterTimeout = setTimeout(() => {
      updateMCPServers().catch(() => {});
    }, 300);
  });
}

statusToggleBtn.addEventListener('click', () => {
  const shown = statusPanel.style.display !== 'none';
  statusPanel.style.display = shown ? 'none' : 'block';
  statusToggleBtn.setAttribute('aria-expanded', String(!shown));
  if (!shown) updateStatusMetrics().catch(() => {});
});

floatingCloseBtn.addEventListener('click', () => {
  statusPanel.style.display = 'none';
  statusToggleBtn.setAttribute('aria-expanded', 'false');
});

// MCP panel event listeners
if (mcpToggleBtn) {
  mcpToggleBtn.addEventListener('click', () => {
    const shown = mcpPanel.style.display !== 'none';
    mcpPanel.style.display = shown ? 'none' : 'block';
    mcpToggleBtn.setAttribute('aria-expanded', String(!shown));
    if (!shown) updateMCPServers().catch(() => {});
  });
}

floatingMCPCloseBtn.addEventListener('click', () => {
  mcpPanel.style.display = 'none';
  if (mcpToggleBtn) mcpToggleBtn.setAttribute('aria-expanded', 'false');
});

// Make panel draggable
(function makeDraggable(headerId, panel) {
  const header = document.getElementById(headerId);
  let isDragging = false;
  let startX = 0, startY = 0, origX = 0, origY = 0;
  header.addEventListener('pointerdown', (ev) => {
    try {
      if (ev.target && ev.target.closest && ev.target.closest('#floatingCloseBtn')) return;
    } catch (e) {}
    isDragging = true;
    startX = ev.clientX; startY = ev.clientY;
    const rect = panel.getBoundingClientRect();
    origX = rect.left; origY = rect.top;
    header.setPointerCapture(ev.pointerId);
  });
  window.addEventListener('pointermove', (ev) => {
    if (!isDragging) return;
    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;
    panel.style.left = (origX + dx) + 'px';
    panel.style.top = (origY + dy) + 'px';
    panel.style.right = 'auto';
  });
  window.addEventListener('pointerup', (ev) => { isDragging = false; });
})('floatingStatusHeader', statusPanel);

// Make MCP panel draggable
(function makeDraggable(headerId, panel) {
  const header = document.getElementById(headerId);
  let isDragging = false;
  let startX = 0, startY = 0, origX = 0, origY = 0;
  header.addEventListener('pointerdown', (ev) => {
    try {
      if (ev.target && ev.target.closest && (
        ev.target.closest('#floatingMCPCloseBtn') ||
        ev.target.closest('#mcpRefreshBtn') ||
        ev.target.closest('#mcpFilterInput') ||
        ev.target.closest('#mcpAutoRefreshToggle') ||
        ev.target.closest('#mcpAutoRefreshInterval')
      )) return;
    } catch (e) {}
    isDragging = true;
    startX = ev.clientX; startY = ev.clientY;
    const rect = panel.getBoundingClientRect();
    origX = rect.left; origY = rect.top;
    header.setPointerCapture(ev.pointerId);
  });
  window.addEventListener('pointermove', (ev) => {
    if (!isDragging) return;
    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;
    panel.style.left = (origX + dx) + 'px';
    panel.style.top = (origY + dy) + 'px';
    panel.style.right = 'auto';
  });
  window.addEventListener('pointerup', (ev) => { isDragging = false; });
})('floatingMCPHeader', mcpPanel);

// Persist position when dragging ends for both panels
function attachDragPersist(headerId, panel, stateKey) {
  const header = document.getElementById(headerId);
  if (!header || !panel) return;
  header.addEventListener('pointerup', () => {
    try {
      const rect = panel.getBoundingClientRect();
      const state = loadPanelState(stateKey) || {};
      state.left = Math.round(rect.left);
      state.top = Math.round(rect.top);
      savePanelState(stateKey, state);
    } catch (e) {}
  });
}
attachDragPersist('floatingStatusHeader', statusPanel, 'floatingStatusPanel');
attachDragPersist('floatingMCPHeader', mcpPanel, 'floatingMCPPanel');

// Auto-refresh disabled by default. Use manual refresh via button to update MCP panel.

// Initialize immediately, but ensure DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  updateStatusMetrics();
  updateMCPServers();
});

// Also call immediately in case DOMContentLoaded already fired
updateStatusMetrics();
updateMCPServers();

function escapeHtml(s) { return s.replace(/[&<>]/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;'}[c])); }
function formatTime(ts) { try { return new Date(ts).toLocaleTimeString(); } catch (e) { return ts; } }

// Toggle MCP server details
function toggleServerDetails(serverId) {
  const detailsContainer = document.getElementById(`details-${serverId}`);
  const indicator = document.getElementById(`indicator-${serverId}`);

  if (detailsContainer) {
    const isVisible = detailsContainer.style.display !== 'none';
    detailsContainer.style.display = isVisible ? 'none' : 'block';

    // Update indicator class (CSS triangle rotates when expanded)
    if (indicator) {
      indicator.classList.toggle('expanded', !isVisible);
    }
  }
}

const activeOperations = new Map();

function addStatusEvent(container, ev) {
  const operationKey = `${ev.server}_${ev.request_id || 'default'}`;
  if (ev.phase === 'start') {
    const operationDiv = document.createElement('div');
    operationDiv.className = 'operation-progress';
    operationDiv.setAttribute('data-operation', operationKey);
    operationDiv.innerHTML = `
      <div class="progress-line">
        <span class="progress-icon"><div class="spinner"></div></span>
        <span class="progress-server">${escapeHtml(ev.server || 'Unknown')}</span>
        <span class="progress-message">${escapeHtml(ev.message || 'Starting...')}</span>
        <span class="progress-time">${formatTime(ev.timestamp)}</span>
      </div>
    `;
    container.appendChild(operationDiv);
    activeOperations.set(operationKey, operationDiv);
  } else if (ev.phase === 'progress') {
    const operationDiv = activeOperations.get(operationKey);
    if (operationDiv) {
      const messageSpan = operationDiv.querySelector('.progress-message');
      if (messageSpan) messageSpan.textContent = ev.message || 'In progress...';
    }
  } else if (ev.phase === 'end') {
    const operationDiv = activeOperations.get(operationKey);
    if (operationDiv) {
      const iconSpan = operationDiv.querySelector('.progress-icon');
      const messageSpan = operationDiv.querySelector('.progress-message');
      if (iconSpan) iconSpan.innerHTML = '<div class="checkmark">✓</div>';
      if (messageSpan) messageSpan.textContent = ev.message || 'Completed';
      operationDiv.classList.add('completed');
    }
    activeOperations.delete(operationKey);
  } else if (ev.phase === 'error') {
    const operationDiv = activeOperations.get(operationKey);
    if (operationDiv) {
      const iconSpan = operationDiv.querySelector('.progress-icon');
      const messageSpan = operationDiv.querySelector('.progress-message');
      if (iconSpan) iconSpan.innerHTML = '<div class="error-mark">✕</div>';
      if (messageSpan) messageSpan.textContent = ev.message || 'Error occurred';
      operationDiv.classList.add('error');
    }
    activeOperations.delete(operationKey);
  }
}

function markdownToHtml(md) {
  if (!md) return '';
  let html = md;
  html = escapeHtml(html);
  html = html.replace(/^### (.*$)/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.*$)/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.*$)/gm, '<h1>$1</h1>');
  html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/__(.*?)__/g, '<strong>$1</strong>');
  html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
  html = html.replace(/_(.*?)_/g, '<em>$1</em>');
  html = html.replace(/```[\s\S]*?```/g, function(match) {
    const content = match.replace(/```.*?\n/, '').replace(/\n```$/, '');
    return `<pre><code>${content}</code></pre>`;
  });
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/^\|(.+)\|$/gm, function(match, content) {
    const cells = content.split('|').map(cell => cell.trim());
    const cellTags = cells.map(cell => `<td>${cell}</td>`).join('');
    return `<tr>${cellTags}</tr>`;
  });
  html = html.replace(/(<tr>.*<\/tr>[\n\r]*)+/g, function(match) {
    const rows = match.trim().split(/[\n\r]+/);
    let tableContent = '';
    let inHeader = true;
    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      if (row.includes('---') || row.includes('===')) { inHeader = false; continue; }
      if (inHeader && i === 0) {
        const headerRow = row.replace(/<td>/g, '<th>').replace(/<\/td>/g, '</th>');
        tableContent += `<thead>${headerRow}</thead><tbody>`;
        inHeader = false;
      } else { tableContent += row; }
    }
    if (!tableContent.includes('<tbody>')) tableContent = `<tbody>${tableContent}</tbody>`;
    else tableContent += '</tbody>';
    return `<table class="markdown-table">${tableContent}</table>`;
  });
  html = html.replace(/^[\s]*[-*] (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>[\n\r]*)+/g, '<ul>$&</ul>');
  html = html.replace(/^[\s]*\d+\. (.+)$/gm, '<li>$1</li>');
  html = html.replace(/\n\n+/g, '</p><p>');
  html = html.replace(/\n/g, '<br>');
  if (!html.match(/^<(h[1-6]|table|ul|ol|pre|div)/)) html = `<p>${html}</p>`;
  html = html.replace(/<p><\/p>/g, '');
  html = html.replace(/<p><br><\/p>/g, '');
  return html;
}

function scrollBottom() { requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' })); }

function addUser(text) {
  const row = document.createElement('div');
  row.className = 'row';
  row.innerHTML = `<div class="msg user">${escapeHtml(text)}</div>`;
  chat.appendChild(row);
  scrollBottom();
}

function addAssistantBlock() {
  const row = document.createElement('div');
  row.className = 'row';
  const box = document.createElement('div');
  box.className = 'msg assistant';
  box.innerHTML = `
    <details id="thinkingBox"><summary>Thinking…</summary><pre id="thinking"></pre></details>
    <div id="conversationFlow" class="conversation-flow">
      <div id="statusContainer" class="status-container">
        <div class="status-header">
          <span class="response-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
              <rect x="3" y="4" width="18" height="16" rx="2" fill="#0f172a" stroke="#58a6ff" stroke-width="0.8" />
              <path d="M7 9l2 2 4-4" stroke="#56d364" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </span>
          <span class="response-label">Status</span>
        </div>
        <div id="statusBody" class="status-body"></div>
      </div>
      <div id="assistantText" class="response-content"></div>
    </div>
  `;
  row.appendChild(box);
  chat.appendChild(row);
  scrollBottom();
  return {
    row, box, t: box.querySelector('#assistantText'), think: box.querySelector('#thinking'),
    status: box.querySelector('#statusBody'), thinkBox: box.querySelector('#thinkingBox'), conversationFlow: box.querySelector('#conversationFlow')
  };
}

form.addEventListener('submit', e => { e.preventDefault(); run(); });

async function run() {
  const taskInput = document.getElementById('task');
  const task = taskInput.value.trim();
  if (!task) return;
  addUser(task); taskInput.value = '';
  const blk = addAssistantBlock(); runBtn.disabled = true;
  let sseOk = false;
  const es = new EventSource(`/events?task=${encodeURIComponent(task)}`);
  let statusEs = null;
  try {
    statusEs = new EventSource('/status/stream');
    statusEs.onmessage = (ev) => { try { const statusData = JSON.parse(ev.data); addStatusEvent(blk.status, statusData); } catch (err) {} };
  } catch (err) { console.warn('Status stream not available:', err); }
  es.onopen = () => { console.info('SSE connected'); sseOk = true; };
  es.onmessage = ev => {
    try {
      const data = JSON.parse(ev.data);
      switch (data.type) {
        case 'thinking':
          if (data.assistant) {
            if (data.assistant.content) blk.think.textContent += `💭 Step ${data.step}: ${data.assistant.content}\n\n`;
            if (data.assistant.tool_calls && data.assistant.tool_calls.length > 0) {
              blk.think.textContent += `🧠 Step ${data.step}: Planning to call ${data.assistant.tool_calls.length} tool(s):\n`;
              data.assistant.tool_calls.forEach((tc, i) => { const func = tc.function || {}; blk.think.textContent += `  ${i + 1}. ${func.name || 'unknown'}\n`; });
              blk.think.textContent += '\n';
            }
          } else { blk.think.textContent += `🤔 Step ${data.step}: Analyzing task...\n`; }
          break;
        case 'final':
          const content = data.summary || data.content || '';
          blk.t.innerHTML = `
            <div class="response-header">
              <span class="response-icon">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                  <rect x="2" y="3" width="20" height="14" rx="3" fill="#0f172a" stroke="#58a6ff" stroke-width="0.8" />
                  <circle cx="8.5" cy="9" r="1.1" fill="#cbd5e1" />
                  <circle cx="15.5" cy="9" r="1.1" fill="#cbd5e1" />
                  <path d="M7 13c1 0 2 0.8 3 0.8s2-0.8 3-0.8" stroke="#9fb8d9" stroke-width="0.9" stroke-linecap="round" stroke-linejoin="round" />
                  <rect x="6" y="15.5" width="6" height="3" rx="0.8" fill="#071028" />
                </svg>
              </span>
              <span class="response-label">Response</span>
            </div>
            <div class="response-text">${markdownToHtml(content)}</div>
          `;
          blk.thinkBox.open = false;
          break;
        case 'end':
          es.close(); if (statusEs) statusEs.close(); runBtn.disabled = false; break;
        case 'error':
          blk.t.innerHTML = `
            <div class="response-header error">
              <span class="response-icon">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                  <path d="M12 2L2 20h20L12 2z" fill="#2b0505" stroke="#f85149" stroke-width="0.8" />
                  <rect x="11" y="8" width="2" height="6" fill="#f85149" />
                  <rect x="11" y="16" width="2" height="2" fill="#f85149" />
                </svg>
              </span>
              <span class="response-label">Error</span>
            </div>
            <div class="response-text">${escapeHtml(data.message)}</div>
          `; break;
      }
      scrollBottom();
    } catch (err) {}
  };

  setTimeout(async () => {
    if (!sseOk) {
      try {
        const r = await fetch('/run?task=' + encodeURIComponent(task), { method: 'POST' });
        const j = await r.json();
        const content = j.summary || JSON.stringify(j, null, 2);
        blk.t.innerHTML = `
          <div class="response-header">
            <span class="response-icon">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                <rect x="2" y="3" width="20" height="14" rx="3" fill="#0f172a" stroke="#58a6ff" stroke-width="0.8" />
                <circle cx="8.5" cy="9" r="1.1" fill="#cbd5e1" />
                <circle cx="15.5" cy="9" r="1.1" fill="#cbd5e1" />
                <path d="M7 13c1 0 2 0.8 3 0.8s2-0.8 3-0.8" stroke="#9fb8d9" stroke-width="0.9" stroke-linecap="round" stroke-linejoin="round" />
                <rect x="6" y="15.5" width="6" height="3" rx="0.8" fill="#071028" />
              </svg>
            </span>
            <span class="response-label">Response</span>
          </div>
          <div class="response-text">${markdownToHtml(content)}</div>
        `;
      } catch (e) {
        blk.t.innerHTML = `
          <div class="response-header error">
            <span class="response-icon">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                <path d="M12 2L2 20h20L12 2z" fill="#2b0505" stroke="#f85149" stroke-width="0.8" />
                <rect x="11" y="8" width="2" height="6" fill="#f85149" />
                <rect x="11" y="16" width="2" height="2" fill="#f85149" />
              </svg>
            </span>
            <span class="response-label">Error</span>
          </div>
          <div class="response-text">Request failed: ${escapeHtml(String(e))}</div>
        `;
      }
      runBtn.disabled = false; es.close(); if (statusEs) statusEs.close();
    }
  }, 1500);

  es.onerror = () => { es.close(); if (statusEs) statusEs.close(); runBtn.disabled = false; };
}

// MCP connected indicator
(function manageMcpIndicator() {
  const indicator = document.getElementById('mcpConnected');
  if (!indicator) return;
  function setConnected(v) {
    if (v) { indicator.classList.add('connected'); indicator.setAttribute('title', 'Connected'); indicator.setAttribute('aria-hidden', 'false'); }
    else { indicator.classList.remove('connected'); indicator.setAttribute('title', 'Disconnected'); indicator.setAttribute('aria-hidden', 'true'); }
  }
  let sse = null;
  function connect() {
    try {
      sse = new EventSource('/status/stream');
      sse.onopen = () => setConnected(true);
      sse.onmessage = () => {};
      sse.onerror = () => { setConnected(false); try { sse.close(); } catch (e) {} sse = null; setTimeout(connect, 3000); };
    } catch (e) { setConnected(false); setTimeout(connect, 3000); }
  }
  connect();
})();

// Make floating panels resizable
function makeResizable(panel) {
  const resizeHandle = panel.querySelector('.resize-handle');
  if (!resizeHandle) return;

  let isResizing = false;
  let startX, startY, startWidth, startHeight;

  resizeHandle.addEventListener('mousedown', (e) => {
    isResizing = true;
    startX = e.clientX;
    startY = e.clientY;
    startWidth = parseInt(document.defaultView.getComputedStyle(panel).width, 10);
    startHeight = parseInt(document.defaultView.getComputedStyle(panel).height, 10);

    e.preventDefault();
    e.stopPropagation();

    document.addEventListener('mousemove', doResize);
    document.addEventListener('mouseup', stopResize);
  });

  function doResize(e) {
    if (!isResizing) return;

    const newWidth = Math.max(250, startWidth + e.clientX - startX);
    const newHeight = Math.max(150, startHeight + e.clientY - startY);

    panel.style.width = newWidth + 'px';
    panel.style.height = newHeight + 'px';
  }

  function stopResize() {
    isResizing = false;
    document.removeEventListener('mousemove', doResize);
    document.removeEventListener('mouseup', stopResize);
    // Persist size change
    try {
      const rect = panel.getBoundingClientRect();
      const state = loadPanelState(panel.id) || {};
      state.width = Math.round(rect.width);
      state.height = Math.round(rect.height);
      savePanelState(panel.id, state);
    } catch (e) {}
  }
}

// Apply resize functionality to both panels
makeResizable(statusPanel);
makeResizable(mcpPanel);
