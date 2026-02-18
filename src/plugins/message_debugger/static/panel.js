/* Message Debugger Panel - JavaScript */
const debugger_ = {
    autoRefreshInterval: null,
    autoRefreshEnabled: false,
    activeTab: 'turns',
    copyDataStore: new Map(),
    copyDataCounter: 0,

    // ---- Init ----
    init() {
        this.loadStats();
        this.loadTurns();
    },

    // ---- Tab switching ----
    switchTab(tab) {
        this.activeTab = tab;
        document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
        document.querySelectorAll('.tab-content').forEach(tc => tc.classList.toggle('active', tc.id === 'tab-' + tab));
        if (tab === 'turns') this.loadTurns();
        else this.loadRequests();
    },

    // ---- Stats ----
    async loadStats() {
        try {
            const res = await fetch('/plugins/message_debugger/stats');
            const s = await res.json();
            document.getElementById('stats-container').innerHTML = `
                <div class="stat-card"><h3>Turns</h3><div class="value">${s.total_turns}</div></div>
                <div class="stat-card blue"><h3>LLM Requests</h3><div class="value">${s.total_llm_requests}</div></div>
                <div class="stat-card orange"><h3>DB Size</h3><div class="value">${s.db_size_mb || 0} MB</div></div>
                <div class="stat-card purple"><h3>Sessions</h3><div class="value">${s.unique_session_count || 0}</div></div>
                <div class="stat-card"><h3>Agents</h3><div class="value">${(s.unique_agents||[]).length}</div></div>
                <div class="stat-card red"><h3>Errors</h3><div class="value">${s.error_count||0}</div></div>
            `;
            document.getElementById('turns-count').textContent = s.total_turns;
            document.getElementById('llm-requests-count').textContent = s.total_llm_requests;
            this.updateFilterDropdowns(s);
        } catch (e) { console.error('Stats error:', e); }
    },

    updateFilterDropdowns(s) {
        const agents = s.unique_agents || [];
        const providers = s.unique_providers || [];

        this._updateSelect('turns-filter-agent', agents, a => a);
        this._updateSelect('req-filter-agent', agents, a => a);
        this._updateSelect('req-filter-provider', providers, p => p);
    },

    _updateSelect(id, items, labelFn) {
        const el = document.getElementById(id);
        if (!el) return;
        const val = el.value;
        const first = el.options[0]?.textContent || 'All';
        el.innerHTML = `<option value="">${first}</option>` + items.map(i =>
            `<option value="${this.esc(i)}" ${i===val?'selected':''}>${this.esc(labelFn(i))}</option>`
        ).join('');
    },

    // ---- Turns ----
    async loadTurns() {
        const container = document.getElementById('turns-list');
        const agent = document.getElementById('turns-filter-agent')?.value || '';
        const session = document.getElementById('turns-filter-session')?.value || '';
        const type = document.getElementById('turns-filter-type')?.value || '';
        const limit = document.getElementById('turns-filter-limit')?.value || 50;

        let url = `/plugins/message_debugger/turns?limit=${limit}`;
        if (agent) url += `&agent_name=${encodeURIComponent(agent)}`;
        if (session) url += `&session_id=${encodeURIComponent(session)}`;
        if (type) url += `&snapshot_type=${encodeURIComponent(type)}`;

        try {
            const res = await fetch(url);
            const data = await res.json();
            if (!data.turns || data.turns.length === 0) {
                container.innerHTML = this.emptyHTML('📭', 'No turns captured yet', 'Run agent requests to see message snapshots here');
                return;
            }
            container.innerHTML = data.turns.map(t => this.renderTurnCard(t)).join('');
        } catch (e) {
            container.innerHTML = this.emptyHTML('⚠️', 'Failed to load turns', e.message);
        }
    },

    renderTurnCard(t) {
        const ts = this.fmtTimestamp(t.timestamp_ms);
        const typeClass = t.snapshot_type === 'pre_llm' ? 'pre-llm' : 'post-llm';
        const typeLabel = t.snapshot_type === 'pre_llm' ? '→ Pre-LLM' : '← Post-LLM';

        // Extract cached % from LLM response usage if available
        let cachedHtml = '';
        const usage = t.llm_response_json?.usage;
        if (usage) {
            const cached = usage.prompt_tokens_details?.cached_tokens
                        || usage.cache_read_input_tokens
                        || 0;
            const prompt = usage.prompt_tokens || usage.input_tokens || 0;
            if (cached > 0 && prompt > 0) {
                const pct = Math.round((cached / prompt) * 100);
                cachedHtml = `<span class="card-metric cached">${pct}% cached</span>`;
            }
        }

        // Short session/request IDs for traceability
        const sessShort = t.session_id ? t.session_id.substring(0, 8) : '';
        const reqShort = t.request_id ? t.request_id.substring(0, 8) : '';

        return `
        <div class="turn-card" onclick="debugger_.showTurnDetail(${t.id})">
            <div class="card-row">
                <div class="card-left">
                    <span class="badge ${typeClass}">${typeLabel}</span>
                    <span class="badge agent">${this.esc(t.agent_name || '?')}</span>
                    <span class="card-meta">Step ${t.step || 0}</span>
                    ${sessShort ? `<span class="card-id" title="Session: ${this.esc(t.session_id)}">S:${this.esc(sessShort)}</span>` : ''}
                    ${reqShort ? `<span class="card-id" title="Request: ${this.esc(t.request_id)}">R:${this.esc(reqShort)}</span>` : ''}
                </div>
                <div class="card-right">
                    <span class="card-metric msgs">${t.message_count} msgs</span>
                    ${t.total_tokens ? `<span class="card-metric tokens">${t.total_tokens.toLocaleString()} tok</span>` : ''}
                    ${cachedHtml}
                    <span class="card-timestamp">${ts}</span>
                </div>
            </div>
        </div>`;
    },

    // ---- LLM Requests ----
    async loadRequests() {
        const container = document.getElementById('requests-list');
        const agent = document.getElementById('req-filter-agent')?.value || '';
        const provider = document.getElementById('req-filter-provider')?.value || '';
        const direction = document.getElementById('req-filter-direction')?.value || '';
        const limit = document.getElementById('req-filter-limit')?.value || 50;

        let url = `/plugins/message_debugger/llm-requests?limit=${limit}`;
        if (agent) url += `&agent_name=${encodeURIComponent(agent)}`;
        if (provider) url += `&provider=${encodeURIComponent(provider)}`;
        if (direction) url += `&direction=${encodeURIComponent(direction)}`;

        try {
            const res = await fetch(url);
            const data = await res.json();
            if (!data.requests || data.requests.length === 0) {
                container.innerHTML = this.emptyHTML('📭', 'No LLM requests captured yet', 'Make agent requests to see raw API logs here');
                return;
            }
            container.innerHTML = data.requests.map(r => this.renderRequestCard(r)).join('');
        } catch (e) {
            container.innerHTML = this.emptyHTML('⚠️', 'Failed to load requests', e.message);
        }
    },

    renderRequestCard(r) {
        const ts = this.fmtTimestamp(r.timestamp_ms);
        const isReq = r.direction === 'request';
        const isRetry = r.finish_reason === 'retry';
        const dirLabel = isReq ? '→ Request' : (isRetry ? '⟳ Retry' : '← Response');
        const dirClass = isReq ? 'request' : (isRetry ? 'response retry' : (r.error ? 'response error' : 'response'));
        const streaming = r.is_streaming ? '<span class="badge streaming">stream</span>' : '';

        // Extract retry label from error (e.g. "[RETRY 1/4] MALFORMED_FUNCTION_CALL" → "1/4")
        let retryInfo = '';
        if (isRetry && r.error) {
            const m = r.error.match(/\[RETRY (\d+\/\d+)\]/);
            if (m) retryInfo = `<span class="card-metric retry-count">${m[1]}</span>`;
        }
        // Short error reason (strip [RETRY x/y] prefix)
        let errorReason = '';
        if (r.error && !isReq) {
            const clean = r.error.replace(/^\[RETRY \d+\/\d+\]\s*/, '');
            if (clean.length > 50) errorReason = `<span class="card-metric error" title="${this.esc(clean)}">${this.esc(clean.substring(0, 50))}…</span>`;
            else errorReason = `<span class="card-metric error">${this.esc(clean)}</span>`;
        }

        return `
        <div class="request-card ${isRetry ? 'retry-card' : ''}" onclick="debugger_.showRequestDetail(${r.id})">
            <div class="card-row">
                <div class="card-left">
                    <span class="badge ${dirClass}">${dirLabel}</span>
                    <span class="badge provider">${this.esc(r.provider || '?')}</span>
                    <span class="badge model">${this.esc(r.model || '?')}</span>
                    ${streaming}
                    <span class="badge agent">${this.esc(r.agent_name || '?')}</span>
                </div>
                <div class="card-right">
                    ${retryInfo}
                    ${r.duration_ms ? `<span class="card-metric duration">${Math.round(r.duration_ms)}ms</span>` : ''}
                    ${errorReason}
                    ${!isRetry && r.finish_reason ? `<span class="card-metric">${r.finish_reason}</span>` : ''}
                    <span class="card-timestamp">${ts}</span>
                </div>
            </div>
        </div>`;
    },

    // ---- Detail Modals ----
    async showTurnDetail(id) {
        try {
            // Clear copy data store for fresh render
            this.copyDataStore.clear();
            this.copyDataCounter = 0;
            const res = await fetch(`/plugins/message_debugger/turns/${id}`);
            const t = await res.json();
            document.getElementById('modal-title').textContent = `Turn #${t.id} — ${t.snapshot_type} (${t.agent_name})`;
            let html = `
            <div class="detail-section">
                <h3>Metadata</h3>
                <div class="detail-grid">
                    <div class="detail-item"><div class="detail-label">Type</div><div class="detail-value">${t.snapshot_type}</div></div>
                    <div class="detail-item"><div class="detail-label">Agent</div><div class="detail-value">${this.esc(t.agent_name)}</div></div>
                    <div class="detail-item"><div class="detail-label">Request ID</div><div class="detail-value">${this.esc(t.request_id)}</div></div>
                    <div class="detail-item"><div class="detail-label">Session ID</div><div class="detail-value">${this.esc(t.session_id)}</div></div>
                    <div class="detail-item"><div class="detail-label">Step</div><div class="detail-value">${t.step}</div></div>
                    <div class="detail-item"><div class="detail-label">Messages</div><div class="detail-value">${t.message_count}</div></div>
                    <div class="detail-item"><div class="detail-label">Tokens</div><div class="detail-value">${(t.total_tokens||0).toLocaleString()}</div></div>
                    <div class="detail-item"><div class="detail-label">Context Window</div><div class="detail-value">${t.context_window || 'N/A'}</div></div>
                    <div class="detail-item"><div class="detail-label">Timestamp</div><div class="detail-value">${this.fmtTimestamp(t.timestamp_ms)}</div></div>
                </div>
            </div>`;

            // Messages
            const msgs = t.messages_json || [];
            if (msgs.length > 0) {
                html += `<div class="detail-section"><h3>Messages (${msgs.length})</h3>`;
                msgs.forEach(m => {
                    const role = m.role || 'unknown';
                    const fullContent = m.content || '';

                    // Build content HTML — try JSON formatting for tool results
                    let contentHtml = '';
                    if (fullContent) {
                        if (m.is_tool_result) {
                            try {
                                const parsed = JSON.parse(fullContent);
                                contentHtml = `<div class="msg-content">${this.formatJson(parsed)}</div>`;
                            } catch { contentHtml = `<div class="msg-content">${this.esc(fullContent)}</div>`; }
                        } else {
                            contentHtml = `<div class="msg-content">${this.esc(fullContent)}</div>`;
                        }
                    }

                    // Store full (non-truncated) copy data in JS Map to avoid HTML attribute issues
                    const copyId = this.copyDataCounter++;
                    const msgData = JSON.stringify({role, content: fullContent, tool_calls: m.tool_calls});
                    this.copyDataStore.set(copyId, msgData);
                    html += `<div class="msg-item ${role}" data-copy-id="${copyId}">
                        <div class="msg-role">
                            <span>${role}</span>
                            <span class="copy-icon msg-copy" title="Copy message">📋</span>
                        </div>
                        ${contentHtml}
                        <div class="msg-meta">
                            ${m.estimated_tokens ? `<span>${m.estimated_tokens} tokens</span>` : ''}
                            ${m.content_length ? `<span>${m.content_length} chars</span>` : ''}
                            ${m.is_tool_result ? '<span>Tool Result</span>' : ''}
                        </div>`;
                    if (m.tool_calls && m.tool_calls.length > 0) {
                        m.tool_calls.forEach(tc => {
                            const args = tc.function?.arguments || '';
                            let argsHtml;
                            try {
                                const parsed = typeof args === 'string' ? JSON.parse(args) : args;
                                argsHtml = this.formatJson(parsed);
                            } catch {
                                argsHtml = `<div class="tool-call-args">${this.esc(args)}</div>`;
                            }
                            html += `<div class="tool-call-block">
                                <div class="tool-call-name">🔧 ${tc.function?.name || '?'}</div>
                                ${argsHtml}
                            </div>`;
                        });
                    }
                    html += `</div>`;
                });
                html += `</div>`;
            }

            // LLM Response
            if (t.llm_response_json) {
                html += `<div class="detail-section"><h3>LLM Response</h3>
                    ${this.formatJson(t.llm_response_json)}
                </div>`;
            }

            document.getElementById('modal-body').innerHTML = html;
            document.getElementById('modal-overlay').classList.add('visible');
            if (typeof Prism !== 'undefined') Prism.highlightAllUnder(document.getElementById('modal-body'));
            this.addCopyIcons();
        } catch (e) { alert('Failed to load turn: ' + e.message); }
    },

    async showRequestDetail(id) {
        try {
            // Clear copy data store for fresh render
            this.copyDataStore.clear();
            this.copyDataCounter = 0;
            const res = await fetch(`/plugins/message_debugger/llm-requests/${id}`);
            const r = await res.json();
            document.getElementById('modal-title').textContent = `LLM ${r.direction} #${r.id} — ${r.provider}/${r.model}`;
            let html = `
            <div class="detail-section">
                <h3>Metadata</h3>
                <div class="detail-grid">
                    <div class="detail-item"><div class="detail-label">Direction</div><div class="detail-value">${r.direction}</div></div>
                    <div class="detail-item"><div class="detail-label">Provider</div><div class="detail-value">${this.esc(r.provider)}</div></div>
                    <div class="detail-item"><div class="detail-label">Model</div><div class="detail-value">${this.esc(r.model)}</div></div>
                    <div class="detail-item"><div class="detail-label">Agent</div><div class="detail-value">${this.esc(r.agent_name)}</div></div>
                    <div class="detail-item"><div class="detail-label">Request ID</div><div class="detail-value">${this.esc(r.request_id)}</div></div>
                    <div class="detail-item"><div class="detail-label">Session ID</div><div class="detail-value">${this.esc(r.session_id)}</div></div>
                    <div class="detail-item"><div class="detail-label">URL</div><div class="detail-value">${this.esc(r.url)}</div></div>
                    <div class="detail-item"><div class="detail-label">Streaming</div><div class="detail-value">${r.is_streaming ? 'Yes' : 'No'}</div></div>
                    <div class="detail-item"><div class="detail-label">Duration</div><div class="detail-value">${r.duration_ms ? Math.round(r.duration_ms) + 'ms' : 'N/A'}</div></div>
                    <div class="detail-item"><div class="detail-label">Finish Reason</div><div class="detail-value">${r.finish_reason || 'N/A'}</div></div>
                    <div class="detail-item"><div class="detail-label">Timestamp</div><div class="detail-value">${this.fmtTimestamp(r.timestamp_ms)}</div></div>
                </div>
            </div>`;

            if (r.error) {
                html += `<div class="detail-section"><h3>Error</h3>
                    <pre style="color:#f14c4c">${this.esc(r.error)}</pre>
                </div>`;
            }

            if (r.usage_json) {
                html += `<div class="detail-section"><h3>Usage</h3>
                    ${this.formatJson(r.usage_json)}
                </div>`;
            }

            if (r.payload_json) {
                html += `<div class="detail-section"><h3>Request Payload</h3>
                    ${this.formatJson(r.payload_json)}
                </div>`;
            }

            if (r.response_json) {
                html += `<div class="detail-section"><h3>Response Data</h3>
                    ${this.formatJson(r.response_json)}
                </div>`;
            }

            document.getElementById('modal-body').innerHTML = html;
            document.getElementById('modal-overlay').classList.add('visible');
            if (typeof Prism !== 'undefined') Prism.highlightAllUnder(document.getElementById('modal-body'));
            this.addCopyIcons();
        } catch (e) { alert('Failed to load request: ' + e.message); }
    },

    closeModal() {
        document.getElementById('modal-overlay').classList.remove('visible');
    },

    // ---- Actions ----
    async refresh() {
        await this.loadStats();
        if (this.activeTab === 'turns') await this.loadTurns();
        else await this.loadRequests();
    },

    toggleAutoRefresh() {
        this.autoRefreshEnabled = !this.autoRefreshEnabled;
        const btn = document.getElementById('auto-refresh-btn');
        if (this.autoRefreshEnabled) {
            this.autoRefreshInterval = setInterval(() => this.refresh(), 5000);
            btn.classList.add('active');
            btn.title = 'Auto-Refresh (5s) - Active';
        } else {
            if (this.autoRefreshInterval) clearInterval(this.autoRefreshInterval);
            this.autoRefreshInterval = null;
            btn.classList.remove('active');
            btn.title = 'Auto-Refresh (5s) - Inactive';
        }
    },

    async clearAll() {
        const confirmed = await this.confirm('Clear ALL captured turns and LLM request logs?\n\nThis action cannot be undone.');
        if (!confirmed) return;
        try {
            const res = await fetch('/plugins/message_debugger/clear', { method: 'DELETE' });
            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: res.statusText }));
                alert('Clear failed: ' + (err.detail || res.statusText));
                return;
            }
            await this.refresh();
        } catch (e) { alert('Clear failed: ' + e.message); }
    },

    async pruneOld() {
        const maxTurns = prompt('Keep how many recent turns?', '5000');
        if (!maxTurns) return;
        const maxReqs = prompt('Keep how many recent LLM requests?', '5000');
        if (!maxReqs) return;
        try {
            const res = await fetch(`/plugins/message_debugger/prune?max_turns=${maxTurns}&max_requests=${maxReqs}&vacuum=true`, { method: 'POST' });
            const data = await res.json();
            alert(`Pruned: ${data.turns_deleted} turns, ${data.requests_deleted} requests deleted.${data.vacuumed ? ' DB vacuumed.' : ''}`);
            await this.refresh();
        } catch (e) { alert('Prune failed: ' + e.message); }
    },

    confirm(message) {
        return new Promise(resolve => {
            this.confirmResolve = resolve;
            document.getElementById('confirm-message').textContent = message;
            document.getElementById('confirm-overlay').classList.add('visible');
        });
    },

    closeConfirm(result) {
        document.getElementById('confirm-overlay').classList.remove('visible');
        if (this.confirmResolve) {
            this.confirmResolve(result);
            this.confirmResolve = null;
        }
    },

    // ---- Helpers ----
    fmtTimestamp(ms) {
        if (!ms) return '';
        const d = new Date(ms);
        const dateStr = d.toLocaleString('de-DE', { 
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
        const msec = String(Math.floor(ms) % 1000).padStart(3, '0');
        return `${dateStr}.${msec}`;
    },

    formatJson(obj) {
        if (!obj) return '';
        const json = JSON.stringify(obj, null, 2);
        const copyId = this.copyDataCounter++;
        this.copyDataStore.set(copyId, json);
        return `<pre data-copy-id="${copyId}"><code class="language-json">${this.esc(json)}</code></pre>`;
    },

    copyToClipboard(text, iconElement) {
        navigator.clipboard.writeText(text).then(() => {
            const originalText = iconElement.textContent;
            iconElement.textContent = '✓';
            setTimeout(() => {
                iconElement.textContent = originalText;
            }, 1000);
        }).catch(err => {
            console.error('Copy failed:', err);
            iconElement.textContent = '✗';
            setTimeout(() => {
                iconElement.textContent = '📋';
            }, 1000);
        });
    },

    addCopyIcons() {
        // Add copy icons to content fields ONLY (skip metadata in detail-grid)
        document.querySelectorAll('.detail-value').forEach(valueDiv => {
            // Skip if already has icon or is in metadata grid
            if (valueDiv.querySelector('.copy-icon')) return;
            if (valueDiv.closest('.detail-grid')) return; // Skip metadata fields
            
            const text = valueDiv.textContent.trim();
            if (!text || text === 'N/A') return;
            
            // Wrap existing text in a span
            const textSpan = document.createElement('span');
            textSpan.textContent = text;
            textSpan.style.flex = '1';
            textSpan.style.minWidth = '0';
            
            // Create icon
            const icon = document.createElement('span');
            icon.className = 'copy-icon';
            icon.textContent = '📋';
            icon.title = 'Copy to clipboard';
            icon.onclick = (e) => {
                e.stopPropagation();
                this.copyToClipboard(text, icon);
            };
            
            // Clear and rebuild
            valueDiv.innerHTML = '';
            valueDiv.appendChild(textSpan);
            valueDiv.appendChild(icon);
        });

        // Add copy handlers to per-message copy icons
        document.querySelectorAll('.msg-copy').forEach(icon => {
            icon.onclick = (e) => {
                e.stopPropagation();
                const msgItem = icon.closest('.msg-item');
                const copyId = parseInt(msgItem.getAttribute('data-copy-id'), 10);
                const msgData = this.copyDataStore.get(copyId);
                if (msgData) {
                    try {
                        const parsed = JSON.parse(msgData);
                        const text = JSON.stringify(parsed, null, 2);
                        this.copyToClipboard(text, icon);
                    } catch {
                        this.copyToClipboard(msgData, icon);
                    }
                }
            };
        });

        // Add copy icons to JSON blocks (in section headers)
        document.querySelectorAll('.detail-section').forEach(section => {
            const pre = section.querySelector('pre[data-copy-id]');
            if (!pre) return;
            
            const h3 = section.querySelector('h3');
            if (!h3 || h3.querySelector('.section-copy-icon')) return;
            
            const copyId = parseInt(pre.getAttribute('data-copy-id'), 10);
            const json = this.copyDataStore.get(copyId) || '';
            const icon = document.createElement('span');
            icon.className = 'copy-icon section-copy-icon';
            icon.textContent = '📋';
            icon.title = 'Copy JSON to clipboard';
            icon.onclick = (e) => {
                e.stopPropagation();
                this.copyToClipboard(json, icon);
            };
            h3.appendChild(icon);
        });
    },

    fmtDuration(ms) {
        if (!ms || ms === 0) return '0s';
        if (ms < 1000) return Math.round(ms) + 'ms';
        if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
        return (ms / 60000).toFixed(1) + 'm';
    },

    esc(text) {
        if (!text) return '';
        const d = document.createElement('div');
        d.textContent = String(text);
        return d.innerHTML;
    },

    emptyHTML(icon, text, hint) {
        return `<div class="empty-state">
            <div class="empty-icon">${icon}</div>
            <div class="empty-text">${text}</div>
            <div class="empty-hint">${hint || ''}</div>
        </div>`;
    }
};

// Keyboard shortcuts
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        // Close confirmation modal first if open, otherwise close detail modal
        const confirmOverlay = document.getElementById('confirm-overlay');
        if (confirmOverlay && confirmOverlay.classList.contains('visible')) {
            debugger_.closeConfirm(false);
        } else {
            debugger_.closeModal();
        }
    }
});

// Initialize
debugger_.init();
