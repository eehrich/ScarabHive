// ScarabHive shell entry: wires header, sessions, chat, panels, launcher and palette.
import { api, ApiError, html, render, icon, placeMenu, setTheme, currentTheme, THEMES } from '/static/kit/panel-kit.js';
import { Workspace } from './workspace.js';
import { SessionManager } from './sessions.js';
import { Launcher } from './launcher.js';
import { Palette } from './palette.js';

const SESSIONS_OPEN_KEY = 'scarabhive.sessionsOpen';
const THEME_ICONS = { system: 'monitor', light: 'sun', dark: 'moon' };

const $ = (id) => document.getElementById(id);
/** A narrow screen as shell.css decides it: the sessions pane is a sheet over the chat. */
const narrow = () => getComputedStyle($('sessionsPane')).position === 'fixed';
let catalog = { categories: [], panels: [], failed: false };
let activeSession = null;
let user = null;

// ------------------------------------------------------------------ auth

async function whoAmI() {
  try {
    return await api('/auth/me', { quiet: true });
  } catch (error) {
    // 401: not signed in; 403: the account was deactivated -- the login page says so
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      // replace: Back from the login page must not land on a page that sends it there again
      window.location.replace(`/login?return=${encodeURIComponent(window.location.pathname + window.location.hash)}`);
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
}

// ----------------------------------------------------------------- theme

let workspace;

function applyTheme(theme) {
  setTheme(theme);
  $('themeButton').innerHTML = String(icon(THEME_ICONS[theme]));
  $('themeButton').title = `Theme: ${theme}`;
  workspace.broadcast('pk:theme', { theme });
}

// -------------------------------------------------------------- sessions

function onSessionChange(session) {
  // the chat reports its session on every message: the panels hear of a change only
  if (session?.id === activeSession?.id && session?.title === activeSession?.title) return;
  activeSession = session;
  $('headerSessionId').textContent = session ? session.title : 'New session';
  $('headerSessionId').disabled = !session;
  document.title = session ? `${session.title} · ScarabHive` : 'ScarabHive';
  workspace.broadcast('pk:session', { session });
}

/** The start page of an empty chat -- drawn again with the list while it is all the chat shows. */
function showWelcome(sessions) {
  const chat = $('chat');
  if (chat.children.length && !(chat.children.length === 1 && $('chatWelcome'))) return;
  const recent = sessions.slice(0, 6);
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

/** On a narrow screen the sessions pane is a sheet over everything: it steps aside for what comes next. */
function closeSheet() {
  if (narrow() && sessionsOpen()) setSessionsOpen(false, { remember: false });
}

// ---------------------------------------------------------------- status

async function pollHealth() {
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

function contextMenu(anchor, context, values) {
  const panels = catalog.panels.filter((p) => p.contexts && p.contexts[context]);
  document.getElementById('contextMenu')?.remove();
  if (!panels.length) return;
  const menu = document.createElement('div');
  menu.id = 'contextMenu';
  menu.className = 'pk-menu';
  menu.popover = 'auto';
  render(menu, html`<div class="pk-menu-label">Open in</div>${panels.map((p) => html`
    <button type="button" class="pk-menu-item" data-panel="${p.id}">${icon(p.icon)} ${p.title}</button>`)}`);
  menu.addEventListener('click', (event) => {
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
  placeMenu(menu, anchor.getBoundingClientRect());
  menu.showPopover();
  menu.querySelector('[data-panel]').focus();
}

// ---------------------------------------------------------------- palette

function paletteEntries(sessions) {
  const entries = [
    { group: 'Actions', icon: 'plus', label: 'New session', run: () => sessions.newConversation() },
    { group: 'Actions', icon: 'panel-left', label: 'Toggle sessions', hint: 'Ctrl B', run: () => setSessionsOpen(!sessionsOpen()) },
    ...THEMES.map((theme) => ({ group: 'Actions', icon: THEME_ICONS[theme], label: `Theme: ${theme}`, keywords: ['appearance'], run: () => applyTheme(theme) })),
  ];
  if (user) entries.push({ group: 'Actions', icon: 'log-out', label: 'Log out', run: logout });
  const groups = new Set();
  catalog.panels.forEach((p) => {
    // Instances of one plugin: listed once, the pick among them comes with typing.
    if (p.group && !groups.has(p.group)) {
      groups.add(p.group);
      const count = catalog.panels.filter((other) => other.group === p.group).length;
      entries.push({ group: 'Panels', icon: p.icon, label: p.group, hint: `${count} instances`, refine: p.group });
    }
    entries.push({
      group: 'Panels', icon: p.icon, label: p.title, hint: p.description, keywords: p.keywords,
      instance: Boolean(p.group), run: () => launcher.open(p.id),
    });
  });
  [...$('agentSelector').options].forEach((option) => entries.push({
    group: 'Agents', icon: 'workflow', label: option.value, hint: 'Use this agent', keywords: ['agent'],
    run: () => window.selectorModule.setAgent(option.value),
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
  showUser();

  workspace = new Workspace({
    catalog: () => catalog,
    theme: currentTheme,
    session: () => activeSession,
    onSetTheme: applyTheme,
    // one layout per account: whoever signs in next in this browser gets their own
    layoutKey: user ? `scarabhive.layout.v1:${user.username}` : 'scarabhive.layout.v1',
    narrow,
    onShow: closeSheet,
  });
  $('themeButton').innerHTML = String(icon(THEME_ICONS[currentTheme()]));

  const sessions = new SessionManager({
    onChange: onSessionChange,
    openContext: contextMenu,
    onShown: closeSheet,
    onListChange: (list) => { if ($('chatWelcome')) showWelcome(list); },
  });
  window.sessionManager = sessions;  // the chat and unmigrated plugin panels call it
  launcher = new Launcher({ catalog: () => catalog, open: (id, options) => workspace.open(id, options) });
  const palette = new Palette(() => paletteEntries(sessions));

  let stored = null;
  try { stored = localStorage.getItem(SESSIONS_OPEN_KEY); } catch { /* storage unavailable */ }
  setSessionsOpen(stored !== 'false' && !narrow(), { remember: false });

  // header and menus
  $('sessionsToggle').addEventListener('click', () => setSessionsOpen(!sessionsOpen()));
  $('paletteButton').addEventListener('click', () => palette.open());
  $('themeButton').addEventListener('click', () => {
    const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
    applyTheme(next);
  });
  $('headerSessionId').addEventListener('click', () => {
    if (activeSession) contextMenu($('headerSessionId'), 'session', { session_id: activeSession.id });
  });
  $('userMenu').addEventListener('click', (event) => {
    const button = event.target.closest('[data-open-panel]');
    if (!button) return;
    $('userMenu').hidePopover();
    launcher.open(button.dataset.openPanel);
  });
  $('logoutButton').addEventListener('click', logout);

  document.addEventListener('keydown', (event) => {
    const mod = event.ctrlKey || event.metaKey;
    if (mod && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      // not over an open question: the rest of the page waits for its answer, and so does the palette
      if (!document.querySelector('dialog.pk-dialog[open]:not(#palette)')) palette.open();
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
    const requestId = event.target.closest('.message-request-id');
    if (requestId) contextMenu(requestId, 'request', { request_id: requestId.querySelector('span')?.textContent || '' });
  });
  window.addEventListener('session:new', () => showWelcome(sessions.sessions));

  const selectors = window.selectorModule.init();  // a restored sub-session checks its agent against the list
  window.fileUploadModule.init();
  const reattached = window.chatModule.init();  // a run still going from before the reload comes back first

  await Promise.all([loadCatalog(), sessions.loadSessions(), pollHealth(), selectors, reattached]);
  workspace.restore();
  await sessions.restore();
  setInterval(pollHealth, 30000);
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
