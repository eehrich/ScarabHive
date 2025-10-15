const messageDebugger = {
    autoRefreshInterval: null,
    expandedSnapshots: new Set(),
    
    async loadStats() {
        try {
            const response = await fetch('/plugins/message_debugger/stats');
            const stats = await response.json();
            
            const container = document.getElementById('stats-container');
            container.innerHTML = `
                <div class="stat-card">
                    <div class="stat-label">Total Snapshots</div>
                    <div class="stat-value">${stats.total_snapshots}</div>
                </div>
                <div class="stat-card green">
                    <div class="stat-label">Total Messages</div>
                    <div class="stat-value">${stats.total_messages}</div>
                </div>
                <div class="stat-card orange">
                    <div class="stat-label">Total Tokens</div>
                    <div class="stat-value">${stats.total_tokens.toLocaleString()}</div>
                </div>
                <div class="stat-card blue">
                    <div class="stat-label">Unique Sessions</div>
                    <div class="stat-value">${stats.unique_sessions.length}</div>
                </div>
            `;
            
            // Update filter dropdowns
            this.updateFilters(stats);
        } catch (error) {
            console.error('Failed to load stats:', error);
        }
    },
    
    updateFilters(stats) {
        const agentSelect = document.getElementById('filter-agent');
        const sessionSelect = document.getElementById('filter-session');
        
        // Update agent filter
        const currentAgent = agentSelect.value;
        agentSelect.innerHTML = '<option value="">All Agents</option>';
        stats.unique_agents.forEach(agent => {
            const option = document.createElement('option');
            option.value = agent;
            option.textContent = agent;
            if (agent === currentAgent) option.selected = true;
            agentSelect.appendChild(option);
        });
        
        // Update session filter
        const currentSession = sessionSelect.value;
        sessionSelect.innerHTML = '<option value="">All Sessions</option>';
        stats.unique_sessions.forEach(session => {
            const option = document.createElement('option');
            option.value = session;
            option.textContent = session.substring(0, 12) + '...';
            if (session === currentSession) option.selected = true;
            sessionSelect.appendChild(option);
        });
    },
    
    async loadSnapshots() {
        const container = document.getElementById('snapshots-container');
        const agentFilter = document.getElementById('filter-agent').value;
        const sessionFilter = document.getElementById('filter-session').value;
        const limit = document.getElementById('filter-limit').value;
        
        try {
            let url = `/plugins/message_debugger/snapshots?limit=${limit}`;
            if (agentFilter) url += `&agent_name=${encodeURIComponent(agentFilter)}`;
            if (sessionFilter) url += `&session_id=${encodeURIComponent(sessionFilter)}`;
            
            const response = await fetch(url);
            const data = await response.json();
            
            if (data.snapshots.length === 0) {
                container.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">📭</div>
                        <div class="empty-text">No message snapshots captured yet</div>
                        <div class="empty-hint">Run some agent requests to see messages appear here</div>
                    </div>
                `;
                return;
            }
            
            container.innerHTML = data.snapshots.map((snapshot, index) => this.renderSnapshot(snapshot, index)).join('');
            
            // Restore expanded state
            this.expandedSnapshots.forEach(snapshotId => {
                const card = document.querySelector(`[data-snapshot-id="${snapshotId}"]`);
                if (card) {
                    card.classList.add('expanded');
                }
            });
        } catch (error) {
            console.error('Failed to load snapshots:', error);
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">⚠️</div>
                    <div class="empty-text">Failed to load snapshots</div>
                    <div class="empty-hint">${error.message}</div>
                </div>
            `;
        }
    },
    
    renderSnapshot(snapshot, index) {
        const timestamp = new Date(snapshot.timestamp).toLocaleString();
        const messages = snapshot.messages || [];
        const snapshotId = `${snapshot.timestamp}_${snapshot.agent_name}_${snapshot.session_id}`;
        
        return `
            <div class="snapshot-card" data-snapshot-id="${snapshotId}" onclick="messageDebugger.toggleSnapshot(this)">
                <div class="snapshot-header">
                    <div class="snapshot-title">Snapshot #${index + 1}</div>
                    <div class="snapshot-timestamp">${timestamp}</div>
                </div>
                <div class="snapshot-meta">
                    <div class="meta-item">
                        <span class="meta-badge agent">${snapshot.agent_name || 'unknown'}</span>
                    </div>
                    ${snapshot.session_id ? `
                        <div class="meta-item">
                            <span class="meta-badge session">${snapshot.session_id.substring(0, 12)}...</span>
                        </div>
                    ` : ''}
                    <div class="meta-item">
                        <span class="meta-label">Messages:</span> ${snapshot.message_count}
                    </div>
                    ${snapshot.total_estimated_tokens ? `
                        <div class="meta-item">
                            <span class="meta-label">Tokens:</span> ${snapshot.total_estimated_tokens.toLocaleString()}
                        </div>
                    ` : ''}
                    ${snapshot.context_window ? `
                        <div class="meta-item">
                            <span class="meta-label">Context Window:</span> ${snapshot.context_window.toLocaleString()}
                        </div>
                    ` : ''}
                </div>
                <div class="snapshot-details">
                    <div class="messages-container">
                        ${messages.map(msg => this.renderMessage(msg)).join('')}
                    </div>
                </div>
            </div>
        `;
    },
    
    renderMessage(msg) {
        const roleClass = msg.role || 'unknown';
        const content = msg.content || '(no content)';
        const tokens = msg.estimated_tokens ? `${msg.estimated_tokens} tokens` : '';
        
        let toolCallsHtml = '';
        if (msg.tool_calls && msg.tool_calls.length > 0) {
            toolCallsHtml = `
                <div class="message-tool-calls">
                    <div style="font-weight: 600; margin-bottom: 8px;">Tool Calls (${msg.tool_calls.length}):</div>
                    ${msg.tool_calls.map(tc => `
                        <div class="tool-call">
                            <div class="tool-call-name">${tc.function.name}</div>
                            <div class="tool-call-args">${tc.function.arguments}</div>
                        </div>
                    `).join('')}
                </div>
            `;
        }
        
        let toolResultBadge = '';
        if (msg.is_tool_result) {
            toolResultBadge = '<span class="meta-badge" style="margin-left: 8px;">Tool Result</span>';
        }
        
        return `
            <div class="message-item ${roleClass}">
                <div class="message-header">
                    <div class="message-role">${msg.role}${toolResultBadge}</div>
                    ${tokens ? `<div class="message-tokens">${tokens}</div>` : ''}
                </div>
                <div class="message-content">${this.escapeHtml(content.substring(0, 500))}${content.length > 500 ? '...' : ''}</div>
                ${toolCallsHtml}
            </div>
        `;
    },
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },
    
    toggleSnapshot(element) {
        const snapshotId = element.getAttribute('data-snapshot-id');
        element.classList.toggle('expanded');
        
        if (element.classList.contains('expanded')) {
            this.expandedSnapshots.add(snapshotId);
        } else {
            this.expandedSnapshots.delete(snapshotId);
        }
    },
    
    async refresh() {
        await this.loadStats();
        await this.loadSnapshots();
    },
    
    async applyFilters() {
        await this.loadSnapshots();
    },
    
    toggleAutoRefresh() {
        const btn = document.getElementById('auto-refresh-btn');
        if (this.autoRefreshInterval) {
            clearInterval(this.autoRefreshInterval);
            this.autoRefreshInterval = null;
            btn.classList.remove('active');
            btn.title = 'Auto-Refresh - Inactive';
        } else {
            this.autoRefreshInterval = setInterval(() => this.refresh(), 5000);
            btn.classList.add('active');
            btn.title = 'Auto-Refresh (5s) - Active';
        }
    },
    
    async clearHistory() {
        if (!confirm('Are you sure you want to clear all message snapshots?')) {
            return;
        }
        
        try {
            await fetch('/plugins/message_debugger/snapshots', { method: 'DELETE' });
            this.expandedSnapshots.clear();
            await this.refresh();
        } catch (error) {
            console.error('Failed to clear history:', error);
            alert('Failed to clear history: ' + error.message);
        }
    }
};

// Initialize on load
messageDebugger.refresh();
