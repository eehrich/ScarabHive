// Sessions: the pane beside the chat and the one owner of "which session is open".
//
// Keeps the contract the chat and older panels use: window.sessionManager with
// loadSession / loadSessions / newConversation / leaveRunningRequest / isBeingDeleted /
// setCurrentSession / messageWritten / onSessionUpdated / getCurrentSessionId, and
// the window events session:loaded and session:new (detail.chosen: the viewer chose it).
import { api, html, render, icon, confirm, prompt, toast } from '/static/kit/panel-kit.js';

function relative(dateString) {
  const minutes = Math.floor((Date.now() - new Date(dateString)) / 60000);
  if (minutes < 1) return 'now';
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return days < 7 ? `${days}d` : new Date(dateString).toLocaleDateString();
}

function dayGroup(dateString) {
  const date = new Date(dateString);
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  if (date >= start) return 'Today';
  if (date >= new Date(start - 86400000)) return 'Yesterday';
  if (date >= new Date(start - 6 * 86400000)) return 'This week';
  return 'Earlier';
}

/**
 * How often the pane asks which sessions are running.
 *
 * A run is the thing being watched, and runs last from seconds to hours -- so the
 * mark has to appear soon after Send, not eventually. Four seconds is short enough
 * that it feels immediate and long enough that an idle pane is quiet; the poll is
 * one request with a list of ids in it, and it sleeps while the tab is hidden.
 */
const ACTIVITY_POLL_MS = 4000;

/** As many ids as one poll asks about; the server caps at the same number. */
const ACTIVITY_POLL_MAX_IDS = 200;

/** What the pane needs of a session -- not its messages, which a loaded session carries. */
function summary(session) {
  const { session_id, title, agent_name, updated_at, has_children } = session;
  return { session_id, title, agent_name, updated_at, has_children };
}

export class SessionManager {
  /**
   * @param {object} deps
   * @param {(session: {id: string, title: string}|null) => void} deps.onChange
   * @param {(anchor: Element, context: string, values: object) => void} deps.openContext
   *   offers the panels that open on a session
   * @param {() => void} deps.onShown  what the viewer picked -- a session, a new one -- is in the chat
   * @param {(sessions: object[]) => void} deps.onListChange  the list of sessions changed
   */
  constructor({ onChange, openContext, onShown, onListChange }) {
    this.onChange = onChange;
    this.openContext = openContext;
    this.onShown = onShown;
    this.onListChange = onListChange;
    this.pane = document.getElementById('sessionsPane');
    this.currentSessionId = null;
    this.currentTitle = null;
    this.sessions = [];
    this.byId = new Map();
    /** parent id -> its loaded sub-sessions, for every node shown expanded */
    this.expanded = new Map();
    this.filter = '';
    /** counted up by every choice of a session; a load whose count moved on is dropped */
    this.loading = 0;
    /** the session last asked for -- by a pick, the restore or the chat: open, or still on its way */
    this.requested = null;
    /** session id -> the messages the chat has written into it, sent or held */
    this.written = new Map();
    /** the sessions whose delete is past its questions and has not answered yet: they go */
    this.going = new Set();
    /** counted up by every reload of the list; an older reload's answer is dropped */
    this.listLoads = 0;
    /** session id -> the run working in it, as the server last reported them */
    this.active = new Map();
    /** counted up by every activity poll; an older poll's answer is dropped */
    this.activityPolls = 0;
    render(this.pane, html`
      <div class="sessions-head">
        <h2 class="pk-grow">Sessions</h2>
        <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="refresh"
                title="Reload the session list" aria-label="Reload the session list">${icon('refresh-cw', { size: 'sm' })}</button>
        <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="collapse-all"
                title="Collapse all sub-sessions">${icon('chevrons-down-up', { size: 'sm' })}</button>
        <button type="button" class="pk-btn pk-btn--sm" data-act="new" title="New session">${icon('plus', { size: 'sm' })} New</button>
      </div>
      <div class="sessions-filter pk-search">${icon('search')}<input class="pk-input pk-input--sm" type="search" placeholder="Filter" aria-label="Filter sessions"></div>
      <div class="sessions-list"></div>`);
    this.list = this.pane.querySelector('.sessions-list');
    this.pane.querySelector('[data-act="new"]').addEventListener('click', () => this.newConversation());
    this.pane.querySelector('[data-act="collapse-all"]').addEventListener('click', () => this.collapseAll());
    // By hand only: the list is reloaded after the chat's own runs, not for what another
    // process did meanwhile -- and a timer would reload it for nobody most of the time.
    this.pane.querySelector('[data-act="refresh"]').addEventListener('click', () => this.loadSessions());
    this.pane.querySelector('input[type="search"]').addEventListener('input', (event) => {
      this.filter = event.target.value.trim().toLowerCase();
      this.render();
    });
    this.list.addEventListener('click', (event) => this.onListClick(event));
  }

  getCurrentSessionId() { return this.currentSessionId; }

  remember(sessions) {
    sessions.forEach((s) => this.byId.set(s.session_id, summary(s)));
  }

  async loadSessions() {
    const load = ++this.listLoads;
    const data = await api('/api/sessions/hierarchy', { quiet: true }).catch(() => null);
    // open branches come along: runs add sub-sessions, renames and deletes change them
    const open = [...this.expanded.keys()];
    const branches = data ? await Promise.all(open.map((id) => this.children(id, { quiet: true }))) : [];
    // the latest reload wins: an older one answering after it may still list a session deleted since
    if (load !== this.listLoads) return;
    if (!data) {
      render(this.list, html`<div class="pk-empty">${icon('circle-alert')}<div>Sessions could not be loaded</div></div>`);
      return;
    }
    this.sessions = data.sessions || [];
    this.remember(this.sessions);
    open.forEach((id, index) => {
      if (!this.expanded.has(id)) return;  // closed meanwhile
      if (branches[index]) this.expanded.set(id, branches[index]);
      else this.expanded.delete(id);
    });
    this.render();
    this.onListChange(this.sessions);
    // a session started from the chat is named by the server only now
    const known = this.byId.get(this.currentSessionId);
    if (known && known.title && known.title !== this.currentTitle) this.setCurrent(this.currentSessionId, known.title);
  }

  row(session, depth) {
    const id = session.session_id;
    const children = this.expanded.get(id);
    return html`
      <div class="session-item" data-id="${id}" aria-current="${String(id === this.currentSessionId)}">
        <div class="session-row" style="--depth: ${depth}">
          ${session.has_children
            ? html`<button type="button" class="session-expand" data-act="expand" aria-expanded="${String(Boolean(children))}" title="Sub-sessions">${icon('chevron-right', { size: 'sm' })}</button>`
            : html`<span class="session-expand-spacer"></span>`}
          <button type="button" class="session-open pk-truncate" data-act="open" title="${session.title || 'Untitled'}">${session.title || 'Untitled'}</button>
          <span class="session-running" title="An agent is working in this session" aria-hidden="true"></span>
          <span class="session-agent pk-truncate" aria-hidden="true"></span>
          <span class="session-running-said pk-sr-only"></span>
          <span class="session-meta">${relative(session.updated_at)}</span>
          <span class="session-actions">
            <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="info" title="Open in a panel">${icon('info', { size: 'sm' })}</button>
            <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="rename" title="Rename">${icon('pencil', { size: 'sm' })}</button>
            <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="delete" title="Delete">${icon('trash-2', { size: 'sm' })}</button>
          </span>
        </div>
        ${children ? html`<div class="session-children">${children.map((child) => this.row(child, depth + 1))}</div>` : ''}
      </div>`;
  }

  render() {
    // re-rendering (after every run) must not throw the keyboard focus out of the list
    const focused = this.list.contains(document.activeElement) ? document.activeElement : null;
    const refocus = focused && { id: focused.closest('[data-id]')?.dataset.id, act: focused.dataset.act };
    const shown = this.sessions.filter((s) => !this.filter
      || `${s.title} ${s.agent_name}`.toLowerCase().includes(this.filter));
    if (!shown.length) {
      render(this.list, html`<div class="pk-empty">${icon('messages-square')}
        <div>${this.filter ? 'No matching sessions' : 'No sessions yet -- the first message starts one.'}</div></div>`);
      return;
    }
    const groups = new Map();
    shown.forEach((s) => {
      const group = dayGroup(s.updated_at);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(s);
    });
    render(this.list, [...groups].map(([group, sessions]) => html`
      <div class="sessions-group">${group}</div>${sessions.map((s) => this.row(s, 0))}`));
    if (refocus?.id) {
      this.list.querySelector(`[data-id="${CSS.escape(refocus.id)}"] [data-act="${refocus.act}"]`)?.focus();
    }
    this.markActive();
  }

  /**
   * Session ids the pane is showing: the roots, plus the children of expanded nodes.
   *
   * Cut to what one poll may ask about. The server cuts too, but silently and after
   * the query string has already been built -- past a few hundred ids that string
   * runs into proxy limits, and the rows beyond the cut would stay unmarked with
   * nothing saying why. Cut here, it is one place and it can be said out loud.
   */
  visibleIds() {
    const ids = this.sessions.map((s) => s.session_id);
    for (const children of this.expanded.values()) {
      for (const child of children || []) ids.push(child.session_id);
    }
    const shown = ids.filter(Boolean);
    if (shown.length > ACTIVITY_POLL_MAX_IDS) {
      console.warn(`[sessions] ${shown.length} sessions shown; only the first `
        + `${ACTIVITY_POLL_MAX_IDS} are checked for a running agent`);
    }
    return shown.slice(0, ACTIVITY_POLL_MAX_IDS);
  }

  /**
   * Put the running marks on the rows, in place.
   *
   * In place rather than through render(): the marks change on their own timer, and
   * redrawing the list for them would fight the focus restore above and throw away the
   * expanded branches' DOM several times a minute for a dot.
   *
   * For anyone not looking at it, the mark is a word in the row's own text, revealed
   * and hidden with the dot. It used to be `aria-busy` on the `.session-item`, which
   * says something else: that the element is being CHANGED and may be skipped for now
   * -- and that element wraps the expanded sub-sessions too, so a whole branch could
   * go quiet for as long as an agent worked in its parent. The dot itself stays
   * decoration (aria-hidden); a title on a span is mouse-only anyway.
   */
  markActive() {
    this.list.querySelectorAll('.session-item').forEach((item) => {
      const entry = this.active.get(item.dataset.id);
      const running = Boolean(entry);
      item.classList.toggle('is-running', running);
      // WHICH agent: the server names it, and a run started from outside -- a book on
      // the server, a woken session -- is exactly the one nobody knows the agent of.
      const agent = (entry && entry.agent_name) || '';
      const row = item.querySelector(':scope > .session-row');
      const label = row && row.querySelector(':scope > .session-agent');
      if (label) label.textContent = agent;
      const said = row && row.querySelector(':scope > .session-running-said');
      if (said) said.textContent = !running ? '' : agent ? `${agent} is working in this session` : 'An agent is working in this session';
      const dot = row && row.querySelector(':scope > .session-running');
      if (dot) dot.title = agent ? `${agent} is working in this session` : 'An agent is working in this session';
    });
  }

  /**
   * The run working in one session right now, or null -- asked of the server.
   *
   * The ENTRY, not its request id: a run worked on by another process carries
   * none, and reading that as "no run" is what let a delete go through under a
   * woken agent. What can be cancelled is `request_id`; that there is something
   * at all is the entry itself.
   *
   * NOT read from `this.active`: that map is drawn from a poll on its own timer and
   * is replaced whole on every tick. A delete that consulted it could find the entry
   * gone a moment after the pane drew the mark, and would then leave the agent
   * working for a session nobody will write again -- silently, since there is nothing
   * left to show. Drawing may be a tick stale; cancelling may not.
   */
  async runningIn(id) {
    const data = await api(`/api/sessions/active?ids=${encodeURIComponent(id)}`, { quiet: true })
      .catch(() => null);
    const run = data?.active?.[id];
    // A run past its answer is left alone, as the server leaves it when IT cancels a
    // deleted session's runs: cancelling one takes its background sub-agents with it,
    // and it is only finishing anyway -- saves and session-end hooks.
    if (!run || run.answered) return null;
    return run;
  }

  /**
   * Which of the shown sessions an agent is working in right now.
   *
   * Polled, not pushed. A run's start and end are all this has to catch, the pane
   * already lives on timers, and a second long-lived stream per tab would need its own
   * reconnect and backoff to say the same thing. The poll asks only about the rows on
   * screen, which is also what keeps the answer free of other users' sessions.
   *
   * A failed poll changes nothing: the marks stand until the next one corrects them.
   * Better a mark one tick stale than a list that flickers empty on one bad request.
   */
  async refreshActivity() {
    if (document.visibilityState !== 'visible') return;
    const ids = this.visibleIds();
    if (!ids.length) return;
    const poll = ++this.activityPolls;
    const data = await api(`/api/sessions/active?ids=${encodeURIComponent(ids.join(','))}`,
      { quiet: true }).catch(() => null);
    if (!data) return;
    // A slow answer must not land on top of a newer one: the marks would jump back to
    // an older picture and stay there until the next tick.
    if (poll !== this.activityPolls) return;
    this.active = new Map(Object.entries(data.active || {}));
    this.markActive();
  }

  /** Start the activity poll. The shell owns the cadence, as it does for health. */
  watchActivity(everyMs = ACTIVITY_POLL_MS) {
    this.refreshActivity();
    document.addEventListener('visibilitychange', () => this.refreshActivity());
    return setInterval(() => this.refreshActivity(), everyMs);
  }

  async onListClick(event) {
    const item = event.target.closest('.session-item');
    const button = event.target.closest('[data-act]');
    if (!item || !button) return;
    const id = item.dataset.id;
    const act = button.dataset.act;
    if (act === 'expand') await this.toggleChildren(id);
    else if (act === 'info') this.openContext(button, 'session', { session_id: id });
    else if (act === 'rename') await this.rename(id);
    else if (act === 'delete') await this.remove(id);
    else if (act === 'open') await this.loadSession(id);
  }

  /** A node's sub-sessions, or null when they could not be loaded. */
  async children(id, { quiet }) {
    const data = await api(`/api/sessions/${encodeURIComponent(id)}/children`, { quiet }).catch(() => null);
    if (!data) return null;
    this.remember(data.sessions || []);
    return data.sessions || [];
  }

  async toggleChildren(id) {
    if (this.expanded.has(id)) {
      this.expanded.delete(id);
    } else {
      const children = await this.children(id, { quiet: false });
      if (!children) return;  // api() has shown the failure; the node stays closed
      this.expanded.set(id, children);
    }
    this.render();
  }

  /**
   * Close every open branch at once. A deep tree is otherwise closed one chevron at a
   * time, and each of those is a click on a row that scrolls away as the list shortens.
   *
   * Offered whether or not anything is open, the way a file tree's is -- and enabled,
   * not merely present. Hiding it once nothing is open takes the focus of whoever just
   * pressed it, measured; DISABLING it does not, also measured, but a disabled button
   * leaves the tab order, so the header's keyboard path would change under the viewer
   * as a side effect of pressing something. Both are pinned by the test. With nothing
   * open it does nothing, and the button stays where the eye last found it.
   *
   * Only the branches are closed. `children()` keeps no cache, so reopening reloads --
   * which is what keeps a branch honest after a run has added sub-sessions to it.
   */
  collapseAll() {
    if (!this.expanded.size) return;
    this.expanded.clear();
    this.render();
  }

  async rename(id) {
    const session = this.byId.get(id);
    const title = await prompt('New title for the session', { title: 'Rename session', value: session?.title || '', confirmLabel: 'Rename' });
    if (!title || !title.trim() || title === session?.title) return;
    await this.renameTo(id, title.trim());
  }

  /**
   * Rename without asking — the chat's `/title <text>` already has one.
   *
   * The write and the two refreshes live here rather than at each caller:
   * the pencil and the command have to leave the list in the same state,
   * and the current session's header with it.
   *
   * @returns {Promise<boolean>} false when the write failed (api() said so).
   */
  async renameTo(id, title) {
    try {
      await api(`/api/sessions/${encodeURIComponent(id)}`, { method: 'PATCH', json: { title } });
    } catch {
      return false;  // api() has shown the failure
    }
    if (id === this.currentSessionId) this.setCurrent(id, title);
    await this.loadSessions();
    return true;
  }

  async remove(id) {
    const session = this.byId.get(id);
    const ok = await confirm(`"${session?.title || 'Untitled'}" and its messages will be deleted. This cannot be undone.`,
      { title: 'Delete session', confirmLabel: 'Delete', danger: true });
    if (!ok) return;
    // Its runs are stopped first: a refusal leaves the session untouched. That takes a while -- a message
    // written into the session meanwhile keeps it; a pick of another session, and a message there, do not.
    const written = this.written.get(id);
    if (!await this.cancelLostRun(id)) return;
    if (id === this.currentSessionId && !await this.leaveRunningRequest('Deleting it')) return;
    if (this.written.get(id) !== written) {
      toast('The session was written into while it was being deleted; it stays', { kind: 'warn' });
      return;
    }
    // From here the session goes: it opens no more, and a message into it -- shown again by a load that was on
    // its way -- waits in the composer, since the server does not keep a run of a deleted session.
    this.going.add(id);
    try {
      if (id === this.currentSessionId) {
        // The chat lets go of it before it is deleted, so neither a message sent meanwhile
        // nor a load of it still on its way brings it back; a pick of another one may take the chat over.
        if (this.requested === id) this.startNew();
        else this.showNew();
      }
      try {
        await api(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
      } catch {
        return;  // api() has shown the failure; a pick of another session on its way still opens
      }
      this.expanded.delete(id);
      // A pick or the restore asked for it before the delete: its answer may be on its way still -- it must not
      // open the session -- or have opened it already, since a browser holds the DELETE back until that load has
      // its response headers.
      if (id === this.requested) {
        if (id === this.currentSessionId || !this.currentSessionId) {
          this.startNew();
        } else {
          this.requested = null;
          this.loading++;
        }
      }
    } finally {
      this.going.delete(id);
    }
    await this.loadSessions();
  }

  /** Whether the session is being deleted past its questions: it takes no message. */
  isBeingDeleted(id) {
    return this.going.has(id);
  }

  /**
   * A run of the session whose connection was lost may still be going, for a session that is about to be gone:
   * deleting the session cancels it first.
   */
  async cancelLostRun(id) {
    // taken before asking: the chat may let the run go while the question is open
    //
    // Two kinds of run end up here. One whose connection was lost, which the chat
    // still has stored -- and, since leaving a session stopped cancelling, one that
    // is simply working in a session the viewer is not looking at. The chat knows
    // nothing of the second: it let go of its stream and forgot it. The activity poll
    // does, which is why it is asked as well. Without this a delete would leave an
    // agent working for a session the server will never write again.
    // The open session's own run is left to leaveRunningRequest below, or both would ask.
    const followedHere = id === this.currentSessionId && window.chatModule.activeRun();
    const lost = window.chatModule.lostRunIn(id);
    const entry = (lost || followedHere) ? null : await this.runningIn(id);
    const run = lost || entry?.request_id || null;
    if (!run) {
      if (!entry) return true;
      // Something IS working, and nothing here can stop it: a woken run is
      // agent-cli in a process of its own, while cancel_job walks this one and
      // there is no request id to walk to. Deleting now is the case the
      // paragraph above is about -- an agent writing for a session that will
      // never be shown again -- only with no way to end it first.
      toast('This session is being worked on in another process, which cannot be stopped from here; it stays',
        { kind: 'warn' });
      return false;
    }
    const ok = await confirm('A run of this session may still be going. Deleting the session cancels it.',
      { title: 'Run may still be going', confirmLabel: 'Cancel the run', danger: true });
    if (!ok) return false;
    if (await window.chatModule.cancelLostRun(run)) return true;
    toast('The server did not confirm that the run was cancelled; the session stays', { kind: 'error' });
    return false;
  }

  /**
   * Whether a running request of the open session may be cancelled. `answer`: 'nothing' (none runs), 'confirmed'
   * or 'declined'; `run`: the chat's run the viewer was asked about.
   */
  async askToCancelRunningRequest(action) {
    const run = window.chatModule.activeRun();
    if (!run) return { answer: 'nothing' };
    const ok = await confirm(`A request is still running in this session. ${action} cancels it.`,
      { title: 'Request running', confirmLabel: 'Cancel the request', danger: true });
    return { answer: ok ? 'confirmed' : 'declined', run };
  }

  /** Cancel it, as confirmed: false, with a toast, when the server did not confirm it has stopped. */
  async cancelRunningRequest(run) {
    const outcome = await window.chatModule.cancelActiveRequest(run);
    if (outcome === 'stopped') return true;
    toast(outcome === 'starting'
      ? 'The request is still starting and cannot be cancelled yet; the session stays open'
      : 'The server did not confirm that the request has stopped; the session stays open', { kind: 'error' });
    return false;
  }

  /** A running request belongs to the open session: leaving it means cancelling it first. */
  async leaveRunningRequest(action) {
    const { answer, run } = await this.askToCancelRunningRequest(action);
    return answer === 'nothing' || (answer === 'confirmed' && await this.cancelRunningRequest(run));
  }

  /** The new session: nothing still loading may replace it. */
  startNew({ chosen = true } = {}) {
    this.loading++;
    this.requested = null;
    this.showNew({ chosen });
  }

  /** An empty chat that continues no session -- chosen, or all that is left when the stored one cannot be shown. */
  showNew({ chosen = true } = {}) {
    this.setCurrent(null, null);
    window.dispatchEvent(new CustomEvent('session:new', { detail: { chosen } }));
  }

  async newConversation() {
    this.startNew();
    this.onShown();
  }

  /**
   * Open a session in the chat. Resolves true when it is shown, false when it could not be, and null when a later
   * click took over: clicked twice quickly, the last click wins, not the last answer. `quiet`: a failed load is the
   * caller's to handle -- no toast, and no new chat in its place.
   */
  async loadSession(id, { quiet = false } = {}) {
    if (this.going.has(id)) {
      toast('The session is being deleted', { kind: 'warn' });
      return false;
    }
    // the open session while its run works: it is already shown, and there is no
    // stream to move -- the chat keeps following the one it has. Still a click: one on
    // another session still loading must not take the chat from it.
    if (id === this.currentSessionId && window.chatModule.activeRun()) {
      this.loading++;
      this.requested = id;
      this.onShown();
      return true;
    }
    const attempt = ++this.loading;
    this.requested = id;
    let session;
    try {
      session = await api(`/api/sessions/${encodeURIComponent(id)}`, { quiet });
    } catch {
      if (attempt !== this.loading) return null;
      // api() already told the user; with no session open the chat offers a start again -- as it did, no choice
      if (!quiet && !this.currentSessionId) this.startNew({ chosen: false });
      return false;
    }
    if (attempt !== this.loading) return null;
    this.show(session);
    this.onShown();
    return true;
  }

  /** Put a fetched session into the chat -- read-only when its sub-agent cannot be picked here. */
  show(session) {
    const id = session.session_id;
    this.remember([session]);
    this.setCurrent(id, session.title || session.name);
    const readOnly = Boolean(session.depth) && !window.selectorModule.hasAgent(session.agent_name);
    window.dispatchEvent(new CustomEvent('session:loaded', {
      detail: {
        session,
        readOnly,
        reason: readOnly ? `Sub-agent "${session.agent_name}" is not available in the agent selector` : null,
      },
    }));
  }

  setCurrent(id, title) {
    this.currentSessionId = id;
    this.currentTitle = id ? title || this.byId.get(id)?.title || 'Untitled' : null;
    if (id) sessionStorage.setItem('lastSessionId', id);
    else sessionStorage.removeItem('lastSessionId');
    this.list.querySelectorAll('.session-item').forEach((item) => {
      item.setAttribute('aria-current', String(item.dataset.id === id));
    });
    this.onChange(id ? { id, title: this.currentTitle } : null);
  }

  /**
   * The chat reports the session it continues: its stream's (a new one on the first message),
   * or a run's reattached after a reload.
   */
  setCurrentSession(id) {
    this.loading++;  // the chat decided: a session still loading must not take the header
    this.requested = id;
    this.setCurrent(id, id ? this.byId.get(id)?.title : null);
  }

  /** The chat writes a message into its session, sent or held: a delete waiting for its run to stop gives way to it. */
  messageWritten(id) {
    this.written.set(id, (this.written.get(id) ?? 0) + 1);
    this.setCurrentSession(id);
  }

  /** A stream names its session: a new one on the first message, or the one of a run reattached after a reload. */
  onSessionUpdated(id) {
    // Naming the session last chosen is no new choice (a delete waiting for its run must not
    // take it for one). Naming another -- a new session, or the chat's own while a pick is on
    // its way -- is the chat's: its run shows there.
    if (id !== this.requested) this.setCurrentSession(id);
    this.loadSessions();
  }

  /**
   * Reopen the session this tab had open; when it is gone, start a new one.
   * Anything chosen before or meanwhile wins -- a pick, a message, and a run
   * the chat reattached after the reload, which reports the session it runs in.
   */
  async restore() {
    if (this.loading) return;  // a session was chosen before the shell was ready: that one stays
    const last = sessionStorage.getItem('lastSessionId');
    const attempt = ++this.loading;
    this.requested = last;
    const session = last && await api(`/api/sessions/${encodeURIComponent(last)}`, { quiet: true }).catch(() => null);
    if (attempt !== this.loading) return;
    if (session) this.show(session);
    else this.startNew({ chosen: false });
  }
}
