// MCP Module - Modern Design (VSCode Theme)
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.MCP = {
  originalServerData: null,
  lastRefreshTime: null,
  autoRefreshInterval: null,
  
  showPanel: function() {
    // Create header with controls (like profiling panel)
    const headerContent = `
      <input id="mcpFilterInput" type="text" placeholder="Filter servers/tools..." 
             style="padding: 6px 10px; border: 1px solid #3e3e42; border-radius: 4px; background: #1e1e1e; color: #d4d4d4; font-size: 12px; width: 180px;" />
      <span id="mcpLastRefresh" style="font-size: 11px; color: #858585; margin-right: 8px;"></span>
      <button id="mcpRefreshBtn" class="icon-btn" title="Refresh (invalidates cache)">🔄</button>
      <button id="mcpAutoRefreshBtn" class="icon-btn" title="Auto-Refresh (30s) - Click to toggle">⏱️</button>
    `;
    
    // Create panel with loading state
    const panel = window.AgentSystem.PanelManager.createPanel(
      'floatingMCPPanel', 
      '🔌 MCP Servers & Tools', 
      '<div class="mcp-loading">Loading...</div>',
      '',
      headerContent
    );
    
    // Add event listeners
    const filterInput = panel.querySelector('#mcpFilterInput');
    const refreshBtn = panel.querySelector('#mcpRefreshBtn');
    const autoRefreshBtn = panel.querySelector('#mcpAutoRefreshBtn');
    
    if (filterInput) {
      filterInput.addEventListener('input', (e) => this.filterServers(e.target.value.toLowerCase()));
    }
    
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => this.loadMCPData(panel, true));
    }
    
    if (autoRefreshBtn) {
      autoRefreshBtn.addEventListener('click', () => this.toggleAutoRefresh(panel));
    }
    
    // Load initial data
    this.loadMCPData(panel, false);
  },
  
  toggleAutoRefresh: function(panel) {
    const btn = panel.querySelector('#mcpAutoRefreshBtn');
    if (this.autoRefreshInterval) {
      this.stopAutoRefresh();
      if (btn) {
        btn.classList.remove('active');
        btn.title = 'Auto-Refresh (30s) - Paused';
      }
    } else {
      this.startAutoRefresh(panel);
      if (btn) {
        btn.classList.add('active');
        btn.title = 'Auto-Refresh (30s) - Active';
      }
    }
  },
  
  startAutoRefresh: function(panel) {
    if (this.autoRefreshInterval) return;
    this.autoRefreshInterval = setInterval(() => this.loadMCPData(panel, false), 30000);
  },
  
  stopAutoRefresh: function() {
    if (this.autoRefreshInterval) {
      clearInterval(this.autoRefreshInterval);
      this.autoRefreshInterval = null;
    }
  },
  
  updateTimestamp: function(panel) {
    const el = panel.querySelector('#mcpLastRefresh');
    if (el && this.lastRefreshTime) {
      el.textContent = 'Updated: ' + this.lastRefreshTime.toLocaleTimeString();
    }
  },
  
  loadMCPData: async function(panel, forceRefresh = false) {
    try {
      const refreshBtn = panel.querySelector('#mcpRefreshBtn');
      if (refreshBtn) refreshBtn.disabled = true;
      
      const url = forceRefresh ? '/mcp/status?force_refresh=true' : '/mcp/status';
      const response = await fetch(url);
      const data = await response.json();
      
      this.originalServerData = data;
      this.lastRefreshTime = new Date();
      this.updateTimestamp(panel);
      
      const content = this.renderMCPContent(data);
      const body = panel.querySelector('.floating-panel-body');
      const contentDiv = body ? body.querySelector('.panel-content') || body : panel.querySelector('.panel-content') || panel;
      contentDiv.innerHTML = content;
      
    } catch (error) {
      console.error('Failed to load MCP data:', error);
      const body = panel.querySelector('.floating-panel-body');
      const contentDiv = body ? body.querySelector('.panel-content') || body : panel.querySelector('.panel-content') || panel;
      contentDiv.innerHTML = '<div class="mcp-error">❌ Failed to load MCP servers</div>';
    } finally {
      const refreshBtn = panel.querySelector('#mcpRefreshBtn');
      if (refreshBtn) refreshBtn.disabled = false;
    }
  },
  
  renderMCPContent: function(data) {
    if (!data.servers || data.servers.length === 0) {
      return '<div class="mcp-empty">No MCP servers configured</div>';
    }
    
    const totalServers = data.servers.length;
    const connectedServers = data.servers.filter(s => s.connected || s.reachable).length;
    const totalTools = data.servers.reduce((sum, s) => sum + (s.tool_count || 0), 0);
    
    return `
      <style>
        .mcp-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }
        .mcp-card { background: #252526; border: 1px solid #3e3e42; border-radius: 6px; padding: 12px; }
        .mcp-card-label { font-size: 11px; color: #858585; margin-bottom: 4px; text-transform: uppercase; }
        .mcp-card-value { font-size: 18px; font-weight: 600; color: #4ec9b0; }
        .mcp-servers-container { display: flex; flex-direction: column; gap: 8px; }
        .mcp-server { background: #252526; border: 1px solid #3e3e42; border-radius: 6px; overflow: hidden; }
        .mcp-server:hover { border-color: #569cd6; }
        .mcp-server-header { display: flex; justify-content: space-between; align-items: center; padding: 12px; cursor: pointer; }
        .mcp-server-header:hover { background: #2a2d2e; }
        .mcp-server-info { display: flex; align-items: center; gap: 10px; }
        .mcp-server-name { font-weight: 600; color: #569cd6; font-size: 14px; }
        .mcp-server-type { font-size: 10px; color: #858585; background: rgba(78, 201, 176, 0.15); padding: 2px 6px; border-radius: 3px; text-transform: uppercase; }
        .mcp-server-meta { display: flex; align-items: center; gap: 10px; }
        .mcp-status { font-size: 11px; padding: 3px 8px; border-radius: 3px; font-weight: 500; }
        .mcp-status.connected { background: rgba(78, 201, 176, 0.2); color: #4ec9b0; }
        .mcp-status.disconnected { background: rgba(244, 135, 113, 0.2); color: #f48771; }
        .mcp-tools-badge { font-size: 11px; color: #dcdcaa; }
        .mcp-expand-icon { color: #858585; font-size: 12px; transition: transform 0.2s; }
        .mcp-expand-icon.expanded { transform: rotate(90deg); }
        .mcp-server-details { display: none; padding: 0 12px 12px 12px; border-top: 1px solid #3e3e42; }
        .mcp-server-details.expanded { display: block; }
        .mcp-tools-list { margin-top: 10px; }
        .mcp-tools-title { font-size: 12px; color: #ce9178; margin-bottom: 8px; font-weight: 500; }
        .mcp-tool { background: #1e1e1e; border: 1px solid #3e3e42; border-radius: 4px; padding: 8px 10px; margin: 4px 0; }
        .mcp-tool-name { font-family: 'Consolas', monospace; color: #dcdcaa; font-size: 12px; }
        .mcp-tool-name.blocked { color: #f48771; text-decoration: line-through; }
        .mcp-tool-desc { font-size: 11px; color: #858585; margin-top: 4px; }
        .mcp-server-url { font-size: 11px; color: #569cd6; background: #1e1e1e; padding: 6px 8px; border-radius: 4px; margin-top: 10px; font-family: 'Consolas', monospace; word-break: break-all; }
        .mcp-server-error { font-size: 11px; color: #f48771; background: rgba(244, 135, 113, 0.1); padding: 6px 8px; border-radius: 4px; margin-top: 10px; }
        .mcp-loading, .mcp-error, .mcp-empty { color: #858585; padding: 20px; text-align: center; }
        .mcp-error { color: #f48771; }
      </style>
      
      <div class="mcp-grid" id="mcpMetrics">
        <div class="mcp-card">
          <div class="mcp-card-label">Total Servers</div>
          <div class="mcp-card-value">${totalServers}</div>
        </div>
        <div class="mcp-card">
          <div class="mcp-card-label">Connected</div>
          <div class="mcp-card-value" style="color: ${connectedServers === totalServers ? '#4ec9b0' : '#dcdcaa'}">${connectedServers}/${totalServers}</div>
        </div>
        <div class="mcp-card">
          <div class="mcp-card-label">Total Tools</div>
          <div class="mcp-card-value">${totalTools}</div>
        </div>
      </div>
      
      <div class="mcp-servers-container" id="mcpServersList">
        ${data.servers.map((server, index) => this.renderServer(server, index)).join('')}
      </div>
    `;
  },
  
  renderServer: function(server, index) {
    const statusClass = (server.connected || server.reachable) ? 'connected' : 'disconnected';
    const statusText = (server.connected || server.reachable) ? '● Connected' : '○ Disconnected';
    const toolCount = server.tool_count || 0;
    const serverId = server.id || index;
    
    // Build tools HTML
    let toolsHtml = '';
    if (server.tools && server.tools.length > 0) {
      if (server.detailed_tools && server.detailed_tools.length > 0) {
        toolsHtml = server.detailed_tools.map(tool => `
          <div class="mcp-tool">
            <div class="mcp-tool-name ${tool.blocked ? 'blocked' : ''}">${tool.name}${tool.blocked ? ' (BLOCKED)' : ''}</div>
            <div class="mcp-tool-desc">${tool.description || 'No description'}</div>
          </div>
        `).join('');
      } else {
        toolsHtml = server.tools.map(tool => `
          <div class="mcp-tool">
            <div class="mcp-tool-name">${tool}</div>
          </div>
        `).join('');
      }
    } else {
      toolsHtml = '<div class="mcp-tool"><div class="mcp-tool-desc">No tools available</div></div>';
    }
    
    return `
      <div class="mcp-server" data-server-name="${server.name}" data-server-type="${server.type || ''}" data-tools="${(server.tools || []).join(' ')}">
        <div class="mcp-server-header" onclick="AgentSystem.MCP.toggleServer('${serverId}')">
          <div class="mcp-server-info">
            <span class="mcp-server-name">${server.name}</span>
            <span class="mcp-server-type">${server.type || 'external'}</span>
          </div>
          <div class="mcp-server-meta">
            <span class="mcp-status ${statusClass}">${statusText}</span>
            <span class="mcp-tools-badge">${toolCount} tools</span>
            <span class="mcp-expand-icon" id="icon-${serverId}">▶</span>
          </div>
        </div>
        <div class="mcp-server-details" id="details-${serverId}">
          ${server.description ? `<div style="color: #858585; font-size: 12px; margin-bottom: 10px;">${server.description}</div>` : ''}
          <div class="mcp-tools-list">
            <div class="mcp-tools-title">Available Tools:</div>
            ${toolsHtml}
          </div>
          ${server.url ? `<div class="mcp-server-url">📍 ${server.url}</div>` : ''}
          ${server.error ? `<div class="mcp-server-error">⚠️ ${server.error}</div>` : ''}
        </div>
      </div>
    `;
  },
  
  toggleServer: function(serverId) {
    const details = document.getElementById(`details-${serverId}`);
    const icon = document.getElementById(`icon-${serverId}`);
    
    if (details && icon) {
      const isExpanded = details.classList.contains('expanded');
      if (isExpanded) {
        details.classList.remove('expanded');
        icon.classList.remove('expanded');
      } else {
        details.classList.add('expanded');
        icon.classList.add('expanded');
      }
    }
  },
  
  filterServers: function(searchTerm) {
    const servers = document.querySelectorAll('.mcp-server');
    let visibleCount = 0;
    let visibleConnected = 0;
    let visibleTools = 0;
    
    servers.forEach((server, index) => {
      const name = server.dataset.serverName?.toLowerCase() || '';
      const type = server.dataset.serverType?.toLowerCase() || '';
      const tools = server.dataset.tools?.toLowerCase() || '';
      
      const matches = !searchTerm || name.includes(searchTerm) || type.includes(searchTerm) || tools.includes(searchTerm);
      server.style.display = matches ? 'block' : 'none';
      
      if (matches && this.originalServerData?.servers[index]) {
        const serverData = this.originalServerData.servers[index];
        visibleCount++;
        if (serverData.connected || serverData.reachable) visibleConnected++;
        visibleTools += serverData.tool_count || 0;
      }
    });
    
    // Update metrics
    const metricsDiv = document.getElementById('mcpMetrics');
    if (metricsDiv && this.originalServerData) {
      const total = this.originalServerData.servers.length;
      metricsDiv.innerHTML = `
        <div class="mcp-card">
          <div class="mcp-card-label">${searchTerm ? 'Filtered' : 'Total'} Servers</div>
          <div class="mcp-card-value">${visibleCount}${searchTerm ? '/' + total : ''}</div>
        </div>
        <div class="mcp-card">
          <div class="mcp-card-label">Connected</div>
          <div class="mcp-card-value" style="color: ${visibleConnected === visibleCount ? '#4ec9b0' : '#dcdcaa'}">${visibleConnected}/${visibleCount}</div>
        </div>
        <div class="mcp-card">
          <div class="mcp-card-label">Total Tools</div>
          <div class="mcp-card-value">${visibleTools}</div>
        </div>
      `;
    }
  }
};
