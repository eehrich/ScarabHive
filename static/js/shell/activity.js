// What works now, at the top of the session pane -- before any session is picked: every agent's turn with the
// sub-agents below it, the panels' tasks and the work that outlived its turn, as a tree by the session each works
// below, each with the step it is on (agent_system/core/turn_runs.py, docs/run_events.md). Under it what waits for
// the person, and what ended within the last hour. A click opens a run where it is followed -- a turn's session in
// the chat, a panel's task in its panel -- and Stop ends one from here. Fed by the run events (run-events.js), like
// the pill in the head, which is this folded to a line. Hidden while there is nothing to say. The terminal reads the
// same rows the same way (agent_system/run_tree.py: `agent-cli runs`, /runs).
import { html, icon, update } from '/static/kit/panel-kit.js';
import { ACTIVE, FINAL, TURNS, followRuns, isSilent, liveRun, stepLabel } from '/static/kit/run-events.js';

/** How often the tree looks again whether a run went quiet or a minute passed, in ms; how many ended runs it lists,
 * how long ago they may have ended (ms) and how many it remembers; how long a Stop that was sent holds its button
 * (ms); the language of the steps (the browser's, as the pill tells them). Tests set them. */
export const ACTIVITY = { checkMs: 5000, ended: 6, endedMs: 3600000, kept: 40, holdMs: 20000,
  lang: (navigator.language || 'en').slice(0, 2).toLowerCase() };

export { TURNS };

/** "now", "12 min", "1 h 05" -- how long since `since` (an ISO time). By the minute: a clock that moves every second
 * would redraw the tree every second. */
export function since(iso, now = Date.now()) {
  const start = Date.parse(iso || '');
  if (Number.isNaN(start)) return '';
  const minutes = Math.floor(Math.max(0, now - start) / 60000);
  if (minutes < 1) return 'now';
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')}`;
}

/** A run that ended and needs nobody, and is a sub-agent's: shown nowhere -- its end is its caller's to hear of. */
const spent = (run) => FINAL.has(run.state) && !run.needsYou && run.source === 'sub_agent';

export class ActivityTree {
  /** host: the element the tree is drawn into; onOpen(run): show the run where it is followed; current(): the id of
   * the session open in the chat; notify(message): say something to the person (a Stop that could not be sent);
   * onMove(run): a run said something -- a step, its end. */
  constructor({ host, onOpen, current = () => '', notify = () => {}, onMove = () => {} }) {
    this.host = host;
    this.onOpen = onOpen;
    this.current = current;
    this.notify = notify;
    this.onMove = onMove;
    this.runs = new Map();
    /** the runs a Stop was sent for, until they end; and those no Stop from here reaches (held by another process,
     * or by this server with nothing to stop them by) */
    this.stopping = new Set();
    this.unstoppable = new Set();
    this.timer = 0;
    this.stream = null;
    host.hidden = true;
    host.addEventListener('click', (event) => {
      const item = event.target.closest('[data-run]');
      const run = item && this.runs.get(item.dataset.run);
      if (!run) return;
      if (event.target.closest('[data-act="stop"]')) this.stop(run);
      else if (event.target.closest('[data-act="open"]')) this.onOpen(run);
    });
  }

  start() {
    this.stream = followRuns({
      onSnapshot: (list) => {
        // What there is -- but a run this page has heard more of than the list knows keeps what it heard: the
        // list is read a moment before it arrives, and an end told meanwhile is not taken back.
        const before = this.runs;
        this.runs = new Map();
        for (const row of list) {
          const had = before.get(row.run_id);
          const run = liveRun(row);
          this.runs.set(row.run_id, had && had.seq > run.seq ? had : run);
        }
        this.forget();
        this.draw();
      },
      onEvent: (event) => {
        const run = liveRun(event, this.runs.get(event.run_id));
        this.runs.set(event.run_id, run);
        this.forget();
        this.draw();
        if (event.type !== 'run.heartbeat') this.onMove(run);
      },
    });
    this.timer = setInterval(() => this.draw(), ACTIVITY.checkMs);
  }

  close() {
    this.stream?.close();
    clearInterval(this.timer);
  }

  /** Of the ended runs only the newest are remembered -- the page may stay open for days -- and of the sub-agents
   * none: a turn with sixty of them would push every other end out. */
  forget() {
    for (const run of [...this.runs.values()]) if (spent(run)) this.runs.delete(run.run_id);
    const ended = [...this.runs.values()].filter((run) => FINAL.has(run.state) && !run.needsYou);
    if (ended.length <= ACTIVITY.kept) return;
    ended.sort((a, b) => (a.ended < b.ended ? -1 : 1));
    for (const run of ended.slice(0, ended.length - ACTIVITY.kept)) this.runs.delete(run.run_id);
  }

  /** The runs at work as a tree, flat in the order it is read: [run, depth]. A run stands below the run of the
   * session it works below while that one is at work too; else on its own. Oldest first: nothing jumps. */
  working() {
    const active = [...this.runs.values()].filter((run) => ACTIVE.has(run.state))
      .sort((a, b) => (a.started < b.started ? -1 : a.started > b.started ? 1 : 0));
    const ids = new Set(active.map((run) => run.run_id));
    const below = new Map();
    for (const run of active) {
      const parent = ids.has(run.parent) && run.parent !== run.run_id ? run.parent : '';
      below.set(parent, [...(below.get(parent) || []), run]);
    }
    const out = [];
    // from the runs on their own down: runs that name each other in a circle hang on none of them, and are not
    // reached -- the walk ends
    const walk = (parent, depth) => {
      for (const run of below.get(parent) || []) {
        out.push([run, depth]);
        walk(run.run_id, depth + 1);
      }
    };
    walk('', 0);
    return out;
  }

  /** The runs that wait for their person, the one that came to wait last first. */
  waiting() {
    return [...this.runs.values()].filter((run) => run.needsYou).sort((a, b) => (a.waits > b.waits ? -1 : a.waits < b.waits ? 1 : 0));
  }

  /** What ended within the last hour and needs nobody, the newest first (the sub-agents are forgotten as they end). */
  ended(now = Date.now()) {
    return [...this.runs.values()]
      .filter((run) => FINAL.has(run.state) && !run.needsYou && now - Date.parse(run.ended) < ACTIVITY.endedMs)
      .sort((a, b) => (a.ended > b.ended ? -1 : 1)).slice(0, ACTIVITY.ended);
  }

  /** Stop, sent once: its button is held until the run ends -- or for a while, where the run does not end (stuck in
   * work that does not look at the stop): then it can be sent again. */
  async stop(run) {
    const id = run.run_id;
    this.stopping.add(id);
    this.draw();
    let refused = '';
    let fixed = false;
    try {
      const response = await fetch(`/api/agent-runs/${encodeURIComponent(id)}/cancel`, { method: 'POST', credentials: 'include' });
      // 409: nothing here stops it. Another process holds it (an agent-cli's turn), or this server has no stop for
      // it (the server says which) -- or it ended as the Stop was on its way, and its end says the rest.
      const elsewhere = response.status === 409 && Boolean(response.headers.get('X-Run-Held-Elsewhere'));
      fixed = elsewhere || (response.status === 409 && Boolean(response.headers.get('X-Run-Takes-No-Stop')));
      if (elsewhere) refused = `${run.agent} runs in another process: stop it there`;
      else if (fixed) refused = `${run.agent} cannot be stopped from here`;
      else if (!response.ok && response.status !== 409) refused = `${run.agent} could not be stopped (${response.status})`;
      if (response.ok) {
        setTimeout(() => { if (this.stopping.delete(id)) this.draw(); }, ACTIVITY.holdMs);
        return;
      }
    } catch {
      refused = `${run.agent} could not be stopped: the server did not answer`;
    }
    this.stopping.delete(id);
    if (fixed) this.unstoppable.add(id);
    if (refused) this.notify(refused);
    this.draw();
  }

  /** What a run says of itself in a line; nothing where its mark says it all (done, stopped). */
  line(run) {
    if (ACTIVE.has(run.state)) {
      const step = stepLabel(run.step, ACTIVITY.lang) || (run.state === 'queued' ? 'Waiting to start' : 'At work');
      return isSilent(run) ? `${step} -- no sign of life for 30 s` : step;
    }
    if (run.state === 'needs_input') return `Asks: ${run.payload?.question || 'a question'}`;
    if (run.state === 'review') return 'Done -- please check the result';
    if (run.state === 'done' || run.state === 'cancelled') return '';
    const why = run.payload?.reason ? `: ${run.payload.reason}` : '';
    return run.state === 'handed_over' ? `Handed over to you${why}` : `Failed${why}`;
  }

  /** How a run that ended did, for a reader who cannot see its mark. */
  said(run) {
    return this.line(run) || (run.state === 'done' ? 'Done' : 'Stopped');
  }

  mark(run) {
    if (ACTIVE.has(run.state)) return html`<span class="ai-live-dot${isSilent(run) ? ' ai-live-dot--quiet' : ''}" aria-hidden="true"></span>`;
    if (run.needsYou) return icon(run.state === 'needs_input' ? 'circle-help' : 'bell', { size: 'sm' });
    return icon(run.state === 'done' ? 'check' : run.state === 'cancelled' ? 'square' : 'x', { size: 'sm' });
  }

  row(run, depth = 0) {
    const line = this.line(run);
    const stoppable = ACTIVE.has(run.state) && !this.unstoppable.has(run.run_id);
    const sending = this.stopping.has(run.run_id);
    return html`<li class="activity-item" data-run="${run.run_id}" data-state="${run.state}" style="--depth: ${depth}"
        aria-current="${String(run.run_id === this.current())}">
      <button type="button" class="activity-open" data-act="open" data-key="activity:${run.run_id}"
              title="${run.agent}${run.title ? ` · ${run.title}` : ''}\n${this.said(run)}">
        <span class="activity-mark">${this.mark(run)}</span>
        <span class="activity-agent pk-truncate">${run.agent}</span>
        <span class="activity-time">${since(FINAL.has(run.state) ? run.ended : run.started)}</span>
        <span class="activity-title pk-truncate">${run.title}</span>
        ${line ? html`<span class="activity-line pk-truncate">${line}</span>` : ''}
      </button>
      ${stoppable ? html`<button type="button" class="activity-stop pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm"
          data-act="stop" data-key="activity-stop:${run.run_id}" ${sending ? html`disabled` : ''}
          title="${sending ? 'Stopping…' : `Stop ${run.agent}`}" aria-label="Stop ${run.agent}">${icon('square', { size: 'sm' })}</button>` : ''}
    </li>`;
  }

  group(name, title, rows) {
    return rows.length ? html`<section class="activity-group" data-activity="${name}">
      <h3 class="activity-heading">${title}<span class="sessions-count">${rows.length}</span></h3>
      <ul class="activity-list">${rows}</ul></section>` : '';
  }

  draw() {
    // a Stop sent, or refused for a run held elsewhere, is that turn's: the session's next turn starts clean
    for (const held of [this.stopping, this.unstoppable]) {
      for (const id of [...held]) if (!ACTIVE.has(this.runs.get(id)?.state)) held.delete(id);
    }
    const working = this.working(), waiting = this.waiting(), ended = this.ended();
    this.host.hidden = working.length + waiting.length + ended.length === 0;
    this.host.dataset.working = String(working.length);
    this.host.dataset.needs = String(waiting.length);
    update(this.host, html`${this.group('working', 'At work', working.map(([run, depth]) => this.row(run, depth)))}
      ${this.group('needs', 'Needs you', waiting.map((run) => this.row(run)))}
      ${this.group('ended', 'Just ended', ended.map((run) => this.row(run)))}`);
  }
}
