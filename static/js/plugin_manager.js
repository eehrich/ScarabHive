// Plugin Manager Module
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.PluginManager = {
  plugins: new Map(),

  async init() {
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
    const panelId = `floating${plugin.panel_group || plugin.id}Panel`;

    window.AgentSystem.PanelManager.togglePanel(panelId, () => {
      // Check if this is a tabbed panel
      if (plugin.panel_type === 'tabbed-iframe' && plugin.panel_tabs && plugin.panel_tabs.length > 1) {
        this.showTabbedPanel({
          id: plugin.panel_group || plugin.id,
          panel_title: plugin.panel_title,
          tabs: plugin.panel_tabs,
          activeTab: plugin.panel_tabs[0].instance_id
        });
      } else {
        this.showPluginPanel(plugin);
      }
    });
  },

  async showPluginPanel(plugin) {
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

    // Inject scroll prevention script after iframe loads
    this._injectScrollPrevention(iframe);

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
        // If the fetched content is a full HTML document, extract just the body innerHTML
        if (/<!doctype html>/i.test(content)) {
          try {
            const parser = new DOMParser();
            const doc = parser.parseFromString(content, 'text/html');
            // Collect head styles so we can inject them explicitly (the later code moves link/style from tempDiv)
            const headLinks = Array.from(doc.head.querySelectorAll('link[rel="stylesheet"]'));
            const headStyles = Array.from(doc.head.querySelectorAll('style'));

            // Extract external scripts that need to be loaded globally BEFORE shadow DOM
            const externalScripts = Array.from(doc.body.querySelectorAll('script[src]'));

            // Load external scripts globally first (they register modules in window.AgentSystem)
            if (externalScripts.length > 0) {
              console.log(`Preloading ${externalScripts.length} external script(s) for ${plugin.id}`);
              await Promise.all(externalScripts.map(scriptEl => {
                return new Promise((resolve, reject) => {
                  const script = document.createElement('script');
                  script.src = scriptEl.src;
                  script.onload = () => {
                    console.log(`Loaded external script: ${scriptEl.src}`);
                    resolve();
                  };
                  script.onerror = () => {
                    console.error(`Failed to load script: ${scriptEl.src}`);
                    reject(new Error(`Failed to load ${scriptEl.src}`));
                  };
                  document.head.appendChild(script);
                });
              }));
            }

            // Build body HTML first
            tempDiv.innerHTML = doc.body ? doc.body.innerHTML : content;
            // Prepend collected head resources to preserve order relative to body content
            const headContainer = document.createElement('div');
            headLinks.forEach(l => headContainer.appendChild(l.cloneNode(true)));
            headStyles.forEach(s => headContainer.appendChild(s.cloneNode(true)));
            tempDiv.prepend(headContainer);
          } catch (e) {
            console.warn('Failed to parse full document plugin content, falling back to raw HTML:', e);
            tempDiv.innerHTML = content;
          }
        } else {
          tempDiv.innerHTML = content;
        }

        // Create a loading wrapper to hide content until styles load
        const loadingWrapper = document.createElement('div');
        loadingWrapper.style.cssText = 'display: flex; align-items: center; justify-content: center; height: 100%; color: #c9d1d9; background: var(--bg-darkest, #0a0e13);';
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
        // External script: preserve as-is so the browser loads it normally
        newScript.src = oldScript.src;
      } else {
        const isModule = (oldScript.getAttribute('type') || '').toLowerCase() === 'module';
        let originalContent = oldScript.textContent || '';

        // Wrap in an IIFE that safely acquires the shadow root. We DO NOT rewrite document.querySelector now to avoid
        // breaking logic that depends on the global document (e.g. multiline tooltip detection that calculates positions).
        // Instead we simply expose a local shadowRoot variable for plugin authors to opt-in to using.
        const wrapped = `\n(function(){\n  try {\n    var __current = document.currentScript;\n    var __root = (__current && typeof __current.getRootNode === 'function') ? __current.getRootNode() : null;\n    // Local helper: points to shadow root if available, else falls back to document.\n    var shadowRoot = (__root instanceof ShadowRoot) ? __root : document;\n    ${originalContent}\n  } catch(e) {\n    console.error('Plugin script execution error for ${plugin.id}:', e);\n  }\n})();\n`;

        if (isModule) {
          // For modules we cannot simply wrap with function + preserve import/export; leave content unchanged
          // but still provide a minimal shim declaring a shadowRoot variable if possible.
          newScript.type = 'module';
          newScript.textContent = `// Module script executed in plugin shadow context.\n// We cannot safely wrap ES modules (would break import/export), so we only predefine a shadowRoot variable.\nconst shadowRoot = document.currentScript && document.currentScript.getRootNode && document.currentScript.getRootNode() instanceof ShadowRoot ? document.currentScript.getRootNode() : document;\n${originalContent}`;
        } else {
          newScript.textContent = wrapped;
        }
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
  },

  /**
   * Toggle a tabbed panel (multiple instances in tabs within one panel)
   * @param {Object} plugin - Plugin config with tabs array
   */
  toggleTabbedPanel(plugin) {
    const panelId = `floating${plugin.id}Panel`;
    
    window.AgentSystem.PanelManager.togglePanel(panelId, () => {
      this.showTabbedPanel(plugin);
    });
  },

  /**
   * Show a tabbed panel with multiple plugin instances as tabs
   * @param {Object} plugin - Plugin config with tabs array and activeTab
   */
  async showTabbedPanel(plugin) {
    try {
      // Create panel with loading state
      const panel = window.AgentSystem.PanelManager.createPanel(
        `floating${plugin.id}Panel`,
        plugin.panel_title,
        '<div class="loading">Loading...</div>',
        'plugin-panel tabbed-panel'
      );

      // Build tab bar and content container
      const contentDiv = panel.querySelector('.panel-content') || panel.querySelector('.floating-panel-body');
      if (!contentDiv) return;

      // Clear and rebuild content
      contentDiv.innerHTML = '';

      // Create tab bar
      const tabBar = document.createElement('div');
      tabBar.className = 'tabbed-panel-tabs';
      tabBar.style.cssText = 'display: flex; gap: 2px; background: #1e1e1e; padding: 4px 4px 0 4px; border-bottom: 1px solid #3e3e42; margin-bottom: 0;';

      // Create iframe container
      const iframeContainer = document.createElement('div');
      iframeContainer.className = 'tabbed-panel-content';
      iframeContainer.style.cssText = 'flex: 1; display: flex; overflow: hidden;';

      // Create tabs
      plugin.tabs.forEach((tab, index) => {
        const tabBtn = document.createElement('button');
        tabBtn.className = 'tabbed-panel-tab';
        tabBtn.dataset.instanceId = tab.instance_id;
        tabBtn.innerHTML = `${tab.icon || ''} ${tab.label}`.trim();
        tabBtn.style.cssText = `
          padding: 8px 16px;
          border: none;
          background: ${tab.instance_id === plugin.activeTab ? '#2d2d30' : '#252526'};
          color: ${tab.instance_id === plugin.activeTab ? '#fff' : '#858585'};
          cursor: pointer;
          border-radius: 4px 4px 0 0;
          font-size: 13px;
          transition: background 0.2s;
          outline: none;
        `;

        tabBtn.addEventListener('click', () => {
          this.switchTab(plugin, tab, tabBar, iframeContainer);
        });

        tabBtn.addEventListener('mouseenter', () => {
          if (tab.instance_id !== this._activeTabbedPanelTab) {
            tabBtn.style.background = '#323232';
          }
        });
        tabBtn.addEventListener('mouseleave', () => {
          if (tab.instance_id !== this._activeTabbedPanelTab) {
            tabBtn.style.background = '#252526';
          }
        });

        tabBar.appendChild(tabBtn);
      });

      contentDiv.style.cssText = 'display: flex; flex-direction: column; height: 100%;';
      contentDiv.appendChild(tabBar);
      contentDiv.appendChild(iframeContainer);

      // Load the initially active tab
      const activeTabData = plugin.tabs.find(t => t.instance_id === plugin.activeTab) || plugin.tabs[0];
      this._activeTabbedPanelTab = activeTabData.instance_id;
      this.loadTabContent(activeTabData, iframeContainer);

    } catch (error) {
      console.error(`Error showing tabbed panel for ${plugin.id}:`, error);
    }
  },

  /**
   * Switch to a different tab in a tabbed panel
   */
  switchTab(plugin, tab, tabBar, iframeContainer) {
    // Update tab styles
    tabBar.querySelectorAll('.tabbed-panel-tab').forEach(btn => {
      const isActive = btn.dataset.instanceId === tab.instance_id;
      btn.style.background = isActive ? '#2d2d30' : '#252526';
      btn.style.color = isActive ? '#fff' : '#858585';
    });

    this._activeTabbedPanelTab = tab.instance_id;
    this.loadTabContent(tab, iframeContainer);
  },

  /**
   * Inject scroll prevention script into iframe
   */
  _injectScrollPrevention(iframe) {
    iframe.addEventListener('load', () => {
      try {
        const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
        if (iframeDoc) {
          const script = iframeDoc.createElement('script');
          script.textContent = `
            // Prevent parent page scroll when scrolling in this panel (iframe)
            document.addEventListener('wheel', function(e) {
              // Check if body or documentElement is scrollable
              const docEl = document.documentElement;
              const body = document.body;
              const docScrollable = docEl.scrollHeight > docEl.clientHeight;
              const bodyScrollable = body.scrollHeight > body.clientHeight;
              
              // Find closest scrollable ancestor (including checking elements)
              let target = e.target;
              let foundScrollable = false;
              
              while (target && target !== body && target !== docEl) {
                const style = window.getComputedStyle(target);
                const overflowY = style.overflowY;
                const isScrollable = (overflowY === 'auto' || overflowY === 'scroll') && target.scrollHeight > target.clientHeight;
                
                if (isScrollable) {
                  foundScrollable = true;
                  const atTop = target.scrollTop <= 0;
                  const atBottom = target.scrollHeight - target.scrollTop <= target.clientHeight + 1;
                  
                  if ((e.deltaY < 0 && !atTop) || (e.deltaY > 0 && !atBottom)) {
                    return; // Allow normal scroll within element
                  }
                  // At boundary of this element - prevent and stop
                  e.preventDefault();
                  return;
                }
                target = target.parentElement;
              }
              
              // Check if body/document itself is scrollable
              if (docScrollable || bodyScrollable) {
                const scrollTop = docEl.scrollTop || body.scrollTop;
                const scrollHeight = Math.max(docEl.scrollHeight, body.scrollHeight);
                const clientHeight = docEl.clientHeight;
                const atTop = scrollTop <= 0;
                const atBottom = scrollHeight - scrollTop <= clientHeight + 1;
                
                if ((e.deltaY < 0 && !atTop) || (e.deltaY > 0 && !atBottom)) {
                  return; // Allow normal page scroll
                }
              }
              
              // At boundary or no scrollable content - prevent parent scroll
              e.preventDefault();
            }, { passive: false });
          `;
          iframeDoc.body.appendChild(script);
        }
      } catch (err) {
        // Cross-origin iframes will throw - ignore silently
        console.debug('Could not inject scroll prevention into iframe:', err.message);
      }
    });
  },

  /**
   * Load content for a tab (creates/reuses iframe)
   */
  loadTabContent(tab, container) {
    // Clear existing content
    container.innerHTML = '';

    // Create iframe for this tab
    const iframe = document.createElement('iframe');
    iframe.src = tab.endpoint;
    iframe.style.cssText = 'width: 100%; height: 100%; border: none;';
    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');

    // Inject scroll prevention
    this._injectScrollPrevention(iframe);

    container.appendChild(iframe);
  }
};