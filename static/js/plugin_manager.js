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

      // Replace panel content with Shadow DOM encapsulation
      const contentDiv = panel.querySelector('.panel-content') || panel.querySelector('.floating-panel-body');
      if (contentDiv) {
        // Make content div non-focusable
        contentDiv.tabIndex = -1;
        contentDiv.style.outline = 'none';

        // Create shadow DOM for complete CSS/JS isolation
        const shadowRoot = contentDiv.attachShadow({ mode: 'open' });

        // Parse the HTML content to extract styles and scripts
        const tempDiv = document.createElement('div');
        tempDiv.innerHTML = content;

        // Create a loading wrapper to hide content until styles load
        const loadingWrapper = document.createElement('div');
        loadingWrapper.style.cssText = 'display: flex; align-items: center; justify-content: center; height: 100%; color: #c9d1d9; background: #0d1117;';
        loadingWrapper.textContent = 'Loading plugin...';
        shadowRoot.appendChild(loadingWrapper);

        // Move all stylesheets to shadow root and wait for them to load
        const links = tempDiv.querySelectorAll('link[rel="stylesheet"]');
        const styleLoadPromises = [];

        links.forEach(link => {
          const newLink = document.createElement('link');
          newLink.rel = 'stylesheet';
          newLink.href = link.href;

          const loadPromise = new Promise((resolve, reject) => {
            newLink.onload = resolve;
            newLink.onerror = reject;
            setTimeout(reject, 5000); // 5s timeout
          });

          styleLoadPromises.push(loadPromise);
          shadowRoot.appendChild(newLink);
          link.remove();
        });

        // Move all style tags to shadow root
        const styles = tempDiv.querySelectorAll('style');
        styles.forEach(style => {
          shadowRoot.appendChild(style.cloneNode(true));
          style.remove();
        });

        // Wait for all stylesheets to load before showing content
        try {
          await Promise.all(styleLoadPromises);
        } catch (error) {
          console.warn('Some stylesheets failed to load, proceeding anyway:', error);
        }

        // Remove loading indicator and add actual content
        shadowRoot.removeChild(loadingWrapper);
        shadowRoot.appendChild(tempDiv);

        // Prevent focus events from bubbling up to the panel container
        contentDiv.addEventListener('focus', (e) => {
          e.stopPropagation();
        }, true);
        contentDiv.addEventListener('focusin', (e) => {
          e.stopPropagation();
        }, true);

        // Execute scripts within shadow DOM context
        this.executeScriptsInShadow(shadowRoot, plugin);

        // Initialize plugin if it has an init function
        // Use a small delay to ensure all scripts are fully loaded and executed
        setTimeout(() => {
          if (window.AgentSystem && window.AgentSystem[plugin.id]) {
            const pluginModule = window.AgentSystem[plugin.id];
            if (typeof pluginModule.init === 'function') {
              console.log(`Initializing plugin ${plugin.id} with Shadow DOM`);
              pluginModule.init(shadowRoot);
            }
          } else {
            console.warn(`Plugin module ${plugin.id} not found in window.AgentSystem:`, window.AgentSystem);
          }
        }, 50);
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

  executeScriptsInShadow(shadowRoot, plugin) {
    // Execute script tags within shadow DOM context
    const scripts = shadowRoot.querySelectorAll('script');
    scripts.forEach(oldScript => {
      const newScript = document.createElement('script');

      // Copy attributes
      Array.from(oldScript.attributes).forEach(attr => {
        newScript.setAttribute(attr.name, attr.value);
      });

      // Copy content and modify context for shadow DOM
      if (oldScript.src) {
        newScript.src = oldScript.src;
      } else {
        let scriptContent = oldScript.textContent;

        // Modify script to work within shadow DOM context
        // Replace document.querySelector calls to use shadowRoot
        scriptContent = scriptContent.replace(
          /document\.querySelector\s*\(/g,
          'shadowRoot.querySelector('
        );
        scriptContent = scriptContent.replace(
          /document\.querySelectorAll\s*\(/g,
          'shadowRoot.querySelectorAll('
        );

        // Inject shadowRoot reference at the beginning
        scriptContent = `
          (function() {
            const shadowRoot = document.currentScript.getRootNode();
            ${scriptContent}
          })();
        `;

        newScript.textContent = scriptContent;
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