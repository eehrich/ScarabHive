// Status Module
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.Status = {
  
  showPanel: function() {
    console.log('Status panel requested');
    
    // Create panel with loading state
    const panel = window.AgentSystem.PanelManager.createPanel(
      'floatingStatusPanel', 
      'Status & Metrics', 
      '<div class="status-metrics" id="floatingStatusMetrics"><div class="metric-item"><span class="metric-label">Loading...</span><span class="metric-value">...</span></div></div>'
    );
    
    // Load status data
    this.loadStatusData(panel);
  },
  
  loadStatusData: async function(panel) {
    try {
      console.log('Loading status data...');
      // Try multiple endpoints to get status information
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
      
      console.log('Status data loaded:', { health: healthData, meta: metaData });
      
      // Combine data from available endpoints and system info
      const combinedData = {
        status: healthData.status || 'Unknown',
        uptime: healthData.uptime_seconds ? this.formatUptime(healthData.uptime_seconds * 1000) : 'Unknown',
        version: healthData.version ? `${healthData.name || 'AgentSystem'} v${healthData.version}` : 'Unknown',
        memory_usage: this.formatMemoryUsage(),
        subscribers: metaData.subscribers || 0,
        events_published: metaData.publish_attempted || 0,
        events_delivered: metaData.delivered || 0,
        handlers_count: metaData.handlers_count || 0
      };
      
      // Update panel content directly (PanelManager already creates .panel-content wrapper)
      const content = this.renderStatusContent(combinedData);
      const body = panel.querySelector('.floating-panel-body');
      const contentDiv = body ? body.querySelector('.panel-content') || body : panel.querySelector('.panel-content') || panel;
      contentDiv.innerHTML = content;
      
    } catch (error) {
      console.error('Failed to load status data:', error);
      const body = panel.querySelector('.floating-panel-body');
      const contentDiv = body ? body.querySelector('.panel-content') || body : panel.querySelector('.panel-content') || panel;
      contentDiv.innerHTML = '<div class="status-metrics"><div class="metric-item"><span class="metric-label">Error</span><span class="metric-value">Failed to load</span></div></div>';
    }
  },
  
  formatUptime: function(milliseconds) {
    const seconds = Math.floor(milliseconds / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);
    
    if (days > 0) return `${days}d ${hours % 24}h ${minutes % 60}m`;
    if (hours > 0) return `${hours}h ${minutes % 60}m`;
    if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
    return `${seconds}s`;
  },
  
  formatMemoryUsage: function() {
    if (performance.memory) {
      const used = Math.round(performance.memory.usedJSHeapSize / 1024 / 1024);
      const total = Math.round(performance.memory.totalJSHeapSize / 1024 / 1024);
      return `${used}MB / ${total}MB`;
    }
    return 'Unknown';
  },
  
  renderStatusContent: function(data) {
    return `
      <div class="status-metrics" id="floatingStatusMetrics">
        <div class="metric-item">
          <span class="metric-label">Status</span>
          <span class="metric-value status-${data.status.toLowerCase()}">${data.status}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Uptime</span>
          <span class="metric-value">${data.uptime}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Version</span>
          <span class="metric-value">${data.version}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Memory Usage</span>
          <span class="metric-value">${data.memory_usage}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Subscribers</span>
          <span class="metric-value">${data.subscribers}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Events Published</span>
          <span class="metric-value">${data.events_published}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Events Delivered</span>
          <span class="metric-value">${data.events_delivered}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Handlers Count</span>
          <span class="metric-value">${data.handlers_count}</span>
        </div>
      </div>
    `;
  }
};

console.log('Status module loaded');