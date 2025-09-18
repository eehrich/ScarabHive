// MCP Module
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.MCP = {
  // Store the original server data for filtering
  originalServerData: null,
  
  showPanel: function() {
    console.log('MCP panel requested');
    
    // Create header content with filter and refresh button (removed auto-refresh)
    const headerContent = `
      <input id="mcpFilterInput" type="text" placeholder="Filter servers/tools..." class="filter-input" title="Filter by server or tool name" />
      <button id="mcpRefreshBtn" class="icon-btn" title="Refresh now" aria-label="Refresh now">
        <svg class="mcp-refresh-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M21 12a9 9 0 10-2.6 6.1" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M21 3v6h-6" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span class="spinner icon-spinner" aria-hidden="true"></span>
      </button>
    `;
    
    // Create panel with loading state
    const panel = window.AgentSystem.PanelManager.createPanel(
      'floatingMCPPanel', 
      'MCP Servers & Tools', 
      '<div class="mcp-metrics" id="floatingMCPMetrics"><div class="metric-item"><span class="metric-label">Loading...</span><span class="metric-value">...</span></div></div>',
      '',
      headerContent
    );
    
    // Add event listeners for the header controls
    const filterInput = panel.querySelector('#mcpFilterInput');
    const refreshBtn = panel.querySelector('#mcpRefreshBtn');
    
    if (filterInput) {
      filterInput.addEventListener('input', (e) => {
        this.filterServers(e.target.value.toLowerCase());
      });
    }
    
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        this.loadMCPData(panel);
      });
    }
    
    // Load MCP data
    this.loadMCPData(panel);
  },
  
  loadMCPData: async function(panel) {
    try {
      console.log('Loading MCP data...');
      const refreshBtn = panel.querySelector('#mcpRefreshBtn');
      if (refreshBtn) {
        refreshBtn.classList.add('loading');
        refreshBtn.disabled = true;
      }
      
      const response = await fetch('/mcp/status');
      const data = await response.json();
      
      console.log('MCP data loaded:', data);
      
      // Store original data for filtering
      this.originalServerData = data;
      
      // Update panel content directly (PanelManager already creates .panel-content wrapper)
      const content = this.renderMCPContent(data);
      const body = panel.querySelector('.floating-panel-body');
      const contentDiv = body ? body.querySelector('.panel-content') || body : panel.querySelector('.panel-content') || panel;
      contentDiv.innerHTML = content;    } catch (error) {
      console.error('Failed to load MCP data:', error);
  const body = panel.querySelector('.floating-panel-body');
  const contentDiv = body ? body.querySelector('.panel-content') || body : panel.querySelector('.panel-content') || panel;
  contentDiv.innerHTML = '<div class="error">Failed to load MCP servers</div>';
    } finally {
      const refreshBtn = panel.querySelector('#mcpRefreshBtn');
      if (refreshBtn) {
        refreshBtn.classList.remove('loading');
        refreshBtn.disabled = false;
      }
    }
  },
  
  renderMCPContent: function(data) {
    if (!data.servers || data.servers.length === 0) {
      return '<div class="mcp-metrics"><div class="metric-item"><span class="metric-label">No MCP servers available</span><span class="metric-value">0</span></div></div>';
    }
    
    // Count statistics
    const totalServers = data.servers.length;
    const connectedServers = data.servers.filter(s => s.connected || s.reachable).length;
    const totalTools = data.servers.reduce((sum, s) => sum + (s.tool_count || 0), 0);
    
    // Build summary metrics (no extra spacing)
    let html = `<div class="mcp-metrics" id="floatingMCPMetrics">
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
      </div><div class="mcp-servers-list" id="floatingMCPServersList">`;
    
    // Build server list
    data.servers.forEach((server, index) => {
      const statusClass = (server.connected || server.reachable) ? 'connected' : 'disconnected';
      const statusText = (server.connected || server.reachable) ? 'Connected' : 'Disconnected';
      
      // Build tools list - check for detailed_tools first for blocked status
      const toolsList = server.tools && server.tools.length > 0
        ? server.detailed_tools && server.detailed_tools.length > 0
          ? server.detailed_tools.map(tool => {
              return `
                <li class="tool-item ${tool.blocked ? 'tool-blocked' : ''}">
                  <div class="tool-name">
                    ${tool.name}
                    ${tool.blocked ? '<span class="tool-status blocked">BLOCKED</span>' : ''}
                  </div>
                  <div class="tool-description">${tool.description || `Tool for ${server.name.toLowerCase()}`}</div>
                </li>`;
            }).join('')
          : server.tools.map(tool => `
              <li class="tool-item">
                <div class="tool-name">${tool}</div>
                <div class="tool-description">Tool for ${server.name.toLowerCase()}</div>
              </li>`).join('')
        : '<li class="tool-item"><div class="tool-name">No tools available</div></li>';
      
      html += `
        <div class="mcp-server" onclick="AgentSystem.MCP.toggleServerDetails('${server.id || index}')">
          <div class="mcp-server-info">
            <div class="server-name-type">
              <strong>${server.name}</strong>
              <span class="server-type">(${server.type || 'external'})</span>
            </div>
            <span class="mcp-status ${statusClass}">${statusText}</span>
          </div>
          <div class="tool-count">${server.tool_count || 0} tools <span class="expand-indicator" id="indicator-${server.id || index}"></span></div>
          <div class="server-details" id="details-${server.id || index}" style="display: none;">
            <div class="tools-list">
              <h4>Available Tools:</h4>
              <ul class="tools-container">${toolsList}</ul>
            </div>
            ${server.url ? `<div class="server-url"><strong>URL:</strong> ${server.url}</div>` : ''}
            ${server.error ? `<div class="server-error"><strong>Error:</strong> ${server.error}</div>` : ''}
          </div>
        </div>`;
    });
    
  html += '</div>';
  // Ensure servers list doesn't exceed panel height; let CSS handle scrolling
    return html;
  },
  
  toggleServerDetails: function(serverId) {
    const details = document.getElementById(`details-${serverId}`);
    const indicator = document.getElementById(`indicator-${serverId}`);
    
    if (details && indicator) {
      if (details.style.display === 'none') {
        details.style.display = 'block';
        indicator.classList.add('expanded');
      } else {
        details.style.display = 'none';
        indicator.classList.remove('expanded');
      }
    }
  },

  // Filter servers based on search term
  filterServers: function(searchTerm) {
    if (!this.originalServerData) return;
    
    const serversList = document.querySelector('.mcp-servers-list');
    if (!serversList) return;
    
    const serverElements = serversList.querySelectorAll('.mcp-server');
    
    serverElements.forEach((serverElement, index) => {
      const server = this.originalServerData.servers[index];
      if (!server) return;
      
      // Check if search term matches server name, type, or tools
      const serverName = (server.name || '').toLowerCase();
      const serverType = (server.type || '').toLowerCase();
      const tools = (server.tools || []).join(' ').toLowerCase();
      
      const matches = searchTerm === '' || 
        serverName.includes(searchTerm) || 
        serverType.includes(searchTerm) || 
        tools.includes(searchTerm);
      
      // Show/hide server element
      serverElement.style.display = matches ? 'block' : 'none';
    });
    
    // Update summary metrics for visible servers
    this.updateFilteredMetrics(searchTerm);
  },

  // Update metrics display based on filtered results
  updateFilteredMetrics: function(searchTerm) {
    if (!this.originalServerData) return;
    
    const metricsDiv = document.querySelector('.mcp-metrics');
    if (!metricsDiv) return;
    
    // Count visible servers
    const visibleServers = document.querySelectorAll('.mcp-server[style="display: block;"], .mcp-server:not([style*="display: none"])');
    const visibleCount = Array.from(visibleServers).filter(el => el.style.display !== 'none').length;
    
    // Calculate metrics for visible servers only
    let visibleConnected = 0;
    let visibleTools = 0;
    
    this.originalServerData.servers.forEach((server, index) => {
      const serverElement = document.querySelectorAll('.mcp-server')[index];
      if (serverElement && serverElement.style.display !== 'none') {
        if (server.connected || server.reachable) visibleConnected++;
        visibleTools += server.tool_count || 0;
      }
    });
    
    // Update metrics display
    const totalLabel = searchTerm ? `Filtered Servers` : `Total Servers`;
    metricsDiv.innerHTML = `
      <div class="metric-item">
        <span class="metric-label">${totalLabel}</span>
        <span class="metric-value">${visibleCount}</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Connected</span>
        <span class="metric-value">${visibleConnected}</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Total Tools</span>
        <span class="metric-value">${visibleTools}</span>
      </div>
    `;
  }
};

console.log('MCP module loaded');