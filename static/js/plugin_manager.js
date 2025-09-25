// Plugin Manager Module
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.PluginManager = {
  plugins: new Map(),
  
  async init() {
    console.log('Initializing Plugin Manager...');
    try {
      await this.loadPlugins();
      this.createPluginButtons();
    } catch (error) {
      console.error('Failed to initialize Plugin Manager:', error);
    }
  },
  
  async loadPlugins() {
    try {
      const response = await fetch('/api/plugins/ui');
      if (!response.ok) {
        throw new Error(`Failed to fetch plugins: ${response.status}`);
      }
      
      const plugins = await response.json();
      console.log(`Loaded ${plugins.length} plugins with UI:`, plugins);
      
      // Store plugins in map
      this.plugins.clear();
      plugins.forEach(plugin => {
        this.plugins.set(plugin.id, plugin);
      });
      
    } catch (error) {
      console.error('Error loading plugins:', error);
      throw error;
    }
  },
  
  createPluginButtons() {
    const headerButtons = document.querySelector('.header-buttons');
    if (!headerButtons) {
      console.error('Header buttons container not found');
      return;
    }
    
    // Find the comment marker for plugin buttons
    const commentMarker = Array.from(headerButtons.childNodes).find(
      node => node.nodeType === Node.COMMENT_NODE && 
      node.textContent.trim().includes('Plugin buttons will be dynamically inserted here')
    );
    
    // Create buttons for each plugin
    this.plugins.forEach((plugin, id) => {
      const button = this.createPluginButton(plugin);
      
      // Insert before the comment marker or at the end
      if (commentMarker) {
        headerButtons.insertBefore(button, commentMarker);
      } else {
        headerButtons.appendChild(button);
      }
    });
  },
  
  createPluginButton(plugin) {
    const button = document.createElement('button');
    button.id = `${plugin.id}ToggleBtn`;
    button.className = 'header-button plugin-button';
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('title', plugin.description || `Show ${plugin.panel_title}`);
    
    // Set button text with optional icon
    const buttonText = plugin.button_icon 
      ? `${plugin.button_icon} ${plugin.button_text}`
      : plugin.button_text;
    button.textContent = buttonText;
    
    // Add click handler
    button.addEventListener('click', () => {
      this.togglePluginPanel(plugin);
    });
    
    return button;
  },
  
  togglePluginPanel(plugin) {
    const panelId = `floating${plugin.id}Panel`;
    
    window.AgentSystem.PanelManager.togglePanel(panelId, () => {
      this.showPluginPanel(plugin);
    });
  },
  
  async showPluginPanel(plugin) {
    console.log(`Showing panel for plugin: ${plugin.id}`);
    
    try {
      // Create panel with loading state
      const panel = window.AgentSystem.PanelManager.createPanel(
        `floating${plugin.id}Panel`,
        plugin.panel_title,
        '<div class="loading">Loading plugin content...</div>',
        'plugin-panel'
      );
      
      // Load plugin content based on panel type
      if (plugin.panel_type === 'iframe') {
        await this.loadIframeContent(panel, plugin);
      } else {
        await this.loadFetchContent(panel, plugin);
      }
      
    } catch (error) {
      console.error(`Error showing plugin panel for ${plugin.id}:`, error);
      
      // Show error in panel
      const panel = window.AgentSystem.PanelManager.createPanel(
        `floating${plugin.id}Panel`,
        plugin.panel_title,
        `<div class="error">Failed to load plugin content: ${error.message}</div>`,
        'plugin-panel error'
      );
    }
  },
  
  async loadIframeContent(panel, plugin) {
    const iframe = document.createElement('iframe');
    iframe.src = plugin.panel_endpoint;
    iframe.style.width = '100%';
    iframe.style.height = '100%';
    iframe.style.border = 'none';
    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
    
    // Replace panel content with iframe
    const contentDiv = panel.querySelector('.panel-content') || panel.querySelector('.floating-panel-body');
    if (contentDiv) {
      contentDiv.innerHTML = '';
      contentDiv.appendChild(iframe);
    }
  },
  
  async loadFetchContent(panel, plugin) {
    try {
      const response = await fetch(plugin.panel_endpoint);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      
      const content = await response.text();
      
      // Replace panel content
      const contentDiv = panel.querySelector('.panel-content') || panel.querySelector('.floating-panel-body');
      if (contentDiv) {
        contentDiv.innerHTML = content;
        
        // Execute any scripts in the loaded content
        this.executeScripts(contentDiv);
        
        // Initialize plugin if it has an init function
        if (window.AgentSystem[plugin.id]) {
          const pluginModule = window.AgentSystem[plugin.id];
          if (typeof pluginModule.init === 'function') {
            pluginModule.init();
          }
        }
      }
      
    } catch (error) {
      console.error(`Error fetching content for ${plugin.id}:`, error);
      throw error;
    }
  },
  
  executeScripts(container) {
    // Execute script tags in loaded content
    const scripts = container.querySelectorAll('script');
    scripts.forEach(oldScript => {
      const newScript = document.createElement('script');
      
      // Copy attributes
      Array.from(oldScript.attributes).forEach(attr => {
        newScript.setAttribute(attr.name, attr.value);
      });
      
      // Copy content
      if (oldScript.src) {
        newScript.src = oldScript.src;
      } else {
        newScript.textContent = oldScript.textContent;
      }
      
      // Replace old script with new one
      oldScript.parentNode.replaceChild(newScript, oldScript);
    });
  },
  
  getPlugin(id) {
    return this.plugins.get(id);
  },
  
  getAllPlugins() {
    return Array.from(this.plugins.values());
  }
};