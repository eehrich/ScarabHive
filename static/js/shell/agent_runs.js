// The agents at work, in the head of the shell: how many run now, what the newest one is doing in plain words, a
// warning when one has gone quiet -- and which wait for their person: a result to check, or a failure handed over.
// On every tab, whichever panel started them. A click opens the panel of the run that needs the person first, else of
// the newest one at work; there its "Agent runs" sheet shows it. When a run comes to need the person while they look
// elsewhere, a system notification says so (once they allowed it with the bell beside the pill). Fed by the run events
// (run-events.js); hidden while no agent works and none waits.
import { html, icon, update } from '/static/kit/panel-kit.js';
import { ACTIVE, FINAL, followRuns, isSilent, liveRun, stepLabel } from '/static/kit/run-events.js';

/** How often the pill looks again whether a run went quiet, in ms, and the language of the steps: the browser's --
 * the shell speaks English, but what an agent does is told in the reader's own language where the agent names it
 * so (tests set both). */
export const PILL = { checkMs: 5000, lang: (navigator.language || 'en').slice(0, 2).toLowerCase() };

/** Whether the person looks at another window or tab right now: then a notification is the way to reach them. */
const away = () => document.hidden || !document.hasFocus();

export class AgentRunsPill {
  /** button: the pill; bell: the button that asks to allow notifications (optional); onOpen(run): open the panel
   * that started `run` ({source, run_id, ...}). */
  constructor({ button, bell = null, onOpen }) {
    this.button = button;
    this.bell = bell;
    this.runs = new Map();
    this.timer = 0;
    this.stream = null;
    button.hidden = true;
    if (bell) {
      bell.hidden = true;
      bell.addEventListener('click', async () => {
        try { await Notification.requestPermission(); } catch { /* an old browser: no notifications */ }
        this.draw();
      });
    }
    button.addEventListener('click', () => {
      const [run] = this.ordered();
      if (run) onOpen(run);
    });
  }

  start() {
    this.stream = followRuns({
      onSnapshot: (list) => {
        this.runs.clear();
        for (const row of list) if (!FINAL.has(row.state) || row.needs_you) this.runs.set(row.run_id, liveRun(row));
        this.draw();
      },
      onEvent: (event) => {
        const before = this.runs.get(event.run_id);
        const run = liveRun(event, before);
        if (FINAL.has(run.state) && !run.needsYou) this.runs.delete(event.run_id);
        else this.runs.set(event.run_id, run);
        this.tell(run, before);
        this.draw();
      },
    });
    this.timer = setInterval(() => this.draw(), PILL.checkMs);
  }

  stop() {
    this.stream?.close();
    clearInterval(this.timer);
  }

  /** The runs at work, the one heard of last first. */
  working() {
    return [...this.runs.values()].filter((run) => ACTIVE.has(run.state)).reverse();
  }

  /** The runs that wait for their person, the one heard of last first. */
  waiting() {
    return [...this.runs.values()].filter((run) => run.needsYou).reverse();
  }

  /** Who a click leads to: the runs that need the person, then the ones at work. */
  ordered() {
    return [...this.waiting(), ...this.working()];
  }

  /** A system notification when a run has come to need its person, or ended, while they look elsewhere. */
  tell(run, before) {
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted' || !away()) return;
    const became = (state) => run.state === state && before?.state !== state;
    let body = '';
    if (became('needs_input')) body = `Has a question for you: ${run.payload?.question || ''}`;
    else if (became('review')) body = 'Done -- please check the result';
    else if (run.needsYou && (became('failed') || became('handed_over'))) body = 'Could not go on -- it needs you';
    else if (became('done') && before?.state !== 'review') body = 'Done';
    if (!body) return;
    try {
      const note = new Notification(`${run.agent}${run.title ? ` · ${run.title}` : ''}`, { body, tag: run.run_id });
      note.onclick = () => { window.focus(); this.button.click(); note.close(); };
    } catch { /* a page that may not notify (some mobile browsers) */ }
  }

  draw() {
    const working = this.working();
    const waiting = this.waiting();
    const shown = working.length + waiting.length > 0;
    this.button.hidden = !shown;
    this.button.dataset.runsWorking = String(working.length);
    this.button.dataset.runsNeeds = String(waiting.length);
    if (this.bell) this.bell.hidden = !shown || typeof Notification === 'undefined' || Notification.permission !== 'default';
    if (!shown) return;
    const quiet = working.filter((run) => isSilent(run)).length;
    const step = working.length ? stepLabel(working[0].step, PILL.lang) : '';
    this.button.dataset.runsQuiet = String(quiet);
    update(this.button, html`${working.length ? html`<span class="ai-live-dot${quiet ? ' ai-live-dot--quiet' : ''}" aria-hidden="true"></span>
      <span class="app-runs-count">${working.length === 1 ? '1 agent at work' : `${working.length} agents at work`}</span>
      ${step ? html`<span class="app-runs-step pk-truncate" data-runs-step>${step}</span>` : ''}` : ''}
      ${quiet ? html`<span class="pk-badge pk-badge--warn" data-runs-quiet>${quiet} quiet</span>` : ''}
      ${waiting.length ? html`<span class="pk-badge pk-badge--warn" data-runs-needs>${icon('bell', { size: 'sm' })} ${waiting.length} need${waiting.length === 1 ? 's' : ''} you</span>` : ''}`);
    this.button.title = [
      ...waiting.map((run) => `${run.agent}${run.title ? ` · ${run.title}` : ''}: ${run.state === 'review' ? 'done, please check' : run.state === 'needs_input' ? 'has a question' : 'needs you'}`),
      ...working.map((run) => `${run.agent}${run.title ? ` · ${run.title}` : ''}: ${stepLabel(run.step, PILL.lang) || 'at work'}${
        isSilent(run) ? ' -- no sign of life for 30 s' : ''}`),
      'Click: open the panel that started it',
    ].join('\n');
  }
}
