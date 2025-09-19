// Context Usage Debug Module for AgentSystem WebUI

window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.ContextDebug = {
  panel: null,
  chart: null,
  updateInterval: null,
  isVisible: false,

  showPanel: function() {
    console.log('Context debug panel requested');

    // Create header with refresh controls
    const headerContent = `
      <label>
        <input type="checkbox" id="contextAutoRefresh" checked> Auto-refresh (5s)
      </label>
      <button id="contextRefreshBtn" class="icon-btn" title="Refresh now">
        <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M21 12a9 9 0 10-2.6 6.1" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M21 3v6h-6" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <button id="contextClearBtn" class="icon-btn" title="Clear history">
        <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6" stroke="#f87171" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
    `;

    // Create panel content with canvas for chart
    const bodyContent = `
      <div class="context-debug-container">
        <div class="context-metrics-summary" id="contextMetricsSummary">
          <div class="metric">
            <span class="label">Current Usage:</span>
            <span class="value" id="currentUsage">Loading...</span>
          </div>
          <div class="metric">
            <span class="label">Messages:</span>
            <span class="value" id="messageCount">--</span>
          </div>
          <div class="metric">
            <span class="label">Warning Level:</span>
            <span class="value" id="warningLevel">--</span>
          </div>
        </div>
        <div class="context-chart-container">
          <canvas id="contextUsageChart" width="800" height="400"></canvas>
        </div>
        <div class="context-statistics" id="contextStatistics">
          <h4>Statistics</h4>
          <div id="statsContent">Loading statistics...</div>
        </div>
        <div class="agent-statistics" id="agentStatistics">
          <h4>Per-Agent Context Usage</h4>
          <div id="agentStatsContent">Loading agent statistics...</div>
        </div>
      </div>
    `;

    // Create the panel
    this.panel = window.AgentSystem.PanelManager.createPanel(
      'floatingContextDebugPanel',
      'Context Usage Debug',
      bodyContent,
      'Large panel for context monitoring', // description
      headerContent
    );

    // Add event listeners
    this._setupEventListeners();

    // Load initial data
    this.loadContextData();

    // Start auto-refresh if enabled
    this._startAutoRefresh();

    this.isVisible = true;
  },

  _setupEventListeners: function() {
    if (!this.panel) return;

    const autoRefreshCheckbox = this.panel.querySelector('#contextAutoRefresh');
    const refreshBtn = this.panel.querySelector('#contextRefreshBtn');
    const clearBtn = this.panel.querySelector('#contextClearBtn');

    if (autoRefreshCheckbox) {
      autoRefreshCheckbox.addEventListener('change', (e) => {
        if (e.target.checked) {
          this._startAutoRefresh();
        } else {
          this._stopAutoRefresh();
        }
      });
    }

    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        this.loadContextData();
      });
    }

    if (clearBtn) {
      clearBtn.addEventListener('click', async () => {
        if (confirm('Clear all context usage history?')) {
          await this.clearHistory();
        }
      });
    }
  },

  _startAutoRefresh: function() {
    this._stopAutoRefresh();
    this.updateInterval = setInterval(() => {
      this.loadContextData();
    }, 5000); // 5 second intervals
  },

  _stopAutoRefresh: function() {
    if (this.updateInterval) {
      clearInterval(this.updateInterval);
      this.updateInterval = null;
    }
  },

  async loadContextData() {
    try {
      const response = await fetch('/debug/context/usage');
      const data = await response.json();

      if (data.error) {
        console.error('Failed to load context data:', data.error);
        return;
      }

      this._updateMetrics(data.latest);
      this._updateChart(data.recent_history);
      this._updateStatistics(data.statistics);
      this._updateAgentStats(data.agents);

    } catch (error) {
      console.error('Failed to load context data:', error);
    }
  },

  _updateMetrics: function(latest) {
    if (!latest || !this.panel) return;

    const currentUsageEl = this.panel.querySelector('#currentUsage');
    const messageCountEl = this.panel.querySelector('#messageCount');
    const warningLevelEl = this.panel.querySelector('#warningLevel');

    if (currentUsageEl) {
      const percentage = latest.usage_percentage.toFixed(1);
      const tokens = latest.total_tokens.toLocaleString();
      currentUsageEl.textContent = `${tokens} tokens (${percentage}%)`;

      // Color code based on usage
      currentUsageEl.className = 'value';
      if (percentage > 90) {
        currentUsageEl.classList.add('critical');
      } else if (percentage > 70) {
        currentUsageEl.classList.add('warning');
      } else {
        currentUsageEl.classList.add('normal');
      }
    }

    if (messageCountEl) {
      messageCountEl.textContent = latest.message_count.toString();
    }

    if (warningLevelEl) {
      const level = latest.warning_level || 'none';
      warningLevelEl.textContent = level;
      warningLevelEl.className = `value warning-${level}`;
    }
  },

  _updateChart: function(history) {
    if (!history || !this.panel) return;

    const canvas = this.panel.querySelector('#contextUsageChart');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const width = canvas.width;
    const height = canvas.height;

    // Clear canvas
    ctx.clearRect(0, 0, width, height);

    if (history.length === 0) {
      ctx.fillStyle = '#666';
      ctx.font = '16px Arial';
      ctx.textAlign = 'center';
      ctx.fillText('No data available', width / 2, height / 2);
      return;
    }

    // Setup chart area
    const margin = { top: 20, right: 20, bottom: 40, left: 60 };
    const chartWidth = width - margin.left - margin.right;
    const chartHeight = height - margin.top - margin.bottom;

    // Find data ranges
    const maxTokens = Math.max(...history.map(d => d.total_tokens));
    const minTokens = Math.min(...history.map(d => d.total_tokens));
    const maxTime = Math.max(...history.map(d => d.timestamp));
    const minTime = Math.min(...history.map(d => d.timestamp));

    // Draw grid lines
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 1;

    // Vertical grid lines (time)
    for (let i = 0; i <= 5; i++) {
      const x = margin.left + (chartWidth * i / 5);
      ctx.beginPath();
      ctx.moveTo(x, margin.top);
      ctx.lineTo(x, height - margin.bottom);
      ctx.stroke();
    }

    // Horizontal grid lines (tokens)
    for (let i = 0; i <= 5; i++) {
      const y = margin.top + (chartHeight * i / 5);
      ctx.beginPath();
      ctx.moveTo(margin.left, y);
      ctx.lineTo(width - margin.right, y);
      ctx.stroke();
    }

    // Draw token usage line
    ctx.strokeStyle = '#4ade80';
    ctx.lineWidth = 2;
    ctx.beginPath();

    history.forEach((point, index) => {
      const x = margin.left + ((point.timestamp - minTime) / (maxTime - minTime)) * chartWidth;
      const y = height - margin.bottom - ((point.total_tokens - minTokens) / (maxTokens - minTokens)) * chartHeight;

      if (index === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });

    ctx.stroke();

    // Draw warning threshold line
    if (history.length > 0 && history[0].context_window) {
      const warningThreshold = history[0].context_window * 0.9; // 90% threshold
      const warningY = height - margin.bottom - ((warningThreshold - minTokens) / (maxTokens - minTokens)) * chartHeight;

      ctx.strokeStyle = '#ef4444';
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 5]);
      ctx.beginPath();
      ctx.moveTo(margin.left, warningY);
      ctx.lineTo(width - margin.right, warningY);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Add labels
    ctx.fillStyle = '#ccc';
    ctx.font = '12px Arial';
    ctx.textAlign = 'center';

    // Y-axis label
    ctx.save();
    ctx.translate(15, height / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText('Tokens', 0, 0);
    ctx.restore();

    // X-axis label
    ctx.fillText('Time', width / 2, height - 10);

    // Y-axis values
    ctx.textAlign = 'right';
    for (let i = 0; i <= 5; i++) {
      const y = margin.top + (chartHeight * i / 5);
      const value = maxTokens - ((maxTokens - minTokens) * i / 5);
      ctx.fillText(Math.round(value).toLocaleString(), margin.left - 5, y + 4);
    }
  },

  _updateStatistics: function(statistics) {
    if (!statistics || !this.panel) return;

    const statsEl = this.panel.querySelector('#statsContent');
    if (!statsEl) return;

    let html = '';

    // Last hour stats
    if (statistics.last_hour && !statistics.last_hour.error) {
      const stats = statistics.last_hour;
      html += `
        <div class="stat-group">
          <h5>Last Hour</h5>
          <div class="stat-row">
            <span>Current:</span>
            <span>${stats.tokens.current.toLocaleString()} tokens (${stats.usage_percentage.current.toFixed(1)}%)</span>
          </div>
          <div class="stat-row">
            <span>Peak:</span>
            <span>${stats.tokens.max.toLocaleString()} tokens (${stats.usage_percentage.max.toFixed(1)}%)</span>
          </div>
          <div class="stat-row">
            <span>Average:</span>
            <span>${Math.round(stats.tokens.avg).toLocaleString()} tokens (${stats.usage_percentage.avg.toFixed(1)}%)</span>
          </div>
          <div class="stat-row">
            <span>Warnings:</span>
            <span>${stats.warnings.total_warnings}</span>
          </div>
          <div class="stat-row">
            <span>Management triggers:</span>
            <span>${stats.warnings.management_triggers}</span>
          </div>
        </div>
      `;
    }

    // All time stats
    if (statistics.all_time && !statistics.all_time.error) {
      const stats = statistics.all_time;
      html += `
        <div class="stat-group">
          <h5>All Time</h5>
          <div class="stat-row">
            <span>Samples:</span>
            <span>${stats.timespan.sample_count}</span>
          </div>
          <div class="stat-row">
            <span>Peak usage:</span>
            <span>${stats.tokens.max.toLocaleString()} tokens (${stats.usage_percentage.max.toFixed(1)}%)</span>
          </div>
          <div class="stat-row">
            <span>Total warnings:</span>
            <span>${stats.warnings.total_warnings}</span>
          </div>
          <div class="stat-row">
            <span>Total management:</span>
            <span>${stats.warnings.management_triggers}</span>
          </div>
        </div>
      `;
    }

    if (!html) {
      html = '<p>No statistics available</p>';
    }

    statsEl.innerHTML = html;
  },

  async clearHistory() {
    try {
      const response = await fetch('/debug/context/usage/clear', { method: 'POST' });
      const result = await response.json();

      if (result.error) {
        alert('Failed to clear history: ' + result.error);
      } else {
        // Reload data to show empty state
        this.loadContextData();
      }
    } catch (error) {
      console.error('Failed to clear history:', error);
      alert('Failed to clear history: ' + error.message);
    }
  },

  _updateAgentStats: function(agentData) {
    if (!agentData || !this.panel) return;

    const agentStatsEl = this.panel.querySelector('#agentStatsContent');
    if (!agentStatsEl) return;

    let html = '';

    if (agentData.count === 0) {
      html = '<div class="no-data">No agents currently tracked</div>';
    } else {
      html += `<div class="agent-summary">Total Agents: ${agentData.count}</div>`;

      // Sort agents by current token usage (descending)
      const agents = Object.values(agentData.details).sort((a, b) => b.current_tokens - a.current_tokens);

      agents.forEach(agent => {
        const usagePercent = agent.context_window > 0 ?
          (agent.current_tokens / agent.context_window * 100).toFixed(1) : '0.0';

        // Calculate session duration
        const sessionDuration = agent.session_start ?
          ((Date.now() / 1000 - agent.session_start) / 60).toFixed(0) : 'Unknown';

        // Color coding based on usage percentage
        let usageClass = 'usage-normal';
        if (parseFloat(usagePercent) > 90) usageClass = 'usage-critical';
        else if (parseFloat(usagePercent) > 75) usageClass = 'usage-warning';
        else if (parseFloat(usagePercent) > 50) usageClass = 'usage-moderate';

        html += `
          <div class="agent-stat-group">
            <h6>${agent.agent_name}</h6>
            <div class="agent-stats-grid">
              <div class="stat-row">
                <span>Current Usage:</span>
                <span class="${usageClass}">${agent.current_tokens.toLocaleString()} tokens (${usagePercent}%)</span>
              </div>
              <div class="stat-row">
                <span>Predicted Tokens:</span>
                <span>${agent.predicted_tokens.toLocaleString()}</span>
              </div>
              <div class="stat-row">
                <span>Actual LLM Tokens:</span>
                <span>${agent.actual_tokens.toLocaleString()}</span>
              </div>
              <div class="stat-row">
                <span>Messages:</span>
                <span>${agent.message_count}</span>
              </div>
              <div class="stat-row">
                <span>Peak Usage:</span>
                <span>${agent.peak_tokens.toLocaleString()} tokens</span>
              </div>
              <div class="stat-row">
                <span>Total LLM Calls:</span>
                <span>${agent.total_llm_calls}</span>
              </div>
              <div class="stat-row">
                <span>Total Processed:</span>
                <span>${agent.total_tokens_processed.toLocaleString()} tokens</span>
              </div>
              <div class="stat-row">
                <span>Summarizations:</span>
                <span>${agent.summarization_count}</span>
              </div>
              <div class="stat-row">
                <span>Session Duration:</span>
                <span>${sessionDuration} minutes</span>
              </div>
              <div class="stat-row">
                <span>Context Window:</span>
                <span>${agent.context_window.toLocaleString()}</span>
              </div>
            </div>

            ${agent.accumulated ? `
            <div class="accumulated-stats">
              <h7>Accumulated Statistics (All Sessions)</h7>
              <div class="agent-stats-grid">
                <div class="stat-row accumulated">
                  <span>Total LLM Tokens:</span>
                  <span class="accumulated-value">${agent.accumulated.total_tokens.toLocaleString()}</span>
                </div>
                <div class="stat-row accumulated">
                  <span>Total LLM Calls:</span>
                  <span class="accumulated-value">${agent.accumulated.total_calls.toLocaleString()}</span>
                </div>
                <div class="stat-row accumulated">
                  <span>Sessions:</span>
                  <span class="accumulated-value">${agent.accumulated.sessions}</span>
                </div>
                <div class="stat-row accumulated">
                  <span>First Seen:</span>
                  <span class="accumulated-value">${agent.accumulated.first_seen ? new Date(agent.accumulated.first_seen * 1000).toLocaleString() : 'Unknown'}</span>
                </div>
              </div>
            </div>
            ` : ''}
          </div>
        `;
      });
    }

    agentStatsEl.innerHTML = html;
  },

  hidePanel: function() {
    this._stopAutoRefresh();
    this.isVisible = false;

    if (this.panel) {
      // Panel will be removed by PanelManager
      this.panel = null;
    }
  }
};