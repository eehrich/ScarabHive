// Settings panel: appearance, chat, profile, password.
import { api, ApiError, toast, setTheme, currentTheme, onThemeChange, announcePreferences } from '/static/kit/panel-kit.js';

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

// How the chat shows a run: kept with the account (PUT /auth/me/preferences), applied
// at once. Each radio saves as it is clicked, the way the theme does -- a Save button
// for three switches would be one more thing to forget.
const chatRadios = [...document.querySelectorAll('#chatPreferences input[type="radio"]')];
let preferences = null;

function showPreferences() {
  chatRadios.forEach((radio) => { radio.checked = preferences.chat[radio.name] === radio.value; });
}

async function loadPreferences() {
  try {
    preferences = await api('/auth/me/preferences', { quiet: true });
  } catch (error) {
    $('chatPreferences').hidden = true;  // no account to keep them: nothing to choose
    if (!(error instanceof ApiError && error.status === 404)) {
      toast(`The chat settings could not be loaded: ${error.message}`, { kind: 'error' });
    }
    return;
  }
  showPreferences();
}

// One save at a time, each built from the radios as they are when its turn comes. Two
// clicks inside one round trip otherwise built the second from the answer BEFORE the
// first -- the server keeps the whole object, so the second save put the first choice
// back, while its radio still showed it.
let saving = Promise.resolve();
let pending = 0;

chatRadios.forEach((radio) => radio.addEventListener('change', () => {
  if (!preferences) return;
  pending += 1;
  saving = saving.then(async () => {
    const shown = Object.fromEntries(chatRadios.filter((r) => r.checked).map((r) => [r.name, r.value]));
    try {
      preferences = await api('/auth/me/preferences', {
        method: 'PUT', json: { ...preferences, chat: { ...preferences.chat, ...shown } } });
      announcePreferences(preferences);
    } catch {
      // Not saved: show what is in effect, not what was clicked -- unless a later click
      // waits its turn; its save carries the radios as they are, this one's included.
      if (pending === 1) showPreferences();
    } finally {
      pending -= 1;
    }
  });
}));

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
loadPreferences();
