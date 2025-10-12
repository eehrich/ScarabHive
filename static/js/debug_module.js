// Debug Module
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.Debug = {
  autoRefresh: true,
  updateInterval: null,
  
  showPanel: function() {
    console.log('Debug panel requested');
    
    // Create header content with auto-refresh toggle and refresh button
    const headerContent = `
      <button id="debugAutoRefreshBtn" class="icon-btn active" title="Auto-refresh: ON" aria-label="Toggle auto-refresh" aria-pressed="true">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" xmlns="http://www.w3.org/2000/svg">
          <circle cx="12" cy="12" r="10" stroke="#9ab" stroke-width="1.4" />
          <path id="debugAutoToggleIcon" d="M9 8h2v8H9V8zm4 0h2v8h-2V8z" fill="#9ab" />
        </svg>
      </button>
      <button id="debugRefreshBtn" class="icon-btn" title="Refresh debug info" aria-label="Refresh debug info">
        <svg class="mcp-refresh-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M21 12a9 9 0 10-2.6 6.1" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M21 3v6h-6" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
    `;
    
    // Create panel with initial content
    const panel = window.AgentSystem.PanelManager.createPanel(
      'floatingDebugPanel', 
      'Debug Messages & Context',
      this.renderDebugContent(),
      '',
      headerContent
    );
    
    // Add auto-refresh toggle button listener
    const autoRefreshBtn = panel.querySelector('#debugAutoRefreshBtn');
    if (autoRefreshBtn) {
      autoRefreshBtn.addEventListener('click', () => {
        this.toggleAutoRefresh();
      });
    }
    
    // Add refresh button listener
    const refreshBtn = panel.querySelector('#debugRefreshBtn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        this.updateDebugInfo();
      });
    }
    
    // Load initial debug data
    this.updateDebugInfo();
    
    // Start updating debug info if auto-refresh is enabled
    if (this.autoRefresh) {
      this.startDebugUpdates(panel);
    }
  },
  
  toggleAutoRefresh: function() {
    this.autoRefresh = !this.autoRefresh;
    const autoRefreshBtn = document.querySelector('#debugAutoRefreshBtn');
    const iconPath = document.querySelector('#debugAutoToggleIcon');
    
    if (autoRefreshBtn) {
      autoRefreshBtn.classList.toggle('active', this.autoRefresh);
      autoRefreshBtn.title = this.autoRefresh ? 'Auto-refresh: ON' : 'Auto-refresh: OFF';
      autoRefreshBtn.setAttribute('aria-pressed', this.autoRefresh ? 'true' : 'false');
    }
    
    if (iconPath) {
      // Pause icon (two bars) when enabled, Play icon (triangle) when disabled
      iconPath.setAttribute('d', this.autoRefresh ? 'M9 8h2v8H9V8zm4 0h2v8h-2V8z' : 'M10 8v8l6-4-6-4z');
    }
    
    if (this.autoRefresh) {
      const panel = document.getElementById('floatingDebugPanel');
      if (panel) {
        this.startDebugUpdates(panel);
      }
    } else {
      this.stopDebugUpdates();
    }
  },
  
  stopDebugUpdates: function() {
    if (this.updateInterval) {
      clearInterval(this.updateInterval);
      this.updateInterval = null;
    }
  },
  
  startDebugUpdates: function(panel) {
    // Clear any existing interval
    this.stopDebugUpdates();
    
    // Update debug info every 5 seconds
    this.updateInterval = setInterval(() => {
      if (!document.getElementById('floatingDebugPanel')) {
        this.stopDebugUpdates();
        return;
      }
      
      this.updateDebugInfo();
    }, 5000);
  },
  
  renderDebugContent: function() {
    // Return the static HTML structure - content will be populated by updateDebugInfo
    return `
      <div class="debug-content" id="floatingDebugContent">
        <div class="debug-section">
          <h3>Context Statistics</h3>
          <div id="debugContextStats" class="debug-stats">
            <div class="metric-item">
              <span class="metric-label">Loading...</span>
              <span class="metric-value">...</span>
            </div>
          </div>
        </div>
        
        <div class="debug-section">
          <h3>Current Messages</h3>
          <div class="debug-messages" id="debugMessages">
            <div class="metric-item">
              <span class="metric-label">Loading...</span>
            </div>
          </div>
        </div>
      </div>
    `;
  },

  // Update debug info with real data from backend
  updateDebugInfo: async function() {
    try {
      // Get current agent selection if selector module is available
      let url = '/debug/context';
      if (window.selectorModule && typeof window.selectorModule.getCurrentAgent === 'function') {
        const currentAgent = window.selectorModule.getCurrentAgent();
        if (currentAgent) {
          url = `/debug/context?agent_name=${encodeURIComponent(currentAgent)}`;
        }
      }
      
      const response = await fetch(url);
      if (response.ok) {
        const data = await response.json();

        // Update context statistics
        const ctxWindow = Number(data.context_window || 0);
        const predFrac = Number(data.prediction_threshold || 0);
        const predPercent = (predFrac * 100).toFixed(1) + "%";
        const predTokens = ctxWindow ? Math.round(ctxWindow * predFrac).toLocaleString() : 'N/A';

        // Summarization threshold
        let sumTokensRaw = data.summarization_threshold;
        let sumTokens = 'N/A';
        let sumPercent = 'N/A';
        if (typeof sumTokensRaw === 'number') {
          if (sumTokensRaw > 1) {
            sumTokens = sumTokensRaw.toLocaleString();
            sumPercent = ctxWindow ? ((sumTokensRaw / ctxWindow) * 100).toFixed(1) + '%' : 'N/A';
          } else {
            sumTokens = ctxWindow ? Math.round(ctxWindow * sumTokensRaw).toLocaleString() : 'N/A';
            sumPercent = (sumTokensRaw * 100).toFixed(1) + '%';
          }
        }

        const contextStatsHtml = `
          <div class="metric-item">
            <span class="metric-label">Context Window</span>
            <span class="metric-value">${ctxWindow ? ctxWindow.toLocaleString() : 'N/A'}</span>
          </div>
          <div class="metric-item">
            <span class="metric-label">Prediction Threshold</span>
            <span class="metric-value">${predTokens} (${predPercent})</span>
          </div>
          <div class="metric-item">
            <span class="metric-label">Summarization Threshold</span>
            <span class="metric-value">${sumTokens} (${sumPercent})</span>
          </div>
          <div class="metric-item">
            <span class="metric-label">Actual Total Tokens</span>
            <span class="metric-value">${(data.actual_usage?.total_tokens || 0).toLocaleString()}</span>
          </div>
          <div class="metric-item">
            <span class="metric-label">Last Call Tokens</span>
            <span class="metric-value">${(data.actual_usage?.last_call_tokens || 0).toLocaleString()}</span>
          </div>
        `;

        // Update messages display
        let messagesHtml = '';
        if (data.messages && data.messages.length > 0) {
          messagesHtml = data.messages.map((msg, index) => {
            // Handle both string and array content (multimodal)
            let fullContent = '';
            if (typeof msg.content === 'string') {
              fullContent = msg.content;
            } else if (Array.isArray(msg.content)) {
              // Multimodal content - format nicely
              fullContent = msg.content.map(item => {
                if (item.type === 'text') {
                  return item.text || '';
                } else if (item.type === 'image_url') {
                  const url = item.image_url?.url || '';
                  if (url.startsWith('data:image/')) {
                    return `[🖼️ Image: ${url.substring(0, 50)}...]`;
                  }
                  return `[🖼️ Image: ${url}]`;
                } else {
                  return `[${item.type || 'unknown'}]`;
                }
              }).join('\n');
            } else {
              fullContent = String(msg.content || '');
            }
            
            const maxPreview = 200;
            const needsTruncate = fullContent.length > maxPreview;
            const preview = needsTruncate ? fullContent.substring(0, maxPreview) + '...' : fullContent;
            const msgId = `debug-msg-${index}`;

            return `
              <div class="debug-message" id="${msgId}">
                <div class="debug-message-header">
                  <div class="debug-header-left">
                    <span class="debug-message-role ${needsTruncate ? 'clickable' : ''}" data-target="${needsTruncate ? msgId : ''}">${msg.role || 'unknown'}</span>
                    <span class="debug-message-index">#${index + 1}</span>
                  </div>
                  <div class="debug-header-right">
                    <span class="debug-message-tokens">${msg.estimated_tokens || '?'} tokens</span>
                  </div>
                </div>
                <div class="debug-message-content">
                  <div class="debug-preview">${this.escapeHtml(preview)}</div>
                  <div class="debug-full" style="display:none">${this.escapeHtml(fullContent)}</div>
                </div>
              </div>
            `;
          }).join('');
        } else {
          messagesHtml = '<div class="metric-item"><span class="metric-label">No messages in conversation</span></div>';
        }

        const debugContextStats = document.getElementById('debugContextStats');
        const debugMessages = document.getElementById('debugMessages');

        if (debugContextStats) debugContextStats.innerHTML = contextStatsHtml;
        if (debugMessages) {
          debugMessages.innerHTML = messagesHtml;
          this.attachToggleHandlers(debugMessages);
        }

      } else {
        this.showDebugError('Failed to load debug info');
      }
    } catch (error) {
      console.error('Failed to update debug info:', error);
      this.showDebugError('Network error');
    }
  },

  // Helper function to escape HTML
  escapeHtml: function(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  },

  // Show error in debug panels
  showDebugError: function(message) {
    const debugContextStats = document.getElementById('debugContextStats');
    const debugMessages = document.getElementById('debugMessages');

    if (debugContextStats) {
      debugContextStats.innerHTML = `
        <div class="metric-item">
          <span class="metric-label">Error</span>
          <span class="metric-value">${message}</span>
        </div>
      `;
    }
    if (debugMessages) {
      debugMessages.innerHTML = `<div class="metric-item"><span class="metric-label">${message}</span></div>`;
    }
  },

  // Attach toggle handlers for expand/collapse functionality
  attachToggleHandlers: function(container) {
    const toggles = container.querySelectorAll('.debug-message-role.clickable');
    toggles.forEach(elem => {
      elem.addEventListener('click', (ev) => {
        const targetId = elem.getAttribute('data-target');
        if (!targetId) return;

        const msgContainer = document.getElementById(targetId);
        if (!msgContainer) return;

        const preview = msgContainer.querySelector('.debug-preview');
        const full = msgContainer.querySelector('.debug-full');
        const roleLabel = msgContainer.querySelector('.debug-message-role.clickable');

        const currentlyExpanded = roleLabel && roleLabel.classList.contains('expanded');

        if (currentlyExpanded) {
          // collapse
          if (full) full.style.display = 'none';
          if (preview) preview.style.display = '';
          if (roleLabel) roleLabel.classList.remove('expanded');
        } else {
          // expand
          if (preview) preview.style.display = 'none';
          if (full) full.style.display = 'block';
          if (roleLabel) roleLabel.classList.add('expanded');
        }
      });
    });
  }
};

console.log('Debug module loaded');