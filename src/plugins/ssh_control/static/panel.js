// SSH Machines: a terminal per machine -- what the agents and the viewer ran there, and a line to run more.
import { api, html, render, icon, confirm, toast } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);
const CUT = 10000;  // the history keeps this much of each output

/** The machines listed, or null: not loaded, or they could not be. */
let machines = null;
/** The name of the machine whose terminal is shown. */
let shown = null;
/** What the terminal holds: {machine, lastRun, history} or {machine, error}. */
let terminal = null;
/** The command of the viewer's on its way, per machine. */
const running = new Map();
/** The command on its way the terminal was drawn with. */
let drawnPending;
let load = 0;
let historyLoad = 0;
let busy = false;
/** Counts the openings of the add dialog: an answer to an earlier one leaves it alone. */
let openings = 0;

const current = () => machines?.find((machine) => machine.name === shown) || null;
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;

function state(machine) {
  if (machine.error) return { kind: 'danger', label: 'Unreachable', title: machine.error };
  if (machine.connected) {
    return { kind: 'ok', label: machine.latency_ms ? `${Math.round(machine.latency_ms)} ms` : 'Connected', title: 'Used in the last five minutes' };
  }
  if (machine.not_yet_connected) return { kind: '', label: 'Not connected yet', title: 'Connects on the first command' };
  return { kind: '', label: 'Idle', title: 'Not used in the last five minutes' };
}

// ------------------------------------------------------------------------ data

async function refresh(event) {
  const auto = Boolean(event?.detail?.auto);
  if (auto && busy) return;  // a tick while the last answer is on its way would only discard it
  const mine = ++load;
  busy = true;
  let listed;
  try {
    // a click asks the machines connected to before; a tick judges by their last use
    listed = await api(`${BASE}api/machines${event && !auto ? '?active=true' : ''}`, { quiet: true });
  } catch (error) {
    if (mine === load) {  // nothing shown before stays, as if it were still so
      machines = null;
      drawMachines();
      render($('notice'), empty('circle-alert', 'Machines could not be loaded', error.message));
    }
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;
  machines = listed.machines;
  if (!current()) shown = machines[0]?.name ?? null;
  drawMachines();
  const machine = current();
  if (!machine) return;
  if (terminal?.machine !== machine.name || terminal.lastRun !== machine.last_run) loadHistory(machine);  // a failed load holds no lastRun
  else if (drawnPending !== running.get(machine.name)) drawOutput();  // a run that left no line in the history
}

async function loadHistory(machine) {
  const mine = ++historyLoad;
  let answer;
  try {
    answer = await api(`${BASE}api/machines/${encodeURIComponent(machine.name)}/history`, { quiet: true });
  } catch (error) {
    if (mine === historyLoad) {
      terminal = { machine: machine.name, error: error.message };
      drawOutput();
    }
    return;
  }
  if (mine !== historyLoad) return;  // another machine was asked for since
  terminal = { machine: machine.name, lastRun: machine.last_run, history: answer.history };
  drawOutput();
}

// -------------------------------------------------------------------- drawing

function drawMachines() {
  const machine = current();
  $('pane').hidden = !machine;
  const focused = document.activeElement?.closest('#tabs [role="tab"]')?.dataset.tab;  // drawn anew, it keeps the focus
  render($('notice'), machines && !machines.length ? empty('server', 'No machines', 'Add one, or configure them for the plugin.') : '');
  render($('tabs'), (machines || []).map((one) => {
    const { kind, label, title } = state(one);
    const selected = one.name === shown;
    return html`<button type="button" class="pk-tab" role="tab" data-tab="${one.name}" aria-selected="${String(selected)}" tabindex="${selected ? 0 : -1}" title="${title}">
      <span class="pk-dot${kind ? ` pk-dot--${kind}` : ''}"></span> ${one.name}${one.connected && one.latency_ms ? html` <span class="pk-tab-count">${label}</span>` : ''}
    </button>`;
  }));
  if (focused !== undefined) [...$('tabs').children].find((tab) => tab.dataset.tab === focused)?.focus();
  if (!machine) return;
  const { kind, label, title } = state(machine);
  $('address').textContent = `${machine.username}@${machine.host}:${machine.port}`;
  render($('state'), html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}" title="${title}">${label}</span>${machine.error ? html` <span class="pk-muted">${machine.error}</span>` : ''}`);
  render($('tags'), machine.tags.map((tag) => html`<span class="pk-badge">${tag}</span>`));
  $('prompt').textContent = `${machine.name}$`;
  const pending = running.has(machine.name);
  $('command').disabled = pending;
  $('runButton').disabled = pending;
}

function entry(name, run) {
  const failed = run.exit_code !== 0;  // null when it did not run to the end
  const cut = run.stdout_preview.length >= CUT || run.stderr_preview.length >= CUT;
  return html`<div class="ssh-entry${failed ? ' ssh-failed' : ''}">
    <div class="ssh-command"><span class="pk-muted">${name}$</span> ${run.command}</div>
    ${run.stdout_preview ? html`<div class="ssh-stdout">${run.stdout_preview}</div>` : ''}
    ${run.stderr_preview ? html`<div class="ssh-stderr">${run.stderr_preview}</div>` : ''}
    ${cut ? html`<div class="ssh-exit">Output cut at 10 KB</div>` : ''}
    <div class="ssh-exit">${run.error || `exit ${run.exit_code}`} · ${run.duration.toFixed(2)} s · ${new Date(run.timestamp * 1000).toLocaleString()}</div>
  </div>`;
}

function drawOutput() {
  const machine = current();
  if (!machine) return;
  const out = $('output');
  const atEnd = out.scrollTop + out.clientHeight >= out.scrollHeight - 8;
  const held = terminal?.machine === machine.name ? terminal : null;
  const pending = running.get(machine.name);
  let body;
  if (!held) body = html`<span class="pk-skeleton"></span>`;
  else if (held.error) body = html`<div class="ssh-stderr">The history could not be loaded: ${held.error}</div>`;
  else if (!held.history.length && !pending) body = html`<div class="pk-muted">No command run on ${machine.name} yet.</div>`;
  else body = held.history.map((run) => entry(machine.name, run));
  render(out, [body, pending
    ? html`<div class="ssh-entry ssh-pending"><div class="ssh-command"><span class="pk-muted">${machine.name}$</span> ${pending} <span class="pk-spinner"></span></div></div>`
    : '']);
  drawnPending = pending;
  if (atEnd) out.scrollTop = out.scrollHeight;
}

function select(name) {
  shown = name;
  drawMachines();
  drawOutput();
  loadHistory(current());
}

// --------------------------------------------------------------------- actions

async function run(event) {
  event.preventDefault();
  const machine = shown;
  const command = $('command').value.trim();
  if (!command) return;
  running.set(machine, command);
  $('command').value = '';
  drawMachines();
  drawOutput();
  // api() shows a failure; a machine unreachable or a command timed out is in the history as well
  await api(`${BASE}api/execute`, { method: 'POST', json: { machine, command } }).catch(() => {});
  running.delete(machine);
  await refresh();
  if (shown === machine && document.activeElement === document.body) $('command').focus();
}

async function remove() {
  const { name } = current();  // the machine shown when clicked, whatever is shown when the answer is in
  const button = $('remove');
  button.disabled = true;  // until answered: a second click would ask again, and remove nothing but a 404
  try {
    if (!await confirm(`Remove the machine “${name}”? It is removed from the machines kept across restarts too; one from the configuration comes back at the next start.`,
      { title: 'Remove machine', confirmLabel: 'Remove', danger: true })) return;
    const result = await api(`${BASE}api/machines/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (result.config_error) toast(`${name} is removed, but not from the store: ${result.config_error}`, { kind: 'warn' });
  } catch {
    // shown by api()
  } finally {
    button.disabled = false;
  }
  refresh();
}

function drawAuth() {
  const method = $('authMethod').value;
  $('keyField').hidden = method !== 'key';
  $('passwordField').hidden = method !== 'password';
}

function openAdd() {
  openings += 1;
  $('addSubmit').disabled = false;
  $('addError').hidden = true;
  drawAuth();
  $('add').showModal();
}

async function add(event) {
  event.preventDefault();
  const form = new FormData($('addForm'));
  const machine = {
    name: form.get('name'),
    host: form.get('host'),
    port: Number(form.get('port')),
    username: form.get('username'),
    auth_method: form.get('auth_method'),
    key_path: form.get('key_path'),
    persistent: form.has('persistent'),
  };
  if (machine.auth_method === 'password') machine.password = form.get('password');  // not one typed before choosing another way
  $('addSubmit').disabled = true;  // the connection test takes a while: once
  const opening = openings;
  const asked = () => opening === openings && $('add').open;  // still the dialog it was asked from
  let result;
  try {
    result = await api(`${BASE}api/machines`, { method: 'POST', json: machine, quiet: true });
  } catch (error) {
    if (asked()) {
      $('addError').textContent = error.message;
      $('addError').hidden = false;
    } else {
      toast(`${machine.name} could not be added: ${error.message}`, { kind: 'error' });
    }
    return;
  } finally {
    if (opening === openings) $('addSubmit').disabled = false;
  }
  if (asked()) $('add').close();
  else toast(`${machine.name} is added`, { kind: 'ok' });
  if (machine.persistent && !result.persistent) toast(`${machine.name} is added, but not kept: ${result.config_error}`, { kind: 'warn' });
  shown = machine.name;
  refresh();
}

// ---------------------------------------------------------------------- wiring

document.addEventListener('refresh', refresh);
$('tabs').addEventListener('tabchange', (event) => select(event.detail.tab));
$('run').addEventListener('submit', run);
$('remove').addEventListener('click', remove);
$('addMachine').addEventListener('click', openAdd);
$('addCancel').addEventListener('click', () => $('add').close());
$('add').addEventListener('close', () => $('addForm').reset());  // no password left in the page
$('authMethod').addEventListener('change', drawAuth);
$('addForm').addEventListener('submit', add);

refresh();
