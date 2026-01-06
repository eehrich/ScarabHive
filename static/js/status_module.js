// Status Module - Modern Design (VSCode Theme)
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.Status = {
  autoRefreshInterval: null,
  lastRefreshTime: null,
  
  showPanel: function() {
    console.log('Status panel requested');
    
    // Create header with controls (like profiling panel)
    const headerContent = `
      <span id="statusLastRefresh" style="font-size: 11px; color: #858585; margin-right: 8px;"></span>
      <button id="statusRefreshBtn" class="icon-btn" title="Refresh">🔄</button>
      <button id="statusAutoRefreshBtn" class="icon-btn active" title="Auto-Refresh (5s) - Active">⏱️</button>
    `;
    
    // Create panel with loading state
    const panel = window.AgentSystem.PanelManager.createPanel(
      'floatingStatusPanel', 
      '📊 System Status', 
      '<div class="status-loading">Loading...</div>',
      '',
      headerContent
    );
    
    // Add event listeners
    const refreshBtn = panel.querySelector('#statusRefreshBtn');
    const autoRefreshBtn = panel.querySelector('#statusAutoRefreshBtn');
    
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => this.loadStatusData(panel));
    }
    
    if (autoRefreshBtn) {
      autoRefreshBtn.addEventListener('click', () => this.toggleAutoRefresh(panel));
    }
    
    // Load initial data
    this.loadStatusData(panel);
    
    // Start auto-refresh
    this.startAutoRefresh(panel);
  },
  
  toggleAutoRefresh: function(panel) {
    const btn = panel.querySelector('#statusAutoRefreshBtn');
    if (this.autoRefreshInterval) {
      this.stopAutoRefresh();
      if (btn) {
        btn.classList.remove('active');
        btn.title = 'Auto-Refresh (5s) - Paused';
      }
    } else {
      this.startAutoRefresh(panel);
      if (btn) {
        btn.classList.add('active');
        btn.title = 'Auto-Refresh (5s) - Active';
      }
    }
  },
  
  startAutoRefresh: function(panel) {
    if (this.autoRefreshInterval) return;
    this.autoRefreshInterval = setInterval(() => this.loadStatusData(panel), 5000);
  },
  
  stopAutoRefresh: function() {
    if (this.autoRefreshInterval) {
      clearInterval(this.autoRefreshInterval);
      this.autoRefreshInterval = null;
    }
  },
  
  updateTimestamp: function(panel) {
    const el = panel.querySelector('#statusLastRefresh');
    if (el && this.lastRefreshTime) {
      el.textContent = 'Updated: ' + this.lastRefreshTime.toLocaleTimeString();
    }
  },
  
  loadStatusData: async function(panel) {
    try {
      const refreshBtn = panel.querySelector('#statusRefreshBtn');
      if (refreshBtn) refreshBtn.disabled = true;
      
      // Only fetch health and event bus meta (no profiling data)
      const [healthResponse, metaResponse] = await Promise.allSettled([
        fetch('/health'),
        fetch('/status/meta')
      ]);
      
      let healthData = {};
      let metaData = {};
      
      if (healthResponse.status === 'fulfilled' && healthResponse.value.ok) {
        healthData = await healthResponse.value.json();
      }
      if (metaResponse.status === 'fulfilled' && metaResponse.value.ok) {
        metaData = await metaResponse.value.json();
      }
      
      this.lastRefreshTime = new Date();
      this.updateTimestamp(panel);
      
      const content = this.renderStatusContent(healthData, metaData);
      const body = panel.querySelector('.floating-panel-body');
      const contentDiv = body ? body.querySelector('.panel-content') || body : panel.querySelector('.panel-content') || panel;
      contentDiv.innerHTML = content;
      
    } catch (error) {
      console.error('Failed to load status data:', error);
      const body = panel.querySelector('.floating-panel-body');
      const contentDiv = body ? body.querySelector('.panel-content') || body : panel.querySelector('.panel-content') || panel;
      contentDiv.innerHTML = '<div class="status-error">❌ Failed to load status data</div>';
    } finally {
      const refreshBtn = panel.querySelector('#statusRefreshBtn');
      if (refreshBtn) refreshBtn.disabled = false;
    }
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
  
  getStatusClass: function(status) {
    if (status === 'healthy' || status === 'ok') return 'status-healthy';
    if (status === 'warning' || status === 'degraded') return 'status-warning';
    return 'status-error';
  },
  
  renderStatusContent: function(health, meta) {
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
  }
};

console.log('Status module loaded');