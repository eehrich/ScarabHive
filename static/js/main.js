// Main AgentSystem initialization - modular but without ES6 imports

document.addEventListener('DOMContentLoaded', function() {
  
  // Wait for all modules to be loaded
  if (typeof window.AgentSystem === 'undefined') {
    console.error('AgentSystem namespace not found');
    return;
  }
  
  // Check if all required modules are loaded
  const requiredModules = ['PanelManager', 'PluginManager', 'MCP', 'Status', 'Selectors'];
  const missingModules = requiredModules.filter(module => !window.AgentSystem[module]);
  
  if (missingModules.length > 0) {
    console.error('Missing modules:', missingModules);
    return;
  }

  // Initialize selector module for agent and LLM profile selection
  if (window.AgentSystem.Selectors && typeof window.AgentSystem.Selectors.init === 'function') {
    try {
      window.AgentSystem.Selectors.init();
    } catch (err) {
      console.error('Selectors.init() failed', err);
    }
  } else {
    console.warn('Selectors module not available');
  }
  
  // Initialize button event listeners
  const statusBtn = document.getElementById('statusToggleBtn');
  const mcpBtn = document.getElementById('mcpToggleBtn');
  
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
  
  // Initialize plugin manager to load dynamic plugin buttons
  if (window.AgentSystem.PluginManager && typeof window.AgentSystem.PluginManager.init === 'function') {
    try {
      window.AgentSystem.PluginManager.init();
    } catch (err) {
      console.error('PluginManager.init() failed', err);
    }
  } else {
    console.warn('PluginManager not available; plugin buttons disabled');
  }
  
  // Initialize dropdown menu system (wait for auth to be ready)
  if (window.AgentSystem.DropdownMenu && typeof window.AgentSystem.DropdownMenu.init === 'function') {
    try {
      // Wait for auth.js to verify token before initializing menus
      if (window.authManager && window.authManager.token) {
        // Auth token exists, wait for verification to complete
        window.authManager.verifyToken().finally(() => {
          window.AgentSystem.DropdownMenu.init();
        });
      } else {
        // No token, init immediately
        window.AgentSystem.DropdownMenu.init();
      }
    } catch (err) {
      console.error('DropdownMenu.init() failed', err);
    }
  } else {
    console.warn('DropdownMenu not available; dropdown menus disabled');
  }
  
  // Initialize chat form
  // Initialize file upload module
  if (window.fileUploadModule && typeof window.fileUploadModule.init === 'function') {
    try {
      window.fileUploadModule.init();
    } catch (err) {
      console.error('fileUploadModule.init() failed', err);
    }
  } else {
    console.warn('fileUploadModule not available; file upload disabled');
  }
  
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