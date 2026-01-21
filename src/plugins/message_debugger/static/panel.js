const messageDebugger = {
    autoRefreshInterval: null,
    autoRefreshEnabled: false,  // Track desired state (default: inactive)
    expandedSnapshots: new Set(),
    expandedMessages: new Set(),  // Track expanded messages (messageId = snapshotId_msgIndex)
    snapshotFilters: {},  // Track filter state per snapshot: { snapshotId: { role: '', search: '' } }
    
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
            
            // Restore filter states
            Object.keys(this.snapshotFilters).forEach(snapshotId => {
                const filterState = this.snapshotFilters[snapshotId];
                const roleFilter = document.querySelector(`.snapshot-role-filter[data-snapshot-id="${snapshotId}"]`);
                const searchInput = document.querySelector(`.snapshot-search[data-snapshot-id="${snapshotId}"]`);
                
                if (roleFilter && filterState.role) {
                    roleFilter.value = filterState.role;
                }
                if (searchInput && filterState.search) {
                    searchInput.value = filterState.search;
                }
                
                // Re-apply the filters
                if (filterState.role || filterState.search) {
                    this.filterMessages(snapshotId);
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
        
        // Get unique roles for filter dropdown
        const roles = [...new Set(messages.map(m => m.role).filter(Boolean))].sort();
        const roleOptions = roles.map(r => `<option value="${r}">${r}</option>`).join('');
        
        // Get current filter state
        const filterState = this.snapshotFilters[snapshotId] || { role: '', search: '' };
        
        return `
            <div class="snapshot-card" data-snapshot-id="${snapshotId}" onclick="messageDebugger.toggleSnapshot(this, event)">
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
                    <div class="snapshot-filters" onclick="event.stopPropagation()">
                        <select class="snapshot-role-filter" data-snapshot-id="${snapshotId}" onchange="messageDebugger.filterMessages('${snapshotId}')">
                            <option value="">All Roles</option>
                            ${roleOptions}
                        </select>
                        <input type="text" class="snapshot-search" data-snapshot-id="${snapshotId}" 
                            placeholder="Search messages..." 
                            oninput="messageDebugger.filterMessages('${snapshotId}')"
                            value="${this.escapeHtml(filterState.search)}">
                        <span class="snapshot-filter-count" data-snapshot-id="${snapshotId}"></span>
                    </div>
                    <div class="messages-container" data-snapshot-id="${snapshotId}">
                        ${messages.map((msg, msgIndex) => this.renderMessage(msg, snapshotId, msgIndex)).join('')}
                    </div>
                </div>
            </div>
        `;
    },
    
    renderMessage(msg, snapshotId, msgIndex) {
        const roleClass = msg.role || 'unknown';
        const content = msg.content || '';
        const tokens = msg.estimated_tokens ? `${msg.estimated_tokens} tokens` : '';
        const messageId = `${snapshotId}_msg${msgIndex}`;
        const isExpanded = this.expandedMessages.has(messageId);
        const toolCallCount = msg.tool_calls ? msg.tool_calls.length : 0;
        
        // Determine preview text - 80 chars, stripped of leading whitespace
        let inlinePreview = '';
        if (content && content.trim()) {
            inlinePreview = content.replace(/^[\s\n\r]+/, '').substring(0, 80).replace(/\n/g, ' ');
            if (content.length > 80) inlinePreview += '…';
        } else if (toolCallCount > 0) {
            // No content but has tool calls
            inlinePreview = `[${toolCallCount} tool call${toolCallCount > 1 ? 's' : ''}]`;
        } else {
            inlinePreview = '[empty]';
        }
        
        // Full content for expanded view
        const displayContent = content.substring(0, 40000);
        
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
            <div class="message-item ${roleClass} ${isExpanded ? 'expanded' : ''}" data-message-id="${messageId}" onclick="messageDebugger.toggleMessage('${messageId}', event)">
                <div class="message-header">
                    <div class="message-role-line">
                        <span class="message-expand-icon">${isExpanded ? '▼' : '▶'}</span>
                        <span class="message-role">${msg.role}${toolResultBadge}</span>
                        <span class="message-preview">${this.escapeHtml(inlinePreview)}</span>
                    </div>
                    <div class="message-header-right">
                        ${tokens ? `<div class="message-tokens">${tokens}</div>` : ''}
                    </div>
                </div>
                <div class="message-details">
                    ${content ? `<div class="message-content">${this.escapeHtml(displayContent)}${content.length > 40000 ? '...' : ''}</div>` : ''}
                    ${toolCallsHtml}
                </div>
            </div>
        `;
    },
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },
    
    toggleSnapshot(element, event) {
        // Don't toggle snapshot if clicking on message expand button
        if (event && event.target.closest('.message-expand-btn')) {
            return;
        }
        
        const snapshotId = element.getAttribute('data-snapshot-id');
        element.classList.toggle('expanded');
        
        if (element.classList.contains('expanded')) {
            this.expandedSnapshots.add(snapshotId);
        } else {
            this.expandedSnapshots.delete(snapshotId);
        }
    },
    
    toggleMessage(messageId, event) {
        event.stopPropagation();  // Don't bubble to snapshot toggle
        
        if (this.expandedMessages.has(messageId)) {
            this.expandedMessages.delete(messageId);
        } else {
            this.expandedMessages.add(messageId);
        }
        
        // Re-render just this message's content without full reload
        const messageEl = document.querySelector(`[data-message-id="${messageId}"]`);
        if (messageEl) {
            const snapshotEl = messageEl.closest('.snapshot-card');
            if (snapshotEl) {
                // Force re-render of this snapshot to update message state
                const snapshotId = snapshotEl.getAttribute('data-snapshot-id');
                // For simplicity, just refresh - expanded states are preserved
                this.loadSnapshots();
            }
        }
    },
    
    filterMessages(snapshotId) {
        const roleFilter = document.querySelector(`.snapshot-role-filter[data-snapshot-id="${snapshotId}"]`);
        const searchInput = document.querySelector(`.snapshot-search[data-snapshot-id="${snapshotId}"]`);
        const container = document.querySelector(`.messages-container[data-snapshot-id="${snapshotId}"]`);
        const countSpan = document.querySelector(`.snapshot-filter-count[data-snapshot-id="${snapshotId}"]`);
        
        if (!container) return;
        
        const roleValue = roleFilter ? roleFilter.value.toLowerCase() : '';
        const searchValue = searchInput ? searchInput.value.toLowerCase() : '';
        
        // Store filter state
        this.snapshotFilters[snapshotId] = { role: roleValue, search: searchValue };
        
        const messages = container.querySelectorAll('.message-item');
        let visibleCount = 0;
        let totalCount = messages.length;
        
        messages.forEach(msg => {
            const role = msg.classList.contains('user') ? 'user' :
                        msg.classList.contains('assistant') ? 'assistant' :
                        msg.classList.contains('system') ? 'system' :
                        msg.classList.contains('tool') ? 'tool' : '';
            
            // Get content from the message-content div or preview
            const contentEl = msg.querySelector('.message-content');
            const previewEl = msg.querySelector('.message-preview');
            const toolCallsEl = msg.querySelector('.message-tool-calls');
            
            let textContent = '';
            if (contentEl) textContent += contentEl.textContent.toLowerCase();
            if (previewEl) textContent += ' ' + previewEl.textContent.toLowerCase();
            if (toolCallsEl) textContent += ' ' + toolCallsEl.textContent.toLowerCase();
            
            const matchesRole = !roleValue || role === roleValue;
            const matchesSearch = !searchValue || textContent.includes(searchValue);
            
            if (matchesRole && matchesSearch) {
                msg.style.display = '';
                visibleCount++;
            } else {
                msg.style.display = 'none';
            }
        });
        
        // Update count display
        if (countSpan) {
            if (roleValue || searchValue) {
                countSpan.textContent = `${visibleCount}/${totalCount}`;
            } else {
                countSpan.textContent = '';
            }
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
        
        // Toggle the desired state
        this.autoRefreshEnabled = !this.autoRefreshEnabled;
        
        if (this.autoRefreshEnabled) {
            // Start auto-refresh
            if (!this.autoRefreshInterval) {
                this.autoRefreshInterval = setInterval(() => this.refresh(), 5000);
            }
            btn.classList.add('active');
            btn.title = 'Auto-Refresh (5s) - Active';
        } else {
            // Stop auto-refresh
            if (this.autoRefreshInterval) {
                clearInterval(this.autoRefreshInterval);
                this.autoRefreshInterval = null;
            }
            btn.classList.remove('active');
            btn.title = 'Auto-Refresh - Inactive';
        }
    },
    
    startAutoRefresh() {
        // Initialize auto-refresh if enabled (called on page load)
        if (this.autoRefreshEnabled && !this.autoRefreshInterval) {
            this.autoRefreshInterval = setInterval(() => this.refresh(), 5000);
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
messageDebugger.startAutoRefresh();  // Start auto-refresh to match button state
