// AgentSystem namespace for modular organization
window.AgentSystem = window.AgentSystem || {};

// Panel Management Module
window.AgentSystem.PanelManager = {
  activePanels: new Map(), // Track multiple panels
  zIndexCounter: 1000,
  Z_INDEX_BASE: 1000,      // Base z-index for panels
  Z_INDEX_MAX: 8999,       // Max z-index before rebase (below dropdown at 9000)
  MIN_WIDTH: 380,
  MIN_HEIGHT: 320,
  ORDER_KEY: 'panelOrder',
  
  // Global drag/resize state - prevents stuck states
  _dragState: null,   // { panel, startX, startY, panelStartX, panelStartY, header }
  _resizeState: null, // { panel, startX, startY, panelStartW, panelStartH, minW, minH }
  _rafId: null,       // requestAnimationFrame ID for throttling
  _initialized: false,
  _iframeOverlay: null, // Overlay to block iframe mouse events during drag/resize

  // Block iframes from capturing mouse events during drag/resize
  _blockIframes: function() {
    // Method 1: Disable pointer-events on ALL iframes
    document.querySelectorAll('iframe').forEach(iframe => {
      iframe.dataset.previousPointerEvents = iframe.style.pointerEvents || '';
      iframe.style.pointerEvents = 'none';
    });
    
    // Method 2: Also add a full-screen overlay as backup
    if (!this._iframeOverlay) {
      this._iframeOverlay = document.createElement('div');
      this._iframeOverlay.id = 'panel-iframe-blocker';
      this._iframeOverlay.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        width: 100vw;
        height: 100vh;
        z-index: 2147483647;
        background: transparent;
        pointer-events: auto;
      `;
    }
    if (!this._iframeOverlay.parentNode) {
      document.body.appendChild(this._iframeOverlay);
    }
  },

  // Remove iframe blocker overlay and restore pointer-events
  _unblockIframes: function() {
    // Remove overlay
    if (this._iframeOverlay && this._iframeOverlay.parentNode) {
      this._iframeOverlay.parentNode.removeChild(this._iframeOverlay);
    }
    
    // Restore pointer-events on all iframes
    document.querySelectorAll('iframe').forEach(iframe => {
      if (iframe.dataset.previousPointerEvents !== undefined) {
        iframe.style.pointerEvents = iframe.dataset.previousPointerEvents;
        delete iframe.dataset.previousPointerEvents;
      }
    });
  },

  createPanel: function(id, title, content = '', additionalClasses = '', headerContent = '') {
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
    panel.tabIndex = -1; // Make panel non-focusable
  panel.style.position = 'fixed';
  panel.style.zIndex = ++this.zIndexCounter;

  // Set default positioning based on panel type with staggering
    const panelCount = this.activePanels.size;
    const offset = panelCount * 30; // Stagger panels

    if (id.includes('status')) {
      panel.style.width = '450px';
      panel.style.height = '650px';
  panel.style.minWidth = '380px';
  panel.style.minHeight = '320px';
      panel.style.right = (24 + offset) + 'px';
      panel.style.top = (80 + offset) + 'px';
    } else if (id.includes('debug')) {
      panel.style.width = '600px';
      panel.style.height = '700px';
  panel.style.minWidth = '380px';
  panel.style.minHeight = '320px';
      panel.style.left = (24 + offset) + 'px';
      panel.style.top = (80 + offset) + 'px';
    } else {
      // MCP panel
      panel.style.width = '760px';
      panel.style.height = '880px';
      panel.style.minWidth = String(this.MIN_WIDTH) + 'px';
      panel.style.minHeight = String(this.MIN_HEIGHT) + 'px';
      panel.style.left = (24 + offset) + 'px';
      panel.style.top = (80 + offset) + 'px';
    }

    // Apply saved state (position/size) if present
    try {
      const raw = localStorage.getItem('panelState:' + id);
      if (raw) {
        const state = JSON.parse(raw);
        const w = parseInt(state.width, 10);
        const h = parseInt(state.height, 10);
        if (!isNaN(w)) panel.style.width = Math.max(w, this.MIN_WIDTH) + 'px';
        if (!isNaN(h)) panel.style.height = Math.max(h, this.MIN_HEIGHT) + 'px';
        if (typeof state.left !== 'undefined') panel.style.left = state.left + 'px';
        if (typeof state.top !== 'undefined') panel.style.top = state.top + 'px';
        if (typeof state.right !== 'undefined') panel.style.right = state.right + 'px';
      }
    } catch (err) {
      console.warn('Failed to apply saved panel state for', id, err);
    }

    // Ensure panel is inside the viewport (handle changed screen resolution)
    try {
      const parsedWidth = parseInt(panel.style.width, 10) || this.MIN_WIDTH;
      const parsedHeight = parseInt(panel.style.height, 10) || this.MIN_HEIGHT;

      // compute left/top if set; allow fallback to defaults
      let left = null;
      let top = null;
      if (panel.style.left && panel.style.left !== 'auto') {
        const lp = parseInt(panel.style.left, 10);
        if (!isNaN(lp)) left = lp;
      }
      if (panel.style.top && panel.style.top !== 'auto') {
        const tp = parseInt(panel.style.top, 10);
        if (!isNaN(tp)) top = tp;
      }

      const maxLeft = Math.max(0, window.innerWidth - parsedWidth - 20);
      const maxTop = Math.max(0, window.innerHeight - parsedHeight - 40);

      if (left === null) left = Math.min(24, maxLeft);
      if (top === null) top = Math.min(80, maxTop);

      // clamp into viewport
      left = Math.max(0, Math.min(left, maxLeft));
      top = Math.max(0, Math.min(top, maxTop));

      panel.style.left = left + 'px';
      panel.style.top = top + 'px';
      panel.style.right = 'auto';
    } catch (err) {
      // silently ignore viewport clamp failures
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
  // Register panel order and bring to front for new panel
  this.updateOrderOnFront(id);

    // Add close button event listener
    const closeBtn = panel.querySelector(`#${id}CloseBtn`);
    if (closeBtn) {
      closeBtn.addEventListener('click', () => this.closePanel(id));
    }

    // Prevent wheel events on panel header from scrolling parent page
    const header = panel.querySelector('.floating-panel-header');
    if (header) {
      header.addEventListener('wheel', (e) => {
        e.preventDefault();
      }, { passive: false });
    }

    // Prevent panel from getting focus outline when child elements are focused
    panel.addEventListener('focus', (e) => {
      e.preventDefault();
      panel.blur();
    });
    panel.addEventListener('focusin', (e) => {
      // Only blur if the focus is on the panel itself, not on child elements
      if (e.target === panel) {
        e.preventDefault();
        panel.blur();
      }
    });

    // Make panel draggable and resizable (only on desktop)
    if (!this._isMobile()) {
      this.makeDraggable(panel);
      this.makeResizable(panel);
    } else {
      // On mobile: add swipe-down-to-close gesture on header
      this._addMobileSwipeClose(panel);
    }

    return panel;
  },
  
  // Check if we're on a mobile device (based on viewport width)
  // Check if we're on a mobile device (based on viewport width)
  _isMobile: function() {
    return window.innerWidth <= 768;
  },
  
  // Add swipe-down-to-close gesture for mobile panels
  _addMobileSwipeClose: function(panel) {
    const header = panel.querySelector('.floating-panel-header');
    if (!header) return;
    
    const self = this;
    let touchStartY = 0;
    let touchCurrentY = 0;
    let isDragging = false;
    
    header.addEventListener('touchstart', function(e) {
      touchStartY = e.touches[0].clientY;
      isDragging = true;
      panel.style.transition = 'none';
    }, { passive: true });
    
    header.addEventListener('touchmove', function(e) {
      if (!isDragging) return;
      touchCurrentY = e.touches[0].clientY;
      const deltaY = touchCurrentY - touchStartY;
      
      // Only allow dragging down
      if (deltaY > 0) {
        panel.style.transform = `translateY(${deltaY}px)`;
      }
    }, { passive: true });
    
    header.addEventListener('touchend', function(e) {
      if (!isDragging) return;
      isDragging = false;
      
      const deltaY = touchCurrentY - touchStartY;
      panel.style.transition = 'transform 0.3s ease';
      
      // If swiped down more than 100px, close the panel
      if (deltaY > 100) {
        panel.style.transform = 'translateY(100%)';
        setTimeout(() => self.closePanel(panel.id), 300);
      } else {
        // Snap back
        panel.style.transform = 'translateY(0)';
      }
      
      touchStartY = 0;
      touchCurrentY = 0;
    });
  },
  
  // Initialize global mouse/pointer event handlers (once)
  _initGlobalHandlers: function() {
    if (this._initialized) return;
    this._initialized = true;
    
    const self = this;
    
    // Global mousemove handler with requestAnimationFrame throttling
    const onMouseMove = (e) => {
      if (!self._dragState && !self._resizeState) return;
      
      // Cancel any pending frame
      if (self._rafId) {
        cancelAnimationFrame(self._rafId);
      }
      
      // Schedule update on next frame for smooth performance
      self._rafId = requestAnimationFrame(() => {
        if (self._dragState) {
          self._handleDragMove(e.clientX, e.clientY);
        }
        if (self._resizeState) {
          self._handleResizeMove(e.clientX, e.clientY);
        }
      });
    };
    
    // Global mouseup handler - reset all drag/resize states
    const onMouseUp = (e) => {
      self._endDrag();
      self._endResize();
    };
    
    // Handle window blur/focus loss - cancel any active operations
    const onBlur = () => {
      self._endDrag();
      self._endResize();
    };
    
    // Handle mouse leaving window - also end operations to prevent stuck state
    const onMouseLeave = (e) => {
      // Only trigger if actually leaving the window (not entering a child element)
      if (e.relatedTarget === null || e.relatedTarget.nodeName === 'HTML') {
        self._endDrag();
        self._endResize();
      }
    };
    
    // Attach to window for reliable event capture even when mouse leaves document
    window.addEventListener('mousemove', onMouseMove, { passive: true });
    window.addEventListener('mouseup', onMouseUp);
    window.addEventListener('blur', onBlur);
    document.addEventListener('mouseleave', onMouseLeave);
    
    // Also handle pointer events for better touch/pen support
    window.addEventListener('pointerup', onMouseUp);
    window.addEventListener('pointercancel', onMouseUp);
    
    // Handle visibility change (tab switch, minimize)
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        self._endDrag();
        self._endResize();
      }
    });
  },
  
  // Handle drag movement
  _handleDragMove: function(clientX, clientY) {
    const state = this._dragState;
    if (!state) return;
    
    const deltaX = clientX - state.startX;
    const deltaY = clientY - state.startY;
    
    const newX = state.panelStartX + deltaX;
    const newY = state.panelStartY + deltaY;
    
    // Keep panel within viewport
    const maxX = window.innerWidth - state.panel.offsetWidth;
    const maxY = window.innerHeight - state.panel.offsetHeight;
    
    state.panel.style.left = Math.max(0, Math.min(newX, maxX)) + 'px';
    state.panel.style.top = Math.max(0, Math.min(newY, maxY)) + 'px';
    state.panel.style.right = 'auto';
  },
  
  // End drag operation
  _endDrag: function() {
    const state = this._dragState;
    if (!state) return;
    
    // Remove iframe blocker
    this._unblockIframes();
    
    state.header.style.cursor = 'grab';
    document.body.style.userSelect = '';
    document.body.style.cursor = '';
    
    // Persist panel position
    try {
      const rect = state.panel.getBoundingClientRect();
      const savedState = JSON.parse(localStorage.getItem('panelState:' + state.panel.id) || '{}');
      savedState.left = Math.round(rect.left);
      savedState.top = Math.round(rect.top);
      localStorage.setItem('panelState:' + state.panel.id, JSON.stringify(savedState));
    } catch (err) {
      console.warn('Failed to save panel position', state.panel.id, err);
    }
    
    this._dragState = null;
  },
  
  // Handle resize movement
  _handleResizeMove: function(clientX, clientY) {
    const state = this._resizeState;
    if (!state) return;
    
    const deltaX = clientX - state.startX;
    const deltaY = clientY - state.startY;
    
    const newWidth = state.panelStartW + deltaX;
    const newHeight = state.panelStartH + deltaY;
    
    // Enforce minimum and maximum sizes
    const maxWidth = window.innerWidth - 20;
    const maxHeight = window.innerHeight - 100;
    
    state.panel.style.width = Math.max(state.minW, Math.min(newWidth, maxWidth)) + 'px';
    state.panel.style.height = Math.max(state.minH, Math.min(newHeight, maxHeight)) + 'px';
  },
  
  // End resize operation
  _endResize: function() {
    const state = this._resizeState;
    if (!state) return;
    
    // Remove iframe blocker
    this._unblockIframes();
    
    document.body.style.userSelect = '';
    document.body.style.cursor = '';
    
    // Persist panel size
    try {
      const rect = state.panel.getBoundingClientRect();
      const savedState = JSON.parse(localStorage.getItem('panelState:' + state.panel.id) || '{}');
      savedState.width = Math.round(Math.max(rect.width, state.minW));
      savedState.height = Math.round(Math.max(rect.height, state.minH));
      localStorage.setItem('panelState:' + state.panel.id, JSON.stringify(savedState));
    } catch (err) {
      console.warn('Failed to save panel size', state.panel.id, err);
    }
    
    this._resizeState = null;
  },

  bringToFront: function(panel) {
    try {
      if (!panel) return;
      
      // Check if we need to rebase z-indexes (compact them back to base range)
      if (this.zIndexCounter >= this.Z_INDEX_MAX) {
        this.rebaseZIndexes();
      }
      
      // Simply increment counter and assign to this panel
      panel.style.zIndex = ++this.zIndexCounter;
      
      // Update saved order for persistence
      this.updateOrderOnFront(panel.id);
    } catch (err) {
      console.warn('bringToFront failed for', panel && panel.id, err);
    }
  },

  // Load saved panel order from localStorage
  loadOrder: function() {
    try {
      const raw = localStorage.getItem(this.ORDER_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed;
    } catch (err) {
      // ignore
    }
    return [];
  },

  // Save panel order array to localStorage
  saveOrder: function(order) {
    try {
      localStorage.setItem(this.ORDER_KEY, JSON.stringify(order));
    } catch (err) {
      console.warn('Failed to save panel order', err);
    }
  },

  // Move id to top (end) of order and persist, then apply ordering
  updateOrderOnFront: function(id) {
    try {
      const order = this.loadOrder().filter(x => x !== id);
      order.push(id);
      this.saveOrder(order);
      // Don't re-apply z-indexes here anymore - that's done in bringToFront
    } catch (err) {
      console.warn('Failed to update panel order for', id, err);
    }
  },
  
  // Rebase all panel z-indexes to compact range starting from Z_INDEX_BASE
  rebaseZIndexes: function() {
    try {
      console.log('Rebasing panel z-indexes...');
      const order = this.loadOrder();
      let z = this.Z_INDEX_BASE;
      const assigned = new Set();
      
      // Assign z-indexes in saved order
      order.forEach(id => {
        const panel = this.activePanels.get(id);
        if (panel) {
          panel.style.zIndex = z++;
          assigned.add(id);
        }
      });
      
      // Assign z-indexes to panels not in order
      this.activePanels.forEach((panel, id) => {
        if (!assigned.has(id)) {
          panel.style.zIndex = z++;
        }
      });
      
      this.zIndexCounter = z;
      console.log(`Rebased ${this.activePanels.size} panels, new counter: ${this.zIndexCounter}`);
    } catch (err) {
      console.warn('Failed to rebase z-indexes', err);
    }
  },

  // Apply saved order to currently active panels (assign z-indexes)
  // Only called on initialization or after rebase
  applyOrderToActivePanels: function(order) {
    try {
      const ord = Array.isArray(order) ? order : this.loadOrder();
      let z = this.Z_INDEX_BASE;
      const assigned = new Set();
      
      // Assign z-indexes in saved order
      ord.forEach(id => {
        const panel = this.activePanels.get(id);
        if (panel) {
          panel.style.zIndex = z++;
          assigned.add(id);
        }
      });
      
      // Assign z-indexes to panels not in order
      this.activePanels.forEach((panel, id) => {
        if (!assigned.has(id)) {
          panel.style.zIndex = z++;
        }
      });
      
      this.zIndexCounter = z;
    } catch (err) {
      console.warn('Failed to apply panel order', err);
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
        // Call plugin cleanup before closing
        try {
          // Extract plugin ID from panel ID (e.g., "floatinglog_viewerPanel" -> "log_viewer")
          const match = id.match(/^floating(.+)Panel$/);
          if (match) {
            const pluginId = match[1];
            if (window.AgentSystem && window.AgentSystem[pluginId]) {
              const pluginModule = window.AgentSystem[pluginId];
              if (typeof pluginModule.destroy === 'function') {
                pluginModule.destroy();
              }
            }
          }
        } catch (err) {
          console.warn('Failed to call plugin destroy for', id, err);
        }

        // Persist current size/position on close so reopening restores layout
        try {
          const rect = panel.getBoundingClientRect();
          const state = JSON.parse(localStorage.getItem('panelState:' + panel.id) || '{}');
          state.width = Math.round(Math.max(rect.width, this.MIN_WIDTH));
          state.height = Math.round(Math.max(rect.height, this.MIN_HEIGHT));
          state.left = Math.round(rect.left);
          state.top = Math.round(rect.top);
          localStorage.setItem('panelState:' + panel.id, JSON.stringify(state));
        } catch (err) {
          console.warn('Failed to save panel state on close', panel.id, err);
        }
        panel.remove();
        this.activePanels.delete(id);
        // remove from saved order
        try {
          const order = this.loadOrder().filter(x => x !== id);
          this.saveOrder(order);
        } catch (err) {
          // ignore
        }
      }
    } else {
      // Close all panels (legacy support)
      this.activePanels.forEach(panel => {
        // Call cleanup for each panel
        this.closePanel(panel.id);
      });
    }
  },

  makeDraggable: function(panel) {
    // Ensure global handlers are initialized (once)
    this._initGlobalHandlers();
    
    const header = panel.querySelector('.floating-panel-header');
    const self = this;

    header.addEventListener('mousedown', function(e) {
      // Bring panel to front when interacting with header
      self.bringToFront(panel);
      
      // Don't start dragging if clicking on buttons or inputs
      if (e.target.tagName === 'BUTTON' || e.target.tagName === 'INPUT' || 
          e.target.closest('button') || e.target.closest('input')) {
        return;
      }

      // Block iframes from capturing mouse events
      self._blockIframes();

      const rect = panel.getBoundingClientRect();
      
      // Set global drag state - global handlers will take over
      self._dragState = {
        panel: panel,
        startX: e.clientX,
        startY: e.clientY,
        panelStartX: rect.left,
        panelStartY: rect.top,
        header: header
      };

      header.style.cursor = 'grabbing';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
  },

  makeResizable: function(panel) {
    // Ensure global handlers are initialized (once)
    this._initGlobalHandlers();
    
    const resizeHandle = panel.querySelector('.resize-handle');
    if (!resizeHandle) return;

    const self = this;
    const minWidth = this.MIN_WIDTH;
    const minHeight = this.MIN_HEIGHT;

    resizeHandle.addEventListener('mousedown', function(e) {
      // Bring panel to front when starting resize
      self.bringToFront(panel);
      
      // Block iframes from capturing mouse events
      self._blockIframes();
      
      const rect = panel.getBoundingClientRect();
      
      // Set global resize state - global handlers will take over
      self._resizeState = {
        panel: panel,
        startX: e.clientX,
        startY: e.clientY,
        panelStartW: rect.width,
        panelStartH: rect.height,
        minW: minWidth,
        minH: minHeight
      };

      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
  }
};