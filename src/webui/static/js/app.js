class AgentUI {
    constructor() {
        // ...existing code...
        this.debugMessagesModal = null;
        this.setupDebugMessagesModal();
    }

    setupDebugMessagesModal() {
        // Create debug messages modal
        this.debugMessagesModal = document.createElement('div');
        this.debugMessagesModal.id = 'debugMessagesModal';
        this.debugMessagesModal.className = 'modal';
        this.debugMessagesModal.innerHTML = `
            <div class="modal-content" style="width: 90%; max-width: 1200px; height: 80%;">
                <div class="modal-header">
                    <h2>Debug: Conversation Messages</h2>
                    <span class="close" onclick="agentUI.closeDebugMessages()">&times;</span>
                </div>
                <div class="modal-body" style="height: calc(100% - 100px); overflow-y: auto;">
                    <div class="debug-stats" id="debugStats"></div>
                    <div class="debug-messages" id="debugMessagesList"></div>
                </div>
                <div class="modal-footer">
                    <button onclick="agentUI.refreshDebugMessages()" class="btn btn-primary">Refresh</button>
                    <button onclick="agentUI.closeDebugMessages()" class="btn btn-secondary">Close</button>
                </div>
            </div>
        `;
        document.body.appendChild(this.debugMessagesModal);
    }

    async openDebugMessages() {
        this.debugMessagesModal.style.display = 'block';
        await this.refreshDebugMessages();
    }

    closeDebugMessages() {
        this.debugMessagesModal.style.display = 'none';
    }

    async refreshDebugMessages() {
        try {
            const response = await fetch('/api/debug/messages');
            const data = await response.json();

            // Update stats
            const statsElement = document.getElementById('debugStats');
            statsElement.innerHTML = `
                <div class="stats-grid">
                    <div class="stat-item">
                        <strong>Messages:</strong> ${data.message_count}
                    </div>
                    <div class="stat-item">
                        <strong>Total Tokens:</strong> ${data.usage_stats?.actual_usage?.total_tokens || 'N/A'}
                    </div>
                    <div class="stat-item">
                        <strong>Context Window:</strong> ${data.usage_stats?.context_window || 'N/A'}
                    </div>
                    <div class="stat-item">
                        <strong>Prediction Threshold:</strong> ${(data.usage_stats?.prediction_threshold * 100) || 90}%
                    </div>
                </div>
            `;

            // Update messages list
            const messagesElement = document.getElementById('debugMessagesList');
            messagesElement.innerHTML = data.messages.map((msg, index) => `
                <div class="debug-message" data-index="${index}">
                    <div class="message-header">
                        <strong>Role:</strong> ${msg.role}
                        <span class="message-index">#${index}</span>
                    </div>
                    <div class="message-content">
                        <pre>${JSON.stringify(msg.content, null, 2)}</pre>
                    </div>
                </div>
            `).join('');

        } catch (error) {
            console.error('Error refreshing debug messages:', error);
            this.showError('Failed to load debug messages');
        }
    }
    // ...existing code...
}