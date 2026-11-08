// The server's own actions, for admins, behind one power button: stop it, reload its config, or restart it so new
// plugins, agents and skills are loaded. Each asks first and says how many requests are running: a stop or a
// restart ends them, a config reload does not.
import { api, confirm, dialog, html, render, showToast } from '/static/kit/panel-kit.js';

/** How long a restarted server may take to answer again before the page stops waiting. */
const RESTART_TIMEOUT = 180000;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

export class ServerControls {
  /** state: the status bar's connection item; onBusy(bool): the health poll steps aside while this one watches. */
  constructor({ button, state, onBusy = () => {} }) {
    this.button = button;
    this.state = state;
    this.onBusy = onBusy;
    this.busy = false;
    button.addEventListener('click', () => this.choose());
  }

  /** Only an admin gets the button: the routes behind it refuse everyone else. */
  show(visible) {
    this.button.hidden = !visible;
  }

  /** How many requests are running, 0 also when the count cannot be had. */
  async running() {
    return api('/admin/active-sessions', { quiet: true }).then((answer) => answer.total || 0).catch(() => 0);
  }

  say(dot, text) {
    render(this.state, html`<span class="pk-dot pk-dot--${dot}"></span> <span>${text}</span>`);
  }

  /** The power button's question. No action is the primary one: the focus starts on Cancel, Enter ends nothing. */
  async choose() {
    if (this.busy || this.choosing) return;  // a second click while the count loads opens no second question
    this.choosing = true;
    try {
      await this.chooseOnce();
    } finally {
      this.choosing = false;
    }
  }

  async chooseOnce() {
    const total = await this.running();
    const running = total
      ? `${plural(total, 'request is', 'requests are')} running: a power off or a restart ${total === 1 ? 'ends it' : 'ends them'}, a config reload does not. `
      : '';
    const action = await dialog({
      title: 'Server',
      message: `${running}Power off stops the ScarabHive server until it is started again on its machine. `
        + 'Reload config re-reads the config files into the running server. '
        + 'Restart loads plugins, agents and skills anew; sessions stay.',
      actions: [
        { label: 'Cancel', value: null },
        { label: 'Power off', value: 'stop', danger: true },
        { label: 'Reload config', value: 'reload' },
        { label: 'Restart', value: 'restart' },
      ],
    });
    if (action === 'stop') await this.stop({ asked: true });
    else if (action === 'reload') await this.reload({ asked: true });
    else if (action === 'restart') await this.restart({ asked: true });
  }

  /** "2 requests are running and end with it. " -- or nothing. */
  async ending() {
    const total = await this.running();
    return total ? `${plural(total, 'request is', 'requests are')} running and ${total === 1 ? 'ends' : 'end'} with it. ` : '';
  }

  /** Asked already (the power button) or asked here (the palette); then the button is off until it is done. */
  async begin(asked, question, options) {
    if (this.busy) return false;
    if (!asked && !await confirm(question(), options)) return false;
    this.busy = true;
    this.button.disabled = true;
    this.onBusy(true);
    return true;
  }

  async post(path) {
    try {
      return await api(path, { method: 'POST' });  // api() shows a refusal
    } catch {
      this.done();
      return null;
    }
  }

  done() {
    this.busy = false;
    this.button.disabled = false;
    this.onBusy(false);
  }

  async stop({ asked = false } = {}) {
    const running = asked ? '' : await this.ending();
    if (!await this.begin(asked, () => `${running}Stop the ScarabHive server? It stays down until it is started again on its machine.`,
      { title: 'Power off', confirmLabel: 'Power off', danger: true })) return;
    if (!await this.post('/admin/server/stop')) return;
    this.say('warn', 'Stopping');
    showToast(document, 'The server is stopping', 'warn');
    // down for good: the button stays off, the health poll takes over and says Offline
    this.busy = false;
    this.onBusy(false);
  }

  /** Re-reads the config files into the running server; what it could not take needs a restart. */
  async reload({ asked = false } = {}) {
    if (!await this.begin(asked, () => 'Re-read the config files into the running server? Running requests go on.',
      { title: 'Reload config', confirmLabel: 'Reload config' })) return;
    const answer = await this.post('/admin/reload-config');
    if (!answer) return;
    this.done();
    const report = answer.report || {};
    const refreshed = (report.refreshed || []).length;
    const errors = report.errors || [];
    const parts = [`Config reloaded: ${plural(refreshed, 'server', 'servers')} changed`];
    if (errors.length) parts.push(`${errors.length} failed: ${errors.map((e) => e.server).join(', ')}`);
    if (report.auth) parts.push(`auth ${report.auth}`);  // read at the start only: "changed on disk: takes effect on a restart"
    showToast(document, parts.join('; '), errors.length || report.auth ? 'warn' : 'ok');
  }

  /** When the answering server started, as this clock sees it; null while none answers. */
  async startedAt() {
    return api('/health', { quiet: true }).then((health) => Date.now() - health.uptime_seconds * 1000).catch(() => null);
  }

  async restart({ asked = false } = {}) {
    const running = asked ? '' : await this.ending();
    if (!await this.begin(asked, () => `${running}Restart the ScarabHive server? Plugins, agents and skills are loaded anew; sessions stay.`,
      { title: 'Restart', confirmLabel: 'Restart' })) return;
    // the start before, taken once the question is answered: a restart meanwhile is not taken for this one's
    const before = await this.startedAt();
    if (!await this.post('/admin/server/restart')) return;
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
        // still here: a panel with unsaved input kept the page ("Stay"), and the server is back -- the health
        // poll takes the status again and the button works
        await sleep(5000);
        this.done();
        return;
      }
    }
    this.say('danger', 'Offline');
    showToast(document, 'The server has not come back. Look at its terminal or log.', 'error');
    this.done();
  }
}
