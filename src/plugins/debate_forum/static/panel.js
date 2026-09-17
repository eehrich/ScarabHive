// Debate Forum: the channels agents debate in, each thread with its rounds, pins and verdict, and a line to post into it.
import { api, html, render, trusted, icon, jsonView, confirm, toast } from '/static/kit/panel-kit.js';

const BASE = new URL('..', import.meta.url).pathname;  // /plugins/<instance>/
const $ = (id) => document.getElementById(id);
const SLOTS = 6;  // role colours in panel.css
const EXPANDED_KEY = 'debate_forum.expanded_groups';

/** {channels, total} as listed, or null: not loaded, or it could not be. */
let listed = null;
let listError = null;
let groups = [];
/** The id of the channel shown, or null. */
let selected = null;
/** What the thread holds: {channel, messages} of the channel it was loaded for. */
let thread = null;
let threadError = null;
let load = 0;
let threadLoad = 0;
let busy = false;
let sending = false;
/** The messages and verdict the thread was drawn from: an unchanged answer is not drawn again. */
let drawnKey = null;
let drawnChannel = null;
/** JSON blocks shown readable, as message id and block index. */
const readable = new Set();
let openings = 0;

let expanded;
try { expanded = new Set(JSON.parse(localStorage.getItem(EXPANDED_KEY)) || []); } catch { expanded = new Set(); }
const keepExpanded = () => { try { localStorage.setItem(EXPANDED_KEY, JSON.stringify([...expanded])); } catch { /* not kept */ } };

const empty = (name, title, text = '') =>
  html`<div class="pk-empty">${icon(name)}<div class="pk-empty-title">${title}</div>${text ? html`<div>${text}</div>` : ''}</div>`;
const STATUS = { active: ['ok', 'Active'], concluded: ['info', 'Concluded'], archived: ['', 'Archived'] };

function slot(role) {
  let hash = 0;
  for (const c of (role || '').trim().toLowerCase()) hash = (hash * 31 + c.charCodeAt(0)) & 0xffff;
  return `df-slot-${hash % SLOTS}`;
}

const initials = (name) => (name || '?').split(/[\s_-]+/).filter(Boolean).map((word) => word[0].toUpperCase()).slice(0, 2).join('') || '?';
const time = (stamp) => (stamp ? new Date(`${stamp.replace(' ', 'T')}Z`) : null);  // the database writes UTC

/** A button disabled while focused hands the focus to the page: it goes back to the first of these still shown. */
function refocus(had, ...candidates) {
  if (!had || document.activeElement !== document.body) return;
  candidates.find((one) => one?.isConnected && one.offsetParent !== null && !one.disabled)?.focus();
}

/** Focus on something about to be drawn anew goes to its successor. */
function keepingFocus(root, draw) {
  const key = document.activeElement?.closest('[data-key]');
  const had = key && root.contains(key) ? key.dataset.key : null;
  draw();
  if (had) root.querySelector(`[data-key="${CSS.escape(had)}"]`)?.focus();
}

// ------------------------------------------------------------------------ data

async function refresh(event) {
  const auto = Boolean(event?.detail?.auto);
  if (auto && busy) return;  // a tick while the last answer is on its way would only discard it
  const mine = ++load;
  busy = true;
  try {
    const query = new URLSearchParams();
    for (const [key, value] of [['status', $('status').value], ['group_id', $('group').value], ['search', $('search').value.trim()]]) {
      if (value) query.set(key, value);
    }
    let answers;
    try {
      answers = await Promise.all([
        api(`${BASE}api/stats`, { quiet: true }),
        api(`${BASE}api/groups`, { quiet: true }),
        api(`${BASE}api/channels?${query}`, { quiet: true }),
      ]);
    } catch (error) {
      if (mine !== load) return;
      listed = null;
      listError = error.message;
      drawStats(null);
      drawChannels();
      return;
    }
    if (mine !== load) return;
    const [stats, groupList, channelList] = answers;
    groups = groupList.groups;
    listed = { ...channelList, filtered: query.size > 0, flat: query.has('group_id') };  // what it was asked with
    listError = null;
    drawStats(stats);
    drawGroups();
    drawChannels();
    if (selected !== null) await loadThread(!auto);
  } finally {
    if (mine === load) busy = false;
  }
}

/** The channel shown; its messages anew when asked for, and otherwise only when they can have changed. */
async function loadThread(full = true) {
  const id = selected;
  const mine = ++threadLoad;
  const held = thread?.channel.id === id ? thread : null;
  try {
    const channel = await api(`${BASE}api/channels/${id}`, { quiet: true });
    if (mine !== threadLoad) return;
    let messages = held?.messages;
    // only an active channel takes posts and chunks; into another one a post comes only through a reopening
    if (full || !held || channel.status === 'active' || channel.message_count !== held.messages.length) {
      messages = (await api(`${BASE}api/channels/${id}/messages`, { quiet: true })).messages;
      if (mine !== threadLoad) return;
    }
    thread = { channel, messages };
    threadError = null;
  } catch (error) {
    if (mine !== threadLoad) return;
    thread = null;
    threadError = error.message;
  }
  drawThread();
}

// -------------------------------------------------------------------- drawing

function drawStats(stats) {
  render($('stats'), stats ? html`
    <span class="pk-badge pk-badge--ok" title="Active channels">${stats.active} active</span>
    <span class="pk-badge pk-badge--info" title="Concluded channels">${stats.concluded} concluded</span>
    <span class="pk-badge" title="Archived channels">${stats.archived} archived</span>
    <span class="pk-badge" title="Messages in all channels">${stats.total_messages} messages</span>` : '');
}

function drawGroups() {
  const select = $('group');
  const chosen = select.value;
  const options = groups.map((group) => [String(group.id), `${group.name} (${group.channel_count})`]);
  if (chosen && !options.some(([id]) => id === chosen)) options.push([chosen, `Group ${chosen}`]);  // gone or older: still the filter
  const key = JSON.stringify(options);
  if (select.dataset.key === key) return;  // redrawn, an open list would close
  select.dataset.key = key;
  render(select, [html`<option value="">Every group</option>`, options.map(([id, label]) => html`<option value="${id}">${label}</option>`)]);
  select.value = chosen;
}

function channelButton(channel) {
  const [kind] = STATUS[channel.status] || [''];
  return html`<button type="button" class="df-channel" data-key="channel:${channel.id}" data-channel="${channel.id}"
      aria-current="${String(channel.id === selected)}" title="${channel.topic || channel.name}">
    <span class="pk-dot${kind ? ` pk-dot--${kind}` : ''}"></span><span class="pk-grow pk-truncate">${channel.name}</span>${
    channel.message_count ? html`<span class="pk-tab-count">${channel.message_count}</span>` : ''}</button>`;
}

function drawChannels() {
  keepingFocus($('channels'), () => {
    if (!listed) {
      render($('channels'), listError ? empty('circle-alert', 'Channels could not be loaded', listError) : html`<span class="pk-skeleton"></span>`);
      return;
    }
    const { channels, total, filtered, flat } = listed;
    if (!channels.length) {
      render($('channels'), filtered ? empty('search', 'No channel matches') : empty('messages-square', 'No channels yet', 'Agents open them with create_channel.'));
      return;
    }
    let body;
    if (flat) {
      body = channels.map(channelButton);
    } else {
      const buckets = new Map();
      for (const channel of channels) {
        const id = channel.group_id || 0;
        if (!buckets.has(id)) buckets.set(id, []);
        buckets.get(id).push(channel);
      }
      const newest = (list) => Math.max(...list.map((channel) => channel.id));
      body = [...buckets].sort((a, b) => newest(b[1]) - newest(a[1])).map(([id, list]) => {
        const name = id ? groups.find((group) => group.id === id)?.name ?? `Group ${id}` : 'Ungrouped';
        list.sort((a, b) => a.id - b.id);  // a debate in the order it was held
        return html`<details class="df-group" data-group="${id}" ${expanded.has(String(id)) ? trusted('open') : ''}>
          <summary data-key="group:${id}" title="${id ? `${name} #${id}` : name}"><span class="pk-grow pk-truncate">${name}</span><span class="pk-tab-count">${list.length}</span></summary>
          ${list.map(channelButton)}
        </details>`;
      });
    }
    render($('channels'), [body, total > channels.length
      ? html`<div class="pk-help df-more">The ${channels.length} most recently active of ${total} channels: search or filter for the others.</div>` : '']);
  });
}

function drawThread() {
  const held = thread && thread.channel.id === selected ? thread : null;
  $('open').hidden = !held;
  if (selected === null) render($('placeholder'), empty('messages-square', 'No channel chosen', 'Choose a channel to read its debate.'));
  else if (held) render($('placeholder'), '');
  else if (threadError) render($('placeholder'), empty('circle-alert', 'The channel could not be loaded', threadError));
  else render($('placeholder'), html`<span class="pk-skeleton"></span>`);
  if (!held) return;
  const { channel, messages } = held;
  const [kind, label] = STATUS[channel.status] || ['', channel.status];
  $('name').textContent = `${channel.name} #${channel.id}`;
  render($('state'), html`<span class="pk-badge${kind ? ` pk-badge--${kind}` : ''}">${label}</span>`);
  $('topic').textContent = channel.topic;
  $('reopen').hidden = channel.status === 'active';
  $('archive').hidden = channel.status === 'archived';
  $('composer').hidden = channel.status !== 'active';
  const people = new Map();
  for (const message of messages) {
    const person = people.get(message.agent_name) || { role: message.agent_role, count: 0 };
    person.count += 1;
    people.set(message.agent_name, person);
  }
  render($('participants'), [...people].map(([name, person]) =>
    html`<span class="pk-badge df-person ${slot(person.role)}" title="${person.role}">${name}<span class="pk-tab-count">${person.count}</span></span>`));
  drawMessages(channel, messages);
}

function drawMessages(channel, messages) {
  const key = JSON.stringify([channel.id, channel.status, channel.verdict_summary_html, channel.verdict_json, messages]);
  if (key === drawnKey) return;
  const out = $('messages');
  const same = drawnChannel === channel.id;
  const atEnd = out.scrollTop + out.clientHeight >= out.scrollHeight - 8;
  drawnKey = key;
  drawnChannel = channel.id;
  let round = -Infinity;
  const posts = messages.map((message) => {
    const opens = message.round > round;  // a later post in an earlier round opens none
    if (opens) round = message.round;
    return html`${opens ? html`<div class="df-round" role="separator">Round ${message.round}</div>` : ''}
      <article class="df-post${message.pinned ? ' df-pinned' : ''}" data-message="${message.id}">
        <div class="df-avatar ${slot(message.agent_role)}" aria-hidden="true">${initials(message.agent_name)}</div>
        <div class="pk-grow">
          <div class="pk-row df-post-head">
            <strong>${message.agent_name}</strong>
            ${message.agent_role ? html`<span class="pk-badge ${slot(message.agent_role)}">${message.agent_role}</span>` : ''}
            <time class="pk-muted" title="${time(message.created_at)?.toLocaleString() ?? ''}">${time(message.created_at)?.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) ?? ''}</time>
            <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm df-pin" data-key="pin:${message.id}" data-pin="${message.id}"
              aria-pressed="${String(Boolean(message.pinned))}" title="${message.pinned ? 'Pinned: always in the participants’ context. Unpin' : 'Pin: always in the participants’ context'}"
              aria-label="${message.pinned ? 'Unpin' : 'Pin'}">${icon('pin', { size: 'sm' })}</button>
          </div>
          <div class="df-md">${trusted(message.content_html)}</div>
        </div>
      </article>`;
  });
  const verdict = channel.status === 'concluded' && (channel.verdict_summary_html || channel.verdict_json) ? verdictBox(channel) : '';
  keepingFocus(out, () => {
    render(out, [messages.length ? posts : empty('messages-square', 'No messages yet', 'The debate has not started.'), verdict]);
    window.Prism?.highlightAllUnder(out);
    readableJson(out);
  });
  if (!same || atEnd) out.scrollTop = out.scrollHeight;
}

function verdictBox(channel) {
  const details = channel.verdict_json && typeof channel.verdict_json === 'object'
    ? Object.fromEntries(Object.entries(channel.verdict_json).filter(([key]) => key !== 'summary' && key !== 'remaining_differences'))
    : channel.verdict_json;
  const more = details && (typeof details !== 'object' || Object.keys(details).length);
  return html`<section class="pk-card df-verdict" data-message="verdict">
    <div class="pk-card-head">${icon('circle-check')}<h3 class="pk-card-title">Verdict</h3></div>
    ${channel.verdict_summary_html ? html`<div class="df-md">${trusted(channel.verdict_summary_html)}</div>` : ''}
    ${more ? html`<details class="df-verdict-details"><summary>Details</summary>${jsonView(details)}</details>` : ''}
  </section>`;
}

/** Tolerant JSON: strict first, then with the raw line breaks and tabs models leave inside strings escaped. */
function parseJson(text) {
  try { return JSON.parse(text); } catch { /* repaired below */ }
  let out = '';
  let inString = false;
  let escaped = false;
  for (const c of text) {
    if (escaped) escaped = false;
    else if (c === '\\') escaped = true;
    else if (c === '"') inString = !inString;
    else if (inString && c.charCodeAt(0) < 0x20) { out += JSON.stringify(c).slice(1, -1); continue; }
    out += c;
  }
  try { return JSON.parse(out); } catch { return undefined; }
}

/** A code block holding a JSON object or array gets a switch to a readable tree of it. */
function readableJson(root) {
  root.querySelectorAll('[data-message]').forEach((post) => {
    post.querySelectorAll('.df-md pre > code').forEach((code, index) => {
      const data = parseJson(code.textContent);
      if (data === null || typeof data !== 'object') return;
      const pre = code.parentElement;
      const key = `${post.dataset.message}:${index}`;
      const on = readable.has(key);
      pre.classList.add('df-json-source');
      pre.insertAdjacentHTML('beforebegin', String(html`<button type="button" class="pk-btn pk-btn--ghost pk-btn--sm df-json-toggle" data-key="json:${key}"
        data-json="${key}" aria-pressed="${String(on)}">${icon('eye', { size: 'sm' })} Readable</button>`));
      pre.insertAdjacentHTML('afterend', String(html`<div class="df-json-view" ${on ? '' : trusted('hidden')}>${jsonView(data)}</div>`));
      pre.hidden = on;
    });
  });
}

// --------------------------------------------------------------------- actions

function select(id) {
  selected = id;
  threadError = null;
  drawChannels();
  drawThread();
  loadThread();
}

function threadText({ channel, messages }) {
  let text = `# ${channel.name}\n${channel.topic ? `Topic: ${channel.topic}\n` : ''}\n`;
  let round = null;
  for (const message of messages) {
    if (message.round !== round) {
      round = message.round;
      text += `--- Round ${round} ---\n\n`;
    }
    text += `[${(message.agent_role || '').toUpperCase()} "${message.agent_name}"]\n${message.content}\n\n`;
  }
  if (channel.verdict_summary) text += `--- Verdict ---\n${channel.verdict_summary}\n`;
  return text;
}

async function copy() {
  const held = thread?.channel.id === selected ? thread : null;
  if (!held) return;
  try {
    await navigator.clipboard.writeText(threadText(held));
    toast('The debate is copied', { kind: 'ok' });
  } catch (error) {
    toast(`The debate could not be copied: ${error.message}`, { kind: 'error' });
  }
}

/** A channel action on the channel shown when clicked; its button stays off until the answer is drawn. */
async function act(event, button, run) {
  if (event.detail > 1 || button.disabled || selected === null) return;  // the second click of a double click, or one still answered
  const id = selected;
  const had = document.activeElement === button;
  button.disabled = true;
  try {
    await run(id);
  } catch {
    // shown by api()
  } finally {
    await refresh();
    button.disabled = false;
    refocus(had, button, $('reopen'), $('archive'));
  }
}

const reopen = (event) => act(event, $('reopen'), (id) => api(`${BASE}api/channels/${id}/reopen`, { method: 'POST' }));
const archive = (event) => act(event, $('archive'), (id) => api(`${BASE}api/channels/${id}/archive`, { method: 'POST' }));
const remove = (event) => act(event, $('delete'), async (id) => {
  const { channel, messages } = thread;
  const count = messages.length === 1 ? 'its message' : `its ${messages.length} messages`;
  if (!await confirm(`Delete the channel “${channel.name}” and ${count} for good? This cannot be undone.`,
    { title: 'Delete channel', confirmLabel: 'Delete', danger: true })) return;
  await api(`${BASE}api/channels/${id}`, { method: 'DELETE' });
  if (selected === id) {
    selected = null;
    thread = null;
    drawThread();
  }
});

async function pin(event) {
  const button = event.target.closest('.df-pin');
  if (!button || button.disabled || event.detail > 1) return;
  const had = document.activeElement === button;
  button.disabled = true;  // until drawn anew
  try {
    await api(`${BASE}api/messages/${button.dataset.pin}/pin`, { method: 'POST', json: { pinned: button.getAttribute('aria-pressed') !== 'true' } });
  } catch {
    // shown by api()
  }
  await loadThread();
  button.disabled = false;  // still there when nothing changed
  refocus(had, $('messages').querySelector(`[data-key="${CSS.escape(button.dataset.key)}"]`));
}

function toggleJson(event) {
  const button = event.target.closest('.df-json-toggle');
  if (!button) return;
  const on = button.getAttribute('aria-pressed') !== 'true';
  if (on) readable.add(button.dataset.json); else readable.delete(button.dataset.json);
  button.setAttribute('aria-pressed', String(on));
  button.nextElementSibling.hidden = on;
  button.nextElementSibling.nextElementSibling.hidden = !on;
}

async function send(event) {
  event.preventDefault();
  const text = $('text').value.trim();
  if (sending || selected === null || !text) return;
  sending = true;
  const had = document.activeElement === $('send');
  $('send').disabled = true;
  try {
    await api(`${BASE}api/channels/${selected}/messages`, { method: 'POST', json: { agent_name: $('author').value, agent_role: $('role').value, content: text } });
    if ($('text').value.trim() === text) $('text').value = '';  // not what was typed since
  } catch {
    // shown by api(); the text stays
  } finally {
    sending = false;
    $('send').disabled = false;
    refocus(had, $('send'));
  }
  refresh();
}

function openCreate() {
  openings += 1;
  $('createSubmit').disabled = false;
  $('createError')?.remove();
  $('create').showModal();
}

async function create(event) {
  event.preventDefault();
  const form = new FormData($('createForm'));
  const channel = { name: form.get('name'), topic: form.get('topic'), context: form.get('context') };
  const opening = openings;
  const asked = () => opening === openings && $('create').open;  // still the dialog it was asked from
  const had = document.activeElement === $('createSubmit');
  $('createSubmit').disabled = true;
  let result;
  try {
    result = await api(`${BASE}api/channels`, { method: 'POST', json: channel, quiet: true });
  } catch (error) {
    if (asked()) {
      $('createError')?.remove();
      $('createForm').querySelector('.pk-dialog-body').insertAdjacentHTML('afterbegin', String(html`<p class="pk-error" id="createError">${error.message}</p>`));
    } else {
      toast(`${channel.name} could not be created: ${error.message}`, { kind: 'error' });
    }
    return;
  } finally {
    if (opening === openings) {
      $('createSubmit').disabled = false;
      refocus(had, $('createSubmit'));
    }
  }
  if (asked()) $('create').close();
  selected = result.channel_id;
  threadError = null;
  drawThread();
  refresh();
}

// ---------------------------------------------------------------------- wiring

let searchTimer;
document.addEventListener('refresh', refresh);
$('status').addEventListener('change', () => refresh());
$('group').addEventListener('change', () => refresh());
$('search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(refresh, 300); });
$('channels').addEventListener('click', (event) => {
  const button = event.target.closest('[data-channel]');
  if (button) select(Number(button.dataset.channel));
});
$('channels').addEventListener('toggle', (event) => {
  const group = event.target.dataset?.group;
  if (group === undefined) return;
  if (event.target.open) expanded.add(group); else expanded.delete(group);
  keepExpanded();
}, true);
$('messages').addEventListener('click', (event) => { pin(event); toggleJson(event); });
$('copy').addEventListener('click', copy);
$('reopen').addEventListener('click', reopen);
$('archive').addEventListener('click', archive);
$('delete').addEventListener('click', remove);
$('composer').addEventListener('submit', send);
$('text').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $('composer').requestSubmit();
  }
});
$('newChannel').addEventListener('click', openCreate);
$('createCancel').addEventListener('click', () => $('create').close());
$('create').addEventListener('close', () => $('createForm').reset());
$('createForm').addEventListener('submit', create);

drawThread();
refresh();
