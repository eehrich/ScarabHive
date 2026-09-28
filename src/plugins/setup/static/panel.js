// Setup: what this installation still lacks, and one request to see whether the chat answers.
import { api, html, update, toast, notice, withBusy, emptyState, isAborted } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);

/** A key's state: badge kind and words. A placeholder is a copied template value the provider refuses. */
const KEY_STATES = {
  set: ['ok', 'Set'],
  missing: ['warn', 'Missing'],
  placeholder: ['danger', 'Placeholder'],
};

const badge = (kind, text) => html`<span class="${kind ? `pk-badge pk-badge--${kind}` : 'pk-badge'}">${text}</span>`;

// update() draws only what changed: a half-typed password survives every reload of the rest.
function renderKeys(keys) {
  if (!keys.length) {
    update($('keys'), emptyState('key-round', 'The configuration names no key'));
    return;
  }
  update($('keys'), html`<div class="pk-table-wrap"><table class="pk-table" data-pk-sort="keys">
    <thead><tr><th>Key</th><th>State</th><th>Named in</th></tr></thead>
    <tbody>${keys.map((key) => {
      const [kind, text] = KEY_STATES[key.state] || ['', key.state];
      const more = key.named_in.length > 3 ? ` +${key.named_in.length - 3}` : '';
      return html`<tr><td><code>${key.name}</code></td>
        <td data-sort-value="${key.state}">${badge(kind, text)}</td>
        <td title="${key.named_in.join('\n')}">${key.named_in.slice(0, 3).join(', ')}${more}</td></tr>`;
    })}</tbody></table></div>`);
}

/** true: something to fix; false: fine; null or missing: cannot be told here -- never read as fine. */
function line(flag, bad, good, unknown) {
  if (flag === true) return html`${badge('danger', 'Fix')} ${bad}`;
  if (flag === false) return html`${badge('ok', 'OK')} ${good}`;
  return html`${badge('', 'Unknown')} ${unknown}`;
}

function renderAccess(auth, me) {
  // Only the default admin can change the default admin's password: PATCH /auth/me changes the viewer's own.
  const own = auth.default_admin_password === true && me?.username === auth.admin;
  update($('access'), html`<dl class="pk-kv">
    <dt>Admin password</dt><dd>${line(auth.default_admin_password,
      html`<code>${auth.admin}</code> still opens with a publicly known password${own ? '' : ' — log in as that user to change it'}`,
      'no admin opens with a publicly known one', 'not checked: authentication is off, or the user database is missing or cannot be read')}</dd>
    <dt>Signing key</dt><dd>${line(auth.shared_signing_key,
      'a known one (shipped in the repository, or empty): anyone can sign a valid login',
      'this installation’s own', 'cannot be told here')}${auth.signing_key_needs_restart
      ? html` — ${badge('warn', 'Restart')} the configuration names another key; it applies after a restart` : ''}</dd>
  </dl>
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
    // Tokens are not tied to the password: a login made before the change stays valid until it expires.
    toast('Password changed. Logins made before stay valid until they expire.', { kind: 'ok' });
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
