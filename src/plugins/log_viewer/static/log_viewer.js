/**
 * Log Viewer Panel JavaScript
 * Handles real-time log streaming and UI interactions
 */

class LogViewer {
    constructor(pluginName) {
        this.pluginName = pluginName;
        this.eventSource = null;
        this.currentLog = '';
        this.autoScroll = true;
        
        this.logSelect = document.getElementById('log-select');
        this.logContainer = document.getElementById('log-container');
        this.statusBar = document.getElementById('status-bar');
        this.connectionStatus = document.getElementById('connection-status');
        this.clearBtn = document.getElementById('clear-btn');
        this.downloadBtn = document.getElementById('download-btn');
        this.autoScrollCheckbox = document.getElementById('auto-scroll');
        
        this.setupEventListeners();
        this.loadLogFiles();
    }
    
    setupEventListeners() {
        this.logSelect.addEventListener('change', (e) => {
            if (e.target.value) {
                this.connectToLog(e.target.value);
            } else {
                this.disconnect();
            }
        });
        
        this.clearBtn.addEventListener('click', () => {
            this.logContainer.innerHTML = '';
        });
        
        this.downloadBtn.addEventListener('click', () => {
            if (this.currentLog) {
                window.open(`/plugins/${this.pluginName}/logs/download/${this.currentLog}`, '_blank');
            }
        });
        
        this.autoScrollCheckbox.addEventListener('change', (e) => {
            this.autoScroll = e.target.checked;
        });
    }
    
    async loadLogFiles() {
        try {
            const response = await fetch(`/plugins/${this.pluginName}/logs/list`);
            const data = await response.json();
            
            this.logSelect.innerHTML = '<option value="">Select a log file...</option>';
            
            data.logs.forEach(log => {
                if (log.exists) {
                    const option = document.createElement('option');
                    option.value = log.name;
                    option.textContent = `${log.name} (${this.formatFileSize(log.size)})`;
                    this.logSelect.appendChild(option);
                }
            });
        } catch (error) {
            this.showStatus(`Failed to load log files: ${error.message}`, 'error');
        }
    }
    
    connectToLog(logName) {
        this.disconnect();
        this.currentLog = logName;
        this.logContainer.innerHTML = '';
        
        this.eventSource = new EventSource(`/plugins/${this.pluginName}/logs/stream/${logName}`);
        
        this.eventSource.onopen = () => {
            this.connectionStatus.textContent = 'Connected';
            this.connectionStatus.style.color = '#28a745';
            this.showStatus(`Connected to ${logName}`, 'info');
        };
        
        this.eventSource.onerror = () => {
            this.connectionStatus.textContent = 'Connection Error';
            this.connectionStatus.style.color = '#dc3545';
            this.showStatus('Connection lost. Attempting to reconnect...', 'error');
        };
        
        this.eventSource.addEventListener('log_line', (event) => {
            const data = JSON.parse(event.data);
            this.appendLogLine(data.line, data.type);
        });
        
        this.eventSource.addEventListener('status', (event) => {
            const data = JSON.parse(event.data);
            this.showStatus(data.message, 'info');
        });
        
        this.eventSource.addEventListener('error', (event) => {
            const data = JSON.parse(event.data);
            this.showStatus(data.error, 'error');
        });
    }
    
    disconnect() {
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }
        this.connectionStatus.textContent = 'Disconnected';
        this.connectionStatus.style.color = '#6c757d';
        this.currentLog = '';
    }
    
    appendLogLine(line, type = 'live') {
        const lineDiv = document.createElement('div');
        lineDiv.className = `log-line ${type}`;
        
        // Add color coding based on log level
        if (line.toLowerCase().includes('error')) {
            lineDiv.classList.add('error');
        } else if (line.toLowerCase().includes('warning') || line.toLowerCase().includes('warn')) {
            lineDiv.classList.add('warning');
        } else if (line.toLowerCase().includes('info')) {
            lineDiv.classList.add('info');
        }
        
        lineDiv.textContent = line;
        this.logContainer.appendChild(lineDiv);
        
        // Auto-scroll to bottom if enabled
        if (this.autoScroll) {
            this.logContainer.scrollTop = this.logContainer.scrollHeight;
        }
        
        // Limit number of lines to prevent memory issues
        const maxLines = 1000;
        if (this.logContainer.children.length > maxLines) {
            this.logContainer.removeChild(this.logContainer.firstChild);
        }
    }
    
    showStatus(message, type = 'info') {
        this.statusBar.textContent = message;
        this.statusBar.className = `status-bar ${type}`;
        this.statusBar.style.display = 'block';
        
        setTimeout(() => {
            this.statusBar.style.display = 'none';
        }, 3000);
    }
    
    formatFileSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }
}

// Export for use in HTML template
window.LogViewer = LogViewer;