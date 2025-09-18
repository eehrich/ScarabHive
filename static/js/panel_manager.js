// AgentSystem namespace for modular organization
window.AgentSystem = window.AgentSystem || {};

// Panel Management Module
window.AgentSystem.PanelManager = {
  activePanels: new Map(), // Track multiple panels
  
  createPanel: function(id, title, content = '', additionalClasses = '', headerContent = '') {
    console.log(`Creating panel: ${id}`);
    
    // Remove existing panel with same ID
    this.closePanel(id);
    
    // Create panel with proper structure
    const panel = document.createElement('div');
    panel.className = `floating-panel ${additionalClasses}`;
    panel.id = id;
    panel.style.display = 'block';
    panel.style.position = 'fixed';
    
    // Set default positioning based on panel type with staggering
    const panelCount = this.activePanels.size;
    const offset = panelCount * 30; // Stagger panels
    
    if (id.includes('status')) {
      panel.style.width = '400px';
      panel.style.height = '700px';
      panel.style.right = (24 + offset) + 'px';
      panel.style.top = (80 + offset) + 'px';
    } else if (id.includes('debug')) {
      panel.style.width = '600px';
      panel.style.height = '700px';
      panel.style.left = (24 + offset) + 'px';
      panel.style.top = (80 + offset) + 'px';
    } else {
      // MCP panel
      panel.style.width = '640px';
      panel.style.height = '800px';
      panel.style.left = (24 + offset) + 'px';
      panel.style.top = (80 + offset) + 'px';
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
      const minWidth = 300;
      const minHeight = 200;
      const maxWidth = window.innerWidth - 20;
      const maxHeight = window.innerHeight - 100;
      
      panel.style.width = Math.max(minWidth, Math.min(newWidth, maxWidth)) + 'px';
      panel.style.height = Math.max(minHeight, Math.min(newHeight, maxHeight)) + 'px';
    });
    
    document.addEventListener('mouseup', () => {
      if (isResizing) {
        isResizing = false;
        document.body.style.userSelect = '';
      }
    });
  }
};

console.log('PanelManager module loaded');