// User view panel (admins): what the simple view shows -- the panels users get and the composer's topics, each with
// the agent behind it and the model profile for Quick and for Thorough (agent_system/ui/views.py).
import { api, errorText, html, icon, notice, render, setDirty, toast, update, withBusy } from '/static/kit/panel-kit.js';

const $ = (id) => document.getElementById(id);
const MAX_TOPICS = 8;
const state = { available: [], panels: new Set(), topics: [], agents: [], profiles: [], dirty: false };

function changed() {
  state.dirty = true;
  setDirty(true);
  $('save').disabled = false;
  drawPreview();
}

function drawPanels() {
  render($('panels'), state.available.length ? html`${state.available.map((panel) => html`<div class="uv-row" data-panel-row="${panel.id}">
      <label class="pk-switch"><input type="checkbox" role="switch" data-panel="${panel.id}" ${state.panels.has(panel.id) ? 'checked' : ''}
        aria-label="Show ${panel.title} to users"></label>
      ${icon(panel.icon)}
      <div class="pk-grow"><strong>${panel.title}</strong><div class="pk-muted">${panel.description}</div></div>
      <span class="pk-badge">${panel.category}</span></div>`)}`
    : html`<p class="pk-muted">No panel is open to users: every panel here is for admins only.</p>`);
}

// a name the list lacks is said so; one only admins may run (`gated`) too: a topic on it reaches no user
const options = (names, chosen, none, gated = new Set()) => html`<option value="" ${chosen ? '' : 'selected'}>${none}</option>${
  [...new Set([...names, ...(chosen && !names.includes(chosen) ? [chosen] : [])])].map((name) =>
    html`<option value="${name}" ${name === chosen ? 'selected' : ''}>${name}${
      !names.includes(name) ? ' -- not available' : gated.has(name) ? ' -- admins only, users will not see this topic' : ''}</option>`)}`;

function drawTopics() {
  render($('topics'), html`${state.topics.map((topic, index) => html`<div class="uv-topic" data-topic="${index}">
    <label class="pk-field"><span class="pk-label">Topic</span>
      <input class="pk-input" data-key="label-${index}" data-field="label" value="${topic.label}" maxlength="40" required></label>
    <label class="pk-field"><span class="pk-label">Example</span>
      <input class="pk-input" data-key="hint-${index}" data-field="hint" value="${topic.hint}" maxlength="120"></label>
    <label class="pk-field"><span class="pk-label">Agent</span>
      <select class="pk-select" data-field="agent">${options(state.agents, topic.agent, 'Default', state.notForUsers)}</select></label>
    <label class="pk-field"><span class="pk-label">Quick</span>
      <select class="pk-select" data-field="quick">${options(state.profiles, topic.quick, "Agent's own")}</select></label>
    <label class="pk-field"><span class="pk-label">Thorough</span>
      <select class="pk-select" data-field="deep">${options(state.profiles, topic.deep, "Agent's own")}</select></label>
    <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon" data-remove="${index}" aria-label="Remove ${topic.label}"
      ${state.topics.length === 1 ? 'disabled' : ''}>${icon('trash-2', { size: 'sm' })}</button></div>`)}`);
  $('addTopic').disabled = state.topics.length >= MAX_TOPICS;
}

function drawPreview() {
  const shown = state.available.filter((panel) => state.panels.has(panel.id));
  $('panelCount').textContent = `${shown.length} of ${state.available.length}`;
  update($('preview'), html`<span class="pk-label">Panels</span>
    ${shown.length ? html`<ul class="uv-preview" data-preview>${shown.map((panel) => html`<li>${icon(panel.icon, { size: 'sm' })} ${panel.title}</li>`)}</ul>`
      : html`<p class="pk-muted">None: users get the chat alone.</p>`}
    <span class="pk-label">Topics</span>
    <ul class="uv-preview">${state.topics.map((topic) => html`<li>${topic.label || '--'}</li>`)}</ul>`);
}

const slug = (label, taken) => {
  const base = (label.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'topic').slice(0, 32);
  let id = /^[a-z0-9]/.test(base) ? base : `t-${base}`;
  for (let n = 2; taken.has(id); n += 1) id = `${base}-${n}`;
  return id;
};

async function load() {
  try {
    const [view, agents, profiles] = await Promise.all([
      api('/api/ui/user-view', { quiet: true }),
      api('/agents', { quiet: true }).catch(() => ({ agents: [] })),
      api('/llm/profiles', { quiet: true }).catch(() => ({ profiles: [] })),
    ]);
    Object.assign(state, {
      available: view.available, panels: new Set(view.panels), topics: view.topics.map((topic) => ({ ...topic })),
      notForUsers: new Set(view.not_for_users || []),
      agents: (agents.agents || []).map((agent) => (typeof agent === 'string' ? agent : agent.name)).sort(),
      profiles: (profiles.profiles || []).map((profile) => profile.name),
    });
    notice($('stale'), view.chosen ? '' : 'Nobody has chosen yet: shown is what users get by default.', { kind: 'info' });
    drawPanels();
    drawTopics();
    drawPreview();
  } catch (error) {
    notice($('stale'), `Not loaded (${errorText(error)}).`);
  }
}

$('panels').addEventListener('change', (event) => {
  const box = event.target.closest('[data-panel]');
  if (!box) return;
  if (box.checked) state.panels.add(box.dataset.panel); else state.panels.delete(box.dataset.panel);
  changed();
});

$('topics').addEventListener('input', (event) => {
  const row = event.target.closest('[data-topic]');
  if (!row || !event.target.dataset.field) return;
  state.topics[Number(row.dataset.topic)][event.target.dataset.field] = event.target.value;
  changed();
});
$('topics').addEventListener('change', (event) => {
  const row = event.target.closest('[data-topic]');
  if (!row || event.target.tagName !== 'SELECT') return;
  state.topics[Number(row.dataset.topic)][event.target.dataset.field] = event.target.value;
  changed();
});
$('topics').addEventListener('click', (event) => {
  const remove = event.target.closest('[data-remove]');
  if (!remove || state.topics.length === 1) return;
  state.topics.splice(Number(remove.dataset.remove), 1);
  drawTopics();
  changed();
});
$('addTopic').addEventListener('click', () => {
  if (state.topics.length >= MAX_TOPICS) return;
  state.topics.push({ id: '', label: 'New topic', hint: '', agent: '', quick: '', deep: '' });
  drawTopics();
  changed();
  $('topics').querySelector(`[data-topic="${state.topics.length - 1}"] input`)?.select();
});

$('save').addEventListener('click', () => withBusy([$('save')], async () => {
  const taken = new Set();
  const topics = state.topics.map((topic) => {
    const id = topic.id && !taken.has(topic.id) ? topic.id : slug(topic.label, taken);
    taken.add(id);
    return { ...topic, id, label: topic.label.trim() };
  });
  try {
    const saved = await api('/api/ui/user-view', { method: 'PUT', json: { panels: state.available.filter((panel) => state.panels.has(panel.id)).map((panel) => panel.id), topics } });
    state.panels = new Set(saved.panels);
    state.topics = saved.topics.map((topic) => ({ ...topic }));
    state.dirty = false;
    setDirty(false);
    notice($('stale'), '');
    drawTopics();
    drawPreview();
    toast('Saved: users see it the next time ScarabHive loads', { kind: 'ok' });
  } catch {
    // api() has shown the refusal; the choice stays to correct
  }
}).then(() => {
  $('save').disabled = !state.dirty;  // withBusy frees every button it held: only unsaved changes keep this one on
}));

load();
