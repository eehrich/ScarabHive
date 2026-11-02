// User view panel (admins): the panels released to people who are not admins (agent_system/ui/views.py).
import { api, errorText, html, icon, notice, render, setDirty, toast, update, withBusy } from '/static/kit/panel-kit.js';

const $ = (id) => document.getElementById(id);
const state = { available: [], panels: new Set(), dirty: false };

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

function drawPreview() {
  const shown = state.available.filter((panel) => state.panels.has(panel.id));
  $('panelCount').textContent = `${shown.length} of ${state.available.length}`;
  update($('preview'), html`<span class="pk-label">Panels</span>
    ${shown.length ? html`<ul class="uv-preview" data-preview>${shown.map((panel) => html`<li>${icon(panel.icon, { size: 'sm' })} ${panel.title}</li>`)}</ul>`
      : html`<p class="pk-muted">None: users get the chat alone.</p>`}`);
}

async function load() {
  try {
    const view = await api('/api/ui/user-view', { quiet: true });
    Object.assign(state, { available: view.available, panels: new Set(view.panels) });
    notice($('stale'), view.chosen ? '' : 'Nobody has chosen yet: shown is what users get by default.', { kind: 'info' });
    drawPanels();
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

$('save').addEventListener('click', () => withBusy([$('save')], async () => {
  try {
    const saved = await api('/api/ui/user-view', { method: 'PUT', json: { panels: state.available.filter((panel) => state.panels.has(panel.id)).map((panel) => panel.id) } });
    state.panels = new Set(saved.panels);
    state.dirty = false;
    setDirty(false);
    notice($('stale'), '');
    drawPreview();
    toast('Saved: users see it the next time ScarabHive loads', { kind: 'ok' });
  } catch {
    // api() has shown the refusal; the choice stays to correct
  }
}).then(() => {
  $('save').disabled = !state.dirty;  // withBusy frees every button it held: only unsaved changes keep this one on
}));

load();
