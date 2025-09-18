// Diagnostic version - test imports one by one
console.log('=== DIAGNOSTIC LOADING ===');

// Test basic imports first
try {
  console.log('Testing updateStatusMetrics import...');
  const { updateStatusMetrics } = await import('./status_functions.js');
  console.log('✓ status_functions.js imported successfully');
  
  console.log('Testing MCP functions import...');
  const { updateMCPServers, manageMcpIndicator } = await import('./mcp_functions.js');
  console.log('✓ mcp_functions.js imported successfully');
  
  console.log('Testing debug functions import...');
  const { updateDebugInfo } = await import('./debug_functions.js');
  console.log('✓ debug_functions.js imported successfully');
  
  console.log('Testing panel functions import...');
  const { loadPanelState, makeDraggable } = await import('./panel_functions.js');
  console.log('✓ panel_functions.js imported successfully');
  
  console.log('Testing chat functions import...');
  const { initializeChatForm } = await import('./chat_functions.js');
  console.log('✓ chat_functions.js imported successfully');
  
  console.log('All imports successful! The issue is elsewhere.');
  
  // Now test a basic button click
  document.addEventListener('DOMContentLoaded', () => {
    console.log('DOM ready, testing basic functionality...');
    const mcpBtn = document.getElementById('mcpToggleBtn');
    console.log('MCP button found:', !!mcpBtn);
    
    if (mcpBtn) {
      mcpBtn.addEventListener('click', () => {
        alert('MCP button works in diagnostic mode!');
      });
      console.log('Basic click listener added to MCP button');
    }
    
    // Test manageMcpIndicator
    try {
      manageMcpIndicator();
      console.log('manageMcpIndicator called successfully');
    } catch (e) {
      console.error('manageMcpIndicator failed:', e);
    }
  });
  
} catch (error) {
  console.error('Import failed:', error);
  console.error('Failed module:', error.message);
}