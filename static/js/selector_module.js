// Agent and LLM Profile Selector Module
(function (global) {
  const SelectorModule = {};

  let currentAgent = null;
  let currentLLMProfile = null;

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

  /**
   * Load available agents from API and populate dropdown
   */
  async function loadAgents(selectElement) {
    try {
      const response = await fetch('/agents');
      const data = await response.json();

      if (!data.agents || data.agents.length === 0) {
        console.warn('No agents available');
        return;
      }

      // Clear existing options
      selectElement.innerHTML = '';

      // Add agents to dropdown
      data.agents.forEach(agentName => {
        const option = document.createElement('option');
        option.value = agentName;
        option.textContent = agentName;
        selectElement.appendChild(option);
      });

      // Set initial selection
      if (data.agents.length > 0) {
        currentAgent = data.agents[0];
        selectElement.value = currentAgent;
      }

      // Update title to remove "coming soon"
      selectElement.title = 'Select agent';

      console.log(`Loaded ${data.agents.length} agents`);
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
      const response = await fetch('/llm/profiles');
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

      // Set default selection
      if (data.default) {
        currentLLMProfile = data.default;
        selectElement.value = currentLLMProfile;
      } else if (data.profiles.length > 0) {
        currentLLMProfile = data.profiles[0].name;
        selectElement.value = currentLLMProfile;
      }

      // Update title to remove "coming soon"
      selectElement.title = 'Select LLM profile';

      console.log(`Loaded ${data.profiles.length} LLM profiles (default: ${data.default})`);
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
      console.warn('Agent not found in dropdown:', agentName);
      return false;
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
      console.warn('LLM profile not found in dropdown:', profileName);
      return false;
    }
  }

  // Expose public API
  SelectorModule.init = init;
  SelectorModule.getCurrentAgent = getCurrentAgent;
  SelectorModule.getCurrentLLMProfile = getCurrentLLMProfile;
  SelectorModule.setAgent = setAgent;
  SelectorModule.setLLMProfile = setLLMProfile;

  // Register in global namespace
  if (!global.AgentSystem) {
    global.AgentSystem = {};
  }
  global.AgentSystem.Selectors = SelectorModule;

  global.selectorModule = SelectorModule;

})(window);
