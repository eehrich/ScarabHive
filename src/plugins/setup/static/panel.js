// Setup: what this installation still lacks, and one request to see whether the chat answers.
import { api, html, update, toast, notice, withBusy, emptyState, isAborted, confirm } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);

/** A key's state: badge kind and words. A placeholder is a copied template value the provider refuses. */
const KEY_STATES = {
  set: ['ok', 'Set'],
  missing: ['warn', 'Missing'],
  placeholder: ['danger', 'Placeholder'],
};

const badge = (kind, text) => html`<span class="${kind ? `pk-badge pk-badge--${kind}` : 'pk-badge'}">${text}</span>`;

const keyForms = () => [...$('keys').querySelectorAll('form[data-name]')];

// update() draws only what changed; when a row changed -- a key saved in another row -- the whole table is drawn
// anew, so what is typed in the other rows is put back (the kit gives the focus back by data-key).
function renderKeys(keys) {
  if (!keys.length) {
    update($('keys'), emptyState('key-round', 'The configuration names no key'));
    return;
  }
  const typed = keyForms().map((form) => [form.dataset.name, form.querySelector('[name=value]').value])
    .filter(([, value]) => value);
  const drew = update($('keys'), html`<div class="pk-table-wrap"><table class="pk-table" data-pk-sort="keys">
    <thead><tr><th>Key</th><th>State</th><th>Named in</th><th data-pk-nosort>Enter</th></tr></thead>
    <tbody>${keys.map((key) => {
      const [kind, text] = KEY_STATES[key.state] || ['', key.state];
      const more = key.named_in.length > 3 ? ` +${key.named_in.length - 3}` : '';
      return html`<tr><td><code>${key.name}</code></td>
        <td data-sort-value="${key.state}">${badge(kind, text)}</td>
        <td title="${key.named_in.join('\n')}">${key.named_in.slice(0, 3).join(', ')}${more}</td>
        <td>${key.from_environment
          ? html`<span class="pk-help">set by the environment the API started with</span>`
          : html`<form class="pk-row" data-name="${key.name}" autocomplete="off">
            <input class="pk-input" type="password" name="value" required aria-label="${key.name}"
              data-key="value:${key.name}" placeholder="${key.state === 'set' ? 'replace' : 'paste the key'}"
              autocomplete="off">
            <button type="submit" class="pk-btn pk-btn--sm" aria-label="Save ${key.name}">Save</button></form>`}</td></tr>`;
    })}</tbody></table></div>`);
  if (!drew) return;
  for (const [name, value] of typed) {
    const form = keyForms().find((one) => one.dataset.name === name);
    if (form) form.querySelector('[name=value]').value = value;
  }
}

/** true: something to fix; false: fine; null or missing: cannot be told here -- never read as fine. */
function line(flag, bad, good, unknown) {
  if (flag === true) return html`${badge('danger', 'Fix')} ${bad}`;
  if (flag === false) return html`${badge('ok', 'OK')} ${good}`;
  return html`${badge('', 'Unknown')} ${unknown}`;
}

/** The key the config file names, which a restart applies: a known one there turns a restart into the harm. */
function nextKey(auth) {
  if (auth.configured_signing_key_known === null) {
    return html` — ${badge('warn', 'Unknown')} the config file’s auth section cannot be read`;
  }
  if (!auth.signing_key_needs_restart) return '';
  return auth.configured_signing_key_known
    ? html` — ${badge('danger', 'Fix')} the config file gives a known or empty key: replace it before the next restart`
    : html` — ${badge('warn', 'Restart')} the config file names another key; it applies after a restart`;
}

function renderAccess(auth, me) {
  // Only the default admin can change the default admin's password: PATCH /auth/me changes the viewer's own.
  const own = auth.default_admin_password === true && me?.username === auth.admin;
  update($('access'), html`<dl class="pk-kv">
    <dt>Admin password</dt><dd>${line(auth.default_admin_password,
      html`<code>${auth.admin}</code> still opens with a publicly known password${own ? '' : ' — log in as that user to change it'}`,
      'no admin opens with a publicly known one', 'not checked: authentication is off, or the user database is missing or cannot be read')}</dd>
    <dt>Signing key</dt><dd>${line(auth.shared_signing_key,
      'a known one (printed in the repository, the model’s default, or empty): replace it',
      'this installation’s own', 'cannot be told here')}${nextKey(auth)}</dd>
  </dl>
  ${auth.configured_signing_key_known === true
    ? html`<div><button type="button" class="pk-btn pk-btn--sm" id="ownKey">Make an own signing key</button></div>`
    : ''}
  ${own ? html`<form id="password" class="pk-form pk-stack" autocomplete="off">
    <div class="pk-row">
      <label class="pk-field pk-grow"><span class="pk-label">Current password</span>
        <input class="pk-input" type="password" name="current" required autocomplete="current-password"></label>
      <label class="pk-field pk-grow"><span class="pk-label">New password</span>
        <input class="pk-input" type="password" name="password" required minlength="8" autocomplete="new-password">
        <span class="pk-help">8 characters or more</span></label>
    </div>
    <div><button type="submit" class="pk-btn pk-btn--primary pk-btn--sm">Change the password</button></div>
  </form>` : ''}`);
}

// One listener on the section, not one per drawing of the form.
$('access').addEventListener('submit', async (event) => {
  if (event.target.id !== 'password') return;
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  await withBusy(form.querySelectorAll('input, button'), async () => {
    try {
      await api('/auth/me', { method: 'PATCH', json: { current_password: data.get('current'), password: data.get('password') } });
    } catch {
      return;  // the kit's toast says why (a wrong current password); the form stays as typed
    }
    form.reset();
    // Nothing on other logins: whether they end with the password is the auth system's (token generations).
    toast('Password changed.', { kind: 'ok' });
    await load();
  });
});

$('access').addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (button?.id !== 'ownKey') return;
  const ok = await confirm('A random key is saved in config/local.env on this machine and named in '
    + 'config/local.yaml. It applies after the next restart of the API, and everyone logs in again then.',
  { title: 'Own signing key', confirmLabel: 'Make it', danger: true });
  if (!ok) return;
  await withBusy(button, async () => {
    try {
      const result = await api(`${BASE}signing-key`, { method: 'POST', json: {} });
      toast(result.message, { kind: 'ok' });
    } catch {
      return;  // the kit's toast says why
    }
    await load();
  });
});

$('keys').addEventListener('submit', async (event) => {
  const form = event.target.closest('form[data-name]');
  if (!form) return;
  event.preventDefault();
  const name = form.dataset.name;
  // a plugin reads its key when the API starts; the chat builds its model's client for each message
  const plugin = (lastState?.keys ?? []).find((key) => key.name === name)?.named_in
    .some((section) => section.startsWith('plugins.'));
  const value = new FormData(form).get('value');
  await withBusy(form.querySelectorAll('input, button'), async () => {
    let result;
    try {
      result = await api(`${BASE}key`, { method: 'POST', json: { name, value } });
    } catch {
      return;  // the kit's toast says why; what was typed stays
    }
    // the form drawn now: a refresh while the request ran may have drawn the table anew, the key put back in it
    (keyForms().find((one) => one.dataset.name === name) ?? form).reset();
    toast(result.reload_error
      ? `${name} saved; the configuration did not reload (${result.reload_error}): restart the API.`
      : plugin
        ? `${name} saved. A plugin that uses it takes it after a restart of the API.`
        : `${name} saved. Test the chat to see whether the provider takes it.`,
    { kind: result.reload_error ? 'warn' : 'ok' });
    await load();
  });
});

function renderChat(chat, probe, testing) {
  const who = html`<dl class="pk-kv"><dt>Default agent</dt><dd>${chat.agent || '—'}</dd>
    <dt>LLM profile</dt><dd>${chat.profile || '—'}</dd>
    ${probe?.model ? html`<dt>Model</dt><dd><code>${probe.model}</code></dd>` : ''}</dl>`;
  let outcome = html`<p class="pk-help">Not tested yet. A test sends one short request.</p>`;
  if (testing) outcome = html`<p><span class="pk-spinner" aria-hidden="true"></span> Testing — up to a minute.</p>`;
  else if (probe?.ok) outcome = html`<p>${badge('ok', 'Answers')} The chat works.</p>`;
  else if (probe) outcome = html`<p>${badge('danger', 'No answer')} <code>${probe.error}</code></p>`;
  $('chat').setAttribute('aria-busy', String(Boolean(testing)));
  update($('chat'), html`${who}${outcome}`);
}

// What the page shows between loads: a reload during a test keeps "Testing".
let lastState = null;
let lastProbe = null;
let testing = false;
let me = null;

async function load() {
  let state;
  try {
    // One latest name for both calls: a newer load aborts this one wherever it waits, so an
    // overtaken load never draws -- nor clears the newer one's notice (a password change reloads mid-refresh).
    state = await api(`${BASE}state`, { quiet: true, latest: 'load' });
    me = await api('/auth/me', { quiet: true, latest: 'load' });
  } catch (error) {
    if (isAborted(error)) return;
    // A failed /auth/me leaves who is looking as it was: the form and what was typed in it stay.
    if (!state) {
      // one line that stays, not a toast per refresh tick (the API may be restarting for a new key)
      notice($('problem'), `The setup state could not be read: ${error.detail || error.message}`);
      return;
    }
  }
  notice($('problem'), '');
  lastState = state;
  renderChat(state.chat, lastProbe, testing);
  renderAccess(state.auth, me);
  renderKeys(state.keys);
}

$('probe').addEventListener('click', async () => {
  const tested = await withBusy($('probe'), async () => {
    testing = true;
    renderChat(lastState?.chat ?? {}, lastProbe, true);  // also before the state was ever read
    try {
      lastProbe = await api(`${BASE}probe`, { method: 'POST', json: {} });
    } catch (error) {
      // the kit's toast says why; an answer from before must not stand for this test
      lastProbe = { ok: false, error: error.detail || error.message };
    } finally {
      testing = false;
    }
    renderChat(lastState?.chat ?? {}, lastProbe, false);
    return true;
  });
  // outside withBusy: the button is free once the answer shows, however long the reload takes
  if (tested) await load();
});

document.addEventListener('refresh', load);
load();
