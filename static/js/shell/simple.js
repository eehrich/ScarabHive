// The simple view of the shell (agent_system/ui/views.py): what someone who is not an admin sees -- the panels the
// admins chose, and in the composer topics in plain words with Quick or Thorough instead of the agent, the model
// profile and the thinking level. Admins see everything and may switch to it in their account menu.
//   GET /api/ui/view -> {role, mode: 'simple'|'admin', can_switch, panels: [ids] | null, topics: [...]}
import { api, html, icon, render } from '/static/kit/panel-kit.js';

const TOPIC_KEY = 'scarabhive.topic';
const DEPTH_KEY = 'scarabhive.depth';
/** The thinking level "Thorough" asks for, where the server takes it. */
const THOROUGH_LEVEL = 'high';
/** What the shell shows an admin when the server cannot say: everything, as before there were views. */
export const FULL_VIEW = Object.freeze({ role: 'admin', mode: 'admin', can_switch: false, panels: null, topics: [] });

/** This viewer's view. When the server cannot say, an admin (or the one owner without auth) sees everything; anyone
 * else the simple view with the panels their role may open: a failure never gives them the admin's choices. */
export async function loadView(user = null) {
  try {
    return await api('/api/ui/view', { quiet: true });
  } catch {
    if (!user || user.role === 'admin') return { ...FULL_VIEW };
    return { role: user.role, mode: 'simple', can_switch: false, panels: null, topics: [] };
  }
}

/** The view's panels from the catalogue, in the order the admins chose; every panel in the admin view. */
export function shownPanels(catalog, view) {
  if (!view?.panels) return catalog.panels;
  return view.panels.map((id) => catalog.panels.find((panel) => panel.id === id)).filter(Boolean);
}

/** The catalogue as the simple view lists it: the same categories, only the view's panels. */
export const shownCatalog = (catalog, view) => ({ ...catalog, panels: shownPanels(catalog, view) });

function remembered(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
}
function remember(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage unavailable */ }
}

/**
 * The composer in the simple view: the agent, model and thinking buttons give way to the topics and Quick or
 * Thorough. A topic picks its agent ("" = the default one) and the profile for the depth ("" = the agent's own);
 * Thorough also asks for more thinking where the server takes it. A topic whose agent this server lacks is left out.
 *
 * The bar says what runs. A new chat runs the topic picked. A session carries the agent that ran it, so a loaded one
 * is followed and never moved: continuing it under another agent would run it with foreign tools and a foreign
 * prompt, so picking another topic there starts a new chat (`sessions`: {active(), startNew()}), as /agent does. The
 * new chat /agent starts (session:new with agentChosen) stays on the agent it names.
 */
export class TopicBar {
  constructor({ bar, setWords, selector, topics, sessions = null }) {
    this.bar = bar;
    this.setWords = setWords;   // what the empty composer says
    this.selector = selector;
    this.sessions = sessions;
    this.topics = (topics || []).filter((topic) => !topic.agent || selector.hasAgent(topic.agent));
    this.picked = this.topics.find((topic) => topic.id === remembered(TOPIC_KEY, ''))?.id || this.topics[0]?.id || '';
    this.topic = this.picked;   // the one shown: the picked one, or the one a loaded session runs on
    this.other = '';            // the agent of a loaded session that no topic names
    this.pickedDeep = remembered(DEPTH_KEY, 'quick') === 'deep';
    this.deep = this.pickedDeep;   // the one shown: the picked one, or what a loaded session runs with
    this.applying = false;
    this.box = document.createElement('div');
    this.box.className = 'composer-topics';
    this.box.dataset.topics = '';
    this.box.addEventListener('click', (event) => {
      const topic = event.target.closest('[data-topic]');
      const depth = event.target.closest('[data-depth]');
      if (topic) this.pick(topic.dataset.topic);
      else if (depth) this.depth(depth.dataset.depth === 'deep');
    });
  }

  /** Put the bar in place of the three choices and apply the remembered topic; a session already shown keeps its own. */
  mount() {
    for (const id of ['agentPicker', 'modelPicker', 'thinkButton']) {
      const button = document.getElementById(id);
      if (button) button.hidden = true;
    }
    this.bar.prepend(this.box);
    window.addEventListener('session:new', (event) => {
      if (event.detail?.agentChosen) this.follow();
      else this.apply();
    });
    // a loaded session's restore, /agent, /model, /think, a rewind: whatever else moved the choice is what the bar shows
    window.addEventListener('selector:change', () => {
      if (!this.applying) this.follow();
    });
    if (this.sessions?.active()) this.follow();
    else this.apply();
  }

  draw() {
    render(this.box, html`${this.topics.length || this.other ? html`<div class="composer-topic-list" role="group" aria-label="What is it about?">${
      this.topics.map((topic) => html`<button type="button" class="composer-topic" data-topic="${topic.id}"
        aria-pressed="${String(topic.id === this.topic)}">${topic.label}</button>`)}${
      this.other ? html`<span class="composer-topic" data-topic-other aria-current="true">${this.other}</span>` : ''}</div>` : ''}
      <div class="composer-depth" role="group" aria-label="How thorough?">
        <button type="button" data-depth="quick" aria-pressed="${String(!this.deep)}" title="An answer in seconds">Quick</button>
        <button type="button" data-depth="deep" aria-pressed="${String(this.deep)}" title="Takes longer, checks more">${icon('brain', { size: 'sm' })} Thorough</button>
      </div>`);
    const hint = this.topics.find((one) => one.id === this.topic)?.hint;
    this.setWords(hint ? `Ask in your own words -- ${hint}` : 'Ask in your own words…');
  }

  pick(id) {
    if (!this.topics.some((topic) => topic.id === id)) return;
    this.picked = id;
    remember(TOPIC_KEY, id);
    if (this.sessions?.active()) this.sessions.startNew();   // session:new applies it
    else this.apply();
  }

  depth(deep) {
    remember(DEPTH_KEY, deep ? 'deep' : 'quick');
    this.pickedDeep = deep;
    this.deep = deep;
    this.quietly(() => this.choose());   // never the agent: a session, a topic or /agent chose that
    this.draw();
  }

  /** The selector's own events are not news while the bar itself sets it. */
  quietly(set) {
    this.applying = true;
    try { set(); } finally { this.applying = false; }
  }

  /** The profile and thinking of the depth for the topic shown. */
  choose() {
    const topic = this.topics.find((one) => one.id === this.topic);
    const levels = this.selector.thinkingLevels();
    this.selector.setChoice({
      override: (this.deep ? topic?.deep : topic?.quick) || null,
      params: this.deep && levels.includes(THOROUGH_LEVEL) ? { thinking_level: THOROUGH_LEVEL } : {},
    });
  }

  /** A new chat: set the agent, profile and thinking of the picked topic -- quietly: no "your model choice went" note. */
  apply() {
    this.topic = this.picked;
    this.other = '';
    this.deep = this.pickedDeep;
    const topic = this.topics.find((one) => one.id === this.topic);
    this.quietly(() => {
      this.selector.setChoice({ override: null, params: {} });
      this.selector.setAgent(topic?.agent || this.selector.defaultAgent());
      this.choose();
    });
    this.draw();
  }

  /** Show what runs now, setting nothing: the topic on that agent (the picked one first), else the agent's name. */
  follow() {
    const agent = this.selector.getCurrentAgent();
    const on = this.topics.filter((one) => (one.agent || this.selector.defaultAgent()) === agent);
    const shown = on.find((one) => one.id === this.picked) || on[0];
    this.topic = shown?.id || '';
    this.other = shown ? '' : agent || '';
    const { override, params } = this.selector.choice();
    this.deep = params?.thinking_level === THOROUGH_LEVEL || Boolean(override && override === shown?.deep);
    this.draw();
  }
}

/** The account menu's view section: admins switch between the two views, others are told who chose theirs. Nothing
 * for the full view when the server could not say which one this is. */
export function viewMenu(view) {
  if (!view.can_switch && view.mode !== 'simple') return html``;
  if (view.can_switch) {
    return html`<div class="app-view-menu" data-view-menu>
      <span class="pk-menu-label">View</span>
      <div class="app-view-switch" role="group" aria-label="View">
        <button type="button" data-view-mode="simple" aria-pressed="${String(view.mode === 'simple')}">Simple</button>
        <button type="button" data-view-mode="admin" aria-pressed="${String(view.mode === 'admin')}">Admin</button>
      </div>
      <span class="app-view-note">${view.mode === 'simple' ? 'What your users see. Your rights stay as they are.'
        : 'Every panel, agent, model profile and server tool.'}</span></div>
      <button type="button" class="pk-menu-item" data-open-panel="user_view">${icon('users')} User view…</button>
      <hr class="pk-menu-separator">`;
  }
  return html`<div class="app-view-menu" data-view-menu><span class="pk-menu-label">View</span>
    <span class="app-view-note">Simple -- put together by your admins. Missing a tool? Ask them.</span></div>
    <hr class="pk-menu-separator">`;
}

/** Switch an admin's view; the page loads again to show it. */
export async function switchView(mode, reload = () => window.location.reload()) {
  await api('/api/ui/view/mode', { method: 'PUT', json: { mode } });
  reload();
}

/** The start page of an empty chat in the simple view: the panels as tiles, what to ask, the recent sessions. */
export function simpleWelcome(panels, recent) {
  return html`<section class="chat-welcome chat-welcome--simple" id="chatWelcome">
    <div class="chat-welcome-brand">
      <svg class="pk-logo" width="36" height="36" role="img" aria-label="ScarabHive"><use href="/static/kit/logo.svg#mark"/></svg>
      <h1>What would you like to do?</h1>
    </div>
    <p>Open one of your areas, or pick a topic below and ask in your own words.</p>
    ${panels.length ? html`<div class="chat-welcome-areas" data-areas>${panels.map((panel) => html`
      <button type="button" class="chat-welcome-area" data-area="${panel.id}">${icon(panel.icon)}
        <strong>${panel.title}</strong><span>${panel.description || ''}</span></button>`)}</div>` : ''}
    ${recent.length ? html`<h2 class="chat-welcome-head">Recent conversations</h2><div class="chat-welcome-recent">${recent.map((s) => html`
      <button type="button" class="chat-welcome-card" data-session="${s.session_id}">
        <strong class="pk-truncate">${s.title || 'Untitled'}</strong><span>${new Date(s.updated_at).toLocaleString()}</span>
      </button>`)}</div>` : ''}
  </section>`;
}
