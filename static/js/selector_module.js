// The agent and LLM profile the chat runs with: the lists to choose from, the choice, and the composer's buttons.
// The dialog to choose in is shell/picker.js; this stays a plain script because the chat scripts read it.
//
// The model follows the agent: a chat runs on the agent's own profile (its chain, its escalation) until a
// person picks another one -- the override -- or sets a thinking level -- the params. Only those go out
// with a run (llm_profile, llm_params). A person's pick of another agent drops both; a session's restore
// takes what the session ran with.
(function (global) {
  const SelectorModule = {};

  /** [{name, description, category, tags, llm_profile}] once loaded, else null */
  let agents = null;
  /** [{name, description, model_ref, max_steps, provider, model, host}] once loaded, else null */
  let profiles = null;
  /** the thinking levels the server takes (llm_params.thinking_level), from /llm/profiles */
  let thinkingLevels = [];
  let defaultAgent = null;
  let defaultLLMProfile = null;
  let currentAgent = null;
  /** a profile a person picked for this chat, null = the agent's own */
  let override = null;
  /** what a person set for the model: {thinking_level} or {} */
  let params = {};
  // Set before the lists arrived (a reconnect's or a session's restore): applied once both are here
  let pendingRestore = null;
  // A list that could not be loaded stays null like one still on its way: these tell the two apart
  let agentsFailed = false;
  let profilesFailed = false;

  const state = (list, failed) => (list ? 'ready' : failed ? 'failed' : 'loading');
  const EMPTY_LABEL = { loading: 'Loading…', failed: 'unavailable', ready: 'none' };
  const has = (list, name) => Boolean(name) && Boolean(list) && list.some((item) => item.name === name);
  const settled = () => state(agents, agentsFailed) !== 'loading' && state(profiles, profilesFailed) !== 'loading';

  /** The profile *agent* runs on by itself, or null when it is not known (or not a profile this server has). */
  function agentDefault(agent = currentAgent) {
    const own = (agents || []).find((a) => a.name === agent)?.llm_profile;
    return own && (!profiles || has(profiles, own)) ? own : null;
  }

  /** Only what the server takes: a stored session may carry more (agent-cli --llm-params). */
  function usableParams(value) {
    const level = value && value.thinking_level;
    // Without the list (it failed to load) a stored level stays: dropping it here, the next message
    // would clear it from the session.
    const known = profilesFailed ? typeof level === 'string' : thinkingLevels.includes(level);
    return typeof level === 'string' && known ? { thinking_level: level } : {};
  }

  function currentProfile() {
    return override || agentDefault();
  }

  function showChoice() {
    const agentLabel = document.getElementById('agentLabel');
    const modelLabel = document.getElementById('modelLabel');
    const modelPicker = document.getElementById('modelPicker');
    const thinkLabel = document.getElementById('thinkLabel');
    const thinkButton = document.getElementById('thinkButton');
    if (agentLabel) agentLabel.textContent = currentAgent || `Agent: ${EMPTY_LABEL[state(agents, agentsFailed)]}`;
    if (modelLabel) {
      const profilesState = state(profiles, profilesFailed);
      modelLabel.textContent = currentProfile()
        || (profilesState === 'ready' ? "Agent's own model" : `Profile: ${EMPTY_LABEL[profilesState]}`);
    }
    if (modelPicker) {
      modelPicker.classList.toggle('is-custom', Boolean(override));
      const own = agentDefault();
      modelPicker.title = override
        ? `Model profile, chosen for this chat${own ? ` -- ${currentAgent} runs on ${own} by itself` : ''}`
        : 'Model profile: the agent\'s own';
    }
    const level = params.thinking_level;
    if (thinkLabel) thinkLabel.textContent = level ? `Thinking: ${level}` : 'Thinking';
    if (thinkButton) {
      // only while the levels load: with the list failed the menu still offers Default, the way back
      thinkButton.disabled = state(profiles, profilesFailed) === 'loading';
      thinkButton.title = level ? `Thinking level ${level}, set for this chat` : 'Thinking level: the model\'s own';
    }
    renderThinkMenu();
  }

  /** The kit's icon('check', {size: 'sm'}), built here: this plain script cannot import the kit module. */
  function checkIcon() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'pk-icon pk-icon--sm');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '/static/kit/icons.svg#check');
    svg.append(use);
    return svg;
  }

  function renderThinkMenu() {
    const menu = document.getElementById('thinkMenu');
    if (!menu) return;
    const current = params.thinking_level || null;
    menu.replaceChildren(...[null, ...thinkingLevels].map((level) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'pk-menu-item';
      item.dataset.level = level || '';
      item.setAttribute('aria-pressed', String(level === current));
      item.append(checkIcon(), level ? level : 'Default (the model\'s own)');
      return item;
    }));
  }

  /** Every change: a person's pick or a session's restore. slash commands follow the agent; dropped names
   *  what a person's agent pick took away (the chat offers it back). */
  function announce(kind, detail = {}) {
    showChoice();
    global.dispatchEvent(new CustomEvent('selector:change', { detail: { kind, ...detail } }));
  }

  async function fetchJson(path) {
    // no-store: the browser must not serve a stale list from its HTTP cache on a normal reload
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    return response.json();
  }

  function settle() {
    if (!settled()) return;
    const wanted = pendingRestore;
    pendingRestore = null;
    if (wanted) {
      applyRestore(wanted);
    } else {
      announce('profile');
    }
  }

  async function loadAgents() {
    try {
      const data = await fetchJson('/agents');
      const details = new Map((data.details || []).map((d) => [d.name, d]));
      agents = (data.agents || []).map((name) => ({ tags: [], ...details.get(name), name }));
      defaultAgent = data.default || agents[0]?.name || null;
      const wanted = pendingRestore?.agent;
      currentAgent = has(agents, wanted) ? wanted : has(agents, defaultAgent) ? defaultAgent : agents[0]?.name || null;
    } catch (error) {
      console.error('Failed to load agents:', error);
      agentsFailed = true;
    }
    announce('agent');
    settle();
  }

  async function loadLLMProfiles() {
    try {
      const data = await fetchJson('/llm/profiles');
      profiles = data.profiles || [];
      thinkingLevels = Array.isArray(data.thinking_levels) ? data.thinking_levels : [];
      defaultLLMProfile = data.default || profiles[0]?.name || null;
    } catch (error) {
      console.error('Failed to load LLM profiles:', error);
      profilesFailed = true;
    }
    settle();
  }

  async function init() {
    document.getElementById('thinkMenu')?.addEventListener('click', (event) => {
      const item = event.target.closest('[data-level]');
      if (!item) return;
      setThinking(item.dataset.level || null);
      event.currentTarget.hidePopover?.();
    });
    showChoice();
    await Promise.all([loadAgents(), loadLLMProfiles()]);
  }

  function applyRestore({ agent, profile, params: stored }) {
    if (agents) {
      const usable = has(agents, defaultAgent) ? defaultAgent : agents[0]?.name || null;
      if (agent && !has(agents, agent)) console.warn(`agent "${agent}" not found, using "${usable}"`);
      currentAgent = has(agents, agent) ? agent : usable;
    }
    const own = agentDefault();
    // the session's profile is an override only where it is not what the agent runs on anyway; without
    // the profile list (it failed) the pick stays -- the server judges it, as it does a stored level
    override = profile && (profilesFailed || has(profiles, profile)) && profile !== own ? profile : null;
    params = usableParams(stored);
    announce('agent');
    announce('profile');
  }

  /** A person's pick of an agent. Their model choice was for the agent they leave: it goes, and the
   *  change event names it (dropped) so the chat can offer it back. True if the agent is known. */
  function setAgent(name) {
    if (!agents) {
      // a person's pick: the waiting session's choice was for its own agent
      pendingRestore = { agent: name };
      return false;
    }
    // A person's pick wins over a restore still waiting for the profiles: the session's choice was for
    // its own agent, not for this one
    pendingRestore = null;
    const known = has(agents, name);
    const usable = has(agents, defaultAgent) ? defaultAgent : agents[0]?.name || null;
    if (!known) console.warn(`agent "${name}" not found, using "${usable}"`);
    const next = known ? name : usable;
    let dropped = null;
    if (next !== currentAgent && (override || params.thinking_level)) {
      dropped = { profile: override, params: { ...params } };
      override = null;
      params = {};
    }
    currentAgent = next;
    announce('agent', { dropped });
    return known;
  }

  /** A person's pick of a profile; the agent's own one is no override. True if the profile is known. */
  function setLLMProfile(name) {
    if (!profiles) return false;
    const known = has(profiles, name);
    if (!known) {
      console.warn(`profile "${name}" not found`);
      return false;
    }
    override = name === agentDefault() ? null : name;
    // A restore still waiting for the agents would put the session's choice over this one at settle
    if (pendingRestore) pendingRestore.profile = name;
    announce('profile');
    return true;
  }

  /** A person's thinking level, null for the model's own. True if the server takes it. */
  function setThinking(level) {
    if (level && !thinkingLevels.includes(level)) return false;
    params = level ? { ...params, thinking_level: level } : {};
    if (pendingRestore) pendingRestore.params = { ...params };
    announce('params');
    return true;
  }

  SelectorModule.init = init;
  SelectorModule.getCurrentAgent = () => currentAgent;
  /** the profile the chat runs on: the override, else the agent's own (null while that is not known) */
  SelectorModule.getCurrentLLMProfile = currentProfile;
  // A message sent while a restore waits for the lists carries the session's choice: sent without it,
  // the run would go to the agent's own model and its save erase the choice from the session.
  const waiting = () => (pendingRestore && !settled() ? pendingRestore : null);
  /** what a run sends as llm_profile: null unless a person picked one */
  SelectorModule.getProfileOverride = () => (waiting() ? waiting().profile || null : override);
  /** what a run sends as llm_params: {} unless a person set something */
  SelectorModule.getLLMParams = () => {
    const stored = waiting() && waiting().params;
    if (waiting()) return typeof stored?.thinking_level === 'string' ? { thinking_level: stored.thinking_level } : {};
    return { ...params };
  };
  SelectorModule.agentDefaultProfile = (agent) => agentDefault(agent);
  SelectorModule.thinkingLevels = () => thinkingLevels.slice();
  SelectorModule.defaultAgent = () => defaultAgent;
  SelectorModule.defaultLLMProfile = () => defaultLLMProfile;
  SelectorModule.agents = () => agents || [];
  SelectorModule.profiles = () => profiles || [];
  SelectorModule.hasAgent = (name) => has(agents, name);
  /** 'loading', 'failed' or 'ready', for kind 'agent' or 'profile' */
  SelectorModule.listState = (kind) => (kind === 'agent' ? state(agents, agentsFailed) : state(profiles, profilesFailed));
  SelectorModule.setAgent = setAgent;
  SelectorModule.setLLMProfile = setLLMProfile;
  SelectorModule.setThinking = setThinking;
  /** What a session ran with -- its agent, profile (the effective one, as stored) and llm_params. */
  SelectorModule.restore = (stored) => {
    if (!settled()) {
      // The agent goes at once where the list is there: a message sent before the profiles arrive
      // must not run the session under another agent. The model part waits for both lists.
      if (agents && stored.agent && has(agents, stored.agent) && stored.agent !== currentAgent) {
        currentAgent = stored.agent;
        announce('agent');
      }
      pendingRestore = { ...stored };
      return;
    }
    applyRestore(stored);
  };
  /** The model choice as it stands, and putting it back (a reload of the same session in between). */
  SelectorModule.choice = () => ({ override, params: { ...params } });
  SelectorModule.setChoice = (choice) => {
    // the check applyRestore makes: without the list (it failed) the pick stays, the server judges it
    override = choice.override && (profilesFailed || has(profiles, choice.override)) && choice.override !== agentDefault()
      ? choice.override : null;
    params = usableParams(choice.params);
    announce('profile');
  };

  global.selectorModule = SelectorModule;
})(window);
