// ScarabHive shell entry: wires header, sessions, chat, panels, launcher and palette.
import { api, ApiError, html, render, icon, placeMenu, setTheme, currentTheme, THEMES, setStyle, currentStyle, STYLES, showToast } from '/static/kit/panel-kit.js';
import { Workspace } from './workspace.js';
import { SessionManager } from './sessions.js';
import { Launcher } from './launcher.js';
import { Palette } from './palette.js';
import { Picker } from './picker.js';
import { ServerControls } from './server.js';
import { AgentRunsPill } from './agent_runs.js';
import { FULL_VIEW, TopicBar, loadView, shownCatalog, shownPanels, simpleWelcome, switchView, viewMenu } from './simple.js';

const SESSIONS_OPEN_KEY = 'scarabhive.sessionsOpen';
const SESSIONS_WIDTH_KEY = 'scarabhive.sessionsWidth';
const SESSIONS_MIN_WIDTH = 200;
/** As shell.css caps it, which caps it again for a window made smaller later. */
const sessionsMaxWidth = () => Math.min(window.innerWidth * 0.4, window.innerWidth - 685);
const THEME_ICONS = { system: 'monitor', light: 'sun', dark: 'moon' };

const $ = (id) => document.getElementById(id);
/** A narrow screen as shell.css decides it: the sessions pane is a sheet over the chat. */
const narrow = () => getComputedStyle($('sessionsPane')).position === 'fixed';
let catalog = { categories: [], panels: [], failed: false };
let activeSession = null;
let user = null;
/** What this viewer sees: the admin view (everything) or the simple one (simple.js) */
let view = { ...FULL_VIEW };

// ------------------------------------------------------------------ auth

async function whoAmI() {
  try {
    return await api('/auth/me', { quiet: true });
  } catch (error) {
    // 401: not signed in; 403: the account was deactivated -- the login page says so
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      // replace: Back from the login page must not land on a page that sends it there again. The query comes
      // along: a ?panel= link opened signed out is followed once signed in.
      const here = window.location.pathname + window.location.search + window.location.hash;
      window.location.replace(`/login?return=${encodeURIComponent(here)}`);
      return new Promise(() => {});  // the page is leaving
    }
    if (error instanceof ApiError && error.status === 404) return null;  // auth disabled: one owner
    throw error;
  }
}

function showUser() {
  const name = user ? (user.full_name || user.username) : 'Owner';
  $('userInitials').textContent = name.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
  $('userMenuName').textContent = user ? `${name} · ${user.role}` : name;
  $('logoutButton').hidden = !user;
  $('viewLabel').hidden = view.mode !== 'simple';
  $('userButton').title = view.mode === 'simple' ? 'Account · simple view' : 'Account';
  render($('viewMenu'), viewMenu(view));
  // the system's status is the admins' business: the simple view leaves it out of the menu
  const system = $('userMenu').querySelector('[data-open-panel="system"]');
  if (system) system.hidden = view.mode === 'simple';
}

// ----------------------------------------------------------------- theme

let workspace;

/** The account menu marks the theme in use. */
function markTheme(theme) {
  document.querySelectorAll('[data-theme-choice]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.themeChoice === theme));
  });
}

function applyTheme(theme) {
  setTheme(theme);
  markTheme(theme);
  workspace.broadcast('pk:theme', { theme });
}

function applyStyle(style) {
  setStyle(style);
  workspace.broadcast('pk:style', { style });
}

// -------------------------------------------------------------- sessions

function onSessionChange(session) {
  // the chat reports its session on every message: the panels hear of a change only
  if (session?.id === activeSession?.id && session?.title === activeSession?.title) return;
  activeSession = session;
  $('headerSessionId').textContent = session ? session.title : 'New session';
  $('headerSessionId').disabled = !session;
  $('sessionPanelsButton').disabled = !session;
  document.title = session ? `${session.title} · ScarabHive` : 'ScarabHive';
  workspace.broadcast('pk:session', { session });
}

/** The start page of an empty chat -- drawn again with the list while it is all the chat shows. */
function showWelcome(sessions) {
  const chat = $('chat');
  if (chat.children.length && !(chat.children.length === 1 && $('chatWelcome'))) return;
  const recent = sessions.slice(0, 6);
  if (view.mode === 'simple') {
    render(chat, simpleWelcome(shownPanels(catalog, view), recent));
    return;
  }
  render(chat, html`
    <section class="chat-welcome" id="chatWelcome">
      <div class="chat-welcome-brand">
        <svg class="pk-logo" width="36" height="36" role="img" aria-label="ScarabHive"><use href="/static/kit/logo.svg#mark"/></svg>
        <h1>What should the agents work on?</h1>
      </div>
      <p>Pick an agent below and write a message, or continue a recent session. Panels live behind ${icon('layout-grid', { size: 'sm' })} and <kbd class="pk-kbd">Ctrl K</kbd>.</p>
      ${recent.length ? html`<div class="chat-welcome-recent">${recent.map((s) => html`
        <button type="button" class="chat-welcome-card" data-session="${s.session_id}">
          <strong class="pk-truncate">${s.title || 'Untitled'}</strong><span>${s.agent_name} · ${new Date(s.updated_at).toLocaleString()}</span>
        </button>`)}</div>` : ''}
    </section>`);
}

// The welcome makes room as soon as the chat shows anything else -- a message,
// a command's note -- and not on a submit that adds nothing (an empty message).
new MutationObserver(() => {
  const welcome = $('chatWelcome');
  if (welcome && $('chat').children.length > 1) welcome.remove();
}).observe($('chat'), { childList: true });

// ------------------------------------------------------------ sessions pane

/** remember: a choice the viewer made, kept for the next visit; a narrow screen's default is not. */
function setSessionsOpen(open, { remember = true } = {}) {
  document.querySelector('.app-body').dataset.sessions = open ? 'open' : 'closed';
  $('sessionsToggle').setAttribute('aria-expanded', String(open));
  if (!remember) return;
  try { localStorage.setItem(SESSIONS_OPEN_KEY, String(open)); } catch { /* storage unavailable */ }
}

function sessionsOpen() {
  return document.querySelector('.app-body').dataset.sessions !== 'closed';
}

function setSessionsWidth(width, { remember = true } = {}) {
  const clamped = Math.round(Math.max(Math.min(width, sessionsMaxWidth()), SESSIONS_MIN_WIDTH));
  document.querySelector('.app-body').style.setProperty('--sessions-open-width', `${clamped}px`);
  if (!remember) return;
  try { localStorage.setItem(SESSIONS_WIDTH_KEY, String(clamped)); } catch { /* storage unavailable */ }
}

/** The pane's right edge: dragged, or Arrow Left/Right on the focused handle. */
function wireSessionsResizer(workspace) {
  const handle = $('sessionsResizer');
  const width = () => $('sessionsPane').getBoundingClientRect().width;
  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    const startX = event.clientX;
    const startWidth = width();
    handle.dataset.dragging = '';
    workspace.track(handle, event, (e) => setSessionsWidth(startWidth + e.clientX - startX, { remember: false }), () => {
      delete handle.dataset.dragging;
      setSessionsWidth(width());
    });
  });
  handle.addEventListener('keydown', (event) => {
    const step = { ArrowLeft: -16, ArrowRight: 16 }[event.key];
    if (!step) return;
    event.preventDefault();
    setSessionsWidth(width() + step);
  });
}

/** On a narrow screen the sessions pane is a sheet over everything: it steps aside for what comes next. */
function closeSheet() {
  if (narrow() && sessionsOpen()) setSessionsOpen(false, { remember: false });
}

// ---------------------------------------------------------------- status

/** Set while a restart is watched: the status bar is that watch's to write until the server is back. */
let serverBusy = false;
let server;

async function pollHealth() {
  if (serverBusy) return;
  const state = $('connectionState');
  try {
    const health = await api('/health', { quiet: true });
    render(state, html`<span class="pk-dot pk-dot--ok"></span> <span>Connected</span>`);
    $('versionLabel').textContent = health.version ? `v${health.version}` : '';
  } catch {
    render(state, html`<span class="pk-dot pk-dot--danger"></span> <span>Offline</span>`);
  }
}

async function loadCatalog() {
  try {
    catalog = { ...await api('/api/ui/catalog'), failed: false };
  } catch {
    catalog = { categories: [], panels: [], failed: true };  // api() has told the viewer; the chat works on
  }
}

// ------------------------------------------------ panels from the chat itself

/** The panels of a session that hold nothing for it (GET /api/ui/session-panels): the menu leaves them out. A
 * failed question leaves none out -- the whole list, as before there was one. */
async function emptyFor(sessionId) {
  try {
    const answer = await api(`/api/ui/session-panels?session_id=${encodeURIComponent(sessionId)}`, { quiet: true });
    return new Set(answer.hidden || []);
  } catch {
    return new Set();
  }
}

let menuTurn = 0;

async function contextMenu(anchor, context, values) {
  const turn = ++menuTurn;
  const box = anchor.getBoundingClientRect();  // before the wait: a redrawn session list drops the anchor
  const all = catalog.panels.filter((p) => p.contexts && p.contexts[context]);
  const hidden = context === 'session' && all.length ? await emptyFor(values.session_id) : new Set();
  if (turn !== menuTurn) return;  // a later click opened its own menu meanwhile
  document.getElementById('contextMenu')?.remove();
  if (!all.length) return;
  const menu = document.createElement('div');
  menu.id = 'contextMenu';
  menu.dataset.anchor = anchor.id;
  menu.className = 'pk-menu';
  menu.popover = 'auto';
  const draw = (whole) => {
    const panels = whole ? all : all.filter((p) => !hidden.has(p.id));
    const left = all.length - panels.length;
    render(menu, html`<div class="pk-menu-label">Open in</div>${panels.map((p) => html`
      <button type="button" class="pk-menu-item" data-panel="${p.id}">${icon(p.icon)} ${p.title}</button>`)}
      ${left ? html`<button type="button" class="pk-menu-item" data-all-panels
        title="Panels that hold nothing for this session">${icon('chevron-down')} All session panels (${all.length})</button>` : ''}`);
  };
  draw(false);
  menu.addEventListener('click', (event) => {
    if (event.target.closest('[data-all-panels]')) {
      draw(true);
      menu.querySelector('[data-panel]')?.focus();
      return;
    }
    const item = event.target.closest('[data-panel]');
    if (!item) return;
    const panel = catalog.panels.find((p) => p.id === item.dataset.panel);
    // A context URL starts with the panel's own URL (the catalogue refuses others).
    const path = panel.contexts[context].slice(panel.url.length)
      .replace(/\{(\w+)\}/g, (_, key) => encodeURIComponent(values[key] ?? ''));
    menu.hidePopover();
    workspace.open(panel.id, { path });
  });
  document.body.appendChild(menu);
  placeMenu(menu, box);
  menu.showPopover();
  menu.querySelector('[data-panel], [data-all-panels]').focus();  // every panel left out: the way to them
}

// ---------------------------------------------------------------- palette

function paletteEntries(sessions) {
  const entries = [
    { group: 'Actions', icon: 'plus', label: 'New session', run: () => sessions.newConversation() },
    { group: 'Actions', icon: 'panel-left', label: 'Toggle sessions', hint: 'Ctrl B', run: () => setSessionsOpen(!sessionsOpen()) },
    { group: 'Actions', icon: 'panel-right', label: 'Toggle panels', hint: 'Ctrl Alt B', run: () => workspace.toggleDock() },
    ...THEMES.map((theme) => ({ group: 'Actions', icon: THEME_ICONS[theme], label: `Theme: ${theme}`, keywords: ['appearance'], run: () => applyTheme(theme) })),
    ...STYLES.map((style) => ({ group: 'Actions', icon: 'layers', label: `Style: ${style}`, keywords: ['appearance', 'look', 'design'], run: () => applyStyle(style) })),
  ];
  if (user?.role === 'admin' && view.mode === 'admin') {
    entries.push({ group: 'Actions', icon: 'rotate-ccw', label: 'Restart server', hint: 'Loads plugins, agents and skills anew',
      keywords: ['reload', 'plugins', 'agents', 'skills'], run: () => server.restart() });
    entries.push({ group: 'Actions', icon: 'power', label: 'Stop server', keywords: ['shutdown', 'quit'], run: () => server.stop() });
  }
  if (user) entries.push({ group: 'Actions', icon: 'log-out', label: 'Log out', run: logout });
  const groups = new Set();
  const listed = shownPanels(catalog, view);
  listed.forEach((p) => {
    // Instances of one plugin: listed once, the pick among them comes with typing.
    if (p.group && !groups.has(p.group)) {
      groups.add(p.group);
      const count = listed.filter((other) => other.group === p.group).length;
      entries.push({ group: 'Panels', icon: p.icon, label: p.group, hint: `${count} instances`, refine: p.group });
    }
    entries.push({
      group: 'Panels', icon: p.icon, label: p.title, hint: p.description, keywords: p.keywords,
      instance: Boolean(p.group), run: () => launcher.open(p.id),
    });
  });
  // the simple view chooses agents by topic, in the composer
  if (view.mode === 'admin') window.selectorModule.agents().forEach((agent) => entries.push({
    group: 'Agents', icon: 'workflow', label: agent.name, hint: agent.description || 'Use this agent',
    keywords: ['agent', agent.category, ...(agent.tags || [])].filter(Boolean),
    run: () => window.selectorModule.setAgent(agent.name),
  }));
  sessions.sessions.forEach((s) => entries.push({
    group: 'Sessions', icon: 'message-square', label: s.title || 'Untitled', hint: s.agent_name,
    run: () => sessions.loadSession(s.session_id),
  }));
  return entries;
}

async function logout() {
  await api('/auth/logout', { method: 'POST', quiet: true }).catch(() => null);
  sessionStorage.clear();
  workspace.forgetLayout();  // its panel paths name this user's sessions
  window.location.href = '/login';
}

// ------------------------------------------------------------------- start

let launcher;

async function start() {
  user = await whoAmI();
  view = await loadView(user);
  document.documentElement.dataset.uiMode = view.mode;
  showUser();

  workspace = new Workspace({
    catalog: () => catalog,
    theme: currentTheme,
    style: currentStyle,
    session: () => activeSession,
    onSetTheme: applyTheme,
    onSetStyle: applyStyle,
    onOpenSession: (id) => sessions.loadSession(id),  // declared below, called only once the shell runs
    // one layout per account: whoever signs in next in this browser gets their own
    layoutKey: user ? `scarabhive.layout.v1:${user.username}` : 'scarabhive.layout.v1',
    narrow,
    onShow: closeSheet,
  });
  markTheme(currentTheme());

  const sessions = new SessionManager({
    onChange: onSessionChange,
    openContext: contextMenu,
    onShown: closeSheet,
    onListChange: (list) => { if ($('chatWelcome')) showWelcome(list); },
  });
  window.sessionManager = sessions;  // the chat and unmigrated plugin panels call it
  // the launcher lists what the view shows; the workspace knows every panel the viewer may open (a link, a restore)
  launcher = new Launcher({ catalog: () => shownCatalog(catalog, view), open: (id, options) => workspace.open(id, options) });
  const palette = new Palette(() => paletteEntries(sessions));

  let stored = null;
  try { stored = localStorage.getItem(SESSIONS_OPEN_KEY); } catch { /* storage unavailable */ }
  setSessionsOpen(stored !== 'false' && !narrow(), { remember: false });
  let storedWidth = null;
  try { storedWidth = Number(localStorage.getItem(SESSIONS_WIDTH_KEY)); } catch { /* storage unavailable */ }
  // unclamped on purpose: the window may be smaller now than when it was set, and shell.css caps it to this one
  if (Number.isFinite(storedWidth) && storedWidth >= SESSIONS_MIN_WIDTH) document.querySelector('.app-body').style.setProperty('--sessions-open-width', `${storedWidth}px`);
  wireSessionsResizer(workspace);

  // header and menus
  $('sessionsToggle').addEventListener('click', () => setSessionsOpen(!sessionsOpen()));
  $('paletteButton').addEventListener('click', () => palette.open());
  $('helpButton').addEventListener('click', () => workspace.openManual());  // a plugin's guide: its panel's own button
  for (const id of ['headerSessionId', 'sessionPanelsButton']) {
    let closing = false;  // the press began on the open menu's own anchor: a toggle, it only closes it
    $(id).addEventListener('pointerdown', () => {
      const menu = $('contextMenu');
      closing = Boolean(menu?.matches(':popover-open') && menu.dataset.anchor === id);
    });
    $(id).addEventListener('click', (event) => {
      const toggle = closing;
      closing = false;
      if (toggle && event.detail) {  // a pointer's click: one by key (detail 0) opens, whatever press went astray
        $('contextMenu')?.hidePopover();
        return;
      }
      if (activeSession) contextMenu($(id), 'session', { session_id: activeSession.id });
    });
  }
  $('userMenu').addEventListener('click', (event) => {
    // a theme picked shows at once; the menu stays open, its choice marked
    const choice = event.target.closest('[data-theme-choice]');
    if (choice) return applyTheme(choice.dataset.themeChoice);
    const mode = event.target.closest('[data-view-mode]');
    if (mode && mode.getAttribute('aria-pressed') !== 'true') {
      switchView(mode.dataset.viewMode).catch(() => { /* api() has said why */ });
      return;
    }
    const button = event.target.closest('[data-open-panel]');
    if (!button) return;
    $('userMenu').hidePopover();
    launcher.open(button.dataset.openPanel);
  });
  $('logoutButton').addEventListener('click', logout);
  server = new ServerControls({
    stopButton: $('stopServerButton'), restartButton: $('restartServerButton'), state: $('connectionState'),
    onBusy: (busy) => { serverBusy = busy; },
  });
  server.show(user?.role === 'admin' && view.mode === 'admin');  // without authentication there are no admin routes, and no buttons
  // the agents at work, on every tab: a click opens the panel that started the newest one
  new AgentRunsPill({
    button: $('agentRunsPill'),
    bell: $('agentRunsBell'),
    onOpen: (run) => {
      const panel = run.source && workspace.panelAt(`/plugins/${run.source}/`);
      if (panel) workspace.open(panel.id);
      else showToast(document, `${run.agent || 'The agent'} was started outside a panel; follow it in its session`, 'info');
    },
  }).start();

  document.addEventListener('keydown', (event) => {
    const mod = event.ctrlKey || event.metaKey;
    if (mod && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      // not over an open question: the rest of the page waits for its answer, and so does the palette
      if (!document.querySelector('dialog.pk-dialog[open]:not(#palette)')) palette.open();
    } else if (mod && event.altKey && event.key.toLowerCase() === 'b') {
      event.preventDefault();
      workspace.toggleDock();
    } else if (mod && event.key.toLowerCase() === 'b') {
      event.preventDefault();
      setSessionsOpen(!sessionsOpen());
    }
  });

  // the sheet also steps aside for a touch on the chat
  document.querySelector('.app-main').addEventListener('pointerdown', closeSheet);

  // the chat
  $('chat').addEventListener('click', (event) => {
    const card = event.target.closest('[data-session]');
    if (card) {
      sessions.loadSession(card.dataset.session);
      return;
    }
    const area = event.target.closest('[data-area]');
    if (area) {
      launcher.open(area.dataset.area);
      return;
    }
    const requestId = event.target.closest('.message-request-id');
    if (requestId) contextMenu(requestId, 'request', { request_id: requestId.querySelector('span')?.textContent || '' });
  });
  window.addEventListener('session:new', () => showWelcome(sessions.sessions));
  // the chat's /help <topic>: the Help panel on the search for it
  window.addEventListener('help:search', (event) => workspace.open('help', { path: `?${new URLSearchParams({ q: event.detail.query })}` }));

  const selectors = window.selectorModule.init();  // a restored sub-session checks its agent against the list
  new Picker();
  window.fileUploadModule.init();
  // a run still going from before the reload comes back first -- its session opened once the agents are known
  const reattached = window.chatModule.init(selectors);

  await Promise.all([loadCatalog(), sessions.loadSessions(), pollHealth(), selectors, reattached]);
  if ($('chatWelcome')) showWelcome(sessions.sessions);  // the areas need the catalogue
  const topics = view.mode === 'simple'
    ? new TopicBar({ bar: document.querySelector('.composer-bar'), setWords: (words) => window.chatModule.setComposerWords(words),
        selector: window.selectorModule, topics: view.topics,
        // a first message on its way is a session too, only not named yet
        sessions: { active: () => Boolean(sessions.getCurrentSessionId()) || window.chatModule.hasActiveRequest(),
          startNew: () => sessions.newConversation() } })
    : null;
  topics?.mount();
  workspace.restore();
  await sessions.restore();
  await openFromLink(sessions);
  setInterval(pollHealth, 30000);
  // After restore, so the first poll asks about the rows that are actually shown.
  sessions.watchActivity();
}

/**
 * /?panel=<a panel page's path>: ScarabHive opened from a panel in a tab of its own (the kit's link there), or from
 * a link someone kept. The panel whose catalogue URL the path starts with opens at that page -- docked, or where it
 * already is. A session the page was pinned to opens in the chat too; the pin stays, for a panel may keep more by
 * it than the session it follows (message_debugger filters by it). The address goes back to plain, so a reload
 * does not open it again.
 */
async function openFromLink(sessions) {
  const wanted = new URLSearchParams(window.location.search).get('panel');
  if (wanted === null) return;
  history.replaceState(history.state, '', window.location.pathname + window.location.hash);
  let page = null;
  try { page = new URL(wanted, window.location.origin); } catch { /* no address at all: it names no panel either */ }
  const path = page && page.pathname + page.search;
  const panel = page?.origin === window.location.origin && workspace.panelAt(path);
  if (!panel) {
    showToast(document, 'The link names no panel of this ScarabHive', 'warn');
    return;
  }
  const sessionId = page.searchParams.get('session_id');
  if (sessionId && panel.contexts?.session) await sessions.loadSession(sessionId);
  // the page itself, also the panel's own URL: a panel that went elsewhere by itself comes to the page linked
  workspace.open(panel.id, { path });
}

function showStartupError(error) {
  console.error('The shell did not start', error);
  render($('chat'), html`
    <div class="pk-empty">${icon('circle-alert')}
      <div class="pk-empty-title">ScarabHive could not start</div>
      <div>${error.message || String(error)}</div>
      <button type="button" class="pk-btn" data-act="reload">${icon('refresh-cw', { size: 'sm' })} Try again</button>
    </div>`);
  $('chat').querySelector('[data-act="reload"]').addEventListener('click', () => window.location.reload());
  render($('connectionState'), html`<span class="pk-dot pk-dot--danger"></span> <span>Offline</span>`);
}

start().catch(showStartupError);
