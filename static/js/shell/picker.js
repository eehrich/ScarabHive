// The agent and model profile pickers: one dialog with a search and a grouping, instead of two long dropdowns.
import { html, render, icon } from '/static/kit/panel-kit.js';
import { matches } from './launcher.js';

const GROUPING_KEY = 'scarabhive.pickerGrouping';
const selector = () => window.selectorModule;

const KINDS = {
  agent: {
    title: 'Choose an agent',
    placeholder: 'Name, description, category or tag',
    items: () => selector().agents(),
    current: () => selector().getCurrentAgent(),
    fallback: () => selector().defaultAgent(),
    pick: (name) => selector().setAgent(name),
    hint: (a) => a.description || '',
    meta: () => '',
    keywords: (a) => [a.category, ...(a.tags || [])],
    groupings: [['category', 'Category', (a) => a.category || 'Other'], ['none', 'A–Z', () => '']],
  },
  profile: {
    title: 'Choose a model profile',
    placeholder: 'Name, description, model or route',
    items: () => selector().profiles(),
    current: () => selector().getCurrentLLMProfile(),
    // the badge goes to what the chat runs on without a pick: the agent's own profile
    fallback: () => selector().agentDefaultProfile(selector().getCurrentAgent()),
    pick: (name) => selector().setLLMProfile(name),
    hint: (p) => (p.description && p.description !== p.name ? p.description : ''),
    meta: (p) => p.model || p.model_ref,
    keywords: (p) => [p.model, p.model_ref, p.host, p.provider],
    // route: where the request goes (OpenRouter, DeepSeek, Google ...) -- the provider plugin alone lumps most together
    groupings: [['route', 'Route', (p) => p.host || p.provider || 'Other'], ['model', 'Model', (p) => p.model || p.model_ref], ['none', 'A–Z', () => '']],
  },
};

function storedGroupings() {
  try { return JSON.parse(localStorage.getItem(GROUPING_KEY)) || {}; } catch { return {}; }
}

export class Picker {
  constructor() {
    this.dialog = document.getElementById('picker');
    this.title = document.getElementById('pickerTitle');
    this.groups = document.getElementById('pickerGroups');
    this.input = document.getElementById('pickerInput');
    this.list = document.getElementById('pickerList');
    this.kind = null;
    this.shown = [];
    this.selected = 0;
    document.getElementById('agentPicker').addEventListener('click', () => this.open('agent'));
    document.getElementById('modelPicker').addEventListener('click', () => this.open('profile'));
    this.input.addEventListener('input', () => this.render('first'));
    this.input.addEventListener('keydown', (event) => this.onKey(event));
    this.list.addEventListener('click', (event) => {
      const row = event.target.closest('[data-index]');
      if (row) this.pick(Number(row.dataset.index));
    });
    this.groups.addEventListener('click', (event) => {
      const button = event.target.closest('[data-grouping]');
      if (!button) return;
      const stored = storedGroupings();
      stored[this.kind] = button.dataset.grouping;
      try { localStorage.setItem(GROUPING_KEY, JSON.stringify(stored)); } catch { /* storage unavailable */ }
      this.render('keep');
      this.input.focus();
    });
    this.dialog.addEventListener('click', (event) => {
      if (event.target === this.dialog) this.dialog.close();
    });
    // opened before its list arrived: it shows the list when it comes, on the current choice
    // (nothing was shown to keep); a restore while a list is shown keeps the viewer's row
    window.addEventListener('selector:change', (event) => {
      if (this.dialog.open && event.detail.kind === this.kind) this.render(this.shown.length ? 'keep' : 'current');
    });
  }

  open(kind) {
    this.kind = kind;
    const spec = KINDS[kind];
    this.title.textContent = spec.title;
    this.input.placeholder = spec.placeholder;
    this.input.value = '';
    this.render('current');
    this.dialog.showModal();
    // only a shown list scrolls: render's own scroll ran while the dialog was still closed
    this.list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
    this.input.focus();
  }

  grouping() {
    const { groupings } = KINDS[this.kind];
    return groupings.find(([key]) => key === storedGroupings()[this.kind]) || groupings[0];
  }

  onKey(event) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      this.selected = (this.selected + step + this.shown.length) % Math.max(this.shown.length, 1);
      this.render('keep');
    } else if (event.key === 'Enter') {
      event.preventDefault();
      this.pick(this.selected);
    }
  }

  pick(index) {
    const item = this.shown[index];
    if (!item) return;
    this.dialog.close();
    KINDS[this.kind].pick(item.name);
  }

  /** select: 'current' (opened), 'keep' (the same item, wherever it moved) or 'first' (the search changed). */
  render(select = 'current') {
    const spec = KINDS[this.kind];
    const [groupingKey, , groupOf] = this.grouping();
    const held = select === 'keep' ? this.shown[this.selected]?.name : select === 'current' ? spec.current() : null;
    render(this.groups, spec.groupings.map(([key, label]) => html`<button type="button" class="pk-btn pk-btn--ghost pk-btn--sm"
      data-grouping="${key}" aria-pressed="${String(key === groupingKey)}">${label}</button>`));

    const query = this.input.value.trim();
    const items = spec.items();
    if (!items.length) {
      this.shown = [];
      this.input.removeAttribute('aria-activedescendant');
      const state = selector().listState(this.kind);
      render(this.list, state === 'loading' ? html`<div class="pk-empty"><span class="pk-spinner"></span><div>Loading…</div></div>`
        : html`<div class="pk-empty">${icon('circle-alert')}<div>${state === 'failed' ? 'The list could not be loaded' : 'Nothing to choose from'}</div></div>`);
      return;
    }
    const byName = (a, b) => a.name.localeCompare(b.name);
    this.shown = items
      .filter((item) => !query || matches({ title: item.name, description: spec.hint(item), keywords: spec.keywords(item) }, query))
      .map((item) => ({ ...item, group: groupOf(item) }))
      .sort((a, b) => (a.group === b.group ? byName(a, b)
        : a.group === 'Other' ? 1 : b.group === 'Other' ? -1 : a.group.localeCompare(b.group)));
    if (!this.shown.length) {
      this.input.removeAttribute('aria-activedescendant');
      render(this.list, html`<div class="pk-empty">${icon('search')}<div>Nothing matches "${query}"</div></div>`);
      return;
    }
    const heldIndex = this.shown.findIndex((item) => item.name === held);
    if (select !== 'keep' || heldIndex !== -1) this.selected = Math.max(heldIndex, 0);

    const current = spec.current();
    const fallback = spec.fallback();
    let group = null;
    render(this.list, this.shown.map((item, index) => {
      const heading = item.group && item.group !== group ? html`<div class="palette-group">${item.group}</div>` : '';
      group = item.group;
      const hint = spec.hint(item);
      return html`${heading}<div class="palette-item picker-item" role="option" id="picker-option-${index}" data-index="${index}"
          aria-selected="${String(index === this.selected)}">
        <span class="picker-check">${item.name === current ? icon('check', { size: 'sm', label: 'Current' }) : ''}</span>
        <span class="picker-text">
          <span class="picker-name"><span class="pk-truncate">${item.name}</span>${item.name === fallback ? html`<span class="pk-badge">default</span>` : ''}</span>
          ${hint ? html`<span class="picker-hint pk-truncate">${hint}</span>` : ''}
        </span>
        ${spec.meta(item) ? html`<span class="palette-hint pk-truncate">${spec.meta(item)}</span>` : ''}
      </div>`;
    }));
    this.input.setAttribute('aria-activedescendant', `picker-option-${this.selected}`);
    this.list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
  }
}
