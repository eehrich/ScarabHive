// Settings panel: appearance, profile, password.
import { api, ApiError, toast, setTheme, currentTheme, onThemeChange } from '/static/kit/panel-kit.js';

const $ = (id) => document.getElementById(id);
const themeRadios = [...document.querySelectorAll('input[name="theme"]')];

function showTheme(theme) {
  themeRadios.forEach((radio) => { radio.checked = radio.value === theme; });
}

themeRadios.forEach((radio) => radio.addEventListener('change', () => setTheme(radio.value)));
showTheme(currentTheme());
onThemeChange(showTheme);  // switched from the shell's header while this panel is open

async function loadProfile() {
  let me;
  try {
    me = await api('/auth/me', { quiet: true });
  } catch (error) {
    // Without authentication the server has no /auth routes: no account to edit.
    if (error instanceof ApiError && error.status === 404) {
      $('account').hidden = true;
      $('noAccounts').hidden = false;
    } else {
      toast(`The profile could not be loaded: ${error.message}`, { kind: 'error' });
    }
    return;
  }
  $('username').value = me.username;
  $('email').value = me.email || '';
  $('fullName').value = me.full_name || '';
  $('role').textContent = me.role;
}

/** PATCH /auth/me; api() has already shown what went wrong when this returns false. */
async function saveMe(json) {
  try {
    await api('/auth/me', { method: 'PATCH', json });
    return true;
  } catch {
    return false;
  }
}

$('profileForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (await saveMe({ email: $('email').value.trim(), full_name: $('fullName').value.trim() })) {
    toast('Profile saved', { kind: 'ok' });
  }
});

$('passwordForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  if (data.password !== data.repeat) {
    toast('The new passwords do not match', { kind: 'warn' });
    return;
  }
  if (await saveMe({ password: data.password, current_password: data.current_password })) {
    form.reset();
    toast('Password changed', { kind: 'ok' });
  }
});

loadProfile();
