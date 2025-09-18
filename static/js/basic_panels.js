// Self-contained panel toggle functionality - no imports
console.log('Self-contained panel script loading...');

function createBasicStatusPanel() {
  const panel = document.createElement('div');
  panel.id = 'basicStatusPanel';
  panel.style.cssText = `
    display: none;
    position: fixed;
    top: 80px;
    right: 24px;
    width: 400px;
    height: 500px;
    background: #1a1a1a;
    border: 1px solid #333;
    border-radius: 8px;
    z-index: 1000;
    color: white;
    font-family: Arial, sans-serif;
  `;
  panel.innerHTML = `
    <div style="padding: 12px; background: #2a2a2a; border-radius: 8px 8px 0 0; display: flex; justify-content: space-between;">
      <span>Status Panel</span>
      <button onclick="this.parentElement.parentElement.style.display='none'" style="background: none; border: none; color: white; cursor: pointer;">✕</button>
    </div>
    <div style="padding: 16px;">
      <p>Status panel is working!</p>
      <p>Time: ${new Date().toLocaleTimeString()}</p>
    </div>
  `;
  document.body.appendChild(panel);
  return panel;
}

function createBasicMCPPanel() {
  const panel = document.createElement('div');
  panel.id = 'basicMCPPanel';
  panel.style.cssText = `
    display: none;
    position: fixed;
    top: 80px;
    left: 24px;
    width: 500px;
    height: 600px;
    background: #1a1a1a;
    border: 1px solid #333;
    border-radius: 8px;
    z-index: 1000;
    color: white;
    font-family: Arial, sans-serif;
  `;
  panel.innerHTML = `
    <div style="padding: 12px; background: #2a2a2a; border-radius: 8px 8px 0 0; display: flex; justify-content: space-between;">
      <span>MCP Panel</span>
      <button onclick="this.parentElement.parentElement.style.display='none'" style="background: none; border: none; color: white; cursor: pointer;">✕</button>
    </div>
    <div style="padding: 16px;">
      <p>MCP panel is working!</p>
      <p>Server count: 14</p>
    </div>
  `;
  document.body.appendChild(panel);
  return panel;
}

function createBasicDebugPanel() {
  const panel = document.createElement('div');
  panel.id = 'basicDebugPanel';
  panel.style.cssText = `
    display: none;
    position: fixed;
    top: 120px;
    right: 450px;
    width: 600px;
    height: 500px;
    background: #1a1a1a;
    border: 1px solid #333;
    border-radius: 8px;
    z-index: 1000;
    color: white;
    font-family: Arial, sans-serif;
  `;
  panel.innerHTML = `
    <div style="padding: 12px; background: #2a2a2a; border-radius: 8px 8px 0 0; display: flex; justify-content: space-between;">
      <span>Debug Panel</span>
      <button onclick="this.parentElement.parentElement.style.display='none'" style="background: none; border: none; color: white; cursor: pointer;">✕</button>
    </div>
    <div style="padding: 16px;">
      <p>Debug panel is working!</p>
      <p>Debug messages would go here</p>
    </div>
  `;
  document.body.appendChild(panel);
  return panel;
}

function setupBasicPanels() {
  console.log('Setting up basic panels...');
  
  // Create panels
  const statusPanel = createBasicStatusPanel();
  const mcpPanel = createBasicMCPPanel();
  const debugPanel = createBasicDebugPanel();
  
  // Get buttons
  const statusBtn = document.getElementById('statusToggleBtn');
  const mcpBtn = document.getElementById('mcpToggleBtn');
  const debugBtn = document.getElementById('debugToggleBtn');
  
  console.log('Buttons found:', {
    status: !!statusBtn,
    mcp: !!mcpBtn,
    debug: !!debugBtn
  });
  
  // Add event listeners
  if (statusBtn) {
    statusBtn.addEventListener('click', () => {
      console.log('Status button clicked');
      const isVisible = statusPanel.style.display !== 'none';
      statusPanel.style.display = isVisible ? 'none' : 'block';
    });
    console.log('Status button listener added');
  }
  
  if (mcpBtn) {
    mcpBtn.addEventListener('click', () => {
      console.log('MCP button clicked');
      const isVisible = mcpPanel.style.display !== 'none';
      mcpPanel.style.display = isVisible ? 'none' : 'block';
    });
    console.log('MCP button listener added');
  }
  
  if (debugBtn) {
    debugBtn.addEventListener('click', () => {
      console.log('Debug button clicked');
      const isVisible = debugPanel.style.display !== 'none';
      debugPanel.style.display = isVisible ? 'none' : 'block';
    });
    console.log('Debug button listener added');
  }
  
  console.log('Basic panel setup complete');
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', setupBasicPanels);

// Also try immediately if DOM is already loaded
if (document.readyState !== 'loading') {
  setupBasicPanels();
}

console.log('Self-contained panel script loaded');