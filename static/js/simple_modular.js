// Simple modular version - step by step build
console.log('=== SIMPLE MODULAR LOADING ===');

// First, just ensure basic functionality works
document.addEventListener('DOMContentLoaded', function() {
  console.log('DOM ready in modular version');
  
  // Basic panel creation function (inline for now)
  function createPanel(id, title, content = '') {
    console.log(`Creating panel: ${id}`);
    
    // Remove existing panel
    const existing = document.getElementById(id);
    if (existing) {
      existing.remove();
    }
    
    // Create panel
    const panel = document.createElement('div');
    panel.className = 'floating-panel';
    panel.id = id;
    panel.innerHTML = `
      <div class="panel-header">
        <h3>${title}</h3>
        <span class="close-btn" onclick="document.getElementById('${id}').remove()">×</span>
      </div>
      <div class="panel-content">
        ${content}
      </div>
    `;
    
    // Position panel
    panel.style.position = 'fixed';
    panel.style.top = '100px';
    panel.style.right = '20px';
    panel.style.width = '400px';
    panel.style.zIndex = '1000';
    
    document.body.appendChild(panel);
    console.log(`Panel ${id} created and added to DOM`);
  }
  
  // Add button event listeners
  const statusBtn = document.getElementById('statusToggleBtn');
  const mcpBtn = document.getElementById('mcpToggleBtn');
  const debugBtn = document.getElementById('debugToggleBtn');
  
  if (statusBtn) {
    statusBtn.addEventListener('click', function() {
      console.log('Status button clicked');
      createPanel('statusPanel', 'Agent Status', '<p>Status panel content here</p>');
    });
  }
  
  if (mcpBtn) {
    mcpBtn.addEventListener('click', function() {
      console.log('MCP button clicked');
      createPanel('mcpPanel', 'MCP Servers', '<p>MCP panel content here</p>');
    });
  }
  
  if (debugBtn) {
    debugBtn.addEventListener('click', function() {
      console.log('Debug button clicked');
      createPanel('debugPanel', 'Debug Console', '<p>Debug panel content here</p>');
    });
  }
  
  console.log('=== SIMPLE MODULAR COMPLETE ===');
});

console.log('Simple modular script loaded');