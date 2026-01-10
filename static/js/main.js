// Main AgentSystem initialization - modular but without ES6 imports

document.addEventListener('DOMContentLoaded', function() {
  
  // Wait for all modules to be loaded
  if (typeof window.AgentSystem === 'undefined') {
    console.error('AgentSystem namespace not found');
    return;
  }
  
  // Check if all required modules are loaded
  const requiredModules = ['PanelManager', 'PluginManager', 'MCP', 'Status', 'Selectors'];
  const missingModules = requiredModules.filter(module => !window.AgentSystem[module]);
  
  if (missingModules.length > 0) {
    console.error('Missing modules:', missingModules);
    return;
  }

  // ==========================================
  // Mobile Menu Toggle - Intelligent Breakpoint
  // ==========================================
  const menuToggleBtn = document.getElementById('menuToggleBtn');
  const headerButtons = document.getElementById('headerButtons');
  const headerElement = document.querySelector('header');
  const headerLeft = document.querySelector('.header-left');
  const headerUserMenu = document.querySelector('.header-user-menu');
  
  // Check if header elements overflow and need hamburger menu
  function checkHeaderOverflow() {
    if (!headerElement || !headerButtons || !menuToggleBtn) return;
    
    // Hide during measurement to prevent visual flicker
    headerButtons.style.visibility = 'hidden';
    menuToggleBtn.style.visibility = 'hidden';
    
    // Temporarily show buttons normally to measure
    headerButtons.classList.remove('collapsed');
    menuToggleBtn.classList.remove('visible');
    
    // Calculate available space vs required space
    const headerWidth = headerElement.clientWidth;
    const headerPadding = 24; // left + right padding
    const gap = 8; // gap between elements
    
    // Get widths of all header elements
    const leftWidth = headerLeft ? headerLeft.scrollWidth : 0;
    const buttonsWidth = headerButtons.scrollWidth;
    const userMenuWidth = headerUserMenu ? headerUserMenu.scrollWidth : 0;
    
    const totalNeeded = leftWidth + buttonsWidth + userMenuWidth + (gap * 3) + headerPadding;
    const availableSpace = headerWidth;
    
    // If not enough space, show hamburger menu
    if (totalNeeded > availableSpace) {
      headerButtons.classList.add('collapsed');
      menuToggleBtn.classList.add('visible');
    } else {
      headerButtons.classList.remove('collapsed');
      menuToggleBtn.classList.remove('visible');
      closeMobileMenu();
    }
    
    // Make visible again (explicitly override CSS initial hidden state)
    headerButtons.style.visibility = 'visible';
    menuToggleBtn.style.visibility = 'visible';
  }
  
  function toggleMobileMenu(e) {
    if (e) e.stopPropagation();
    const isOpen = menuToggleBtn.getAttribute('aria-expanded') === 'true';
    menuToggleBtn.setAttribute('aria-expanded', !isOpen);
    headerButtons.classList.toggle('open', !isOpen);
  }
  
  function closeMobileMenu() {
    if (!headerButtons.classList.contains('open')) return;
    menuToggleBtn.setAttribute('aria-expanded', 'false');
    headerButtons.classList.remove('open');
  }
  
  if (menuToggleBtn) {
    menuToggleBtn.addEventListener('click', toggleMobileMenu);
  }
  
  // Close menu when clicking outside
  document.addEventListener('click', function(e) {
    if (!headerButtons.classList.contains('open')) return;
    
    // Don't close if clicking inside the menu or on the toggle button
    if (headerButtons.contains(e.target) || menuToggleBtn.contains(e.target)) {
      return;
    }
    closeMobileMenu();
  });
  
  // Close menu when clicking a button inside it (after the action)
  if (headerButtons) {
    headerButtons.addEventListener('click', function(e) {
      if (e.target.tagName === 'BUTTON' || e.target.closest('button')) {
        // Small delay to allow the button action to complete
        setTimeout(closeMobileMenu, 150);
      }
    });
  }
  
  // Check overflow on resize
  let resizeTimeout;
  window.addEventListener('resize', function() {
    clearTimeout(resizeTimeout);
    resizeTimeout = setTimeout(checkHeaderOverflow, 100);
  });
  
  // Watch for new buttons being added to header
  if (headerButtons) {
    const observer = new MutationObserver(function() {
      // Debounce the check
      clearTimeout(resizeTimeout);
      resizeTimeout = setTimeout(checkHeaderOverflow, 100);
    });
    observer.observe(headerButtons, { childList: true, subtree: true });
  }
  
  // Initial check immediately to prevent visual flicker on page load
  checkHeaderOverflow();
  // Re-check after a short delay (let other modules add buttons)
  setTimeout(checkHeaderOverflow, 300);
  // Re-check after plugins load (they might add more buttons)
  setTimeout(checkHeaderOverflow, 1000);
  setTimeout(checkHeaderOverflow, 2000);
  
  // Close menu on Escape key
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && headerButtons.classList.contains('open')) {
      closeMobileMenu();
    }
  });

  // Initialize selector module for agent and LLM profile selection
  if (window.AgentSystem.Selectors && typeof window.AgentSystem.Selectors.init === 'function') {
    try {
      window.AgentSystem.Selectors.init();
    } catch (err) {
      console.error('Selectors.init() failed', err);
    }
  } else {
    console.warn('Selectors module not available');
  }
  
  // Initialize button event listeners
  const statusBtn = document.getElementById('statusToggleBtn');
  const mcpBtn = document.getElementById('mcpToggleBtn');
  
  if (statusBtn) {
    statusBtn.addEventListener('click', function() {
      window.AgentSystem.PanelManager.togglePanel('floatingStatusPanel', () => window.AgentSystem.Status.showPanel());
    });
  }
  
  if (mcpBtn) {
    mcpBtn.addEventListener('click', function() {
      window.AgentSystem.PanelManager.togglePanel('floatingMCPPanel', () => window.AgentSystem.MCP.showPanel());
    });
  }
  
  // Initialize plugin manager to load dynamic plugin buttons
  if (window.AgentSystem.PluginManager && typeof window.AgentSystem.PluginManager.init === 'function') {
    try {
      window.AgentSystem.PluginManager.init();
    } catch (err) {
      console.error('PluginManager.init() failed', err);
    }
  } else {
    console.warn('PluginManager not available; plugin buttons disabled');
  }
  
  // Initialize dropdown menu system (wait for auth to be ready)
  if (window.AgentSystem.DropdownMenu && typeof window.AgentSystem.DropdownMenu.init === 'function') {
    try {
      // Wait for auth.js to verify token before initializing menus
      if (window.authManager && window.authManager.token) {
        // Auth token exists, wait for verification to complete
        window.authManager.verifyToken().finally(() => {
          window.AgentSystem.DropdownMenu.init();
        });
      } else {
        // No token, init immediately
        window.AgentSystem.DropdownMenu.init();
      }
    } catch (err) {
      console.error('DropdownMenu.init() failed', err);
    }
  } else {
    console.warn('DropdownMenu not available; dropdown menus disabled');
  }
  
  // Initialize chat form
  // Initialize file upload module
  if (window.fileUploadModule && typeof window.fileUploadModule.init === 'function') {
    try {
      window.fileUploadModule.init();
    } catch (err) {
      console.error('fileUploadModule.init() failed', err);
    }
  } else {
    console.warn('fileUploadModule not available; file upload disabled');
  }
  
  // Initialize chat module (extracted)
  if (window.chatModule && typeof window.chatModule.init === 'function') {
    try {
      window.chatModule.init();
    } catch (err) {
      console.error('chatModule.init() failed', err);
    }
  } else {
    console.warn('chatModule not available; chat features disabled');
  }
  
  
});