// Log Viewer Plugin Module
console.log('Loading log_viewer_module.js v2.0 - cache busted!');
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.log_viewer = {
  eventSource: null,
  currentLogFile: null,
  autoScroll: true,
  autoRefresh: false,
  searchTerm: '',
  lineLimit: 50,  // Default line limit
  levelFilters: {
    error: true,
    warning: true,
    info: true,
    debug: true
  },
  
  init() {
    console.log('Initializing Log Viewer plugin...');
    this.setupEventHandlers();
    this.setupResizeHandler();
    this.loadLogFiles();
  },
  
  setupEventHandlers() {
    const controls = document.querySelector('.log-controls-fixed');
    if (!controls) return;
    
    // File selector
    const fileSelect = controls.querySelector('#logFileSelect');
    if (fileSelect) {
      fileSelect.addEventListener('change', (e) => {
        this.selectLogFile(e.target.value);
      });
    }
    
    // Auto-refresh toggle
    const autoRefreshBtn = controls.querySelector('#autoRefreshBtn');
    if (autoRefreshBtn) {
      autoRefreshBtn.addEventListener('click', () => {
        this.toggleAutoRefresh();
      });
      // Set initial state
      autoRefreshBtn.title = this.autoRefresh ? 'Auto-refresh: ON' : 'Auto-refresh: OFF';
      autoRefreshBtn.classList.toggle('active', this.autoRefresh);
    }
    
    // Auto-scroll toggle icon button
    const autoScrollBtn = controls.querySelector('#autoScrollBtn');
    if (autoScrollBtn) {
      autoScrollBtn.addEventListener('click', () => {
        this.autoScroll = !this.autoScroll;
        this.updateAutoScrollUI();
      });
      // Set initial state
      autoScrollBtn.classList.toggle('active', this.autoScroll);
    }
    
    // Search input
    const searchInput = controls.querySelector('#searchInput');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        this.searchTerm = e.target.value.toLowerCase();
        this.applyFilters();
      });
    }
    
    // Line limit selector
    const lineLimitSelect = controls.querySelector('#lineLimitSelect');
    if (lineLimitSelect) {
      lineLimitSelect.addEventListener('change', (e) => {
        this.lineLimit = parseInt(e.target.value);
        // Reload current log with new limit
        if (this.currentFile) {
          this.loadInitialLogContent(this.currentFile, true);
        }
      });
      // Set initial value
      this.lineLimit = parseInt(lineLimitSelect.value) || 50;
    }
    
    // Dropdown filter
    const dropdownBtn = controls.querySelector('#filterDropdownBtn');
    const dropdownMenu = controls.querySelector('#filterDropdownMenu');
    if (dropdownBtn && dropdownMenu) {
      dropdownBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        dropdownMenu.style.display = dropdownMenu.style.display === 'block' ? 'none' : 'block';
      });

      // Close dropdown when clicking outside
      document.addEventListener('click', () => {
        dropdownMenu.style.display = 'none';
      });

      dropdownMenu.addEventListener('click', (e) => {
        e.stopPropagation();
      });

      // Set up filter checkboxes in dropdown
      const filterCheckboxes = dropdownMenu.querySelectorAll('input[type="checkbox"]');
      filterCheckboxes.forEach(checkbox => {
        checkbox.addEventListener('change', () => {
          this.levelFilters[checkbox.value] = checkbox.checked;
          this.updateDropdownLabel();
          this.applyFilters();
        });
      });

      // Set up Select All/None buttons
      const selectAllBtn = dropdownMenu.querySelector('#selectAllLevels');
      const selectNoneBtn = dropdownMenu.querySelector('#selectNoneLevels');
      
      if (selectAllBtn) {
        selectAllBtn.addEventListener('click', () => {
          Object.keys(this.levelFilters).forEach(level => {
            this.levelFilters[level] = true;
            const checkbox = dropdownMenu.querySelector(`input[value="${level}"]`);
            if (checkbox) checkbox.checked = true;
          });
          this.updateDropdownLabel();
          this.applyFilters();
        });
      }

      if (selectNoneBtn) {
        selectNoneBtn.addEventListener('click', () => {
          Object.keys(this.levelFilters).forEach(level => {
            this.levelFilters[level] = false;
            const checkbox = dropdownMenu.querySelector(`input[value="${level}"]`);
            if (checkbox) checkbox.checked = false;
          });
          this.updateDropdownLabel();
          this.applyFilters();
        });
      }

      // Initialize dropdown state
      this.syncDropdownState();
      this.updateDropdownLabel();
    }
    
    // Refresh button
    const refreshBtn = controls.querySelector('#refreshBtn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        this.refreshLogs();
      });
    }
  },
  
  async loadLogFiles() {
    console.log('Loading log files...');
    try {
      console.log('Fetching from /plugins/log_viewer/logs/list');
      const response = await fetch('/plugins/log_viewer/logs/list');
      console.log('Fetch response:', response.status, response.statusText);
      
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      
      const data = await response.json();
      console.log('Received log data:', data);
      
      this.populateFileSelect(data.logs || []);
      
      // Auto-select first existing file if available
      const existingFiles = (data.logs || []).filter(file => file.exists);
      if (existingFiles.length > 0) {
        console.log('Auto-selecting first file:', existingFiles[0].name);
        this.selectLogFile(existingFiles[0].name);
      } else {
        console.log('No existing files found to auto-select');
      }
      
    } catch (error) {
      console.error('Error loading log files:', error);
      this.showError(`Failed to load log files: ${error.message}`);
    }
  },
  
  populateFileSelect(files) {
    const fileSelect = document.querySelector('#logFileSelect');
    if (!fileSelect) {
      console.error('Log file select element not found');
      return;
    }
    
    console.log('Populating file select with files:', files);
    fileSelect.innerHTML = '<option value="">Select a log file...</option>';
    
    // Only show files that exist
    const existingFiles = files.filter(file => file.exists);
    console.log('Filtered to existing files:', existingFiles);
    
    existingFiles.forEach(file => {
      const option = document.createElement('option');
      option.value = file.name;
      option.textContent = `${file.name} (${this.formatFileSize(file.size)})`;
      fileSelect.appendChild(option);
    });
    
    console.log(`Added ${existingFiles.length} files to dropdown`);
  },
  
  formatFileSize(bytes) {
    if (!bytes) return 'unknown size';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  },
  
  selectLogFile(filename) {
    if (!filename) return;
    
    this.currentLogFile = filename;
    this.updateStatus(`Connecting to ${filename}...`);
    
    // Stop any existing polling
    this.stopPolling();
    
    // Clear logs when selecting new file
    const logContainer = document.querySelector('#logContainer');
    if (logContainer) {
      logContainer.innerHTML = '';
    }
    
    // Start new polling
    this.startStreaming(filename);
  },
  
  async loadInitialLogContent(filename, isRefresh = false) {
    if (!filename) return;
    
    try {
      // Get fresh content based on selected limit (more for refresh)
      const lines = isRefresh ? Math.max(this.lineLimit * 2, 100) : this.lineLimit;
      const url = `/plugins/log_viewer/logs/content/${encodeURIComponent(filename)}?lines=${lines}&since_timestamp=0`;
      
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`Failed to load log: ${response.status}`);
      }
      
      const data = await response.json();
      
      if (isRefresh) {
        // For refresh, replace content smoothly
        this.replaceLogContent(data.lines);
        this.lastTimestamp = data.current_timestamp || Date.now() / 1000;
      } else {
        // For initial load, append normally
        if (data.lines && data.lines.length > 0) {
          data.lines.forEach(line => this.appendLogLine(line));
          this.lastTimestamp = data.current_timestamp || Date.now() / 1000;
        }
      }
      
      this.updateStatus(`${filename} loaded (${data.lines?.length || 0} lines)`);
      
    } catch (error) {
      console.error('Failed to load initial log content:', error);
      this.showError(`Failed to load ${filename}: ${error.message}`);
    }
  },
  
  startStreaming(filename) {
    // Use polling instead of EventSource to avoid infinite loops
    this.currentFile = filename;
    this.lastTimestamp = 0;
    this.stopPolling(); // Stop any existing polling
    
    console.log(`Starting polling for ${filename}`);
    this.updateStatus(`Loading ${filename}...`);
    
    // Initial load
    this.loadInitialLogContent(filename, false);
    
    // Set up periodic polling if auto-refresh is enabled
    this.startPolling();
  },
  
  async pollForUpdates() {
    if (!this.currentFile) return;
    
    try {
      const url = `/plugins/log_viewer/logs/content/${encodeURIComponent(this.currentFile)}?lines=${this.lineLimit}&since_timestamp=${this.lastTimestamp}`;
      console.log(`Polling for updates: ${url}`);
      
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      
      const data = await response.json();
      console.log('Poll response:', data);
      console.log(`Poll found ${data.lines?.length || 0} new lines since timestamp ${this.lastTimestamp}`);
      
      if (data.error) {
        this.showError(data.error);
        return;
      }
      
      // Display lines
      if (data.lines && data.lines.length > 0) {
        data.lines.forEach(line => {
          this.appendLogLine(line);
        });
        
        // Update last timestamp
        if (data.current_timestamp) {
          this.lastTimestamp = data.current_timestamp;
        }
        
        this.updateStatus(`Connected to ${this.currentFile} (${data.total_lines} lines)`);
      } else if (this.lastTimestamp === 0) {
        // First load with no data
        this.updateStatus(`${this.currentFile} is empty or no new data`);
      }
      
    } catch (error) {
      console.error('Failed to poll for updates:', error);
      this.showError(`Failed to load log data: ${error.message}`);
    }
  },
  
  stopPolling() {
    if (this.pollingInterval) {
      clearInterval(this.pollingInterval);
      this.pollingInterval = null;
    }
  },
  
  appendLogLine(data) {
    const logContainer = document.querySelector('#logContainer');
    if (!logContainer) return;
    
    const logLine = this.createLogLineElement(data);
    logLine.classList.add('live'); // Mark as live content
    
    logContainer.appendChild(logLine);
    
    // Apply filters to new line
    this.applyFilters();
    
    // Auto-scroll if enabled
    if (this.autoScroll) {
      logContainer.scrollTop = logContainer.scrollHeight;
    }
    
    // Limit number of lines to prevent memory issues
    const maxLines = 1000;
    while (logContainer.children.length > maxLines) {
      logContainer.removeChild(logContainer.firstChild);
    }
  },
  
  escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  },
  
  highlightLogMessage(message) {
    // Escape HTML first to prevent XSS and HTML corruption
    message = this.escapeHtml(message);
    
    // Simple check - if the message already contains style attributes, skip highlighting
    // to avoid corrupting existing HTML
    if (message.includes('style=') || message.includes('color:')) {
      return message;
    }
    
    // Apply syntax highlighting safely on escaped content
    
    // Highlight file paths  
    message = message.replace(/([a-zA-Z]:\\[^\s]*|\/[^\s]*\.[a-zA-Z]+)/g, 
      '<span style="color: #79c0ff; font-weight: 500;">$1</span>');
    
    // Highlight URLs
    message = message.replace(/(https?:\/\/[^\s]+)/g, 
      '<span style="color: #a5a5a5; text-decoration: underline;">$1</span>');
    
    // Highlight numbers
    message = message.replace(/\b(\d+)\b/g, 
      '<span style="color: #79c0ff;">$1</span>');
    
    // Highlight quoted strings - using escaped quotes
    message = message.replace(/&apos;([^&]+?)&apos;/g, 
      '<span style="color: #a5f3fc;">&apos;$1&apos;</span>');
    message = message.replace(/&quot;([^&]+?)&quot;/g, 
      '<span style="color: #a5f3fc;">&quot;$1&quot;</span>');
    
    // Highlight status codes
    message = message.replace(/\b(200|201|204|301|302|400|401|403|404|500|502|503)\b/g, (match) => {
      const code = parseInt(match);
      let color = '#79c0ff'; // default blue
      if (code >= 200 && code < 300) color = '#56d364'; // success green
      else if (code >= 300 && code < 400) color = '#ffa657'; // redirect orange
      else if (code >= 400 && code < 500) color = '#ff7b72'; // client error red
      else if (code >= 500) color = '#da3633'; // server error dark red
      return `<span style="color: ${color}; font-weight: 600;">${match}</span>`;
    });
    
    // Highlight UUIDs and hashes
    message = message.replace(/\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/gi,
      '<span style="color: #d2a8ff; font-family: monospace;">$1</span>');
    
    return message;
  },
  
  toggleAutoScroll() {
    this.autoScroll = !this.autoScroll;
    this.updateAutoScrollUI();
  },
  
  updateAutoScrollUI() {
    const autoScrollBtn = document.querySelector('#autoScrollBtn');
    if (autoScrollBtn) {
      autoScrollBtn.classList.toggle('active', this.autoScroll);
    }
  },
  
  toggleAutoRefresh() {
    this.autoRefresh = !this.autoRefresh;
    const autoRefreshBtn = document.querySelector('#autoRefreshBtn');
    if (autoRefreshBtn) {
      autoRefreshBtn.title = this.autoRefresh ? 'Auto-refresh: ON' : 'Auto-refresh: OFF';
      autoRefreshBtn.classList.toggle('active', this.autoRefresh);
    }
    
    if (!this.autoRefresh) {
      this.stopPolling();
    } else if (this.currentLogFile) {
      this.startPolling();
    }
  },
  
  updateLevelFilters() {
    // This method is no longer needed since we manage filters directly in dropdown handlers
    // But keeping it for backward compatibility
  },

  syncDropdownState() {
    const dropdownMenu = document.querySelector('#filterDropdownMenu');
    if (!dropdownMenu) return;

    // Sync checkbox states with levelFilters
    Object.keys(this.levelFilters).forEach(level => {
      const checkbox = dropdownMenu.querySelector(`input[value="${level}"]`);
      if (checkbox) {
        checkbox.checked = this.levelFilters[level];
      }
    });
  },

  updateDropdownLabel() {
    const dropdownBtn = document.querySelector('#filterDropdownBtn');
    if (!dropdownBtn) return;

    const activeFilters = Object.entries(this.levelFilters)
      .filter(([level, active]) => active)
      .map(([level, active]) => level.toUpperCase());

    const labelText = activeFilters.length === 4 ? 'Levels: All' : 
                     activeFilters.length === 0 ? 'Levels: None' : 
                     `Levels: ${activeFilters.join(', ')}`;

    const filterLabel = document.querySelector('#filterLabel');
    if (filterLabel) {
      filterLabel.textContent = labelText;
    }
  },
  
  applyFilters() {
    const logContainer = document.querySelector('#logContainer');
    if (!logContainer) return;
    
    const logLines = logContainer.querySelectorAll('.log-line');
    
    logLines.forEach(line => {
      const levelSpan = line.querySelector('.log-level');
      const messageSpan = line.querySelector('.log-message');
      
      let level = 'info';
      if (levelSpan) {
        level = levelSpan.textContent.toLowerCase();
        if (level === 'warn') level = 'warning';
      }
      
      // Check level filter
      const levelVisible = this.levelFilters[level] ?? true;
      
      // Check search filter
      let searchVisible = true;
      if (this.searchTerm) {
        const lineText = line.textContent.toLowerCase();
        searchVisible = lineText.includes(this.searchTerm);
      }
      
      // Show/hide line based on filters
      const shouldShow = levelVisible && searchVisible;
      line.style.display = shouldShow ? '' : 'none';
    });
    
    // Update status with filter count
    const visibleCount = Array.from(logLines).filter(line => line.style.display !== 'none').length;
    this.updateStatus(`Showing ${visibleCount} of ${logLines.length} log entries`);
  },
  
  startPolling() {
    if (!this.autoRefresh || !this.currentFile) return;
    
    this.pollingInterval = setInterval(() => {
      this.pollForUpdates();
    }, 3000);
  },
  
  setupResizeHandler() {
    const resizeHandle = document.querySelector('#resizeHandle');
    const logContainer = document.querySelector('.log-viewer-container');
    
    if (!resizeHandle || !logContainer) return;
    
    let isResizing = false;
    let startY = 0;
    let startHeight = 0;
    
    resizeHandle.addEventListener('mousedown', (e) => {
      isResizing = true;
      startY = e.clientY;
      startHeight = logContainer.offsetHeight;
      document.body.style.cursor = 'ns-resize';
      document.addEventListener('mousemove', handleResize);
      document.addEventListener('mouseup', stopResize);
      e.preventDefault();
    });
    
    const handleResize = (e) => {
      if (!isResizing) return;
      
      const deltaY = e.clientY - startY;
      const newHeight = Math.max(200, startHeight + deltaY);
      logContainer.style.height = `${newHeight}px`;
    };
    
    const stopResize = () => {
      isResizing = false;
      document.body.style.cursor = '';
      document.removeEventListener('mousemove', handleResize);
      document.removeEventListener('mouseup', stopResize);
    };
  },
  
  replaceLogContent(newLines) {
    const logContainer = document.querySelector('#logContainer');
    if (!logContainer || !newLines) return;
    
    // Store current scroll position
    const wasScrolledToBottom = logContainer.scrollHeight - logContainer.scrollTop <= logContainer.clientHeight + 100;
    
    // Create document fragment for efficient DOM manipulation
    const fragment = document.createDocumentFragment();
    
    // Add all new lines to fragment (line numbers come from backend)
    newLines.forEach(line => {
      const logLine = this.createLogLineElement(line);
      fragment.appendChild(logLine);
    });
    
    // Replace content atomically to prevent flickering
    logContainer.innerHTML = '';
    logContainer.appendChild(fragment);
    
    // Apply filters to all content
    this.applyFilters();
    
    // Restore scroll position
    if (wasScrolledToBottom && this.autoScroll) {
      logContainer.scrollTop = logContainer.scrollHeight;
    }
  },
  
  createLogLineElement(data, lineNumber = null) {
    const logLine = document.createElement('div');
    
    // Parse log level from the message if not provided
    let level = data.level;
    let message = data.message || data.line || '';
    
    // Extract level from common log formats
    if (!level) {
      const levelMatch = message.match(/\b(ERROR|WARN|WARNING|INFO|DEBUG)\b/i);
      if (levelMatch) {
        level = levelMatch[1].toUpperCase();
      }
    }
    
    // Determine line class based on level
    const levelClass = level ? level.toLowerCase() : 'info';
    logLine.className = `log-line ${levelClass}`;
    
    // Parse timestamp - try multiple formats
    let timestamp = data.timestamp;
    if (!timestamp) {
      const timeMatch = message.match(/^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,.]?\d*)/);
      if (timeMatch) {
        timestamp = timeMatch[1];
      }
    }
    
    // Format timestamp for display
    let timeDisplay = '';
    if (timestamp) {
      try {
        const date = new Date(timestamp.replace(',', '.'));
        timeDisplay = date.toLocaleTimeString('en-US', { 
          hour12: false, 
          hour: '2-digit', 
          minute: '2-digit', 
          second: '2-digit' 
        });
      } catch (e) {
        timeDisplay = timestamp.substring(11, 19) || new Date().toLocaleTimeString();
      }
    } else {
      timeDisplay = new Date().toLocaleTimeString();
    }
    
    // Use the message as-is since server parsing should have handled extraction properly
    let cleanMessage = message;
    
    // Only remove timestamp if it somehow got included (fallback)
    cleanMessage = cleanMessage.replace(/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,.]?\d*\s*/, '');
    
    // Extract logger name and actual message for separate styling
    let loggerName = '';
    let actualMessage = cleanMessage;
    
    // Try to extract logger name from the message (format: "logger.name Actual message")
    const loggerMatch = cleanMessage.match(/^([^\s]+(?:\.[^\s]+)*)\s+(.*)$/);
    if (loggerMatch) {
      loggerName = loggerMatch[1];
      actualMessage = loggerMatch[2];
    }
    
    // Highlight important parts of the actual message
    const highlightedMessage = this.highlightLogMessage(actualMessage);
    
    // Use provided line number from backend data or fallback
    const displayLineNumber = data.line_number || lineNumber || '?';
    
    // Add tooltip support for multiline entries
    const hasMultiline = data.has_multiline || false;
    const fullContent = data.full_content || message;
    const multilineIndicator = hasMultiline ? ' <span class="multiline-indicator">📋</span>' : '';
    
    console.log('Creating log line:', data.line_number, 'hasMultiline:', hasMultiline, 'data:', data);
    
    logLine.innerHTML = `
      <span class="log-line-number">${displayLineNumber}</span>
      <span class="log-timestamp">${timeDisplay}</span>
      <span class="log-level ${level || 'INFO'}">${level || 'INFO'}</span>
      ${loggerName ? `<span class="log-logger">${loggerName}</span>` : ''}
      <span class="log-message" ${hasMultiline ? 'data-multiline="true"' : ''}>${highlightedMessage}${multilineIndicator}</span>
    `;
    
    // Add custom tooltip for multiline entries
    if (hasMultiline) {
      const messageSpan = logLine.querySelector('.log-message');
      console.log('Adding tooltip for multiline entry:', data.line_number, 'messageSpan:', messageSpan);
      if (messageSpan) {
        this.addMultilineTooltip(messageSpan, fullContent);
      } else {
        console.error('Could not find .log-message element in logLine');
      }
    }
    
    return logLine;
  },
  
  async refreshLogs() {
    if (this.currentLogFile) {
      // Smart refresh - get fresh data without clearing screen
      this.updateStatus('Refreshing...');
      
      // Stop polling during refresh to avoid conflicts
      this.stopPolling();
      
      // Reset timestamp to get fresh content
      this.lastTimestamp = 0;
      
      // Get latest content
      await this.loadInitialLogContent(this.currentLogFile, true);
      
      // Restart polling if auto-refresh is enabled
      this.startPolling();
    }
  },
  
  addMultilineTooltip(element, fullContent) {
    console.log('Setting up tooltip for element:', element, 'with content length:', fullContent.length);
    let tooltip = null;
    
    element.addEventListener('mouseenter', (e) => {
      console.log('Mouse enter triggered! Creating tooltip...');
      // Create tooltip
      tooltip = document.createElement('div');
      tooltip.className = 'multiline-tooltip';
      tooltip.textContent = fullContent;
      
      // Position tooltip
      const rect = element.getBoundingClientRect();
      tooltip.style.left = rect.left + 'px';
      tooltip.style.top = (rect.bottom + 5) + 'px';
      
      document.body.appendChild(tooltip);
      
      // Adjust position if it goes off screen
      const tooltipRect = tooltip.getBoundingClientRect();
      if (tooltipRect.right > window.innerWidth - 10) {
        tooltip.style.left = (window.innerWidth - tooltipRect.width - 10) + 'px';
      }
      if (tooltipRect.bottom > window.innerHeight - 10) {
        tooltip.style.top = (rect.top - tooltipRect.height - 5) + 'px';
      }
    });
    
    element.addEventListener('mouseleave', () => {
      if (tooltip) {
        document.body.removeChild(tooltip);
        tooltip = null;
      }
    });
  },
  
  updateStatus(message) {
    const statusElement = document.querySelector('#logStatus');
    if (statusElement) {
      statusElement.textContent = message;
    }
  },
  
  showError(message) {
    const logContainer = document.querySelector('#logContainer');
    if (logContainer) {
      const errorDiv = document.createElement('div');
      errorDiv.className = 'log-error';
      errorDiv.textContent = `Error: ${message}`;
      logContainer.appendChild(errorDiv);
    }
    this.updateStatus(message);
  },
  
  destroy() {
    // Cleanup when panel is closed
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }
};