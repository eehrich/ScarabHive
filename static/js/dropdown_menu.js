/**
 * Dropdown Menu System
 * Generic dropdown menus for header navigation
 * Supports plugin-contributed menus and menu items
 */

window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.DropdownMenu = {
  menus: new Map(),  // menuId -> { definition, items, btnElement, panelElement }
  openMenuId: null,
  initialized: false,

  /**
   * Initialize the dropdown menu system
   */
  async init() {
    if (this.initialized) {
      return;
    }
    
    try {
      await this.loadMenuData();
    } catch (error) {
      console.error('Failed to load menu data:', error);
      // Continue with empty menus - will show login button
    }
    
    // Always render menus (shows login button if no menus/not authenticated)
    this.renderMenus();
    this.setupGlobalListeners();
    this.initialized = true;
  },

  /**
   * Load menu definitions and items from API
   */
  async loadMenuData() {
    try {
      // Prepare fetch options with authentication
      const fetchOptions = { 
        credentials: 'include',
        headers: {}
      };
      
      // No need for Authorization header - cookie is sent automatically

      const [defsResponse, itemsResponse] = await Promise.all([
        fetch('/api/menu-definitions', fetchOptions),
        fetch('/api/menu-items', fetchOptions)
      ]);

      if (!defsResponse.ok || !itemsResponse.ok) {
        throw new Error('Failed to load menu data');
      }

      const definitions = await defsResponse.json();
      const items = await itemsResponse.json();

      // Organize data by menu
      definitions.forEach(def => {
        this.menus.set(def.id, {
          definition: def,
          items: items.filter(item => item.menu_id === def.id),
          btnElement: null,
          panelElement: null
        });
      });

    } catch (error) {
      console.error('Failed to load menu data:', error);
      throw error;
    }
  },

  /**
   * Render all dropdown menus in the header
   */
  renderMenus() {
    const headerButtons = document.querySelector('.header-buttons');
    const headerUserMenu = document.querySelector('.header-user-menu');
    
    if (!headerButtons) {
      console.error('Header buttons container not found');
      return;
    }

    // Clear existing menus before re-rendering
    if (headerUserMenu) {
      headerUserMenu.innerHTML = '';
    }

    // Find plugin buttons comment marker
    const pluginMarker = Array.from(headerButtons.childNodes).find(
      node => node.nodeType === Node.COMMENT_NODE &&
      node.textContent.trim().includes('Plugin buttons will be dynamically inserted here')
    );

    // If no menus (not logged in), show login button
    if (this.menus.size === 0) {
      this.renderLoginButton(headerUserMenu || headerButtons, pluginMarker);
      return;
    }

    // Render each menu
    this.menus.forEach((menuData, menuId) => {
      const container = this.createMenuContainer(menuId, menuData);
      
      // Insert based on position
      if (menuData.definition.position === 'left') {
        // Insert early in header (after main heading)
        const firstButton = headerButtons.querySelector('button');
        if (firstButton) {
          headerButtons.insertBefore(container, firstButton);
        } else {
          headerButtons.appendChild(container);
        }
      } else {
        // User menu (right side) goes to separate container to stay visible on mobile
        if (headerUserMenu) {
          headerUserMenu.appendChild(container);
        } else if (pluginMarker) {
          headerButtons.insertBefore(container, pluginMarker);
        } else {
          headerButtons.appendChild(container);
        }
      }

      // Store references
      menuData.btnElement = container.querySelector('.dropdown-menu-btn');
      // Panel is attached to body (portal mode), find it by ID
      menuData.panelElement = document.getElementById(`menu-panel-${menuId}`);
    });
  },

  /**
   * Render login button when not authenticated
   */
  renderLoginButton(container, pluginMarker) {
    console.log('[DropdownMenu] Rendering login button, container:', container?.id || container?.className);
    
    // Create login link/button
    const loginLink = document.createElement('a');
    loginLink.href = '/login';
    loginLink.className = 'header-login-btn';
    loginLink.id = 'headerLoginBtn';
    loginLink.textContent = 'Login';
    loginLink.title = 'Login to your account';
    
    // Append directly to container
    container.appendChild(loginLink);
    console.log('[DropdownMenu] Login button added to DOM');
  },

  /**
   * Create menu container with button and panel
   */
  createMenuContainer(menuId, menuData) {
    const { definition, items } = menuData;
    
    const container = document.createElement('div');
    container.className = 'dropdown-menu-container';
    if (definition.position === 'left') {
      container.classList.add('align-left');
    }
    container.id = `menu-${menuId}`;

    // Create button
    const button = document.createElement('button');
    button.className = 'dropdown-menu-btn';
    button.id = `menu-btn-${menuId}`;
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-haspopup', 'true');
    if (definition.tooltip) {
      button.title = definition.tooltip;
    }

    // Button icon (if any)
    if (definition.icon) {
      const icon = document.createElement('span');
      icon.className = 'menu-btn-avatar';
      icon.textContent = definition.icon;
      button.appendChild(icon);
    }

    // Button label
    const label = document.createElement('span');
    label.textContent = definition.label;
    button.appendChild(label);

    // Dropdown arrow
    const arrow = document.createElement('span');
    arrow.className = 'menu-btn-arrow';
    arrow.textContent = '▼';
    button.appendChild(arrow);

    // Create dropdown panel as PORTAL (attached to body, not header)
    const panel = document.createElement('div');
    panel.className = 'dropdown-menu-panel';
    panel.id = `menu-panel-${menuId}`;
    panel.setAttribute('role', 'menu');
    panel.dataset.menuId = menuId;
    panel.dataset.alignLeft = definition.position === 'left' ? 'true' : 'false';

    // Create inner wrapper for multi-column support
    const inner = document.createElement('div');
    inner.className = 'dropdown-menu-panel-inner';

    // Organize items by section
    const sections = this.groupItemsBySection(items);
    
    if (sections.size === 0) {
      // Empty menu
      const empty = document.createElement('div');
      empty.className = 'dropdown-menu-empty';
      empty.textContent = 'No items available';
      inner.appendChild(empty);
    } else {
      // Render sections
      sections.forEach((sectionItems, sectionName) => {
        const section = this.createMenuSection(sectionName, sectionItems);
        inner.appendChild(section);
      });
    }
    
    panel.appendChild(inner);

    // PORTAL PATTERN: Attach panel to body instead of header
    // This breaks out of the header's stacking context (z-index: 100)
    // allowing the panel (z-index: 9000) to appear above floating panels (z-index: 1000-8999)
    document.body.appendChild(panel);

    // Only button goes in container (header)
    container.appendChild(button);

    // Event listeners
    button.addEventListener('click', (e) => {
      e.stopPropagation();
      this.toggleMenu(menuId);
    });

    return container;
  },

  /**
   * Group menu items by section
   */
  groupItemsBySection(items) {
    const sections = new Map();
    
    items.forEach(item => {
      const section = item.section || 'default';
      if (!sections.has(section)) {
        sections.set(section, []);
      }
      sections.get(section).push(item);
    });

    // Sort items within each section by order
    sections.forEach((sectionItems) => {
      sectionItems.sort((a, b) => (a.order || 100) - (b.order || 100));
    });

    return sections;
  },

  /**
   * Create a menu section with items
   */
  createMenuSection(sectionName, items) {
    const section = document.createElement('div');
    section.className = 'dropdown-menu-section';

    // Section header (if not default)
    if (sectionName !== 'default') {
      const header = document.createElement('div');
      header.className = 'dropdown-menu-header';
      header.textContent = sectionName.charAt(0).toUpperCase() + sectionName.slice(1);
      section.appendChild(header);
    }

    // Add items
    items.forEach((item, index) => {
      // Divider before item
      if (item.divider_before) {
        const divider = document.createElement('div');
        divider.className = 'dropdown-menu-divider';
        section.appendChild(divider);
      }

      const menuItem = this.createMenuItem(item);
      section.appendChild(menuItem);

      // Divider after item
      if (item.divider_after) {
        const divider = document.createElement('div');
        divider.className = 'dropdown-menu-divider';
        section.appendChild(divider);
      }
    });

    return section;
  },

  /**
   * Create a single menu item
   */
  createMenuItem(item) {
    // Use <a> for URLs, <button> for actions/onclick
    const hasUrl = item.url && item.url.trim().length > 0;
    const menuItem = document.createElement(hasUrl ? 'a' : 'button');
    menuItem.className = 'dropdown-menu-item';
    menuItem.setAttribute('role', 'menuitem');

    if (hasUrl) {
      menuItem.href = item.url;
      // Open in new tab if target specified or if external URL
      const isExternal = item.url.startsWith('http://') || item.url.startsWith('https://');
      menuItem.target = item.target || (isExternal ? '_blank' : '_self');
    } else {
      // Handle internal action/onclick
      menuItem.addEventListener('click', (e) => {
        e.preventDefault();
        this.handleMenuItemClick(item);
      });
    }

    // Icon
    if (item.icon) {
      const icon = document.createElement('span');
      icon.className = 'dropdown-menu-item-icon';
      icon.textContent = item.icon;
      menuItem.appendChild(icon);
    }

    // Label
    const label = document.createElement('span');
    label.className = 'dropdown-menu-item-label';
    label.textContent = item.label;
    menuItem.appendChild(label);

    // Badge
    if (item.badge) {
      const badge = document.createElement('span');
      badge.className = 'dropdown-menu-item-badge';
      badge.textContent = item.badge;
      menuItem.appendChild(badge);
    }

    // Shortcut
    if (item.shortcut) {
      const shortcut = document.createElement('span');
      shortcut.className = 'dropdown-menu-item-shortcut';
      shortcut.textContent = item.shortcut;
      menuItem.appendChild(shortcut);
    }

    // Special styling
    if (item.id === 'logout' || item.label.toLowerCase().includes('logout')) {
      menuItem.classList.add('danger');
    }

    return menuItem;
  },

  /**
   * Handle menu item click
   */
  handleMenuItemClick(item) {
    // Priority: action > onclick > url
    if (item.action) {
      // Handle built-in actions
      if (item.action === 'showProfile') {
        if (window.AgentSystem && window.AgentSystem.UserProfile) {
          window.AgentSystem.UserProfile.showProfile();
        } else {
          console.error('UserProfile module not loaded');
        }
      } else if (item.action === 'showSettings') {
        if (window.AgentSystem && window.AgentSystem.UserProfile) {
          window.AgentSystem.UserProfile.showSettings();
        } else {
          console.error('UserProfile module not loaded');
        }
      } else if (item.action === 'openPanel') {
        // Open plugin panel in floating panel
        if ((item.panel_id || item.panel_endpoint) && window.AgentSystem && window.AgentSystem.PluginManager) {
          // Check if this is a tabbed panel (has panel_tabs array)
          if (item.panel_tabs && item.panel_tabs.length > 1) {
            // Create a tabbed panel
            const tabbedPlugin = {
              id: item.panel_id || item.id,
              panel_title: item.panel_title || item.label || 'Panel',
              panel_type: 'tabbed-iframe',
              description: item.tooltip || '',
              tabs: item.panel_tabs,
              // Default to the clicked instance's tab
              activeTab: item.instance_id || item.panel_tabs[0].instance_id
            };
            window.AgentSystem.PluginManager.toggleTabbedPanel(tabbedPlugin);
          } else {
            // Create a pseudo-plugin object from the panel_id or panel_endpoint
            const pseudoPlugin = {
              id: item.panel_id || item.id,
              panel_title: item.panel_title || item.label || 'Panel',
              panel_endpoint: item.panel_endpoint || `/plugins/${item.panel_id}/`,
              panel_type: 'iframe',
              description: item.tooltip || ''
            };
            
            // Use PluginManager's togglePluginPanel method
            window.AgentSystem.PluginManager.togglePluginPanel(pseudoPlugin);
          }
        } else {
          console.error('Panel ID or endpoint not provided, or PluginManager not available');
        }
      } else if (typeof window[item.action] === 'function') {
        // Call global function
        window[item.action]();
      } else {
        console.warn(`Action function ${item.action} not found`);
      }
    } else if (item.onclick) {
      // Legacy onclick support
      if (item.onclick === 'handleLogout') {
        // Call the global handleLogout function which handles UI updates
        if (typeof window.handleLogout === 'function') {
          window.handleLogout();
        } else {
          console.error('handleLogout function not found');
        }
      } else if (typeof window[item.onclick] === 'function') {
        window[item.onclick]();
      } else {
        console.warn(`Function ${item.onclick} not found`);
      }
    } else if (item.url && !item.url.startsWith('http')) {
      // Internal URL (not external http/https)
      window.location.href = item.url;
    }

    // Close menu
    this.closeMenu();
  },

  /**
   * Toggle menu open/close
   */
  toggleMenu(menuId) {
    if (this.openMenuId === menuId) {
      this.closeMenu();
    } else {
      this.openMenu(menuId);
    }
  },

  /**
   * Open a specific menu
   */
  openMenu(menuId) {
    // Close any open menu first
    if (this.openMenuId) {
      this.closeMenu();
    }

    const menuData = this.menus.get(menuId);
    if (!menuData) return;

    menuData.btnElement?.classList.add('open');
    menuData.btnElement?.setAttribute('aria-expanded', 'true');
    
    // Position panel under button (portal mode)
    this.positionPanel(menuData.btnElement, menuData.panelElement);
    
    menuData.panelElement?.classList.add('open');
    this.openMenuId = menuId;
    
    // Check if menu needs multi-column layout
    this.checkMenuOverflow(menuData.panelElement);
  },
  
  /**
   * Position dropdown panel under button (for portal mode)
   */
  positionPanel(button, panel) {
    if (!button || !panel) return;
    
    const btnRect = button.getBoundingClientRect();
    const alignLeft = panel.dataset.alignLeft === 'true';
    
    // Position below button
    panel.style.position = 'fixed';
    panel.style.top = `${btnRect.bottom + 8}px`;
    
    if (alignLeft) {
      panel.style.left = `${btnRect.left}px`;
      panel.style.right = 'auto';
    } else {
      panel.style.right = `${window.innerWidth - btnRect.right}px`;
      panel.style.left = 'auto';
    }
  },
  
  /**
   * Check if menu overflows viewport and apply multi-column if needed
   */
  checkMenuOverflow(panel) {
    if (!panel) return;
    
    const inner = panel.querySelector('.dropdown-menu-panel-inner');
    if (!inner) return;
    
    // Reset any previous multi-column state
    inner.classList.remove('multi-column');
    inner.style.maxHeight = '';
    inner.style.columnCount = '';
    
    // Get measurements
    const viewportHeight = window.innerHeight;
    const panelRect = panel.getBoundingClientRect();
    const maxAllowedHeight = viewportHeight - panelRect.top - 20; // 20px margin from bottom
    
    // If content is taller than available space, use multi-column
    if (inner.scrollHeight > maxAllowedHeight) {
      const columns = Math.ceil(inner.scrollHeight / maxAllowedHeight);
      inner.classList.add('multi-column');
      inner.style.maxHeight = maxAllowedHeight + 'px';
      inner.style.columnCount = columns;
    }
  },

  /**
   * Close currently open menu
   */
  closeMenu() {
    if (!this.openMenuId) return;

    const menuData = this.menus.get(this.openMenuId);
    if (menuData) {
      menuData.btnElement?.classList.remove('open');
      menuData.btnElement?.setAttribute('aria-expanded', 'false');
      menuData.panelElement?.classList.remove('open');
    }

    this.openMenuId = null;
  },

  /**
   * Setup global event listeners
   */
  setupGlobalListeners() {
    // Close menu when clicking outside
    document.addEventListener('click', (e) => {
      if (this.openMenuId) {
        const openMenu = this.menus.get(this.openMenuId);
        const container = openMenu?.btnElement?.closest('.dropdown-menu-container');
        
        if (container && !container.contains(e.target)) {
          this.closeMenu();
        }
      }
    });

    // Close menu on Escape key
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.openMenuId) {
        this.closeMenu();
      }
    });
  },

  /**
   * Reload menus (e.g., after auth state change)
   */
  async reload() {
    // Clear existing menus
    this.menus.forEach((menuData) => {
      menuData.btnElement?.closest('.dropdown-menu-container')?.remove();
    });
    this.menus.clear();
    this.openMenuId = null;

    // Reload
    try {
      await this.loadMenuData();
      this.renderMenus();
    } catch (error) {
      console.error('Failed to reload menus:', error);
    }
  }
};

// Export for modules
if (typeof module !== 'undefined' && module.exports) {
  module.exports = window.AgentSystem.DropdownMenu;
}

// Global helper for logout
window.handleLogout = async function() {
  if (!window.authManager) {
    console.error('authManager not available');
    return;
  }
  
  try {
    // Perform logout
    await window.authManager.logout();
    
    // Clear session data from storage (important: sessions are user-specific!)
    sessionStorage.removeItem('lastSessionId');
    sessionStorage.removeItem('currentSessionId');
    localStorage.removeItem('lastSessionId');
    localStorage.removeItem('currentSessionId');
    
    // Clear active request tracking
    sessionStorage.removeItem('activeRequestId');
    sessionStorage.removeItem('activeRequestTask');
    sessionStorage.removeItem('activeRequestTimestamp');
    
    // Reload page to reset all UI state (cleanest way to handle role-based UI)
    window.location.reload();
    
  } catch (error) {
    console.error('Logout failed:', error);
    // Force reload as fallback
    window.location.reload();
  }
};
