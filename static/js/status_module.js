// Status Module - Modern Design (VSCode Theme) with MCP Integration
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.Status = {
  autoRefreshInterval: null,
  lastRefreshTime: null,
  activeTab: 'status', // 'status' or 'mcp'
  mcpData: null,
  mcpLastRefreshTime: null,
  
  /**
   * Prevent wheel events from scrolling parent page
   */
  _addScrollPrevention: function(element) {
    element.addEventListener('wheel', (e) => {
      let target = e.target;
      while (target && target !== element) {
        const style = window.getComputedStyle(target);
        const overflowY = style.overflowY;
        const isScrollable = (overflowY === 'auto' || overflowY === 'scroll') && target.scrollHeight > target.clientHeight;
        
        if (isScrollable) {
          const atTop = target.scrollTop <= 0;
          const atBottom = target.scrollHeight - target.scrollTop <= target.clientHeight + 1;
          
          if ((e.deltaY < 0 && !atTop) || (e.deltaY > 0 && !atBottom)) {
            return;
          }
          e.preventDefault();
          return;
        }
        target = target.parentElement;
      }
      // Check if element itself is scrollable
      const style = window.getComputedStyle(element);
      const overflowY = style.overflowY;
      const isScrollable = (overflowY === 'auto' || overflowY === 'scroll') && element.scrollHeight > element.clientHeight;
      if (isScrollable) {
        const atTop = element.scrollTop <= 0;
        const atBottom = element.scrollHeight - element.scrollTop <= element.clientHeight + 1;
        if ((e.deltaY < 0 && !atTop) || (e.deltaY > 0 && !atBottom)) {
          return;
        }
      }
      e.preventDefault();
    }, { passive: false });
  },
  
  showPanel: function() {
    // Create header with controls only (tabs are in content area)
    const headerContent = `
      <input id="mcpFilterInput" type="text" placeholder="Filter..." 
             style="display:none;padding:5px 8px;border:1px solid #3e3e42;border-radius:4px;background:#1e1e1e;color:#d4d4d4;font-size:11px;width:120px;" />
      <span id="statusLastRefresh" style="font-size:11px;color:#858585;margin-right:8px;"></span>
      <button id="statusRefreshBtn" class="icon-btn" title="Refresh">🔄</button>
      <button id="statusAutoRefreshBtn" class="icon-btn" title="Auto-Refresh - Paused">⏱️</button>
    `;
    
    // Create panel with loading state
    const panel = window.AgentSystem.PanelManager.createPanel(
      'floatingStatusPanel', 
      '📊 System', 
      '<div class="status-loading">Loading...</div>',
      '',
      headerContent
    );
    
    // Add scroll prevention to panel body
    const panelBody = panel.querySelector('.floating-panel-body');
    if (panelBody) {
      this._addScrollPrevention(panelBody);
    }
    
    // Add event listeners
    const refreshBtn = panel.querySelector('#statusRefreshBtn');
    const autoRefreshBtn = panel.querySelector('#statusAutoRefreshBtn');
    const filterInput = panel.querySelector('#mcpFilterInput');
    
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => this.refreshCurrentTab(panel, true));
    }
    
    if (autoRefreshBtn) {
      autoRefreshBtn.addEventListener('click', () => this.toggleAutoRefresh(panel));
    }
    
    if (filterInput) {
      filterInput.addEventListener('input', (e) => this.filterMCPServers(e.target.value.toLowerCase()));
    }
    
    // Render initial content with tabs
    this.renderPanelWithTabs(panel);
    
    // Load initial data
    this.loadStatusData(panel);
  },
  
  renderPanelWithTabs: function(panel) {
    const body = panel.querySelector('.floating-panel-body');
    const contentDiv = body ? body.querySelector('.panel-content') || body : panel.querySelector('.panel-content') || panel;
    
    contentDiv.innerHTML = `
      <style>
        .system-tabs { display:flex; gap:0; margin-bottom:12px; border-bottom:1px solid #3e3e42; }
        .system-tab { padding:8px 16px; background:transparent; border:none; color:#858585; cursor:pointer; font-size:13px; transition:all 0.2s; border-radius:4px 4px 0 0; font-family:inherit; outline:none; }
        .system-tab:hover { color:#d4d4d4; background:#2d2d30; }
        .system-tab:focus { outline:none; }
        .system-tab.active { color:#569cd6; background:#252526; border:1px solid #3e3e42; }
        .system-tab-content { display:none; }
        .system-tab-content.active { display:block; }
      </style>
      <div class="system-tabs">
        <button class="system-tab active" data-tab="status">📊 Status</button>
        <button class="system-tab" data-tab="mcp">🔌 MCP Servers</button>
      </div>
      <div class="system-tab-content active" id="system-tab-status">
        <div class="status-loading">Loading...</div>
      </div>
      <div class="system-tab-content" id="system-tab-mcp">
        <div class="status-loading">Loading...</div>
      </div>
    `;
    
    // Add tab click handlers
    const tabs = contentDiv.querySelectorAll('.system-tab');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => this.switchTab(panel, tab.dataset.tab));
    });
  },
  
  switchTab: function(panel, tab) {
    this.activeTab = tab;
    
    // Update tab button states
    const tabs = panel.querySelectorAll('.system-tab');
    tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    
    // Update tab content visibility
    const statusContent = panel.querySelector('#system-tab-status');
    const mcpContent = panel.querySelector('#system-tab-mcp');
    if (statusContent) statusContent.classList.toggle('active', tab === 'status');
    if (mcpContent) mcpContent.classList.toggle('active', tab === 'mcp');
    
    // Show/hide filter input based on tab
    const filterInput = panel.querySelector('#mcpFilterInput');
    if (filterInput) {
      filterInput.style.display = tab === 'mcp' ? 'block' : 'none';
    }
    
    // Load appropriate content
    if (tab === 'status') {
      this.loadStatusData(panel);
    } else {
      this.loadMCPData(panel, false);
    }
  },
  
  refreshCurrentTab: function(panel, forceRefresh = false) {
    if (this.activeTab === 'status') {
      this.loadStatusData(panel);
    } else {
      this.loadMCPData(panel, forceRefresh);
    }
  },
  
  toggleAutoRefresh: function(panel) {
    const btn = panel.querySelector('#statusAutoRefreshBtn');
    if (this.autoRefreshInterval) {
      this.stopAutoRefresh();
      if (btn) {
        btn.classList.remove('active');
        btn.title = 'Auto-Refresh - Paused';
      }
    } else {
      this.startAutoRefresh(panel);
      if (btn) {
        btn.classList.add('active');
        btn.title = 'Auto-Refresh - Active';
      }
    }
  },
  
  startAutoRefresh: function(panel) {
    if (this.autoRefreshInterval) return;
    // Status refreshes every 5s, MCP every 30s
    this.autoRefreshInterval = setInterval(() => {
      this.refreshCurrentTab(panel, false);
    }, this.activeTab === 'status' ? 5000 : 30000);
  },
  
  stopAutoRefresh: function() {
    if (this.autoRefreshInterval) {
      clearInterval(this.autoRefreshInterval);
      this.autoRefreshInterval = null;
    }
  },
  
  updateTimestamp: function(panel) {
    const el = panel.querySelector('#statusLastRefresh');
    const time = this.activeTab === 'status' ? this.lastRefreshTime : this.mcpLastRefreshTime;
    if (el && time) {
      el.textContent = 'Updated: ' + time.toLocaleTimeString();
    }
  },
  
  // ==========================================
  // STATUS TAB
  // ==========================================
  
  loadStatusData: async function(panel) {
    try {
      const refreshBtn = panel.querySelector('#statusRefreshBtn');
      if (refreshBtn) refreshBtn.disabled = true;
      
      // Check if user is admin for active sessions
      const isAdmin = window.authManager && window.authManager.isAdmin();
      
      // Build fetch requests
      const fetchPromises = [
        fetch('/health'),
        fetch('/status/meta')
      ];
      
      // Add admin-only active sessions request
      if (isAdmin) {
        fetchPromises.push(
          window.authManager.authFetch('/admin/active-sessions')
        );
      }
      
      const responses = await Promise.allSettled(fetchPromises);
      
      let healthData = {};
      let metaData = {};
      let activeSessionsData = null;
      
      if (responses[0].status === 'fulfilled' && responses[0].value.ok) {
        healthData = await responses[0].value.json();
      }
      if (responses[1].status === 'fulfilled' && responses[1].value.ok) {
        metaData = await responses[1].value.json();
      }
      if (isAdmin && responses[2] && responses[2].status === 'fulfilled' && responses[2].value.ok) {
        activeSessionsData = await responses[2].value.json();
      }
      
      this.lastRefreshTime = new Date();
      this.updateTimestamp(panel);
      
      const content = this.renderStatusContent(healthData, metaData, activeSessionsData, isAdmin);
      const tabContent = panel.querySelector('#system-tab-status');
      if (tabContent) {
        tabContent.innerHTML = content;
      }
      
      // Attach cancel button event handlers if admin
      if (isAdmin && activeSessionsData && activeSessionsData.sessions) {
        this.attachCancelHandlers(panel);
      }
      
    } catch (error) {
      console.error('Failed to load status data:', error);
      const tabContent = panel.querySelector('#system-tab-status');
      if (tabContent) {
        tabContent.innerHTML = '<div class="status-error">❌ Failed to load status data</div>';
      }
    } finally {
      const refreshBtn = panel.querySelector('#statusRefreshBtn');
      if (refreshBtn) refreshBtn.disabled = false;
    }
  },
  
  attachCancelHandlers: function(panel) {
    const cancelButtons = panel.querySelectorAll('.session-cancel-btn');
    cancelButtons.forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const requestId = e.target.dataset.requestId;
        if (!requestId) return;
        
        // Confirm cancellation
        if (!confirm(`Cancel request ${requestId}?`)) return;
        
        btn.disabled = true;
        btn.textContent = '⏳';
        
        try {
          const response = await window.authManager.authFetch(
            `/admin/active-sessions/${requestId}/cancel`,
            { method: 'POST' }
          );
          
          if (response.ok) {
            const result = await response.json();
            if (result.status === 'cancelled') {
              btn.textContent = '✓';
              btn.classList.add('cancelled');
              // Refresh after short delay
              setTimeout(() => this.loadStatusData(panel), 1000);
            } else {
              btn.textContent = '?';
              btn.title = result.message || 'Not found';
            }
          } else {
            btn.textContent = '✗';
            btn.title = 'Failed to cancel';
          }
        } catch (error) {
          console.error('Failed to cancel request:', error);
          btn.textContent = '✗';
          btn.title = error.message;
        }
      });
    });
  },
  
  formatUptime: function(seconds) {
    const secs = Math.floor(seconds);
    const mins = Math.floor(secs / 60);
    const hours = Math.floor(mins / 60);
    const days = Math.floor(hours / 24);
    
    if (days > 0) return `${days}d ${hours % 24}h ${mins % 60}m`;
    if (hours > 0) return `${hours}h ${mins % 60}m`;
    if (mins > 0) return `${mins}m ${secs % 60}s`;
    return `${secs}s`;
  },
  
  formatDuration: function(seconds) {
    const secs = Math.floor(seconds);
    const mins = Math.floor(secs / 60);
    const hours = Math.floor(mins / 60);
    
    if (hours > 0) return `${hours}h ${mins % 60}m ${secs % 60}s`;
    if (mins > 0) return `${mins}m ${secs % 60}s`;
    return `${secs}s`;
  },
  
  getStatusClass: function(status) {
    if (status === 'healthy' || status === 'ok') return 'status-healthy';
    if (status === 'warning' || status === 'degraded') return 'status-warning';
    return 'status-error';
  },
  
  getSessionStatusClass: function(status) {
    if (status === 'running') return 'session-running';
    if (status === 'cancelling') return 'session-cancelling';
    return 'session-unknown';
  },
  
  renderActiveSessionsSection: function(sessionsData) {
    if (!sessionsData || !sessionsData.sessions) {
      return '';
    }
    
    const sessions = sessionsData.sessions;
    
    if (sessions.length === 0) {
      return `
        <div class="status-section">
          <div class="status-section-title">🔄 Active Sessions (Admin)</div>
          <div class="status-empty">No active sessions</div>
        </div>
      `;
    }
    
    const sessionRows = sessions.map(session => {
      const statusClass = this.getSessionStatusClass(session.status);
      const duration = this.formatDuration(session.duration_seconds);
      const shortSessionId = session.session_id ? session.session_id.substring(0, 8) + '...' : '-';
      
      return `
        <tr>
          <td title="${session.user_id}">${session.user_id}</td>
          <td title="${session.agent_name}">${session.agent_name}</td>
          <td class="value">${duration}</td>
          <td><span class="session-status ${statusClass}">${session.status}</span></td>
          <td class="request-id-cell" title="Click to copy">${session.request_id}</td>
          <td>
            <button class="session-cancel-btn" data-request-id="${session.request_id}" 
                    title="Cancel this request" ${session.status === 'cancelling' ? 'disabled' : ''}>
              ${session.status === 'cancelling' ? '⏳' : '✕'}
            </button>
          </td>
        </tr>
      `;
    }).join('');
    
    return `
      <div class="status-section">
        <div class="status-section-title">🔄 Active Sessions (Admin) <span class="session-count">${sessions.length}</span></div>
        <table class="status-table sessions-table">
          <tr>
            <th>User</th>
            <th>Agent</th>
            <th>Duration</th>
            <th>Status</th>
            <th>Request ID</th>
            <th>Action</th>
          </tr>
          ${sessionRows}
        </table>
      </div>
    `;
  },
  
  renderStatusContent: function(health, meta, activeSessions, isAdmin) {
    const status = health.status || 'Unknown';
    const uptime = health.uptime_seconds ? this.formatUptime(health.uptime_seconds) : 'N/A';
    const version = health.version ? `v${health.version}` : 'N/A';
    const pythonVersion = health.python_version || 'N/A';
    
    // Build packages section if available
    let packagesHtml = '';
    if (health.packages && Object.keys(health.packages).length > 0) {
      const packages = Object.entries(health.packages)
        .map(([name, ver]) => `<tr><td>${name}</td><td class="value">${ver}</td></tr>`)
        .join('');
      packagesHtml = `
        <div class="status-section">
          <div class="status-section-title">📦 Key Packages</div>
          <table class="status-table">
            <tr><th>Package</th><th>Version</th></tr>
            ${packages}
          </table>
        </div>
      `;
    }
    
    // Build active sessions section (admin only)
    let activeSessionsHtml = '';
    if (isAdmin) {
      activeSessionsHtml = this.renderActiveSessionsSection(activeSessions);
    }
    
    return `
      <style>
        .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }
        .status-card { background: #252526; border: 1px solid #3e3e42; border-radius: 6px; padding: 12px; }
        .status-card-label { font-size: 11px; color: #858585; margin-bottom: 4px; text-transform: uppercase; }
        .status-card-value { font-size: 18px; font-weight: 600; color: #4ec9b0; }
        .status-card-value.status-healthy { color: #4ec9b0; }
        .status-card-value.status-warning { color: #dcdcaa; }
        .status-card-value.status-error { color: #f48771; }
        .status-section { background: #252526; border: 1px solid #3e3e42; border-radius: 6px; padding: 16px; margin-bottom: 16px; }
        .status-section-title { font-size: 14px; color: #ce9178; margin-bottom: 12px; font-weight: 600; }
        .status-table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .status-table th { text-align: left; color: #569cd6; font-size: 11px; text-transform: uppercase; padding: 8px; border-bottom: 1px solid #3e3e42; }
        .status-table td { padding: 8px; border-bottom: 1px solid #3e3e42; color: #d4d4d4; }
        .status-table td.value { color: #4ec9b0; font-weight: 500; }
        .status-loading { color: #858585; padding: 20px; text-align: center; }
        .status-error { color: #f48771; padding: 20px; text-align: center; }
        .status-empty { color: #858585; font-style: italic; padding: 10px 0; }
        
        /* Active sessions styles */
        .sessions-table td { font-size: 12px; padding: 6px 8px; }
        .sessions-table th { font-size: 10px; padding: 6px 8px; }
        .session-count { background: #0e639c; color: white; font-size: 11px; padding: 2px 6px; border-radius: 10px; margin-left: 8px; }
        .session-status { padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 500; }
        .session-status.session-running { background: #2d5016; color: #89d185; }
        .session-status.session-cancelling { background: #5c4016; color: #dcdcaa; }
        .session-status.session-unknown { background: #3e3e42; color: #858585; }
        .request-id-cell { 
          font-family: 'Courier New', monospace; 
          font-size: 11px; 
          color: #4ec9b0; 
          cursor: pointer;
          user-select: all;
        }
        .request-id-cell:hover { background: #2a2d2e; }
        .session-cancel-btn { 
          background: #5a1d1d; 
          border: 1px solid #8b3232; 
          color: #f48771; 
          padding: 3px 8px; 
          border-radius: 3px; 
          cursor: pointer; 
          font-size: 11px;
          transition: all 0.2s;
        }
        .session-cancel-btn:hover:not(:disabled) { background: #8b3232; color: white; }
        .session-cancel-btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .session-cancel-btn.cancelled { background: #2d5016; border-color: #4d7c0f; color: #89d185; }
      </style>
      
      <div class="status-grid">
        <div class="status-card">
          <div class="status-card-label">Status</div>
          <div class="status-card-value ${this.getStatusClass(status)}">${status}</div>
        </div>
        <div class="status-card">
          <div class="status-card-label">Uptime</div>
          <div class="status-card-value">${uptime}</div>
        </div>
        <div class="status-card">
          <div class="status-card-label">Version</div>
          <div class="status-card-value">${version}</div>
        </div>
        <div class="status-card">
          <div class="status-card-label">Python</div>
          <div class="status-card-value">${pythonVersion}</div>
        </div>
      </div>
      
      ${activeSessionsHtml}
      
      <div class="status-section">
        <div class="status-section-title">📡 Event Bus</div>
        <table class="status-table">
          <tr><th>Metric</th><th>Value</th></tr>
          <tr><td>Subscribers</td><td class="value">${meta.subscribers || 0}</td></tr>
          <tr><td>Events Published</td><td class="value">${meta.publish_attempted || 0}</td></tr>
          <tr><td>Events Delivered</td><td class="value">${meta.delivered || 0}</td></tr>
          <tr><td>Handlers</td><td class="value">${meta.handlers_count || 0}</td></tr>
        </table>
      </div>
      
      ${packagesHtml}
    `;
  },
  
  // ==========================================
  // MCP TAB
  // ==========================================
  
  loadMCPData: async function(panel, forceRefresh = false) {
    try {
      const refreshBtn = panel.querySelector('#statusRefreshBtn');
      if (refreshBtn) refreshBtn.disabled = true;
      
      const url = forceRefresh ? '/mcp/status?force_refresh=true' : '/mcp/status';
      const response = await fetch(url);
      const data = await response.json();
      
      this.mcpData = data;
      this.mcpLastRefreshTime = new Date();
      this.updateTimestamp(panel);
      
      const content = this.renderMCPContent(data);
      const tabContent = panel.querySelector('#system-tab-mcp');
      if (tabContent) {
        tabContent.innerHTML = content;
        // Re-apply filter if any
        const filterInput = panel.querySelector('#mcpFilterInput');
        if (filterInput && filterInput.value) {
          this.filterMCPServers(filterInput.value.toLowerCase());
        }
      }
      
    } catch (error) {
      console.error('Failed to load MCP data:', error);
      const tabContent = panel.querySelector('#system-tab-mcp');
      if (tabContent) {
        tabContent.innerHTML = '<div class="status-error">❌ Failed to load MCP servers</div>';
      }
    } finally {
      const refreshBtn = panel.querySelector('#statusRefreshBtn');
      if (refreshBtn) refreshBtn.disabled = false;
    }
  },
  
  renderMCPContent: function(data) {
    if (!data.servers || data.servers.length === 0) {
      return '<div class="status-empty" style="padding:20px;text-align:center;">No MCP servers configured</div>';
    }
    
    const totalServers = data.servers.length;
    const connectedServers = data.servers.filter(s => s.connected || s.reachable).length;
    const totalTools = data.servers.reduce((sum, s) => sum + (s.tool_count || 0), 0);
    
    return `
      <style>
        .mcp-summary { display:flex; gap:16px; margin-bottom:12px; padding:8px 0; border-bottom:1px solid #3e3e42; font-size:13px; color:#858585; }
        .mcp-summary-item { display:flex; gap:4px; }
        .mcp-summary-value { color:#4ec9b0; font-weight:500; }
        .mcp-servers-container { display:flex; flex-direction:column; gap:1px; }
        .mcp-server { background:#252526; border-radius:2px; overflow:hidden; }
        .mcp-server-header { display:flex; align-items:center; padding:6px 10px; cursor:pointer; gap:10px; }
        .mcp-server-header:hover { background:#2a2d2e; }
        .mcp-status { font-size:12px; line-height:1; }
        .mcp-status.connected { color:#4ec9b0; }
        .mcp-status.disconnected { color:#f48771; }
        .mcp-server-name { color:#d4d4d4; font-size:13px; flex:1; }
        .mcp-server-meta { display:flex; align-items:center; gap:8px; font-size:12px; color:#858585; margin-left:auto; }
        .mcp-expand-icon { color:#858585; font-size:11px; transition:transform 0.2s; }
        .mcp-expand-icon.expanded { transform:rotate(90deg); }
        .mcp-server-details { display:none; padding:8px 10px 8px 32px; background:#1e1e1e; }
        .mcp-server-details.expanded { display:block; }
        .mcp-tools-title { font-size:12px; color:#ce9178; margin-bottom:6px; }
        .mcp-tool { padding:3px 0; }
        .mcp-tool-name { font-family:'Consolas',monospace; color:#dcdcaa; font-size:12px; }
        .mcp-tool-name.blocked { color:#f48771; text-decoration:line-through; }
        .mcp-tool-desc { font-size:11px; color:#858585; margin-left:10px; }
        .mcp-server-url { font-size:11px; color:#569cd6; margin-top:6px; font-family:'Consolas',monospace; }
        .mcp-server-error { font-size:11px; color:#f48771; margin-top:6px; }
      </style>
      
      <div class="mcp-summary" id="mcpMetrics">
        <div class="mcp-summary-item">Servers: <span class="mcp-summary-value">${totalServers}</span></div>
        <div class="mcp-summary-item">Connected: <span class="mcp-summary-value" style="color:${connectedServers === totalServers ? '#4ec9b0' : '#dcdcaa'}">${connectedServers}/${totalServers}</span></div>
        <div class="mcp-summary-item">Tools: <span class="mcp-summary-value">${totalTools}</span></div>
      </div>
      
      <div class="mcp-servers-container" id="mcpServersList">
        ${data.servers.map((server, index) => this.renderMCPServer(server, index)).join('')}
      </div>
    `;
  },
  
  renderMCPServer: function(server, index) {
    const statusClass = (server.connected || server.reachable) ? 'connected' : 'disconnected';
    const toolCount = server.tool_count || 0;
    const serverId = server.id || index;
    
    // Build tools HTML
    let toolsHtml = '';
    if (server.tools && server.tools.length > 0) {
      if (server.detailed_tools && server.detailed_tools.length > 0) {
        toolsHtml = server.detailed_tools.map(tool => `
          <div class="mcp-tool">
            <div class="mcp-tool-name ${tool.blocked ? 'blocked' : ''}">${tool.name}${tool.blocked ? ' ✗' : ''}</div>
            ${tool.description ? `<div class="mcp-tool-desc">${tool.description}</div>` : ''}
          </div>
        `).join('');
      } else {
        toolsHtml = server.tools.map(tool => `
          <div class="mcp-tool"><div class="mcp-tool-name">${tool}</div></div>
        `).join('');
      }
    } else {
      toolsHtml = '<div style="color:#858585;font-size:10px;font-style:italic;">No tools</div>';
    }
    
    return `
      <div class="mcp-server" data-server-name="${server.name}" data-server-type="${server.type || ''}" data-tools="${(server.tools || []).join(' ')}">
        <div class="mcp-server-header" onclick="AgentSystem.Status.toggleMCPServer('${serverId}')">
          <span class="mcp-status ${statusClass}">●</span>
          <div class="mcp-server-name">${server.name}</div>
          <div class="mcp-server-meta">
            <span>${toolCount} tools</span>
            <span class="mcp-expand-icon" id="mcp-icon-${serverId}">▶</span>
          </div>
        </div>
        <div class="mcp-server-details" id="mcp-details-${serverId}">
          ${server.description ? `<div style="color:#858585;font-size:9px;margin-bottom:4px;">${server.description}</div>` : ''}
          <div class="mcp-tools-title">Tools:</div>
          ${toolsHtml}
          ${server.url ? `<div class="mcp-server-url">📍 ${server.url}</div>` : ''}
          ${server.error ? `<div class="mcp-server-error">⚠️ ${server.error}</div>` : ''}
        </div>
      </div>
    `;
  },
  
  toggleMCPServer: function(serverId) {
    const details = document.getElementById(`mcp-details-${serverId}`);
    const icon = document.getElementById(`mcp-icon-${serverId}`);
    
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
  
  filterMCPServers: function(searchTerm) {
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
      
      if (matches && this.mcpData?.servers[index]) {
        const serverData = this.mcpData.servers[index];
        visibleCount++;
        if (serverData.connected || serverData.reachable) visibleConnected++;
        visibleTools += serverData.tool_count || 0;
      }
    });
    
    // Update summary
    const metricsDiv = document.getElementById('mcpMetrics');
    if (metricsDiv && this.mcpData) {
      const total = this.mcpData.servers.length;
      metricsDiv.innerHTML = `
        <div class="mcp-summary-item">${searchTerm ? 'Filtered' : 'Servers'}: <span class="mcp-summary-value">${visibleCount}${searchTerm ? '/' + total : ''}</span></div>
        <div class="mcp-summary-item">Connected: <span class="mcp-summary-value" style="color:${visibleConnected === visibleCount ? '#4ec9b0' : '#dcdcaa'}">${visibleConnected}/${visibleCount}</span></div>
        <div class="mcp-summary-item">Tools: <span class="mcp-summary-value">${visibleTools}</span></div>
      `;
    }
  }
};
