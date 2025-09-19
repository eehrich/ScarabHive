// AgentSystem namespace for modular organization
window.AgentSystem = window.AgentSystem || {};

// Panel Management Module
window.AgentSystem.PanelManager = {
  activePanels: new Map(), // Track multiple panels
  zIndexCounter: 1000,
  
  createPanel: function(id, title, content = '', additionalClasses = '', headerContent = '') {
    console.log(`Creating panel: ${id}`);

    // If panel already exists return it and update content
    if (this.activePanels.has(id)) {
      const existing = this.activePanels.get(id);
      const body = existing.querySelector('.floating-panel-body');
      const contentDiv = body ? body.querySelector('.panel-content') || body : existing.querySelector('.panel-content') || existing;
      contentDiv.innerHTML = content;
      // bring existing panel to front
      this.bringToFront(existing);
      return existing;
    }
    
    // Create panel with proper structure
    const panel = document.createElement('div');
    panel.className = `floating-panel ${additionalClasses}`;
    panel.id = id;
    panel.style.display = 'block';
  panel.style.position = 'fixed';
  panel.style.zIndex = ++this.zIndexCounter;
    
  // Set default positioning based on panel type with staggering
    const panelCount = this.activePanels.size;
    const offset = panelCount * 30; // Stagger panels
    
    if (id.includes('status')) {
      panel.style.width = '400px';
      panel.style.height = '700px';
      panel.style.minWidth = '360px';
      panel.style.minHeight = '300px';
      panel.style.right = (24 + offset) + 'px';
      panel.style.top = (80 + offset) + 'px';
    } else if (id.includes('debug')) {
      panel.style.width = '600px';
      panel.style.height = '700px';
      panel.style.minWidth = '360px';
      panel.style.minHeight = '300px';
      panel.style.left = (24 + offset) + 'px';
      panel.style.top = (80 + offset) + 'px';
    } else {
      // MCP panel
      panel.style.width = '640px';
      panel.style.height = '800px';
      panel.style.minWidth = '360px';
      panel.style.minHeight = '300px';
      panel.style.left = (24 + offset) + 'px';
      panel.style.top = (80 + offset) + 'px';
    }

    // Apply saved state (position/size) if present
    try {
      const raw = localStorage.getItem('panelState:' + id);
      if (raw) {
        const state = JSON.parse(raw);
        if (state.width) panel.style.width = state.width + 'px';
        if (state.height) panel.style.height = state.height + 'px';
        if (typeof state.left !== 'undefined') panel.style.left = state.left + 'px';
        if (typeof state.top !== 'undefined') panel.style.top = state.top + 'px';
        if (typeof state.right !== 'undefined') panel.style.right = state.right + 'px';
      }
    } catch (err) {
      console.warn('Failed to apply saved panel state for', id, err);
    }
    
    panel.innerHTML = `
      <div class="floating-panel-header" id="${id}Header">
        <span>${title}</span>
        <div style="margin-left:8px;flex:1"></div>
        ${headerContent}
        <button id="${id}CloseBtn" title="Close" aria-label="Close">✕</button>
      </div>
      <div class="floating-panel-body" id="${id}Body">
        <div class="panel-content">
          ${content}
        </div>
      </div>
      <div class="resize-handle"></div>
    `;
    
    document.body.appendChild(panel);
    this.activePanels.set(id, panel);
    
    // Add close button event listener
    const closeBtn = panel.querySelector(`#${id}CloseBtn`);
    if (closeBtn) {
      closeBtn.addEventListener('click', () => this.closePanel(id));
    }
    
    // Make panel draggable and resizable
    this.makeDraggable(panel);
    this.makeResizable(panel);
    
    console.log(`Panel ${id} created and added to DOM`);
    
    return panel;
  },

  bringToFront: function(panel) {
    try {
      if (!panel) return;
      this.zIndexCounter += 1;
      panel.style.zIndex = this.zIndexCounter;
    } catch (err) {
      console.warn('bringToFront failed for', panel && panel.id, err);
    }
  },

  togglePanel: function(id, createCallback) {
    if (this.activePanels.has(id)) {
      this.closePanel(id);
      return null;
    }

    if (typeof createCallback === 'function') {
      // createCallback should call createPanel or otherwise add the panel
      createCallback();
      return this.activePanels.get(id) || null;
    }

    return null;
  },
  
  closePanel: function(id) {
    if (id) {
      // Close specific panel
      const panel = this.activePanels.get(id);
      if (panel) {
        panel.remove();
        this.activePanels.delete(id);
      }
    } else {
      // Close all panels (legacy support)
      this.activePanels.forEach(panel => panel.remove());
      this.activePanels.clear();
    }
  },
  
  makeDraggable: function(panel) {
    const header = panel.querySelector('.floating-panel-header');
    let isDragging = false;
    let dragStart = { x: 0, y: 0 };
    let panelStart = { x: 0, y: 0 };
    
    header.addEventListener('mousedown', (e) => {
      // bring panel to front when interacting with header
      this.bringToFront(panel);
      // Don't start dragging if clicking on buttons or inputs
      if (e.target.tagName === 'BUTTON' || e.target.tagName === 'INPUT' || e.target.closest('button') || e.target.closest('input')) {
        return;
      }
      
      isDragging = true;
      dragStart.x = e.clientX;
      dragStart.y = e.clientY;
      
      const rect = panel.getBoundingClientRect();
      panelStart.x = rect.left;
      panelStart.y = rect.top;
      
      header.style.cursor = 'grabbing';
      document.body.style.userSelect = 'none';
    });
    
    document.addEventListener('mousemove', (e) => {
      if (!isDragging) return;
      
      const deltaX = e.clientX - dragStart.x;
      const deltaY = e.clientY - dragStart.y;
      
      const newX = panelStart.x + deltaX;
      const newY = panelStart.y + deltaY;
      
      // Keep panel within viewport
      const maxX = window.innerWidth - panel.offsetWidth;
      const maxY = window.innerHeight - panel.offsetHeight;
      
      panel.style.left = Math.max(0, Math.min(newX, maxX)) + 'px';
      panel.style.top = Math.max(0, Math.min(newY, maxY)) + 'px';
      panel.style.right = 'auto';
    });
    
    document.addEventListener('mouseup', () => {
      if (isDragging) {
        isDragging = false;
        header.style.cursor = 'grab';
        document.body.style.userSelect = '';
        // Persist panel position
        try {
          const rect = panel.getBoundingClientRect();
          const state = JSON.parse(localStorage.getItem('panelState:' + panel.id) || '{}');
          state.left = Math.round(rect.left);
          state.top = Math.round(rect.top);
          localStorage.setItem('panelState:' + panel.id, JSON.stringify(state));
        } catch (err) {
          console.warn('Failed to save panel position', panel.id, err);
        }
      }
    });
  },
  
  makeResizable: function(panel) {
    const resizeHandle = panel.querySelector('.resize-handle');
    if (!resizeHandle) return;
    
    let isResizing = false;
    let resizeStart = { x: 0, y: 0 };
    let panelStart = { width: 0, height: 0 };
    
    resizeHandle.addEventListener('mousedown', (e) => {
      // bring panel to front when starting resize
      this.bringToFront(panel);
      isResizing = true;
      resizeStart.x = e.clientX;
      resizeStart.y = e.clientY;
      
      const rect = panel.getBoundingClientRect();
      panelStart.width = rect.width;
      panelStart.height = rect.height;
      
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    
    document.addEventListener('mousemove', (e) => {
      if (!isResizing) return;
      
      const deltaX = e.clientX - resizeStart.x;
      const deltaY = e.clientY - resizeStart.y;
      
      const newWidth = panelStart.width + deltaX;
      const newHeight = panelStart.height + deltaY;
      
      // Enforce minimum and maximum sizes
  const minWidth = 360;
  const minHeight = 300;
      const maxWidth = window.innerWidth - 20;
      const maxHeight = window.innerHeight - 100;
      
      panel.style.width = Math.max(minWidth, Math.min(newWidth, maxWidth)) + 'px';
      panel.style.height = Math.max(minHeight, Math.min(newHeight, maxHeight)) + 'px';
    });
    
    document.addEventListener('mouseup', () => {
      if (isResizing) {
        isResizing = false;
        document.body.style.userSelect = '';
        // Persist panel size
        try {
          const rect = panel.getBoundingClientRect();
          const state = JSON.parse(localStorage.getItem('panelState:' + panel.id) || '{}');
          // enforce minimum when persisting
          state.width = Math.round(Math.max(rect.width, minWidth));
          state.height = Math.round(Math.max(rect.height, minHeight));
          localStorage.setItem('panelState:' + panel.id, JSON.stringify(state));
        } catch (err) {
          console.warn('Failed to save panel size', panel.id, err);
        }
      }
    });
  }
};

console.log('PanelManager module loaded');