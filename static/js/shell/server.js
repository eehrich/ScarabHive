// The server's own two actions, for admins: stop it, or restart it so new plugins, agents and skills are loaded.
// Both ask first and say how many requests are running: those end with the server.
import { api, confirm, html, render, showToast } from '/static/kit/panel-kit.js';

/** How long a restarted server may take to answer again before the page stops waiting. */
const RESTART_TIMEOUT = 180000;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class ServerControls {
  /** state: the status bar's connection item; onBusy(bool): the health poll steps aside while this one watches. */
  constructor({ stopButton, restartButton, state, onBusy = () => {} }) {
    this.buttons = [stopButton, restartButton];
    this.state = state;
    this.onBusy = onBusy;
    this.busy = false;
    stopButton.addEventListener('click', () => this.stop());
    restartButton.addEventListener('click', () => this.restart());
  }

  /** Only an admin gets the buttons: the routes behind them refuse everyone else. */
  show(visible) {
    this.buttons.forEach((button) => { button.hidden = !visible; });
  }

  /** "2 requests are running and end with it. " -- or nothing, also when the count cannot be had. */
  async running() {
    const total = await api('/admin/active-sessions', { quiet: true }).then((answer) => answer.total).catch(() => 0);
    return total ? `${total} ${total === 1 ? 'request is' : 'requests are'} running and ${total === 1 ? 'ends' : 'end'} with it. ` : '';
  }

  say(dot, text) {
    render(this.state, html`<span class="pk-dot pk-dot--${dot}"></span> <span>${text}</span>`);
  }

  async ask(action, question, options) {
    if (this.busy) return null;
    if (!await confirm(`${await this.running()}${question}`, options)) return null;
    this.busy = true;
    this.buttons.forEach((button) => { button.disabled = true; });
    this.onBusy(true);
    try {
      return await api(`/admin/server/${action}`, { method: 'POST' });  // api() shows a refusal
    } catch {
      this.done();
      return null;
    }
  }

  done() {
    this.busy = false;
    this.buttons.forEach((button) => { button.disabled = false; });
    this.onBusy(false);
  }

  async stop() {
    const answer = await this.ask('stop', 'Stop the ScarabHive server? It stays down until it is started again on its machine.',
      { title: 'Stop server', confirmLabel: 'Stop server', danger: true });
    if (!answer) return;
    this.say('warn', 'Stopping');
    showToast(document, 'The server is stopping', 'warn');
    // down for good: the buttons stay off, the health poll takes over and says Offline
    this.busy = false;
    this.onBusy(false);
  }

  /** When the answering server started, as this clock sees it; null while none answers. */
  async startedAt() {
    return api('/health', { quiet: true }).then((health) => Date.now() - health.uptime_seconds * 1000).catch(() => null);
  }

  async restart() {
    const before = await this.startedAt();
    const answer = await this.ask('restart', 'Restart the ScarabHive server? Plugins, agents and skills are loaded anew; sessions stay.',
      { title: 'Restart server', confirmLabel: 'Restart server' });
    if (!answer) return;
    this.say('warn', 'Restarting');
    showToast(document, 'The server is restarting', 'info');
    const end = Date.now() + RESTART_TIMEOUT;
    let wentDown = false;
    while (Date.now() < end) {
      await sleep(1000);
      const started = await this.startedAt();
      if (started === null) {
        wentDown = true;
      } else if (wentDown || before === null || started > before + 500) {
        // (a start later than the one before: a server back before it was ever seen down is a new one too)
        // a new process: its agents, panels and commands are other ones, so the page starts over
        window.location.reload();
        return;
      }
    }
    this.say('danger', 'Offline');
    showToast(document, 'The server has not come back. Look at its terminal or log.', 'error');
    this.done();
  }
}
