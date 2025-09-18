/*
  panel_functions.js — Floating panel management for Agent System
*/

// Panel state persistence functions
export function loadPanelState(id) {
  try {
    const raw = localStorage.getItem('panelState:' + id);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) {
    return null;
  }
}

export function savePanelState(id, state) {
  try {
    localStorage.setItem('panelState:' + id, JSON.stringify(state));
  } catch (e) {
    // ignore localStorage errors
  }
}

export function applyPanelState(panel, state) {
  if (!panel || !state) return;

  // Use consistent left positioning for all panels
  if (state.left !== undefined) panel.style.left = state.left + 'px';
  if (state.top !== undefined) panel.style.top = state.top + 'px';
  if (state.width !== undefined) panel.style.width = state.width + 'px';
  if (state.height !== undefined) panel.style.height = state.height + 'px';
  
  // Always use left positioning, remove any right positioning
  panel.style.right = 'auto';
  
  if (state.visible) panel.style.display = 'block';
}

// Make panel draggable by header
export function makeDraggable(headerId, panel) {
  const header = document.getElementById(headerId);
  if (!header || !panel) return;
  
  let isDragging = false;
  let startX = 0, startY = 0, origX = 0, origY = 0;
  let activePointerId = null;

  // Improve touch/pen behavior
  try {
    header.style.touchAction = 'none';
    header.style.userSelect = 'none';
    header.style.cursor = 'move';
  } catch (e) {
    // ignore styling errors
  }
  
  // Direct drag handler on the header
  header.addEventListener('pointerdown', (ev) => {
    // If the pointerdown originated on an interactive control, ignore so clicks work
    const interactive = ev.target && (
      ev.target.tagName === 'INPUT' || 
      ev.target.tagName === 'BUTTON' || 
      ev.target.tagName === 'SELECT' || 
      ev.target.tagName === 'TEXTAREA' || 
      ev.target.tagName === 'LABEL'
    );
    if (interactive) return;

    // Also ignore specific interactive elements
    if (ev.target && ev.target.closest && (
      ev.target.closest('#floatingCloseBtn') || 
      ev.target.closest('#floatingMCPCloseBtn') || 
      ev.target.closest('#floatingDebugCloseBtn') ||
      ev.target.closest('#mcpRefreshBtn') ||
      ev.target.closest('#debugRefreshBtn') ||
      ev.target.closest('#mcpFilterInput') ||
      ev.target.closest('.custom-toggle') ||
      ev.target.closest('#mcpAutoRefreshInterval')
    )) return;

    isDragging = true;
    activePointerId = ev.pointerId;
    startX = ev.clientX; 
    startY = ev.clientY;
    const rect = panel.getBoundingClientRect();
    origX = rect.left; 
    origY = rect.top;

    try {
      header.setPointerCapture(ev.pointerId);
    } catch (e) {
      // ignore pointer capture errors
    }
    ev.preventDefault();
  });
  
  window.addEventListener('pointermove', (ev) => {
    if (!isDragging) return;
    if (activePointerId !== null && ev.pointerId !== activePointerId) return;

    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;

    let newLeft = origX + dx;
    let newTop = origY + dy;

    // Keep panel within viewport bounds
    const rect = panel.getBoundingClientRect();
    const maxLeft = window.innerWidth - rect.width;
    const maxTop = window.innerHeight - rect.height;

    newLeft = Math.max(0, Math.min(maxLeft, newLeft));
    newTop = Math.max(0, Math.min(maxTop, newTop));

    panel.style.left = newLeft + 'px';
    panel.style.top = newTop + 'px';
    panel.style.right = 'auto'; // Always use left positioning during drag
  });
  
  window.addEventListener('pointerup', (ev) => {
    if (!isDragging) return;
    isDragging = false;
    if (activePointerId !== null) {
      try {
        header.releasePointerCapture(activePointerId);
      } catch (e) {
        // ignore pointer capture errors
      }
      activePointerId = null;
    }

    // Save position
    savePanelPosition(panel);
  });
}

// Make panel resizable via the .resize-handle element
export function makeResizable(panel) {
  if (!panel) return;
  const handle = panel.querySelector('.resize-handle');
  if (!handle) return;

  let isResizing = false;
  let startX = 0, startY = 0, startWidth = 0, startHeight = 0;

  handle.addEventListener('pointerdown', (ev) => {
    ev.preventDefault();
    isResizing = true;
    startX = ev.clientX;
    startY = ev.clientY;
    const rect = panel.getBoundingClientRect();
    startWidth = rect.width;
    startHeight = rect.height;
    try {
      handle.setPointerCapture(ev.pointerId);
    } catch (e) {
      // ignore pointer capture errors
    }
  });

  window.addEventListener('pointermove', (ev) => {
    if (!isResizing) return;
    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;
    const newWidth = Math.max(200, Math.round(startWidth + dx));
    const newHeight = Math.max(120, Math.round(startHeight + dy));
    panel.style.width = newWidth + 'px';
    panel.style.height = newHeight + 'px';
    // Keep panel anchored by left/top
    panel.style.right = 'auto';
  });

  window.addEventListener('pointerup', (ev) => {
    if (!isResizing) return;
    isResizing = false;
    // Save size/position
    savePanelPosition(panel);
  });
}

// Save panel position/size
export function savePanelPosition(panel) {
  try {
    const rect = panel.getBoundingClientRect();
    const state = loadPanelState(panel.id) || {};
    
    // Always save as left positioning for consistency
    state.left = Math.round(rect.left);
    state.top = Math.round(rect.top);
    state.width = Math.round(rect.width);
    state.height = Math.round(rect.height);
    
    // Remove any old right positioning
    delete state.right;
    
    savePanelState(panel.id, state);
  } catch (e) {
    console.warn('Failed to save panel position:', e);
  }
}

// Window resize handler to keep panels in bounds
export function handleWindowResize(panels) {
  panels.forEach(panel => {
    if (!panel || panel.style.display === 'none') return;
    
    const rect = panel.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    
    let needsUpdate = false;
    let newLeft = rect.left;
    let newTop = rect.top;
    
    // Keep panel within viewport
    if (rect.right > viewportWidth) {
      newLeft = viewportWidth - rect.width;
      needsUpdate = true;
    }
    if (rect.bottom > viewportHeight) {
      newTop = viewportHeight - rect.height;
      needsUpdate = true;
    }
    if (rect.left < 0) {
      newLeft = 0;
      needsUpdate = true;
    }
    if (rect.top < 0) {
      newTop = 0;
      needsUpdate = true;
    }
    
    if (needsUpdate) {
      panel.style.left = newLeft + 'px';
      panel.style.top = newTop + 'px';
      panel.style.right = 'auto';
      savePanelPosition(panel);
    }
  });
}

// Global function to reset panel positions
export function resetPanelPositions(statusPanel, mcpPanel, debugPanel) {
  localStorage.removeItem('panelState:floatingStatusPanel');
  localStorage.removeItem('panelState:floatingMCPPanel');
  localStorage.removeItem('panelState:floatingDebugPanel');

  // Reset status panel to right side with left positioning
  statusPanel.style.left = (window.innerWidth - 424) + 'px';
  statusPanel.style.right = 'auto';
  statusPanel.style.top = '80px';
  statusPanel.style.width = '400px';
  statusPanel.style.height = '500px';

  // Reset MCP panel to left side
  mcpPanel.style.left = '24px';
  mcpPanel.style.right = 'auto';
  mcpPanel.style.top = '80px';
  mcpPanel.style.width = '640px';
  mcpPanel.style.height = '800px';

  // Reset debug panel to center-left
  debugPanel.style.left = '24px';
  debugPanel.style.right = 'auto';
  debugPanel.style.top = '80px';
  debugPanel.style.width = '600px';
  debugPanel.style.height = '700px';

  console.log('Panel positions reset');
}