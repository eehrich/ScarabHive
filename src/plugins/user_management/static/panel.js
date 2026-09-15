// Users: the accounts of the auth database -- created, edited, deactivated and deleted by an admin.
import { api, html, render, icon, confirm, toast } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);
const ROLES = { admin: 'accent', user: '', guest: 'info' };

/** The accounts listed, or null: not loaded, or they could not be. */
let users = null;
/** The id of the admin looking. */
let me = null;
let load = 0;
let busy = false;
/** The row actions on their way, by key: their buttons stay off until answered, also when drawn anew. */
const pending = new Set();
/** The account the editor saves, null for a new one. */
let editing = null;
/** Counts the openings of the editor: an answer to an earlier one leaves it alone. */
let openings = 0;

const badge = (kind, content) => html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${content}</span>`;
const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const stat = (key, label, value) =>
  html`<div class="pk-stat" data-stat="${key}"><div class="pk-stat-label">${label}</div><div class="pk-stat-value">${value}</div></div>`;
const day = (stamp) => (stamp ? new Date(stamp).toLocaleDateString() : '');
const userUrl = (id) => `${BASE}users/${encodeURIComponent(id)}`;

// ------------------------------------------------------------------------ data

async function refresh(event) {
  if (event?.detail?.auto && busy) return;  // a tick while the last answer is on its way would only discard it
  const mine = ++load;
  busy = true;
  let listed;
  try {
    listed = await api(`${BASE}users`, { quiet: true });
  } catch (error) {
    if (mine === load) {  // nothing shown before stays, as if it were still so
      users = null;
      render($('stats'), '');
      render($('users'), empty('circle-alert', 'Users could not be loaded', error.message));
    }
    return;
  } finally {
    if (mine === load) busy = false;
  }
  if (mine !== load) return;
  users = listed.users;
  me = listed.me;
  draw();
}

// -------------------------------------------------------------------- drawing

function draw() {
  if (!users) return;
  const active = users.filter((user) => user.is_active);
  render($('stats'), [
    stat('total', 'Users', users.length),
    stat('active', 'Active', active.length),
    stat('admins', 'Active admins', active.filter((user) => user.role === 'admin').length),
  ]);
  const query = $('search').value.trim().toLowerCase();
  const shown = users.filter((user) => !query
    || [user.username, user.email, user.full_name || ''].some((text) => text.toLowerCase().includes(query)));
  const focused = document.activeElement?.closest('#users [data-key]')?.dataset.key;  // drawn anew, it keeps the focus
  render($('users'), shown.length ? html`<div class="pk-table-wrap"><table class="pk-table">
    <thead><tr><th>User</th><th>Email</th><th>Role</th><th>Status</th><th>Created</th><th>Last login</th><th></th></tr></thead>
    <tbody>${shown.map(row)}</tbody>
  </table></div>` : empty('users', users.length ? `No user matches “${$('search').value.trim()}”` : 'No users'));
  if (focused) $('users').querySelector(`[data-key="${CSS.escape(focused)}"]`)?.focus();
}

function row(user) {
  const self = user.id === me;
  const button = (act, name, label, off = false) => {
    const key = `${user.id}:${act === 'activate' || act === 'deactivate' ? 'toggle' : act}`;
    return html`<button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="${act}"
      data-key="${key}" title="${label}" aria-label="${label}" ${off || pending.has(key) ? 'disabled' : ''}>${icon(name, { size: 'sm' })}</button>`;
  };
  return html`<tr data-user="${user.id}" class="${user.is_active ? '' : 'um-inactive'}">
    <td><div class="um-name">${user.username}${self ? html` <span class="pk-muted">(you)</span>` : ''}${user.has_api_key
      ? html` <span class="um-key" title="Has an API key">${icon('key-round', { size: 'sm', label: 'Has an API key' })}</span>` : ''}</div>
      ${user.full_name ? html`<div class="pk-muted">${user.full_name}</div>` : ''}</td>
    <td>${user.email}</td>
    <td>${badge(ROLES[user.role], user.role)}</td>
    <td>${user.is_active ? badge('ok', 'Active') : badge('', 'Inactive')}</td>
    <td>${day(user.created_at)}</td>
    <td>${user.last_login ? day(user.last_login) : html`<span class="pk-muted">Never</span>`}</td>
    <td class="um-actions">
      ${button('edit', 'pencil', `Edit ${user.username}`)}
      ${user.is_active
      ? button('deactivate', 'ban', self ? 'You cannot deactivate yourself' : `Deactivate ${user.username}`, self)
      : button('activate', 'circle-check', `Activate ${user.username}`)}
      ${button('delete', 'trash-2', self ? 'You cannot delete yourself' : `Delete ${user.username}`, self)}
    </td>
  </tr>`;
}

// --------------------------------------------------------------------- actions

async function act(button) {
  const { key, act: action } = button.dataset;
  const user = users.find((one) => String(one.id) === button.closest('tr').dataset.user);
  if (action === 'edit') {
    openEditor(user);
    return;
  }
  const had = document.activeElement === button;
  pending.add(key);
  button.disabled = true;  // until answered: a second click would ask again
  try {
    if (action === 'delete' && !await confirm(`Delete the account “${user.username}”? It cannot be undone.`,
      { title: 'Delete user', confirmLabel: 'Delete', danger: true })) return;
    if (action === 'deactivate' && !await confirm(`Deactivate “${user.username}”? They can no longer sign in.`,
      { title: 'Deactivate user', confirmLabel: 'Deactivate', danger: true })) return;
    await api(userUrl(user.id), action === 'delete'
      ? { method: 'DELETE' }
      : { method: 'PUT', json: { is_active: action === 'activate' } });
  } catch {
    // shown by api()
  } finally {
    pending.delete(key);
    await refresh();
    if (had && (document.activeElement === document.body || !document.activeElement)) {
      const successor = $('users').querySelector(`[data-key="${CSS.escape(key)}"]:not(:disabled)`)
        || $('users').querySelector(`tr[data-user="${user.id}"] [data-act="edit"]`) || $('create');
      successor.focus();
    }
  }
}

// ---------------------------------------------------------------------- editor

function openEditor(user = null) {
  openings += 1;
  editing = user;
  const self = user?.id === me;
  const fields = $('editorForm').elements;
  $('editorForm').reset();
  $('editorTitle').textContent = user ? `Edit ${user.username}` : 'New user';
  $('save').textContent = user ? 'Save' : 'Create user';
  $('save').disabled = false;
  $('editorError').hidden = true;
  $('usernameField').hidden = Boolean(user);
  fields.username.disabled = Boolean(user);
  fields.password.required = !user;
  $('passwordLabel').textContent = user ? 'New password' : 'Password';
  $('passwordHelp').textContent = user ? 'Leave empty to keep the password. At least 8 characters.' : 'At least 8 characters.';
  fields.role.disabled = self;
  fields.is_active.disabled = self;
  $('selfHelp').hidden = !self;
  if (user) {
    fields.email.value = user.email;
    fields.full_name.value = user.full_name || '';
    fields.role.value = user.role;
    fields.is_active.checked = user.is_active;
  }
  $('editor').showModal();
}

/** What the editor sends: everything for a new account, only what was changed for an existing one. */
function changes() {
  const fields = $('editorForm').elements;
  const typed = { email: fields.email.value.trim(), full_name: fields.full_name.value.trim(), role: fields.role.value,
    is_active: fields.is_active.checked };
  if (!editing) return { ...typed, full_name: typed.full_name || null, username: fields.username.value.trim(), password: fields.password.value };
  const sent = Object.fromEntries(Object.entries(typed).filter(([name, value]) => value !== (editing[name] ?? '')));
  if (fields.password.value) sent.password = fields.password.value;
  return sent;
}

async function save(event) {
  event.preventDefault();
  const body = changes();
  const user = editing;
  const name = user ? user.username : body.username;
  $('save').disabled = true;
  const opening = openings;
  const asked = () => opening === openings && $('editor').open;  // still the dialog it was asked from
  try {
    await api(user ? userUrl(user.id) : `${BASE}users`, { method: user ? 'PUT' : 'POST', json: body, quiet: true });
  } catch (error) {
    const detail = Array.isArray(error.detail) ? error.detail.map((one) => one.msg).join('; ') : error.message;
    if (asked()) {
      $('editorError').textContent = detail;
      $('editorError').hidden = false;
    } else {
      toast(`${name} could not be saved: ${detail}`, { kind: 'error' });
    }
    return;
  } finally {
    if (opening === openings) $('save').disabled = false;
  }
  if (asked()) $('editor').close();
  else toast(`${name} is saved`, { kind: 'ok' });
  refresh();
}

// ---------------------------------------------------------------------- wiring

document.addEventListener('refresh', refresh);
$('search').addEventListener('input', draw);
$('users').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-act]');
  // not the second click of a double click: on a table drawn anew in between it would hit another row's button
  if (button && event.detail < 2) act(button);
});
$('create').addEventListener('click', () => openEditor());
$('cancel').addEventListener('click', () => $('editor').close());
// no password left in the page; the event comes a moment later, and not over an editor opened again since
$('editor').addEventListener('close', () => $('editor').open || $('editorForm').reset());
$('editorForm').addEventListener('submit', save);

refresh();
