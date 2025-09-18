// Ultra-basic test - no modules, no imports
console.log('=== BASIC TEST LOADING ===');

document.addEventListener('DOMContentLoaded', function() {
  console.log('DOM ready');
  
  // Test if buttons exist
  const statusBtn = document.getElementById('statusToggleBtn');
  const mcpBtn = document.getElementById('mcpToggleBtn');
  const debugBtn = document.getElementById('debugToggleBtn');
  
  console.log('Buttons found:', {
    status: !!statusBtn,
    mcp: !!mcpBtn,
    debug: !!debugBtn
  });
  
  // Test MCP indicator
  const mcpIndicator = document.getElementById('mcpConnected');
  console.log('MCP indicator found:', !!mcpIndicator);
  if (mcpIndicator) {
    console.log('MCP indicator content:', mcpIndicator.textContent);
    mcpIndicator.style.background = 'red'; // Make it visible if it exists
  }
  
  // Add basic click handlers
  if (statusBtn) {
    statusBtn.addEventListener('click', function() {
      alert('Status button clicked!');
    });
    console.log('Status button listener added');
  }
  
  if (mcpBtn) {
    mcpBtn.addEventListener('click', function() {
      alert('MCP button clicked!');
    });
    console.log('MCP button listener added');
  }
  
  if (debugBtn) {
    debugBtn.addEventListener('click', function() {
      alert('Debug button clicked!');
    });
    console.log('Debug button listener added');
  }
  
  console.log('=== BASIC TEST COMPLETE ===');
});

console.log('Basic test script loaded');