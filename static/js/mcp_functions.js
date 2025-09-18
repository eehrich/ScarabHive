/*
  mcp_functions.js — MCP server and tools management for Agent System
*/

// Auto-refresh control
let mcpAutoRefreshTimer = null;

// MCP servers update function with state preservation and filtering
export async function updateMCPServers() {
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
          <div class="mcp-server" onclick="window.toggleServerDetails('${server.id}')">
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
}

// Toggle MCP server details
export function toggleServerDetails(serverId) {
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

// Auto-refresh management
export function startMcpAutoRefresh(intervalSec) {
  stopMcpAutoRefresh();
  const ms = Math.max(5000, (intervalSec || 30) * 1000);
  mcpAutoRefreshTimer = setInterval(() => { updateMCPServers().catch(()=>{}); }, ms);
}

export function stopMcpAutoRefresh() {
  if (mcpAutoRefreshTimer) {
    clearInterval(mcpAutoRefreshTimer);
    mcpAutoRefreshTimer = null;
  }
}

// MCP connected indicator management
export function manageMcpIndicator() {
  const indicator = document.getElementById('mcpConnected');
  if (!indicator) return;

  function updateCount(count) {
    indicator.textContent = count || '0';
    indicator.setAttribute('title', `${count} MCP servers connected`);
  }

  // Update count when MCP data is refreshed
  function updateFromMetrics() {
    try {
      const metrics = document.getElementById('floatingMCPMetrics');
      if (metrics) {
        const serverElements = metrics.querySelectorAll('.mcp-server');
        updateCount(serverElements.length);
      }
    } catch (e) {
      updateCount(0);
    }
  }

  // Call update periodically and when MCP panel is refreshed
  updateFromMetrics();
  setInterval(updateFromMetrics, 5000);

  // Also update when MCP refresh button is clicked
  const mcpRefreshBtn = document.getElementById('mcpRefreshBtn');
  if (mcpRefreshBtn) {
    mcpRefreshBtn.addEventListener('click', () => {
      setTimeout(updateFromMetrics, 1000); // Delay to allow refresh to complete
    });
  }
}