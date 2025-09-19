// Main AgentSystem initialization - modular but without ES6 imports

document.addEventListener('DOMContentLoaded', function() {
  
  // Wait for all modules to be loaded
  if (typeof window.AgentSystem === 'undefined') {
    console.error('AgentSystem namespace not found');
    return;
  }
  
  // Check if all required modules are loaded
  const requiredModules = ['PanelManager', 'MCP', 'Status', 'Debug', 'ContextDebug'];
  const missingModules = requiredModules.filter(module => !window.AgentSystem[module]);
  
  if (missingModules.length > 0) {
    console.error('Missing modules:', missingModules);
    return;
  }
  
  // Initialize button event listeners
  const statusBtn = document.getElementById('statusToggleBtn');
  const mcpBtn = document.getElementById('mcpToggleBtn');
  const debugBtn = document.getElementById('debugToggleBtn');
  const contextDebugBtn = document.getElementById('contextDebugToggleBtn');
  
  if (statusBtn) {
    statusBtn.addEventListener('click', function() {
      window.AgentSystem.PanelManager.togglePanel('floatingStatusPanel', () => window.AgentSystem.Status.showPanel());
    });
  }
  
  if (mcpBtn) {
    mcpBtn.addEventListener('click', function() {
      window.AgentSystem.PanelManager.togglePanel('floatingMCPPanel', () => window.AgentSystem.MCP.showPanel());
    });
  }
  
  if (debugBtn) {
    debugBtn.addEventListener('click', function() {
      window.AgentSystem.PanelManager.togglePanel('floatingDebugPanel', () => window.AgentSystem.Debug.showPanel());
    });
  }
  
  if (contextDebugBtn) {
    contextDebugBtn.addEventListener('click', function() {
      window.AgentSystem.PanelManager.togglePanel('floatingContextDebugPanel', () => window.AgentSystem.ContextDebug.showPanel());
    });
  }
  
  // Initialize chat form
  // Initialize chat module (extracted)
  if (window.chatModule && typeof window.chatModule.init === 'function') {
    try {
      window.chatModule.init();
    } catch (err) {
      console.error('chatModule.init() failed', err);
    }
  } else {
    console.warn('chatModule not available; chat features disabled');
  }
  
  
});