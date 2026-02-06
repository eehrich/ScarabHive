// Log Viewer Plugin Module
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.log_viewer = {
  eventSource: null,
  currentLogFile: null,
  autoScroll: true,
  autoRefresh: false,
  searchTerm: '',
  lineLimit: 50,  // Default line limit
  rootElement: null, // Will hold reference to shadow root or document
  levelFilters: {
    error: true,
    warning: true,
    info: true,
    debug: true
  },

  // Storage key for plugin settings
  getStorageKey() {
    return 'pluginState:log_viewer';
  },

  // Load persisted settings from localStorage (non-blocking)
  loadState() {
    try {
      const raw = localStorage.getItem(this.getStorageKey());
      if (!raw) return;
      const state = JSON.parse(raw);
      if (!state) return;

      // Apply saved values if present
      if (state.levelFilters) {
        this.levelFilters = Object.assign({}, this.levelFilters, state.levelFilters);
      }
      if (typeof state.autoScroll !== 'undefined') this.autoScroll = !!state.autoScroll;
      if (typeof state.autoRefresh !== 'undefined') this.autoRefresh = !!state.autoRefresh;
      if (typeof state.searchTerm !== 'undefined') this.searchTerm = state.searchTerm || '';
      if (typeof state.lineLimit !== 'undefined') this.lineLimit = parseInt(state.lineLimit) || this.lineLimit;

      // Remember desired file selection to attempt after file list loads
      this._desiredFileSelection = state.currentLogFile || null;
    } catch (err) {
      console.warn('Failed to load saved log viewer settings:', err);
    }
  },

  // Persist current settings to localStorage
  saveState() {
    try {
      const state = {
        levelFilters: this.levelFilters,
        autoScroll: this.autoScroll,
        autoRefresh: this.autoRefresh,
        searchTerm: this.searchTerm,
        lineLimit: this.lineLimit,
        currentLogFile: this.currentLogFile || null
      };
      localStorage.setItem(this.getStorageKey(), JSON.stringify(state));
    } catch (err) {
      console.warn('Failed to save log viewer settings:', err);
    }
  },

  _initialized: false,  // Guard against multiple init calls

  init(shadowRoot = null) {
    // Guard against multiple initialization (can happen with retry logic)
    if (this._initialized && this.rootElement === (shadowRoot || document)) {
      console.log('Log viewer already initialized, skipping');
      return;
    }
    
    // Set root element for queries (shadow root or document)
    this.rootElement = shadowRoot || document;
    this._initialized = true;

    // Load persisted settings (if any)
    this.loadState();

    // Clear any conflicting styles from previous sessions
    const logContainer = this.rootElement.querySelector('#logContainer');
    const viewerContainer = this.rootElement.querySelector('.log-viewer-container');

    if (logContainer) {
      logContainer.style.height = '';
      logContainer.style.overflow = '';  // Let CSS handle overflow
    }

    if (viewerContainer) {
      viewerContainer.style.height = '';
      viewerContainer.style.flex = '1';
      viewerContainer.style.minHeight = '0';
      viewerContainer.style.overflow = 'hidden';  // Critical for flex child height calculation
    }

    this.setupEventHandlers();
  // No internal resize handler - parent application controls outer panel size
    this.loadLogFiles();
  },

  setupEventHandlers() {
    const controls = this.rootElement.querySelector('.log-controls-fixed');
    if (!controls) return;

    // File selector
    const fileSelect = controls.querySelector('#logFileSelect');
    if (fileSelect) {
      fileSelect.addEventListener('change', (e) => {
        this.selectLogFile(e.target.value);
        this.saveState();
      });
    }

    // Auto-refresh toggle
    const autoRefreshBtn = controls.querySelector('#autoRefreshBtn');
    if (autoRefreshBtn) {
      autoRefreshBtn.addEventListener('click', () => {
        this.toggleAutoRefresh();
        this.saveState();
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
        this.saveState();
      });
      // Set initial state
      autoScrollBtn.classList.toggle('active', this.autoScroll);
    }

    // Search input - reload logs on change
    const searchInput = controls.querySelector('#searchInput');
    if (searchInput) {
      // Use debounce to avoid too many requests
      let searchTimeout;
      searchInput.addEventListener('input', (e) => {
        this.searchTerm = e.target.value.toLowerCase();
        this.saveState();
        
        // Debounce search - reload after 500ms of no typing
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => {
          if (this.currentFile) {
            this.loadInitialLogContent(this.currentFile, true);
          }
        }, 500);
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
        this.saveState();
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
      (this.rootElement === document ? document : this.rootElement.host).addEventListener('click', () => {
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
          // Reload logs with new filter
          if (this.currentFile) {
            this.loadInitialLogContent(this.currentFile, true);
          }
          this.saveState();
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
          // Reload logs with new filter
          if (this.currentFile) {
            this.loadInitialLogContent(this.currentFile, true);
          }
          this.saveState();
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
          // Reload logs with new filter
          if (this.currentFile) {
            this.loadInitialLogContent(this.currentFile, true);
          }
          this.saveState();
        });
      }

      // Initialize dropdown label only
      this.updateDropdownLabel();
    }

    // Refresh button
    const refreshBtn = controls.querySelector('#refreshBtn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        this.refreshLogs();
      });
    }

    // After wiring handlers, apply any persisted UI state
    this.applyStateToUI();
  },

  // Reflect persisted state into UI controls
  applyStateToUI() {
    const root = this.rootElement;
    try {
      const fileSelect = root.querySelector('#logFileSelect');
      const autoRefreshBtn = root.querySelector('#autoRefreshBtn');
      const autoScrollBtn = root.querySelector('#autoScrollBtn');
      const searchInput = root.querySelector('#searchInput');
      const lineLimitSelect = root.querySelector('#lineLimitSelect');
      const filterDropdownMenu = root.querySelector('#filterDropdownMenu');

      if (autoRefreshBtn) autoRefreshBtn.classList.toggle('active', this.autoRefresh);
      if (autoRefreshBtn) autoRefreshBtn.title = this.autoRefresh ? 'Auto-refresh: ON' : 'Auto-refresh: OFF';
      if (autoScrollBtn) autoScrollBtn.classList.toggle('active', this.autoScroll);
      if (searchInput) searchInput.value = this.searchTerm || '';
      if (lineLimitSelect) lineLimitSelect.value = String(this.lineLimit || 50);

      // Apply level filters to checkboxes if dropdown exists
      if (filterDropdownMenu) {
        Object.keys(this.levelFilters).forEach(level => {
          const cb = filterDropdownMenu.querySelector(`input[value="${level}"]`);
          if (cb) cb.checked = !!this.levelFilters[level];
        });
      }

      // Note: File selection is handled in populateFileSelect() after file list loads
      // Don't trigger selectLogFile here to avoid double-loading
    } catch (err) {
      console.warn('Failed to apply UI state:', err);
    }
  },

  async loadLogFiles() {
    try {
      const response = await fetch('/plugins/log_viewer/logs/list');

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const data = await response.json();

      const fileSelected = this.populateFileSelect(data.logs || []);

      // Auto-select first existing file ONLY if populateFileSelect didn't already select one
      if (!fileSelected) {
        const existingFiles = (data.logs || []).filter(file => file.exists);
        if (existingFiles.length > 0) {
          this.selectLogFile(existingFiles[0].name);
        }
      }

    } catch (error) {
      console.error('Error loading log files:', error);
      this.showError(`Failed to load log files: ${error.message}`);
    }
  },

  populateFileSelect(files) {
    const fileSelect = this.rootElement.querySelector('#logFileSelect');
    if (!fileSelect) {
      console.error('Log file select element not found');
      return false;
    }

    fileSelect.innerHTML = '<option value="">Select a log file...</option>';

    // Only show files that exist
    const existingFiles = files.filter(file => file.exists);

    existingFiles.forEach(file => {
      const option = document.createElement('option');
      option.value = file.name;
      option.textContent = `${file.name} (${this.formatFileSize(file.size)})`;
      fileSelect.appendChild(option);
    });

    // If user had a saved file selection, try to select it now
    if (this._desiredFileSelection) {
      const opt = Array.from(fileSelect.options).find(o => o.value === this._desiredFileSelection);
      if (opt) {
        fileSelect.value = opt.value;
        // select without saving again (already saved)
        this.selectLogFile(opt.value);
        // Clear desired selection and signal that we selected a file
        this._desiredFileSelection = null;
        return true; // Signal: file was selected
      }
      // Clear even if file not found
      this._desiredFileSelection = null;
    }

    // Ensure other UI state is applied (checkboxes, toggles, search, limits)
    this.applyStateToUI();
    return false; // Signal: no file was selected
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
    this.updateStatus(`Connecting...`);

    // Stop any existing polling
    this.stopPolling();

    // Clear logs when selecting new file
    const logContainer = this.rootElement.querySelector('#logContainer');
    if (logContainer) {
      logContainer.innerHTML = '';
    }

    // Show loading indicator
    this.showLoading('Loading log file...');

    // Start new polling
    this.startStreaming(filename);

    // Persist choice
    this.saveState();
  },

  async loadInitialLogContent(filename, isRefresh = false) {
    if (!filename) return;

    // Show loading for initial load (not refresh which is quick)
    if (!isRefresh) {
      this.showLoading('Loading log entries...');
    }

    try {
      // Always respect user's line limit selection
      const lines = this.lineLimit;
      
      // Build URL with filters
      let url = `/plugins/log_viewer/logs/content/${encodeURIComponent(filename)}?lines=${lines}`;
      
      // Add level filter if not all levels are selected
      const activeLevels = Object.keys(this.levelFilters).filter(level => this.levelFilters[level]);
      if (activeLevels.length > 0 && activeLevels.length < Object.keys(this.levelFilters).length) {
        url += `&levels=${activeLevels.join(',')}`;
      }
      
      // Add search term if present
      if (this.searchTerm) {
        url += `&search=${encodeURIComponent(this.searchTerm)}`;
      }

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

      this.updateStatus(`Updated: ${new Date().toLocaleTimeString()}`);

    } catch (error) {
      console.error('Failed to load initial log content:', error);
      this.showError(`Failed to load ${filename}: ${error.message}`);
    } finally {
      // Always hide loading indicator
      this.hideLoading();
    }
  },

  async startStreaming(filename) {
    // Use polling instead of EventSource to avoid infinite loops
    this.currentFile = filename;
    this.lastTimestamp = 0;
    this.stopPolling(); // Stop any existing polling

    // Initial load - wait for it to complete before starting polling
    // This ensures lastTimestamp is set before polling begins
    await this.loadInitialLogContent(filename, false);

    // Set up periodic polling if auto-refresh is enabled
    this.startPolling();
  },

  async pollForUpdates() {
    if (!this.currentFile) return;

    try {
      // Build URL with filters
      let url = `/plugins/log_viewer/logs/content/${encodeURIComponent(this.currentFile)}?lines=${this.lineLimit}&since_timestamp=${this.lastTimestamp}`;
      
      // Add level filter if not all levels are selected
      const activeLevels = Object.keys(this.levelFilters).filter(level => this.levelFilters[level]);
      if (activeLevels.length > 0 && activeLevels.length < Object.keys(this.levelFilters).length) {
        url += `&levels=${activeLevels.join(',')}`;
      }
      
      // Add search term if present
      if (this.searchTerm) {
        url += `&search=${encodeURIComponent(this.searchTerm)}`;
      }

      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const data = await response.json();

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
    const logContainer = this.rootElement.querySelector('#logContainer');
    if (!logContainer) return;

    const logLine = this.createLogLineElement(data);

    logContainer.appendChild(logLine);

    // Apply filters to new line
    this.applyFilters();

    // Auto-scroll if enabled
    if (this.autoScroll) {
      requestAnimationFrame(() => {
        const scrollContainer = this.rootElement.querySelector('#logContainer');
        if (scrollContainer) {
          scrollContainer.scrollTop = scrollContainer.scrollHeight;
        }
      });
    }

    // Limit number of lines to user's selection (or 1000 max for memory)
    const maxLines = Math.min(this.lineLimit, 1000);
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
    const autoScrollBtn = this.rootElement.querySelector('#autoScrollBtn');
    if (autoScrollBtn) {
      autoScrollBtn.classList.toggle('active', this.autoScroll);
    }
  },

  toggleAutoRefresh() {
    this.autoRefresh = !this.autoRefresh;
    const autoRefreshBtn = this.rootElement.querySelector('#autoRefreshBtn');
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

  // Removed obsolete updateLevelFilters and syncDropdownState methods

  updateDropdownLabel() {
    const dropdownBtn = this.rootElement.querySelector('#filterDropdownBtn');
    if (!dropdownBtn) return;

    const activeFilters = Object.entries(this.levelFilters)
      .filter(([level, active]) => active)
      .map(([level, active]) => level.toUpperCase());

    const labelText = activeFilters.length === 4 ? 'Levels: All' :
                     activeFilters.length === 0 ? 'Levels: None' :
                     `Levels: ${activeFilters.join(', ')}`;

    const filterLabel = this.rootElement.querySelector('#filterLabel');
    if (filterLabel) {
      filterLabel.textContent = labelText;
    }
  },

  applyFilters() {
    const logContainer = this.rootElement.querySelector('#logContainer');
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

    // Stop any existing polling interval before creating a new one
    if (this.pollingInterval) {
      clearInterval(this.pollingInterval);
      this.pollingInterval = null;
    }

    this.pollingInterval = setInterval(() => {
      this.pollForUpdates();
    }, 3000);
  },

  replaceLogContent(newLines) {
    const logContainer = this.rootElement.querySelector('#logContainer');
    if (!logContainer || !newLines) return;

    // Limit lines to user's selection (take last N lines to show most recent)
    const linesToShow = newLines.slice(-this.lineLimit);

    // Create document fragment for efficient DOM manipulation
    const fragment = document.createDocumentFragment();

    // Add limited lines to fragment (line numbers come from backend)
    linesToShow.forEach(line => {
      const logLine = this.createLogLineElement(line);
      fragment.appendChild(logLine);
    });

    // Replace content atomically to prevent flickering
    logContainer.innerHTML = '';
    logContainer.appendChild(fragment);

    // Apply filters to all content
    this.applyFilters();

    // Auto-scroll to bottom if enabled (after filters are applied)
    if (this.autoScroll) {
      requestAnimationFrame(() => {
        const scrollContainer = this.rootElement.querySelector('#logContainer');
        if (scrollContainer) {
          scrollContainer.scrollTop = scrollContainer.scrollHeight;
        }
      });
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
    let tooltip = null;

    // Use the correct document context for creating elements
    const ownerDocument = element.ownerDocument || document;

    element.addEventListener('mouseenter', (e) => {
      // Create tooltip in the same document context as the element
      tooltip = ownerDocument.createElement('div');
      tooltip.className = 'multiline-tooltip';
      tooltip.textContent = fullContent;

      // Position tooltip with more explicit styling for debugging
      const rect = element.getBoundingClientRect();
      tooltip.style.position = 'fixed';
      tooltip.style.left = rect.left + 'px';
      tooltip.style.top = (rect.bottom + 5) + 'px';
      tooltip.style.zIndex = '99999';

      // Add debug styling to make sure tooltip is visible
      tooltip.style.backgroundColor = '#161b22';
      tooltip.style.color = '#e6edf3';
      tooltip.style.border = '2px solid #ff6b6b'; // Red border for debugging
      tooltip.style.padding = '12px 16px';
      tooltip.style.borderRadius = '6px';
      tooltip.style.fontSize = '12px';
      tooltip.style.maxWidth = '700px';
      tooltip.style.whiteSpace = 'pre-wrap';

      // Append tooltip to appropriate container (shadow root or document body)
      let tooltipContainer;
      if (this.rootElement === document) {
        tooltipContainer = document.body;
      } else {
        // In Shadow DOM, append to the shadow root itself
        tooltipContainer = this.rootElement;
      }
      tooltipContainer.appendChild(tooltip);

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
      if (tooltip && tooltip.parentNode) {
        tooltip.parentNode.removeChild(tooltip);
        tooltip = null;
      }
    });
  },

  updateStatus(message) {
    const statusElement = this.rootElement.querySelector('#logStatus');
    if (statusElement) {
      statusElement.textContent = message;
    }
  },

  showError(message) {
    const logContainer = this.rootElement.querySelector('#logContainer');
    if (logContainer) {
      const errorDiv = document.createElement('div');
      errorDiv.className = 'log-error';
      errorDiv.textContent = `Error: ${message}`;
      logContainer.appendChild(errorDiv);
    }
    this.updateStatus(message);
  },

  showLoading(message = 'Loading...') {
    const viewerContainer = this.rootElement.querySelector('.log-viewer-container');
    if (!viewerContainer) return;

    // Remove existing loading overlay if any
    this.hideLoading();

    // Create loading overlay
    const overlay = document.createElement('div');
    overlay.className = 'log-loading-overlay';
    overlay.innerHTML = `
      <div class="log-loading-spinner"></div>
      <div class="log-loading-text">${message}</div>
    `;

    // Make container relative for absolute positioning of overlay
    viewerContainer.style.position = 'relative';
    viewerContainer.appendChild(overlay);
  },

  hideLoading() {
    const viewerContainer = this.rootElement.querySelector('.log-viewer-container');
    if (!viewerContainer) return;

    const overlay = viewerContainer.querySelector('.log-loading-overlay');
    if (overlay) {
      overlay.remove();
    }
  },

  destroy() {
    // Cleanup when panel is closed
    this.stopPolling();
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }
};