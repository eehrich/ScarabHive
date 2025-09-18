/*
  index_fixed.js — Fixed modular version using dynamic imports to catch errors
*/

console.log('Loading fixed modular index...');

// Global panel variables
let statusPanel, mcpPanel, debugPanel;

// Panel creation functions (self-contained)
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

// Dynamic import with error handling
async function loadModules() {
  try {
    console.log('Loading modules...');
    
    // Load all modules dynamically
    const [
      statusModule,
      mcpModule, 
      debugModule,
      chatModule
    ] = await Promise.all([
      import('./status_functions.js'),
      import('./mcp_functions.js'),
      import('./debug_functions.js'), 
      import('./chat_functions.js')
    ]);
    
    console.log('All modules loaded successfully');
    return {
      updateStatusMetrics: statusModule.updateStatusMetrics,
      updateMCPServers: mcpModule.updateMCPServers,
      manageMcpIndicator: mcpModule.manageMcpIndicator,
      updateDebugInfo: debugModule.updateDebugInfo,
      initializeChatForm: chatModule.initializeChatForm
    };
    
  } catch (error) {
    console.error('Module loading failed:', error);
    return null;
  }
}

// Initialize with loaded modules
async function initialize() {
  console.log('Starting initialization...');
  
  // Create panels first
  statusPanel = createStatusPanel();
  mcpPanel = createMCPPanel();
  debugPanel = createDebugPanel();
  
  console.log('Panels created');
  
  // Load modules
  const modules = await loadModules();
  
  if (!modules) {
    console.error('Failed to load modules, using basic functionality only');
  }
  
  // Setup event listeners
  setupEventListeners(modules);
  
  // Initialize modules if loaded
  if (modules) {
    try {
      modules.initializeChatForm();
      modules.manageMcpIndicator();
      modules.updateStatusMetrics();
      modules.updateMCPServers();
    } catch (error) {
      console.error('Module initialization failed:', error);
    }
  }
  
  console.log('Initialization complete');
}

function setupEventListeners(modules) {
  // Get buttons
  const statusToggleBtn = document.getElementById('statusToggleBtn');
  const mcpToggleBtn = document.getElementById('mcpToggleBtn');
  const debugToggleBtn = document.getElementById('debugToggleBtn');
  
  console.log('Setting up event listeners for buttons:', {
    status: !!statusToggleBtn,
    mcp: !!mcpToggleBtn, 
    debug: !!debugToggleBtn
  });
  
  // Status panel toggle
  if (statusToggleBtn) {
    statusToggleBtn.addEventListener('click', () => {
      console.log('Status button clicked');
      const shown = statusPanel.style.display !== 'none';
      statusPanel.style.display = shown ? 'none' : 'block';
      
      if (!shown) {
        statusPanel.style.position = 'fixed';
        statusPanel.style.right = '24px';
        statusPanel.style.top = '80px';
        statusPanel.style.zIndex = '1000';
        
        if (modules && modules.updateStatusMetrics) {
          modules.updateStatusMetrics();
        }
      }
      
      statusToggleBtn.setAttribute('aria-expanded', String(!shown));
    });
  }
  
  // MCP panel toggle
  if (mcpToggleBtn) {
    mcpToggleBtn.addEventListener('click', () => {
      console.log('MCP button clicked');
      const shown = mcpPanel.style.display !== 'none';
      mcpPanel.style.display = shown ? 'none' : 'block';
      
      if (!shown) {
        mcpPanel.style.position = 'fixed';
        mcpPanel.style.left = '24px';
        mcpPanel.style.top = '80px';
        mcpPanel.style.zIndex = '1000';
        
        if (modules && modules.updateMCPServers) {
          modules.updateMCPServers();
        }
      }
      
      mcpToggleBtn.setAttribute('aria-expanded', String(!shown));
    });
  }
  
  // Debug panel toggle
  if (debugToggleBtn) {
    debugToggleBtn.addEventListener('click', () => {
      console.log('Debug button clicked');
      const shown = debugPanel.style.display !== 'none';
      debugPanel.style.display = shown ? 'none' : 'block';
      
      if (!shown) {
        debugPanel.style.position = 'fixed';
        debugPanel.style.left = '24px';
        debugPanel.style.top = '80px';
        debugPanel.style.zIndex = '1000';
        
        if (modules && modules.updateDebugInfo) {
          modules.updateDebugInfo();
        }
      }
      
      debugToggleBtn.setAttribute('aria-expanded', String(!shown));
    });
  }
  
  // Close button listeners
  document.getElementById('floatingCloseBtn')?.addEventListener('click', () => {
    statusPanel.style.display = 'none';
    statusToggleBtn?.setAttribute('aria-expanded', 'false');
  });
  
  document.getElementById('floatingMCPCloseBtn')?.addEventListener('click', () => {
    mcpPanel.style.display = 'none';
    mcpToggleBtn?.setAttribute('aria-expanded', 'false');
  });
  
  document.getElementById('floatingDebugCloseBtn')?.addEventListener('click', () => {
    debugPanel.style.display = 'none';
    debugToggleBtn?.setAttribute('aria-expanded', 'false');
  });
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', initialize);

// Also call immediately if DOM already loaded
if (document.readyState !== 'loading') {
  initialize();
}

console.log('Fixed modular index loaded');