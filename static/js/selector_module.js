// Agent and LLM Profile Selector Module
(function (global) {
  const SelectorModule = {};

  let currentAgent = null;
  let currentLLMProfile = null;
  // Pending values to set after dropdowns are loaded (for reconnect race condition)
  let pendingLLMProfile = null;
  let pendingAgent = null;

  /**
   * Initialize the selector dropdowns
   */
  async function init() {
    const agentSelector = document.getElementById('agentSelector');
    const modelSelector = document.getElementById('modelSelector');

    if (!agentSelector || !modelSelector) {
      console.warn('Selector elements not found');
      return;
    }

    // Load and populate agents
    await loadAgents(agentSelector);

    // Load and populate LLM profiles
    await loadLLMProfiles(modelSelector);

    // Set up change event listeners
    agentSelector.addEventListener('change', () => {
      currentAgent = agentSelector.value;
      console.log('Agent changed to:', currentAgent);
    });

    modelSelector.addEventListener('change', () => {
      currentLLMProfile = modelSelector.value;
      console.log('LLM profile changed to:', currentLLMProfile);
    });

    console.log('Selector module initialized');
  }

  // Store default values from API for fallback
  let defaultAgent = null;
  let defaultLLMProfile = null;

  /**
   * Load available agents from API and populate dropdown
   */
  async function loadAgents(selectElement) {
    try {
      // cache:'no-store' — the browser must not serve a stale agent list from
      // its HTTP cache on a normal reload (new agents only appeared after a
      // force reload). The server also sends Cache-Control: no-store now.
      const response = await fetch('/agents', { cache: 'no-store' });
      const data = await response.json();

      if (!data.agents || data.agents.length === 0) {
        console.warn('No agents available');
        return;
      }

      // Store default agent from API
      defaultAgent = data.default || data.agents[0];

      // Clear existing options
      selectElement.innerHTML = '';

      // Add agents to dropdown
      data.agents.forEach(agentName => {
        const option = document.createElement('option');
        option.value = agentName;
        option.textContent = agentName;
        selectElement.appendChild(option);
      });

      // Set initial selection to default agent
      if (data.agents.includes(defaultAgent)) {
        currentAgent = defaultAgent;
        selectElement.value = defaultAgent;
      } else if (data.agents.length > 0) {
        currentAgent = data.agents[0];
        selectElement.value = currentAgent;
      }

      // Update title to remove "coming soon"
      selectElement.title = 'Select agent';

      console.log(`Loaded ${data.agents.length} agents`);
      
      // Apply pending agent if set (from reconnect before dropdown was ready)
      if (pendingAgent) {
        const pendingOption = Array.from(selectElement.options).find(opt => opt.value === pendingAgent);
        if (pendingOption) {
          selectElement.value = pendingAgent;
          currentAgent = pendingAgent;
          console.log('Applied pending agent:', pendingAgent);
        }
        pendingAgent = null;
      }
    } catch (error) {
      console.error('Failed to load agents:', error);
      selectElement.innerHTML = '<option value="">Error loading agents</option>';
    }
  }

  /**
   * Load available LLM profiles from API and populate dropdown
   */
  async function loadLLMProfiles(selectElement) {
    try {
      const response = await fetch('/llm/profiles', { cache: 'no-store' });
      const data = await response.json();

      if (!data.profiles || data.profiles.length === 0) {
        console.warn('No LLM profiles available');
        return;
      }

      // Clear existing options
      selectElement.innerHTML = '';

      // Add profiles to dropdown
      data.profiles.forEach(profile => {
        const option = document.createElement('option');
        option.value = profile.name;
        // Show description in the option text
        option.textContent = `${profile.name} - ${profile.description}`;
        option.title = `Model: ${profile.model_ref}, Max steps: ${profile.max_steps}`;
        selectElement.appendChild(option);
      });

      // Store and set default selection
      defaultLLMProfile = data.default || (data.profiles.length > 0 ? data.profiles[0].name : null);
      if (defaultLLMProfile) {
        currentLLMProfile = defaultLLMProfile;
        selectElement.value = currentLLMProfile;
      } else if (data.profiles.length > 0) {
        currentLLMProfile = data.profiles[0].name;
        selectElement.value = currentLLMProfile;
      }

      // Update title to remove "coming soon"
      selectElement.title = 'Select LLM profile';

      console.log(`Loaded ${data.profiles.length} LLM profiles (default: ${data.default})`);
      
      // Apply pending profile if set (from reconnect before dropdown was ready)
      if (pendingLLMProfile) {
        const pendingOption = Array.from(selectElement.options).find(opt => opt.value === pendingLLMProfile);
        if (pendingOption) {
          selectElement.value = pendingLLMProfile;
          currentLLMProfile = pendingLLMProfile;
          console.log('Applied pending LLM profile:', pendingLLMProfile);
        }
        pendingLLMProfile = null;
      }
    } catch (error) {
      console.error('Failed to load LLM profiles:', error);
      selectElement.innerHTML = '<option value="">Error loading profiles</option>';
    }
  }

  /**
   * Get current agent selection
   */
  function getCurrentAgent() {
    return currentAgent;
  }

  /**
   * Get current LLM profile selection
   */
  function getCurrentLLMProfile() {
    return currentLLMProfile;
  }

  /**
   * Set current agent selection programmatically
   */
  function setAgent(agentName) {
    const agentSelector = document.getElementById('agentSelector');
    if (!agentSelector) {
      console.warn('Agent selector not found');
      return false;
    }
    
    // Check if the agent exists in the dropdown
    const option = Array.from(agentSelector.options).find(opt => opt.value === agentName);
    if (option) {
      agentSelector.value = agentName;
      currentAgent = agentName;
      console.log('Agent set to:', agentName);
      return true;
    } else {
      // Agent not found in dropdown - check if dropdown is loaded
      if (agentSelector.options.length > 0) {
        // Dropdown is loaded but agent not found - use default agent
        const fallbackAgent = defaultAgent || agentSelector.options[0].value;
        console.warn(`Agent "${agentName}" not found, using default agent "${fallbackAgent}"`);
        agentSelector.value = fallbackAgent;
        currentAgent = fallbackAgent;
        return false;
      } else {
        // Dropdown not loaded yet - store as pending
        console.log('Agent dropdown not ready, storing pending:', agentName);
        pendingAgent = agentName;
        return false;
      }
    }
  }

  /**
   * Set current LLM profile selection programmatically
   */
  function setLLMProfile(profileName) {
    const modelSelector = document.getElementById('modelSelector');
    if (!modelSelector) {
      console.warn('LLM profile selector not found');
      return false;
    }
    
    // Check if the profile exists in the dropdown
    const option = Array.from(modelSelector.options).find(opt => opt.value === profileName);
    if (option) {
      modelSelector.value = profileName;
      currentLLMProfile = profileName;
      console.log('LLM profile set to:', profileName);
      return true;
    } else {
      // Profile not found in dropdown - check if dropdown is loaded
      if (modelSelector.options.length > 0) {
        // Dropdown is loaded but profile not found - use default profile
        const fallbackProfile = defaultLLMProfile || modelSelector.options[0].value;
        console.warn(`LLM profile "${profileName}" not found, using default profile "${fallbackProfile}"`);
        modelSelector.value = fallbackProfile;
        currentLLMProfile = fallbackProfile;
        return false;
      } else {
        // Dropdown not loaded yet - store as pending
        console.log('LLM profile dropdown not ready, storing pending:', profileName);
        pendingLLMProfile = profileName;
        return false;
      }
    }
  }

  // Expose public API
  SelectorModule.init = init;
  SelectorModule.getCurrentAgent = getCurrentAgent;
  SelectorModule.getCurrentLLMProfile = getCurrentLLMProfile;
  SelectorModule.setAgent = setAgent;
  SelectorModule.setLLMProfile = setLLMProfile;

  global.selectorModule = SelectorModule;

})(window);
