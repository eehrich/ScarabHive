/*
  index.js — Main application entry point for Agent System WebUI
  Modular version that imports and coordinates all UI components
*/

// Import all modules
import { updateStatusMetrics, addStatusEvent } from './status_functions.js';
import { updateMCPServers, toggleServerDetails, startMcpAutoRefresh, stopMcpAutoRefresh, manageMcpIndicator } from './mcp_functions.js';
import { updateDebugInfo } from './debug_functions.js';
import { 
  loadPanelState, 
  savePanelState, 
  applyPanelState, 
  makeDraggable, 
  makeResizable, 
  handleWindowResize, 
  resetPanelPositions 
} from './panel_functions.js';
import { initializeChatForm, run as chatRun } from './chat_functions.js';

// Make functions available globally for onclick handlers and debugging
window.toggleServerDetails = toggleServerDetails;
window.addStatusEvent = addStatusEvent;
window.resetPanelPositions = resetPanelPositions;

// Debug function to test buttons manually
window.testButtons = function() {
  console.log('=== TESTING BUTTONS ===');
  const statusBtn = document.getElementById('statusToggleBtn');
  const mcpBtn = document.getElementById('mcpToggleBtn');
  const debugBtn = document.getElementById('debugToggleBtn');
  
  console.log('Buttons found:', {
    status: !!statusBtn,
    mcp: !!mcpBtn,
    debug: !!debugBtn
  });
  
  console.log('Panels exist:', {
    status: !!statusPanel,
    mcp: !!mcpPanel,
    debug: !!debugPanel
  });
  
  if (statusBtn && statusPanel) {
    console.log('Manually triggering status panel...');
    statusPanel.style.display = 'block';
    statusPanel.style.left = '100px';
    statusPanel.style.top = '100px';
    console.log('Status panel should be visible now');
  }
};

// Global panel variables
let statusPanel, mcpPanel, debugPanel;

// Panel creation functions
function createStatusPanel() {
  const panel = document.createElement('div');
  panel.id = 'floatingStatusPanel';
  panel.className = 'floating-panel';
  panel.style.display = 'none';
  panel.style.top = '80px';
  panel.style.position = 'fixed';
  panel.style.right = '24px';
  panel.style.left = 'auto';
  panel.style.width = '400px';
  panel.style.height = '700px';
  panel.innerHTML = `
    <div class="floating-panel-header" id="floatingStatusHeader">
      <span>Status & Metrics</span>
      <div style="margin-left:8px;flex:1"></div>
      <button id="floatingCloseBtn" title="Close" aria-label="Close">✕</button>
    </div>
    <div class="floating-panel-body" id="floatingStatusBody">
      <div class="status-metrics" id="floatingStatusMetrics">
        <div class="metric-item"><span class="metric-label">Loading...</span><span class="metric-value">...</span></div>
      </div>
    </div>
    <div class="resize-handle"></div>
  `;
  document.body.appendChild(panel);
  return panel;
}

function createMCPPanel() {
  const panel = document.createElement('div');
  panel.id = 'floatingMCPPanel';
  panel.className = 'floating-panel';
  panel.style.display = 'none';
  panel.style.position = 'fixed';
  panel.innerHTML = `
    <div class="floating-panel-header" id="floatingMCPHeader">
      <span>MCP Servers & Tools</span>
      <div style="margin-left:8px;flex:1"></div>
      <input id="mcpFilterInput" type="text" placeholder="Filter servers/tools..." class="filter-input" title="Filter by server or tool name" />
      <button id="mcpRefreshBtn" class="icon-btn" title="Refresh now" aria-label="Refresh now">
        <svg class="mcp-refresh-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M21 12a9 9 0 10-2.6 6.1" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M21 3v6h-6" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span class="spinner icon-spinner" aria-hidden="true"></span>
      </button>
      <div id="mcpAutoRefreshToggle" class="custom-toggle" style="display: flex; align-items: center; gap: 8px; margin-right: 8px; cursor: pointer;" title="Toggle auto-refresh">
        <div class="toggle-track">
          <div class="toggle-knob"></div>
        </div>
        <span style="color: #9ab; font-size: 13px; user-select: none;">Auto-refresh</span>
      </div>
      <input id="mcpAutoRefreshInterval" class="number-input" type="number" min="5" step="5" value="30" title="Auto-refresh interval (seconds)" />
      <button id="floatingMCPCloseBtn" title="Close" aria-label="Close">✕</button>
    </div>
    <div class="floating-panel-body" id="floatingMCPBody">
      <div class="mcp-metrics" id="floatingMCPMetrics">
        <div class="metric-item"><span class="metric-label">Loading...</span><span class="metric-value">...</span></div>
      </div>
    </div>
    <div class="resize-handle"></div>
  `;
  document.body.appendChild(panel);
  return panel;
}

function createDebugPanel() {
  const panel = document.createElement('div');
  panel.id = 'floatingDebugPanel';
  panel.className = 'floating-panel';
  panel.style.display = 'none';
  panel.style.top = '80px';
  panel.style.position = 'fixed';
  panel.style.left = '24px';
  panel.style.width = '600px';
  panel.style.height = '700px';
  panel.innerHTML = `
    <div class="floating-panel-header" id="floatingDebugHeader">
      <span>Debug Messages & Context</span>
      <div style="margin-left:8px;flex:1"></div>
      <button id="debugRefreshBtn" class="icon-btn" title="Refresh debug info" aria-label="Refresh debug info">
        <svg class="mcp-refresh-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M21 12a9 9 0 10-2.6 6.1" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M21 3v6h-6" stroke="#9ab" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <button id="floatingDebugCloseBtn" title="Close" aria-label="Close">✕</button>
    </div>
    <div class="floating-panel-body" id="floatingDebugBody">
      <div class="debug-content" id="floatingDebugContent">
        <div class="debug-section">
          <h3>Context Statistics</h3>
          <div id="debugContextStats" class="debug-stats">
            <div class="metric-item"><span class="metric-label">Loading...</span><span class="metric-value">...</span></div>
          </div>
        </div>
        <div class="debug-section">
          <h3>Current Messages</h3>
          <div id="debugMessages" class="debug-messages">
            <div class="metric-item"><span class="metric-label">No messages yet</span></div>
          </div>
        </div>
      </div>
    </div>
    <div class="resize-handle"></div>
  `;
  document.body.appendChild(panel);
  return panel;
}

// Initialize all event listeners
function initializeEventListeners() {
  // Get references to button elements
  const statusToggleBtn = document.getElementById('statusToggleBtn');
  const mcpToggleBtn = document.getElementById('mcpToggleBtn');
  const debugToggleBtn = document.getElementById('debugToggleBtn');
  const floatingCloseBtn = document.getElementById('floatingCloseBtn');
  const floatingMCPCloseBtn = document.getElementById('floatingMCPCloseBtn');
  const floatingDebugCloseBtn = document.getElementById('floatingDebugCloseBtn');
  
  // Ensure panels exist before setting up event listeners
  if (!statusPanel || !mcpPanel || !debugPanel) {
    console.error('Panels not created before event listener setup');
    return;
  }

  // Status panel event listeners
  if (statusToggleBtn) {
    statusToggleBtn.addEventListener('click', (e) => {
      console.log('Status button clicked!');
      e.preventDefault();
      const shown = statusPanel.style.display !== 'none';
      console.log('Panel currently shown:', shown, 'display:', statusPanel.style.display);
      statusPanel.style.display = shown ? 'none' : 'block';
      console.log('Panel display set to:', statusPanel.style.display);
      
      if (!shown) {
        // Position the panel
        statusPanel.style.position = 'fixed';
        statusPanel.style.left = (window.innerWidth - 424) + 'px';
        statusPanel.style.right = 'auto';
        statusPanel.style.top = '80px';
        statusPanel.style.zIndex = '1000';
        console.log('Panel positioned at:', statusPanel.style.left, statusPanel.style.top);
        // Update status metrics
        updateStatusMetrics().catch(console.error);
      }
      
      statusToggleBtn.setAttribute('aria-expanded', String(!shown));
    });
    console.log('Status button listener added');
  } else {
    console.log('Status button not found');
  }

  if (floatingCloseBtn) {
    floatingCloseBtn.addEventListener('click', () => {
      statusPanel.style.display = 'none';
      if (statusToggleBtn) statusToggleBtn.setAttribute('aria-expanded', 'false');
    });
  }

  // MCP panel event listeners
  if (mcpToggleBtn) {
    mcpToggleBtn.addEventListener('click', () => {
      const shown = mcpPanel.style.display !== 'none';
      mcpPanel.style.display = shown ? 'none' : 'block';
      mcpToggleBtn.setAttribute('aria-expanded', String(!shown));
      if (!shown) {
        updateMCPServers().catch(console.error);
      }
    });
  }

  if (floatingMCPCloseBtn) {
    floatingMCPCloseBtn.addEventListener('click', () => {
      mcpPanel.style.display = 'none';
      if (mcpToggleBtn) mcpToggleBtn.setAttribute('aria-expanded', 'false');
    });
  }

  // Debug panel event listeners
  if (debugToggleBtn) {
    debugToggleBtn.addEventListener('click', () => {
      const shown = debugPanel.style.display !== 'none';
      debugPanel.style.display = shown ? 'none' : 'block';
      debugToggleBtn.setAttribute('aria-expanded', String(!shown));
      if (!shown) {
        updateDebugInfo().catch(() => {});
        try {
          if (window._debugPollInterval) clearInterval(window._debugPollInterval);
        } catch (e) {}
        window._debugPollInterval = setInterval(() => updateDebugInfo().catch(() => {}), 2000);
      } else {
        try {
          if (window._debugPollInterval) {
            clearInterval(window._debugPollInterval);
            window._debugPollInterval = null;
          }
        } catch (e) {}
      }
    });
  }

  if (floatingDebugCloseBtn) {
    floatingDebugCloseBtn.addEventListener('click', () => {
      debugPanel.style.display = 'none';
      if (debugToggleBtn) debugToggleBtn.setAttribute('aria-expanded', 'false');
      try {
        if (window._debugPollInterval) {
          clearInterval(window._debugPollInterval);
          window._debugPollInterval = null;
        }
      } catch (e) {}
    });
  }

  if (debugRefreshBtn) {
    debugRefreshBtn.addEventListener('click', () => {
      updateDebugInfo().catch(() => {});
    });
  }

  // MCP refresh and filter handlers
  if (mcpRefreshBtn) {
    mcpRefreshBtn.addEventListener('click', async (ev) => {
      try {
        mcpRefreshBtn.classList.add('loading');
        mcpRefreshBtn.setAttribute('aria-busy', 'true');
        mcpRefreshBtn.disabled = true;
        await updateMCPServers();
      } catch (e) {
        console.error('Manual MCP refresh failed', e);
      } finally {
        mcpRefreshBtn.classList.remove('loading');
        mcpRefreshBtn.removeAttribute('aria-busy');
        mcpRefreshBtn.disabled = false;
      }
    });
  }

  if (mcpFilterInput) {
    let filterTimeout;
    mcpFilterInput.addEventListener('input', () => {
      clearTimeout(filterTimeout);
      filterTimeout = setTimeout(() => {
        updateMCPServers().catch(() => {});
      }, 300);
    });
  }

  // Initialize auto-refresh functionality
  initializeAutoRefresh(autoToggle, autoIntervalInput);
}

// Initialize auto-refresh functionality
function initializeAutoRefresh(autoToggle, autoIntervalInput) {
  let autoRefreshEnabled = false;
  
  try {
    const pref = loadPanelState('floatingMCPPanel') || {};
    if (pref.autoRefresh) {
      autoRefreshEnabled = true;
      if (autoToggle) {
        const track = autoToggle.querySelector('.toggle-track');
        if (track) track.classList.add('active');
      }
      const interval = pref.autoRefreshInterval || 30;
      if (autoIntervalInput) autoIntervalInput.value = interval;
      startMcpAutoRefresh(interval);
    }
  } catch (e) {}

  if (autoToggle) {
    autoToggle.addEventListener('click', (ev) => {
      ev.stopPropagation();
      autoRefreshEnabled = !autoRefreshEnabled;
      const track = autoToggle.querySelector('.toggle-track');
      if (track) track.classList.toggle('active', autoRefreshEnabled);

      const interval = parseInt(autoIntervalInput.value || '30', 10);
      const state = loadPanelState('floatingMCPPanel') || {};
      state.autoRefresh = autoRefreshEnabled;
      state.autoRefreshInterval = interval;
      savePanelState('floatingMCPPanel', state);
      
      if (autoRefreshEnabled) {
        startMcpAutoRefresh(interval);
      } else {
        stopMcpAutoRefresh();
      }
    });
  }

  if (autoIntervalInput) {
    autoIntervalInput.addEventListener('change', () => {
      const interval = parseInt(autoIntervalInput.value || '30', 10);
      const state = loadPanelState('floatingMCPPanel') || {};
      state.autoRefreshInterval = interval;
      savePanelState('floatingMCPPanel', state);
      if (autoRefreshEnabled) startMcpAutoRefresh(interval);
    });
  }
}

// Initialize panel positioning and drag/resize functionality
function initializePanels() {
  // Restore panel states
  const savedMcp = loadPanelState('floatingMCPPanel');
  if (savedMcp) applyPanelState(mcpPanel, savedMcp);

  const savedStatus = loadPanelState('floatingStatusPanel');
  if (savedStatus) {
    applyPanelState(statusPanel, savedStatus);
  } else {
    statusPanel.style.left = (window.innerWidth - 424) + 'px';
    statusPanel.style.right = 'auto';
    statusPanel.style.top = '80px';
    statusPanel.style.width = '400px';
    statusPanel.style.height = '500px';
  }

  const savedDebug = loadPanelState('floatingDebugPanel');
  if (savedDebug) applyPanelState(debugPanel, savedDebug);

  // Apply dragging to all panels
  makeDraggable('floatingStatusHeader', statusPanel);
  makeDraggable('floatingMCPHeader', mcpPanel);
  makeDraggable('floatingDebugHeader', debugPanel);

  // Apply resizing to all panels
  makeResizable(statusPanel);
  makeResizable(mcpPanel);
  makeResizable(debugPanel);

  // Add window resize listener
  window.addEventListener('resize', () => handleWindowResize([statusPanel, mcpPanel, debugPanel]));

  // Make resetPanelPositions available globally for debugging
  window.resetPanelPositions = () => resetPanelPositions(statusPanel, mcpPanel, debugPanel);
}

// Main initialization function
function initialize() {
  // Create floating panels first
  statusPanel = createStatusPanel();
  mcpPanel = createMCPPanel();
  debugPanel = createDebugPanel();
  
  // Initialize chat functionality
  initializeChatForm();

  // Initialize event listeners
  initializeEventListeners();

  // Initialize MCP indicator management
  manageMcpIndicator();

  // Initial data load
  updateStatusMetrics();
  updateMCPServers();
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', initialize);

// Also call immediately in case DOMContentLoaded already fired
if (document.readyState === 'loading') {
  // Still loading, wait for DOMContentLoaded
} else {
  // Already loaded
  initialize();
}