// The agent and LLM profile the chat runs with: the lists to choose from, the choice, and the composer's two buttons.
// The dialog to choose in is shell/picker.js; this stays a plain script because the chat scripts read it.
(function (global) {
  const SelectorModule = {};

  /** [{name, description, category, tags}] once loaded, else null */
  let agents = null;
  /** [{name, description, model_ref, max_steps, provider, model, host}] once loaded, else null */
  let profiles = null;
  let defaultAgent = null;
  let defaultLLMProfile = null;
  let currentAgent = null;
  let currentLLMProfile = null;
  // Set before the lists arrived (a reconnect's session restore): applied once they are here
  let pendingAgent = null;
  let pendingLLMProfile = null;
  // A list that could not be loaded stays null like one still on its way: these tell the two apart
  let agentsFailed = false;
  let profilesFailed = false;

  const state = (list, failed) => (list ? 'ready' : failed ? 'failed' : 'loading');
  const EMPTY_LABEL = { loading: 'Loading…', failed: 'unavailable', ready: 'none' };

  function showChoice() {
    const agentLabel = document.getElementById('agentLabel');
    const modelLabel = document.getElementById('modelLabel');
    if (agentLabel) agentLabel.textContent = currentAgent || `Agent: ${EMPTY_LABEL[state(agents, agentsFailed)]}`;
    if (modelLabel) modelLabel.textContent = currentLLMProfile || `Profile: ${EMPTY_LABEL[state(profiles, profilesFailed)]}`;
  }

  /** Every change of either choice, a person's pick or a session's restore: slash commands follow the agent. */
  function announce(kind) {
    showChoice();
    global.dispatchEvent(new CustomEvent('selector:change', { detail: { kind } }));
  }

  async function fetchJson(path) {
    // no-store: the browser must not serve a stale list from its HTTP cache on a normal reload
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    return response.json();
  }

  async function loadAgents() {
    try {
      const data = await fetchJson('/agents');
      const details = new Map((data.details || []).map((d) => [d.name, d]));
      agents = (data.agents || []).map((name) => ({ tags: [], ...details.get(name), name }));
      defaultAgent = data.default || agents[0]?.name || null;
      const wanted = pendingAgent;
      pendingAgent = null;
      currentAgent = has(agents, wanted) ? wanted : has(agents, defaultAgent) ? defaultAgent : agents[0]?.name || null;
    } catch (error) {
      console.error('Failed to load agents:', error);
      agentsFailed = true;
    }
    announce('agent');
  }

  async function loadLLMProfiles() {
    try {
      const data = await fetchJson('/llm/profiles');
      profiles = data.profiles || [];
      defaultLLMProfile = data.default || profiles[0]?.name || null;
      const wanted = pendingLLMProfile;
      pendingLLMProfile = null;
      currentLLMProfile = has(profiles, wanted) ? wanted : has(profiles, defaultLLMProfile) ? defaultLLMProfile : profiles[0]?.name || null;
    } catch (error) {
      console.error('Failed to load LLM profiles:', error);
      profilesFailed = true;
    }
    announce('profile');
  }

  const has = (list, name) => Boolean(name) && Boolean(list) && list.some((item) => item.name === name);

  async function init() {
    showChoice();
    await Promise.all([loadAgents(), loadLLMProfiles()]);
  }

  /**
   * Known: chosen, true. Lists loaded but unknown: the default is chosen, false.
   * Lists not loaded yet: kept for when they are, false.
   */
  function choose(list, name, fallback, setCurrent, setPending, kind) {
    if (!list) {
      setPending(name);
      return false;
    }
    const known = has(list, name);
    // the same rule as at load: a default the list does not hold is no choice either
    const usable = has(list, fallback) ? fallback : list[0]?.name || null;
    if (!known) console.warn(`${kind} "${name}" not found, using "${usable}"`);
    setCurrent(known ? name : usable);
    announce(kind);
    return known;
  }

  SelectorModule.init = init;
  SelectorModule.getCurrentAgent = () => currentAgent;
  SelectorModule.getCurrentLLMProfile = () => currentLLMProfile;
  SelectorModule.defaultAgent = () => defaultAgent;
  SelectorModule.defaultLLMProfile = () => defaultLLMProfile;
  SelectorModule.agents = () => agents || [];
  SelectorModule.profiles = () => profiles || [];
  SelectorModule.hasAgent = (name) => has(agents, name);
  /** 'loading', 'failed' or 'ready', for kind 'agent' or 'profile' */
  SelectorModule.listState = (kind) => (kind === 'agent' ? state(agents, agentsFailed) : state(profiles, profilesFailed));
  SelectorModule.setAgent = (name) => choose(agents, name, defaultAgent,
    (value) => { currentAgent = value; }, (value) => { pendingAgent = value; }, 'agent');
  SelectorModule.setLLMProfile = (name) => choose(profiles, name, defaultLLMProfile,
    (value) => { currentLLMProfile = value; }, (value) => { pendingLLMProfile = value; }, 'profile');

  global.selectorModule = SelectorModule;
})(window);
