// Session Info Module - Shows context variables and context window stats
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.SessionInfo = {
  currentSessionId: null,
  
  init: function() {
    const btn = document.getElementById('sessionInfoBtn');
    if (btn) {
      btn.addEventListener('click', () => this.togglePanel());
    }
  },
  
  togglePanel: function() {
    const existingPanel = document.getElementById('floatingSessionInfoPanel');
    if (existingPanel) {
      window.AgentSystem.PanelManager.closePanel('floatingSessionInfoPanel');
      return;
    }
    this.showPanel();
  },
  
  showPanel: function() {
    // Get current session ID from the session module
    const sessionId = window.AgentSystem.Sessions?.getCurrentSessionId() ||
                      window.AgentSystem.Sessions?.currentSessionId || 
                      document.getElementById('headerSessionId')?.textContent?.trim();
    
    this.currentSessionId = sessionId;
    
    const headerContent = `
      <button id="sessionInfoRefreshBtn" class="icon-btn" title="Refresh">🔄</button>
    `;
    
    const panel = window.AgentSystem.PanelManager.createPanel(
      'floatingSessionInfoPanel',
      'ℹ️ Session Info',
      '<div class="session-info-loading">Loading...</div>',
      '',
      headerContent
    );
    
    // Position near bottom-right (above input)
    panel.style.bottom = '80px';
    panel.style.right = '20px';
    panel.style.top = 'auto';
    panel.style.left = 'auto';
    panel.style.width = '500px';
    panel.style.height = '600px';
    panel.style.minWidth = '380px';
    panel.style.minHeight = '320px';
    
    // Add refresh handler
    const refreshBtn = panel.querySelector('#sessionInfoRefreshBtn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => this.loadData(panel));
    }
    
    this.loadData(panel);
  },
  
  loadData: async function(panel) {
    const body = panel.querySelector('.floating-panel-body');
    if (!body) return;
    
    const sessionId = this.currentSessionId;
    
    if (!sessionId || sessionId === '--' || sessionId === 'New') {
      body.innerHTML = this.renderNoSession();
      return;
    }
    
    try {
      // Fetch session data and context usage tracker data in parallel
      const headers = window.AgentSystem.Auth?.getAuthHeaders() || {};
      
      const [sessionResponse, usageResponse] = await Promise.all([
        fetch(`/api/sessions/${sessionId}`, { headers }),
        fetch(`/plugins/context_usage_tracker/usage?session_id=${encodeURIComponent(sessionId)}`, { headers }).catch(() => null)
      ]);
      
      if (!sessionResponse.ok) {
        throw new Error(`Failed to load session: ${sessionResponse.status}`);
      }
      
      const data = await sessionResponse.json();
      
      // Extract tool_definition_tokens from context_usage_tracker if available
      let trackerData = null;
      if (usageResponse && usageResponse.ok) {
        try {
          trackerData = await usageResponse.json();
        } catch (e) {
          // Ignore parse errors
        }
      }
      
      body.innerHTML = this.renderContent(data, trackerData);
      
    } catch (error) {
      console.error('Failed to load session info:', error);
      body.innerHTML = `<div class="session-info-error">Failed to load: ${error.message}</div>`;
    }
  },
  
  renderNoSession: function() {
    return `
      <div class="session-info-empty">
        <div class="empty-icon">📋</div>
        <div class="empty-text">No active session</div>
        <div class="empty-hint">Start a conversation to see session info</div>
      </div>
    `;
  },
  
  renderContent: function(data, trackerData) {
    const contextVars = data.context_vars || {};
    const descendants = data.descendants_context_vars || [];
    const messages = data.messages || [];

    // Calculate context distribution
    const stats = this.calculateContextStats(messages, trackerData);

    return `
      <div class="session-info-content">
        ${this.renderContextVarsSection(contextVars, descendants)}
        ${this.renderContextWindowSection(stats, trackerData)}
        ${this.renderMessagesSection(stats)}
      </div>
    `;
  },

  renderContextVarsSection: function(contextVars, descendants = []) {
    const keys = Object.keys(contextVars);
    const ownVarsHtml = keys.length > 0 ? this.renderVarsList(contextVars) : '';
    const ownEmpty = keys.length === 0;
    const descendantsHtml = this.renderDescendantsTree(descendants);
    const hasAny = !ownEmpty || descendantsHtml.length > 0;

    if (!hasAny) {
      return `
        <div class="si-section">
          <div class="si-section-header">
            <span class="si-icon">🏷️</span>
            <span class="si-title">Context Variables</span>
          </div>
          <div class="si-empty-hint">No context variables set</div>
        </div>
      `;
    }

    const totalCount = keys.length + this.countDescendantVars(descendants);

    return `
      <div class="si-section">
        <div class="si-section-header">
          <span class="si-icon">🏷️</span>
          <span class="si-title">Context Variables</span>
          <span class="si-count">${totalCount}</span>
        </div>
        ${ownEmpty ? '' : `<div class="si-vars-list">${ownVarsHtml}</div>`}
        ${ownEmpty && descendantsHtml ? '<div class="si-empty-hint" style="margin-bottom:6px">No vars on this session — showing sub-sessions</div>' : ''}
        ${descendantsHtml}
      </div>
    `;
  },

  renderVarsList: function(contextVars) {
    const phaseColors = {
      'planning': '#569cd6',
      'characters': '#c586c0',
      'structure': '#4ec9b0',
      'content': '#dcdcaa',
      'review': '#ce9178'
    };

    let varsHtml = '';
    for (const [key, value] of Object.entries(contextVars)) {
      let valueHtml = '';

      if (key === 'workflow_phase' && phaseColors[value]) {
        const color = phaseColors[value];
        valueHtml = `<span class="si-phase-badge" style="background:${color}20;color:${color};border:1px solid ${color}40;">${value}</span>`;
      } else if (key === 'book_id' && value) {
        valueHtml = `<span class="si-book-badge">📚 ${value}</span>`;
      } else {
        valueHtml = `<span class="si-var-value">${this.escapeHtml(String(value))}</span>`;
      }

      varsHtml += `
        <div class="si-var-row">
          <span class="si-var-name">${this.escapeHtml(key)}</span>
          ${valueHtml}
        </div>
      `;
    }
    return varsHtml;
  },

  countDescendantVars: function(descendants) {
    let n = 0;
    for (const d of descendants || []) {
      n += Object.keys(d.context_vars || {}).length;
      n += this.countDescendantVars(d.children || []);
    }
    return n;
  },

  renderDescendantsTree: function(descendants, depth = 1) {
    if (!descendants || descendants.length === 0) return '';
    let html = '';
    for (const d of descendants) {
      const sid = d.session_id || '';
      const agent = d.agent_name || '?';
      const cv = d.context_vars || {};
      const cvKeys = Object.keys(cv);
      const childrenHtml = this.renderDescendantsTree(d.children || [], depth + 1);

      // Skip nodes with no vars and no descendant-vars to avoid noise
      if (cvKeys.length === 0 && childrenHtml.length === 0) continue;

      const indentPx = 12 * depth;
      const varsListHtml = cvKeys.length > 0 ? `<div class="si-vars-list">${this.renderVarsList(cv)}</div>` : '';
      html += `
        <div class="si-subsession" style="margin-left:${indentPx}px;border-left:2px solid #3a3a3a;padding-left:8px;margin-top:8px">
          <div class="si-subsession-header" style="font-size:11px;color:#9cdcfe;margin-bottom:4px">
            <span style="color:#888">⤷</span>
            <span class="si-subsession-agent">${this.escapeHtml(agent)}</span>
            <span class="si-subsession-id" style="color:#666;font-family:monospace">(${this.escapeHtml(sid)})</span>
            <span class="si-count" style="margin-left:6px">${cvKeys.length}</span>
          </div>
          ${varsListHtml}
          ${childrenHtml}
        </div>
      `;
    }
    return html;
  },
  
  renderContextWindowSection: function(stats, trackerData) {
    const total = stats.totalTokens;
    
    // Get context_window from tracker data if available, otherwise use default
    let maxContext = 128000;
    if (trackerData?.latest?.context_window && trackerData.latest.context_window > 0) {
      maxContext = trackerData.latest.context_window;
    }
    
    const usagePercent = Math.min(100, (total / maxContext) * 100);
    
    // Color based on usage
    let barColor = '#4ec9b0'; // green
    if (usagePercent > 70) barColor = '#dcdcaa'; // yellow
    if (usagePercent > 90) barColor = '#f14c4c'; // red
    
    return `
      <div class="si-section">
        <div class="si-section-header">
          <span class="si-icon">📊</span>
          <span class="si-title">Context Window</span>
        </div>
        <div class="si-context-bar-container">
          <div class="si-context-bar">
            <div class="si-context-bar-fill" style="width:${usagePercent}%;background:${barColor}"></div>
          </div>
          <div class="si-context-label">
            ~${this.formatNumber(total)} / ${this.formatNumber(maxContext)} tokens (${usagePercent.toFixed(1)}%)
          </div>
        </div>
        <div class="si-breakdown">
          ${this.renderBreakdownItem('System', stats.systemTokens, total, '#569cd6')}
          ${this.renderBreakdownItem('Tool Definitions', stats.toolDefinitionTokens, total, '#d7ba7d')}
          ${this.renderBreakdownItem('User', stats.userTokens, total, '#4ec9b0')}
          ${this.renderBreakdownItem('Assistant', stats.assistantTokens, total, '#dcdcaa')}
          ${this.renderBreakdownItem('Tool Calls', stats.toolCallTokens, total, '#c586c0')}
          ${this.renderBreakdownItem('Tool Results', stats.toolResultTokens, total, '#ce9178')}
          ${stats.imageCount > 0 ? this.renderBreakdownItem('Images', stats.imageTokens, total, '#f14c4c', `(${stats.imageCount} files)`) : ''}
        </div>
      </div>
    `;
  },
  
  renderBreakdownItem: function(label, tokens, total, color, extra = '') {
    const percent = total > 0 ? ((tokens / total) * 100).toFixed(1) : 0;
    return `
      <div class="si-breakdown-row">
        <span class="si-breakdown-dot" style="background:${color}"></span>
        <span class="si-breakdown-label">${label}</span>
        <span class="si-breakdown-value">${this.formatNumber(tokens)} <span class="si-breakdown-pct">(${percent}%)</span> ${extra}</span>
      </div>
    `;
  },
  
  renderMessagesSection: function(stats) {
    return `
      <div class="si-section">
        <div class="si-section-header">
          <span class="si-icon">💬</span>
          <span class="si-title">Messages</span>
          <span class="si-count">${stats.messageCount}</span>
        </div>
        <div class="si-stats-grid">
          <div class="si-stat">
            <span class="si-stat-value">${stats.userMessages}</span>
            <span class="si-stat-label">User</span>
          </div>
          <div class="si-stat">
            <span class="si-stat-value">${stats.assistantMessages}</span>
            <span class="si-stat-label">Assistant</span>
          </div>
          <div class="si-stat">
            <span class="si-stat-value">${stats.toolCalls}</span>
            <span class="si-stat-label">Tool Calls</span>
          </div>
          <div class="si-stat">
            <span class="si-stat-value">${stats.imageCount}</span>
            <span class="si-stat-label">Images</span>
          </div>
        </div>
      </div>
    `;
  },
  
  calculateContextStats: function(messages, trackerData) {
    let stats = {
      messageCount: messages.length,
      userMessages: 0,
      assistantMessages: 0,
      toolCalls: 0,
      imageCount: 0,
      totalTokens: 0,
      systemTokens: 0,
      userTokens: 0,
      assistantTokens: 0,
      toolCallTokens: 0,
      toolResultTokens: 0,
      toolDefinitionTokens: 0,
      imageTokens: 0
    };
    
    // System prompts are not stored in session messages (they're generated dynamically at runtime)
    // but contribute to context window. Use estimated value based on typical system prompt size.
    // Average system prompt WITHOUT tools: ~500 tokens (tools are now tracked separately)
    const estimatedSystemTokens = 500;
    let hasExplicitSystemMessage = false;
    
    // Get tool definition tokens from context_usage_tracker if available
    if (trackerData?.latest?.tool_definition_tokens && trackerData.latest.tool_definition_tokens > 0) {
      stats.toolDefinitionTokens = trackerData.latest.tool_definition_tokens;
    }
    
    for (const msg of messages) {
      const role = msg.role;
      const content = msg.content || '';
      
      // Use pre-computed estimated_tokens if available (from session_service.py)
      // Otherwise fall back to rough char-based estimate
      let tokens;
      if (typeof msg.estimated_tokens === 'number' && msg.estimated_tokens > 0) {
        tokens = msg.estimated_tokens;
      } else {
        const contentLen = typeof content === 'string' ? content.length : JSON.stringify(content).length;
        tokens = Math.ceil(contentLen / 4);
      }
      
      if (role === 'system') {
        hasExplicitSystemMessage = true;
        stats.systemTokens += tokens;
      } else if (role === 'user') {
        stats.userMessages++;
        stats.userTokens += tokens;
        
        // Check for images in content
        if (Array.isArray(content)) {
          for (const part of content) {
            if (part.type === 'image_url' || part.type === 'image') {
              stats.imageCount++;
              stats.imageTokens += 1000; // Approximate image token cost
            }
          }
        }
      } else if (role === 'assistant') {
        stats.assistantMessages++;
        
        // Check for tool calls
        if (msg.tool_calls && msg.tool_calls.length > 0) {
          stats.toolCalls += msg.tool_calls.length;
          
          // If estimated_tokens is pre-computed, it already includes tool_calls
          // So we need to split: content goes to assistantTokens, tool_calls go separately
          if (typeof msg.estimated_tokens === 'number' && msg.estimated_tokens > 0) {
            // Estimate tool call portion
            let toolCallTokens = 0;
            for (const tc of msg.tool_calls) {
              toolCallTokens += Math.ceil(JSON.stringify(tc).length / 4);
            }
            // Attribute tool calls to toolCallTokens, rest to assistantTokens
            stats.toolCallTokens += toolCallTokens;
            stats.assistantTokens += Math.max(0, tokens - toolCallTokens);
          } else {
            // Fallback: use old calculation
            stats.assistantTokens += tokens;
            for (const tc of msg.tool_calls) {
              const tcLen = JSON.stringify(tc).length;
              stats.toolCallTokens += Math.ceil(tcLen / 4);
            }
          }
        } else {
          // No tool calls - all tokens are content
          stats.assistantTokens += tokens;
        }
      } else if (role === 'tool') {
        stats.toolResultTokens += tokens;
      }
    }
    
    // If no explicit system message was found in the session, use estimated value
    // (System prompts are generated dynamically at runtime and not persisted in sessions)
    if (!hasExplicitSystemMessage) {
      stats.systemTokens = estimatedSystemTokens;
    }
    
    stats.totalTokens = stats.systemTokens + stats.toolDefinitionTokens + stats.userTokens + stats.assistantTokens + 
                        stats.toolCallTokens + stats.toolResultTokens + stats.imageTokens;
    
    return stats;
  },
  
  formatNumber: function(num) {
    if (num >= 1000000) {
      return (num / 1000000).toFixed(1) + 'M';
    } else if (num >= 1000) {
      return (num / 1000).toFixed(1) + 'K';
    }
    return num.toString();
  },
  
  escapeHtml: function(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  },
  
  // Called when session changes
  onSessionChange: function(sessionId) {
    this.currentSessionId = sessionId;
    const panel = document.getElementById('floatingSessionInfoPanel');
    if (panel) {
      this.loadData(panel);
    }
  }
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  window.AgentSystem.SessionInfo.init();
});
